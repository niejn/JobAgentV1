"""Structured logging and node tracing for the JobAgent runtime."""

from __future__ import annotations

import json
import logging
import logging.config
import re
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml
from langchain.agents.middleware import AgentMiddleware

_SENSITIVE_KEY = re.compile(r"token|key|cookie|password|secret|authorization", re.I)
_SENSITIVE_QUERY = re.compile(
    r"(?i)(xsec_token|token|key|cookie|password|secret|authorization)=([^&\s]+)"
)


class StructuredFormatter(logging.Formatter):
    """Format node events as compact JSON while preserving ordinary log messages."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "node_name",
            "phase",
            "duration_ms",
            "input_summary",
            "output_summary",
            "error_type",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(config_path: Path | None = None) -> None:
    """Load logging.yml and create its rotating log directory if necessary."""

    path = (config_path or Path("logging.yml")).expanduser().resolve()
    if not path.is_file():
        logging.basicConfig(level=logging.INFO)
        return
    with path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    handlers = config.get("handlers", {}) if isinstance(config, dict) else {}
    file_handler = handlers.get("rotating_file") if isinstance(handlers, dict) else None
    if isinstance(file_handler, dict) and isinstance(file_handler.get("filename"), str):
        (path.parent / file_handler["filename"]).parent.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig(config)


class NodeTraceMiddleware(AgentMiddleware):
    """Trace model and Tool nodes with bounded, redacted input/output summaries."""

    name = "NodeTraceMiddleware"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("jobagent.node")
        self._model_started: ContextVar[float | None] = ContextVar(
            "jobagent_model_started", default=None
        )

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        started = time.perf_counter()
        token = self._model_started.set(started)
        input_summary = summarize_state(getattr(request, "state", {}))
        self._emit("model", "entry", input_summary=input_summary)
        try:
            response = await handler(request)
        except Exception as exc:
            self._emit(
                "model",
                "exit",
                duration_ms=_duration_ms(started),
                output_summary="error",
                error_type=type(exc).__name__,
            )
            raise
        finally:
            self._model_started.reset(token)
        self._emit(
            "model",
            "exit",
            duration_ms=_duration_ms(started),
            output_summary=summarize_model_response(response),
        )
        return response

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        started = time.perf_counter()
        tool_call = getattr(request, "tool_call", {}) or {}
        node_name = f"tools.{tool_call.get('name', 'unknown')}"
        self._emit(
            node_name,
            "entry",
            input_summary=summarize_value(tool_call.get("args", {})),
        )
        try:
            response = await handler(request)
        except Exception as exc:
            self._emit(
                node_name,
                "exit",
                duration_ms=_duration_ms(started),
                output_summary="error",
                error_type=type(exc).__name__,
            )
            raise
        self._emit(
            node_name,
            "exit",
            duration_ms=_duration_ms(started),
            output_summary=summarize_value(getattr(response, "content", response)),
        )
        return response

    def _emit(self, node_name: str, phase: str, **fields: Any) -> None:
        self._logger.info(
            "agent.node.%s",
            phase,
            extra={"node_name": node_name, "phase": phase, **fields},
        )


def summarize_state(state: Any) -> dict[str, Any]:
    messages = state.get("messages", []) if isinstance(state, dict) else []
    last = messages[-1] if messages else None
    return {
        "message_count": len(messages),
        "last_type": getattr(last, "type", None),
        "last_chars": len(_message_text(last)) if last is not None else 0,
    }


def summarize_model_response(response: Any) -> dict[str, Any]:
    results = getattr(response, "result", []) or []
    return {
        "message_count": len(results),
        "tool_calls": sum(len(getattr(item, "tool_calls", []) or []) for item in results),
        "visible_chars": sum(len(_message_text(item)) for item in results),
    }


def summarize_value(value: Any) -> str:
    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else scrub(child)
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [scrub(child) for child in item[:10]]
        if isinstance(item, str):
            return _SENSITIVE_QUERY.sub(r"\1=[REDACTED]", item[:500])
        return item

    try:
        return json.dumps(scrub(value), ensure_ascii=False, default=str)[:1_000]
    except (TypeError, ValueError):
        return "[unserializable]"


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return str(content) if content is not None else ""


def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
