"""Runtime tool-surface regression: middleware-injected tools must be pinned.

Security review 2026-09-22 found that deepagents hands EVERY declarative
subagent an unrestricted default FilesystemMiddleware (tools=None → execute/
write_file/edit_file/…), so six platform subagents silently held unguarded
shells while the spec-level test suite stayed green — spec dicts don't show
middleware-injected tools. This test drives real ``task`` delegation per
subagent and records what each agent actually binds.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from jobagent.agent import build_job_agent
from jobagent.config import Settings

PLATFORM_SUBAGENTS = (
    "boss_discovery",
    "boss_greeting",
    "boss_engagement",
    "boss_verification",
    "xhs_recruiting",
    "resume_crafting",
)
ALL_SUBAGENTS = (*PLATFORM_SUBAGENTS, "code_runner")
# deepagents' auto-added general-purpose subagent is DISABLED (it inherited
# all root tools incl. HITL writes with zero approval); a same-name stub
# suppresses it and the probe asserts the stub binds read_file only.
PROBE_DISABLED = ("general-purpose",)


class _ProbeState:
    def __init__(self, script: list[str]) -> None:
        self.binds: list[set[str]] = []
        self.pending: list[str] = list(script)
        # Root context is identified by the `task` tool in the latest binding;
        # subagents never receive it.
        self.last_bind_has_task = True
        # One delegation per user turn: after the task result returns, root
        # answers in plain text instead of chaining the next script entry
        # (chaining all seven delegations inside one turn blew the recursion
        # budget).
        self.current_human = ""
        self.turn_task_issued = False


class _TaskProbingModel(FakeListChatModel):
    """Root context delegates one task per user turn; subagents answer."""

    state: Any = None  # assigned via constructor kwargs

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        names = {getattr(t, "name", str(t)) for t in tools}
        self.state.binds.append(names)
        self.state.last_bind_has_task = "task" in names
        return self

    def _generate(
        self,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        state = self.state
        issue_task = False
        if state.last_bind_has_task:
            latest_human = next(
                (
                    str(m.content)
                    for m in reversed(messages)
                    if getattr(m, "type", "") == "human"
                ),
                "",
            )
            if latest_human != state.current_human:
                state.current_human = latest_human
                state.turn_task_issued = False
            issue_task = bool(state.pending) and not state.turn_task_issued
        if issue_task:
            state.turn_task_issued = True
            target = state.pending.pop(0).split(":", 1)[1]
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "subagent_type": target,
                            "description": "runtime surface probe",
                        },
                        "id": f"probe-{target}",
                        "type": "tool_call",
                    }
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=message)])
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="done"))]
        )


@pytest.mark.asyncio
async def test_runtime_tool_surface_execute_only_in_code_runner(tmp_path: Any) -> None:
    """Root and platform subagents must never bind execute; code_runner must."""

    state = _ProbeState(
        [f"task:{name}" for name in (*ALL_SUBAGENTS, *PROBE_DISABLED)]
    )
    model = _TaskProbingModel(responses=["done"], state=state)
    settings = Settings(
        _env_file=None,
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
    )
    agent = build_job_agent(settings, model=model)
    try:
        graph = await agent._ensure_deep_agent()
        assert graph is not None
        for index in range(len(ALL_SUBAGENTS) + len(PROBE_DISABLED)):
            await graph.ainvoke(
                {"messages": [("user", f"probe {index}")]},
                config={
                    "configurable": {"thread_id": f"surface-{index}"},
                    "recursion_limit": 30,
                },
            )
    finally:
        await agent.close()

    root_binds = [bound for bound in state.binds if "task" in bound]
    assert root_binds, "root binding never recorded"
    for bound in root_binds:
        assert "execute" not in bound, "root must not bind execute"

    # Interleaving is not guaranteed (root may bind more than once per turn),
    # so classify by structure: subagent tool sets never contain `task`.
    subagent_binds = [bound for bound in state.binds if "task" not in bound]
    # Our seven subagents plus the general-purpose STUB: the auto-added
    # unrestricted default is suppressed by shipping a spec with that exact
    # name, so the stub binds read_file only (covered by the forbidden-set
    # assertion below).
    assert len(subagent_binds) == len(ALL_SUBAGENTS) + len(PROBE_DISABLED), (
        f"expected exactly {len(ALL_SUBAGENTS) + len(PROBE_DISABLED)} subagent "
        f"bindings, got {len(subagent_binds)}"
    )
    dangerous = {"write_file", "edit_file", "delete"}
    forbidden = dangerous | {"execute"}
    with_execute = [bound for bound in subagent_binds if "execute" in bound]
    assert with_execute, "code_runner never bound execute (probe failed)"
    for bound in with_execute:
        assert not (bound & dangerous), "code_runner must stay file-write-free"
    for bound in subagent_binds:
        if "execute" in bound:
            continue
        assert not (bound & forbidden), (
            f"platform subagent bound forbidden tools: {bound & forbidden}"
        )
