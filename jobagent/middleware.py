"""Message compatibility middleware for durable JobAgent conversations."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest
from langchain_core.messages import AIMessage, AnyMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime


class ModelCapabilityRegistry:
    """Small local registry of capabilities learned from provider responses."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def is_text_only(self, model_key: str) -> bool:
        entry = self._load().get(model_key)
        return isinstance(entry, dict) and entry.get("input_modalities") == ["text"]

    def mark_text_only(self, model_key: str, *, reason: str) -> None:
        data = self._load()
        data[model_key] = {
            "input_modalities": ["text"],
            "source": "provider_400",
            "reason": reason[:500],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


def _text_only_message(message: AnyMessage) -> AnyMessage:
    """Remove non-text content blocks while preserving the message envelope."""

    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return message
    text_blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if not text_blocks:
        text_blocks = [{"type": "text", "text": "[非文本内容已省略：当前模型不支持视觉输入]"}]
    if len(text_blocks) == len(content) and all(
        a == b for a, b in zip(text_blocks, content, strict=True)
    ):
        return message
    return message.model_copy(update={"content": text_blocks})


def _message_tool_call_ids(message: AnyMessage) -> set[str]:
    return set(_ordered_tool_call_ids(message))


def _ordered_tool_call_ids(message: AnyMessage) -> list[str]:
    if not isinstance(message, AIMessage):
        return []
    return [
        str(call["id"])
        for call in (*message.tool_calls, *message.invalid_tool_calls)
        if call.get("id")
    ]


def _repair_dangling_tool_calls(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """Close missing tool results without replaying the old tool call."""

    prepared: list[AnyMessage] = []
    for message in messages:
        if not isinstance(message, AIMessage) or not message.invalid_tool_calls:
            prepared.append(message)
            continue
        cleaned = message.model_copy(update={"invalid_tool_calls": []})
        if cleaned.tool_calls or cleaned.content:
            prepared.append(cleaned)

    valid_call_ids = set().union(*(_message_tool_call_ids(message) for message in prepared))
    actual_results = {
        str(message.tool_call_id): message
        for message in prepared
        if isinstance(message, ToolMessage)
        and message.tool_call_id
        and str(message.tool_call_id) in valid_call_ids
    }
    output: list[AnyMessage] = []
    emitted_results: set[str] = set()
    seen_call_ids: set[str] = set()
    for message in prepared:
        if isinstance(message, ToolMessage):
            # Tool results are emitted in the corresponding AI call order.
            continue
        output.append(message)
        if not isinstance(message, AIMessage):
            continue
        for tool_call_id in _ordered_tool_call_ids(message):
            seen_call_ids.add(tool_call_id)
            if tool_call_id in emitted_results:
                continue
            output.append(actual_results.get(tool_call_id) or ToolMessage(
                content="该工具调用因会话重启已取消",
                name="unknown",
                tool_call_id=tool_call_id,
            ))
            emitted_results.add(tool_call_id)
    return output


def sanitize_messages(messages: Sequence[AnyMessage], *, remove_images: bool) -> list[AnyMessage]:
    """Return a provider-safe request projection without mutating checkpoint history.

    The returned list is a request projection. The middleware never writes it
    back to the checkpoint, so switching to a multimodal model later can still
    recover the original image content.
    """

    prepared = _repair_dangling_tool_calls(messages)
    if remove_images:
        prepared = [_text_only_message(message) for message in prepared]
    return prepared


def _changed(original: Sequence[AnyMessage], prepared: Sequence[AnyMessage]) -> bool:
    return len(original) != len(prepared) or any(
        a != b for a, b in zip(original, prepared, strict=True)
    )


class MessageCompatibilityMiddleware(AgentMiddleware):
    """Make durable history compatible with the selected model's capabilities."""

    def __init__(self, registry: ModelCapabilityRegistry, model_key: str) -> None:
        super().__init__()
        self._registry = registry
        self._model_key = model_key

    def before_agent(
        self, state: AgentState, runtime: Runtime[Any]  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Persist tool-chain repair, never image compatibility projection."""

        messages = state["messages"]
        repaired = _repair_dangling_tool_calls(messages)
        if not _changed(messages, repaired):
            return None
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired]}

    @staticmethod
    def _is_unsupported_content_error(exc: BaseException) -> bool:
        status_code = getattr(exc, "status_code", None)
        text = str(exc).lower()
        return status_code == 400 and (
            "content.type" in text or "image_url" in text or "image input" in text
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        messages = request.state["messages"]
        prepared = sanitize_messages(
            messages,
            remove_images=self._registry.is_text_only(self._model_key),
        )
        projected_request = (
            request.override(messages=prepared) if _changed(messages, prepared) else request
        )
        try:
            return handler(projected_request)
        except Exception as exc:
            if not self._is_unsupported_content_error(exc):
                raise
            self._registry.mark_text_only(self._model_key, reason=str(exc))
            messages = request.state["messages"]
            prepared = sanitize_messages(messages, remove_images=True)
            if not _changed(messages, prepared):
                raise
            return handler(request.override(messages=prepared))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        messages = request.state["messages"]
        prepared = sanitize_messages(
            messages,
            remove_images=self._registry.is_text_only(self._model_key),
        )
        projected_request = (
            request.override(messages=prepared) if _changed(messages, prepared) else request
        )
        try:
            return await handler(projected_request)
        except Exception as exc:
            if not self._is_unsupported_content_error(exc):
                raise
            self._registry.mark_text_only(self._model_key, reason=str(exc))
            messages = request.state["messages"]
            prepared = sanitize_messages(messages, remove_images=True)
            if not _changed(messages, prepared):
                raise
            return await handler(request.override(messages=prepared))
