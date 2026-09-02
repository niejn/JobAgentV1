from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from jobagent.middleware import (
    MessageCompatibilityMiddleware,
    ModelCapabilityRegistry,
    sanitize_messages,
)


def test_text_model_removes_image_blocks() -> None:
    message = HumanMessage(
        content=[
            {"type": "text", "text": "检查这份简历"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,secret"}},
        ]
    )

    cleaned = sanitize_messages([message], remove_images=True)

    assert cleaned[0].content == [{"type": "text", "text": "检查这份简历"}]


def test_visual_model_preserves_image_blocks() -> None:
    message = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}])

    cleaned = sanitize_messages([message], remove_images=False)

    assert cleaned == [message]


def test_dangling_tool_call_gets_cancelled_result() -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"name": "install_skill", "args": {}, "id": "call-1", "type": "tool_call"}],
    )

    cleaned = sanitize_messages([message], remove_images=False)

    assert isinstance(cleaned[1], ToolMessage)
    assert cleaned[1].tool_call_id == "call-1"
    assert cleaned[1].content == "该工具调用因会话重启已取消"


def test_registry_learns_text_only_capability(tmp_path: Path) -> None:
    registry = ModelCapabilityRegistry(tmp_path / "capabilities.json")
    assert not registry.is_text_only("test-model@https://test")

    registry.mark_text_only("test-model@https://test", reason="400 image input")

    assert registry.is_text_only("test-model@https://test")


def test_before_agent_persists_tool_repair_but_preserves_images(tmp_path: Path) -> None:
    image = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}])
    call = AIMessage(
        content="",
        tool_calls=[
            {"name": "one", "args": {}, "id": "call-1", "type": "tool_call"},
            {"name": "two", "args": {}, "id": "call-2", "type": "tool_call"},
            {"name": "three", "args": {}, "id": "call-3", "type": "tool_call"},
        ],
    )
    done = ToolMessage(content="done", name="one", tool_call_id="call-1")
    registry = ModelCapabilityRegistry(tmp_path / "capabilities.json")
    middleware = MessageCompatibilityMiddleware(registry, "test-model@https://test")

    update = middleware.before_agent({"messages": [image, call, done]}, None)

    assert isinstance(update["messages"][0], RemoveMessage)
    assert update["messages"][0].id == REMOVE_ALL_MESSAGES
    repaired = update["messages"][1:]
    assert repaired[0].content[0]["type"] == "image_url"
    assert [item.tool_call_id for item in repaired if isinstance(item, ToolMessage)] == [
        "call-1",
        "call-2",
        "call-3",
    ]


def test_invalid_calls_and_orphan_results_are_removed() -> None:
    invalid = AIMessage(
        content="",
        invalid_tool_calls=[{"name": "broken", "args": "{", "id": None, "error": "invalid"}],
    )
    orphan = ToolMessage(content="orphan", name="broken", tool_call_id="missing")

    cleaned = sanitize_messages([invalid, orphan], remove_images=False)

    assert cleaned == []


def test_unsupported_image_error_learns_capability_and_retries(tmp_path: Path) -> None:
    image = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}])
    registry = ModelCapabilityRegistry(tmp_path / "capabilities.json")
    middleware = MessageCompatibilityMiddleware(registry, "model@https://provider")

    class Request:
        state = {"messages": [image]}

        def override(self, **kwargs):
            request = Request()
            request.state = {**self.state, **kwargs}
            return request

    class BadRequest(Exception):
        status_code = 400

        def __str__(self) -> str:
            return "messages.content.type only accepts text"

    calls = []

    def handler(request):
        calls.append(request.state["messages"])
        if len(calls) == 1:
            raise BadRequest()
        return "ok"

    assert middleware.wrap_model_call(Request(), handler) == "ok"
    assert len(calls) == 2
    assert calls[0][0].content[0]["type"] == "image_url"
    assert calls[1][0].content[0]["type"] == "text"
    assert registry.is_text_only("model@https://provider")
