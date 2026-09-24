"""RepeatedToolCallGuardMiddleware — identical-call loop breaker.

Field incident 2026-09-23 16:43 (checkpoint window 1f1b70xx): GLM-5.3
re-issued the same ``write_file`` 20+ times in one turn, every retry with a
fresh tool_call_id while each success ToolMessage was already in context.
These tests pin the physical guard that breaks that loop.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from jobagent.middleware import RepeatedToolCallGuardMiddleware


def _request(
    call_id: str,
    name: str = "write_file",
    args: dict[str, Any] | None = None,
) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": name,
            "args": args or {"file_path": "/workspace/s.py", "content": "x"},
            "id": call_id,
            "type": "tool_call",
        },
        tool=None,
        state={},
        runtime=cast(Any, None),
    )


def _handler(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(
        content="Updated file", name=request.tool_call["name"], tool_call_id=request.tool_call["id"]
    )


async def _ahandler(request: ToolCallRequest) -> ToolMessage:
    return _handler(request)


def test_fourth_identical_consecutive_call_is_blocked() -> None:
    guard = RepeatedToolCallGuardMiddleware()
    for i in range(3):
        result = guard.wrap_tool_call(_request(f"call-{i}"), _handler)
        assert result.content == "Updated file"
    blocked = guard.wrap_tool_call(_request("call-3"), _handler)
    assert "已拦截" in str(blocked.content)
    assert "不要再次发起相同调用" in str(blocked.content)
    assert blocked.tool_call_id == "call-3"


def test_different_call_resets_the_streak() -> None:
    guard = RepeatedToolCallGuardMiddleware()
    for i in range(3):
        guard.wrap_tool_call(_request(f"a-{i}"), _handler)
    guard.wrap_tool_call(_request("other-0", name="ls", args={"path": "/"}), _handler)
    # Same write_file args again: streak restarted, execution allowed.
    result = guard.wrap_tool_call(_request("a-9"), _handler)
    assert result.content == "Updated file"


def test_args_key_order_is_irrelevant() -> None:
    guard = RepeatedToolCallGuardMiddleware()
    for i in range(3):
        guard.wrap_tool_call(
            _request(f"o-{i}", args={"content": "x", "file_path": "/workspace/s.py"}),
            _handler,
        )
    # Same mapping, insertion order flipped: still the identical call.
    blocked = guard.wrap_tool_call(
        _request("o-3", args={"file_path": "/workspace/s.py", "content": "x"}),
        _handler,
    )
    assert "已拦截" in str(blocked.content)


def test_before_agent_clears_counters_for_new_turn() -> None:
    guard = RepeatedToolCallGuardMiddleware()
    for i in range(3):
        guard.wrap_tool_call(_request(f"t-{i}"), _handler)
    guard.before_agent({"messages": []}, cast(Any, None))
    result = guard.wrap_tool_call(_request("t-4"), _handler)
    assert result.content == "Updated file"


@pytest.mark.asyncio
async def test_async_wrapper_blocks_the_same_loop() -> None:
    guard = RepeatedToolCallGuardMiddleware()
    for i in range(3):
        result = await guard.awrap_tool_call(_request(f"ac-{i}"), _ahandler)
        assert result.content == "Updated file"
    blocked = await guard.awrap_tool_call(_request("ac-3"), _ahandler)
    assert "已拦截" in str(blocked.content)
