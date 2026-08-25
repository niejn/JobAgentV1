"""PS-4: iteration budget (recursion_limit) and graceful exhaustion.

Covers three behaviors:
1. The configured recursion_limit reaches deep_agent.astream's config
   (default 90 from settings, not LangGraph's implicit 25).
2. GraphRecursionError triggers a graceful closing summary instead of a
   raw exception: a status event, a tool-free summary token, and a done
   event. The summary is also persisted to the checkpoint.
3. If the closing summary itself fails, the user still gets a done event
   with a bounded fallback message - never a raw crash.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models import FakeListChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from jobagent.agent import JobAgent
from jobagent.config import Settings


class ToolBindableFakeListChatModel(FakeListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class BudgetExhaustionFakeModel(ToolBindableFakeListChatModel):
    """First call raises via recursion (see test); closing call answers.

    The closing-summary path is identified by the injected
    <budget_exhaustion_policy> system prompt.
    """

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: object,
    ) -> Any:
        from langchain_core.outputs import ChatGeneration, ChatResult

        if any(
            isinstance(message, SystemMessage)
            and "budget_exhaustion_policy" in str(message.content)
            for message in messages
        ):
            answer = AIMessage(
                content="阶段性总结：已分析 3 个岗位，未完成对比表格。建议拆分任务。"
            )
        else:
            # Normal-turn response: plain text answer.
            answer = AIMessage(content="普通回答")
        return ChatResult(generations=[ChatGeneration(message=answer)])


def _make_agent(
    tmp_path: Any,
    *,
    model: Any,
    recursion_limit: int,
) -> JobAgent:
    settings = Settings(
        _env_file=None,
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
    )
    return JobAgent(
        model=model,
        tools=[],
        system_prompt="test system prompt",
        checkpoint_db=settings.jobagent_checkpoint_db,
        recursion_limit=recursion_limit,
    )


@pytest.mark.asyncio
async def test_settings_default_recursion_limit_is_90() -> None:
    """The implicit LangGraph default (25) is replaced by an explicit 90."""

    settings = Settings(_env_file=None)
    assert settings.jobagent_recursion_limit == 90


@pytest.mark.asyncio
async def test_recursion_limit_reaches_astream_config(tmp_path: Any) -> None:
    """The budget flows into deep_agent.astream's config via a monkeypatched
    astream that records the config it was called with."""

    model = BudgetExhaustionFakeModel(responses=["普通回答"])
    agent = _make_agent(tmp_path, model=model, recursion_limit=7)

    captured_configs: list[dict] = []

    async def fake_astream(input: Any, config: dict, **kwargs: Any):  # noqa: ANN401
        captured_configs.append(config)
        # stream_mode="messages" event shape consumed by
        # _stream_reply_events: {"type": "messages", "data": (chunk, meta)}
        from langchain_core.messages import AIMessageChunk

        yield {
            "type": "messages",
            "data": (
                AIMessageChunk(content="普通回答"),
                {"langgraph_node": "model"},  # required by _stream_reply_events
            ),
        }

    try:
        deep_agent = await agent._ensure_deep_agent()
        deep_agent.astream = fake_astream  # type: ignore[method-assign]
        response = await agent.reply("你好", thread_id="cfg-check")
    finally:
        await agent.close()

    assert response == "普通回答"
    assert captured_configs, "astream was never called"
    assert captured_configs[0]["recursion_limit"] == 7
    assert captured_configs[0]["configurable"]["thread_id"] == "cfg-check"


@pytest.mark.asyncio
async def test_budget_exhaustion_yields_graceful_summary(tmp_path: Any) -> None:
    """GraphRecursionError surfaces as status + summary token + done events,
    and the summary is persisted into the checkpoint thread."""

    model = BudgetExhaustionFakeModel(responses=["x"])
    agent = _make_agent(tmp_path, model=model, recursion_limit=1)

    # Force the underlying astream to raise GraphRecursionError immediately:
    # recursion_limit=1 cannot even finish one model->tool superstep cycle
    # when the fake model requests a tool... but with tools=[] the model
    # answers directly, so we patch astream to raise deterministically.
    from langgraph.errors import GraphRecursionError

    async def raising_astream(input: Any, config: dict, **kwargs: Any):  # noqa: ANN401
        raise GraphRecursionError("Recursion limit of 1 reached")
        yield  # pragma: no cover - makes this an async generator

    try:
        deep_agent = await agent._ensure_deep_agent()
        deep_agent.astream = raising_astream  # type: ignore[method-assign]

        events = []
        async for event in agent.stream_reply("复杂任务", thread_id="budget"):
            events.append(event)
    finally:
        await agent.close()

    kinds = [event.kind for event in events]
    # Graceful path: status notice -> summary token -> done. No exception.
    assert "done" in kinds
    statuses = [event.text for event in events if event.kind == "status"]
    assert any("最大运行步数" in text for text in statuses)
    tokens = [event.text for event in events if event.kind == "token"]
    assert any("阶段性总结" in text for text in tokens)

    # The summary must also be persisted so the next turn stays coherent.
    # Reopen a fresh agent on the same checkpoint and inspect history.
    model2 = BudgetExhaustionFakeModel(responses=["x"])
    agent2 = _make_agent(tmp_path, model=model2, recursion_limit=90)
    try:
        history = await agent2.resume_thread("budget")
        texts = [entry.text for entry in history.recent]
        assert any("阶段性总结" in t for t in texts)
    finally:
        await agent2.close()


@pytest.mark.asyncio
async def test_budget_exhaustion_summary_failure_falls_back(tmp_path: Any) -> None:
    """If the closing summary itself fails, the user still gets done +
    a bounded fallback message - never a raw crash."""

    model = BudgetExhaustionFakeModel(responses=["x"])
    agent = _make_agent(tmp_path, model=model, recursion_limit=1)

    from langgraph.errors import GraphRecursionError

    async def raising_astream(input: Any, config: dict, **kwargs: Any):  # noqa: ANN401
        raise GraphRecursionError("Recursion limit of 1 reached")
        yield  # pragma: no cover

    # aget_state is called twice: once by _compact_history (before astream,
    # must succeed) and once by _graceful_budget_exhaustion (closing summary,
    # must fail to exercise the fallback path).
    get_state_calls = {"count": 0}

    async def failing_get_state(config: dict) -> Any:
        get_state_calls["count"] += 1
        if get_state_calls["count"] >= 2:
            raise RuntimeError("checkpoint unavailable")
        # Minimal viable StateSnapshot for _compact_history.
        class _Snapshot:
            values = {"messages": ()}

        return _Snapshot()

    try:
        deep_agent = await agent._ensure_deep_agent()
        deep_agent.astream = raising_astream  # type: ignore[method-assign]
        deep_agent.aget_state = failing_get_state  # type: ignore[method-assign]

        events = []
        async for event in agent.stream_reply("复杂任务", thread_id="fallback"):
            events.append(event)
    finally:
        await agent.close()

    kinds = [event.kind for event in events]
    assert "done" in kinds
    tokens = [event.text for event in events if event.kind == "token"]
    fallback = [text for text in tokens if "最大运行步数" in text]
    assert fallback, "expected bounded fallback message, got: " + repr(tokens)
