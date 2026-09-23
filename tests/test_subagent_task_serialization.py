"""Root middleware keeps native DeepAgents task delegation serial."""

from __future__ import annotations

import re

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from jobagent.agent import _PLATFORM_WRITE_TOOL_NAMES, build_job_agent
from jobagent.config import Settings
from jobagent.middleware import SingleSubagentTaskMiddleware


def _task_call(call_id: str, subagent_type: str) -> dict[str, object]:
    return {
        "name": "task",
        "args": {"subagent_type": subagent_type, "description": "test"},
        "id": call_id,
        "type": "tool_call",
    }


def test_only_first_native_task_is_left_for_the_tool_node() -> None:
    response = ModelResponse(
        result=[
            AIMessage(
                content="",
                tool_calls=[
                    _task_call("one", "boss_recruiting"),
                    _task_call("two", "xhs_recruiting"),
                ],
            )
        ]
    )

    serialized = SingleSubagentTaskMiddleware._serialize(response)

    message = serialized.result[0]
    assert isinstance(message, AIMessage)
    assert [call["id"] for call in message.tool_calls] == ["one"]
    rejection = serialized.result[1]
    assert isinstance(rejection, ToolMessage)
    assert rejection.tool_call_id == "two"
    assert rejection.status == "error"


def test_missing_task_ids_still_leave_only_one_task() -> None:
    response = ModelResponse(
        result=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "task", "args": {"description": "one"}, "id": "", "type": "tool_call"},
                    {"name": "task", "args": {"description": "two"}, "id": "", "type": "tool_call"},
                ],
            )
        ]
    )

    serialized = SingleSubagentTaskMiddleware._serialize(response)

    assert len(serialized.result[0].tool_calls) == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_async_model_wrapper_applies_the_same_serialization() -> None:
    middleware = SingleSubagentTaskMiddleware()
    response = ModelResponse(
        result=[
            AIMessage(
                content="",
                tool_calls=[
                    _task_call("one", "boss_recruiting"),
                    _task_call("two", "xhs_recruiting"),
                ],
            )
        ]
    )

    async def handler(_: object) -> ModelResponse:
        return response

    serialized = await middleware.awrap_model_call(None, handler)  # type: ignore[arg-type]
    assert len(serialized.result) == 2


class _ToolBindableFakeModel(FakeListChatModel):
    def bind_tools(self, tools: object, **kwargs: object) -> object:  # noqa: ARG002
        return self


class _ToolRecordingFakeModel(FakeListChatModel):
    """Records the tool set the runtime binds to the model."""


    bound_tool_names: list[str] = []

    def bind_tools(self, tools: object, **kwargs: object) -> object:  # noqa: ARG002
        self.bound_tool_names = sorted(
            tool.name for tool in tools  # type: ignore[attr-defined]
        )
        return self


@pytest.mark.asyncio
async def test_root_agent_binds_write_todos_for_planning(tmp_path) -> None:
    """deepagents 0.7 made TodoListMiddleware opt-in; jobagent opts in at the
    root so multi-step plans are tracked as todos in checkpointed state."""

    settings = Settings(
        _env_file=None,
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
    )
    model = _ToolRecordingFakeModel(responses=["ok"])
    agent = build_job_agent(settings, model=model)
    try:
        graph = await agent._ensure_deep_agent()
        assert graph is not None
        await graph.ainvoke(
            {"messages": [("user", "hi")]},
            config={"configurable": {"thread_id": "todo-binding-test"}},
        )
    finally:
        await agent.close()

    assert "write_todos" in model.bound_tool_names

@pytest.mark.asyncio
async def test_default_agent_exposes_platform_tools_only_to_their_subagents(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
    )
    agent = build_job_agent(settings, model=_ToolBindableFakeModel(responses=["ok"]))
    try:
        graph = await agent._ensure_deep_agent()
        root_names = {tool.name for tool in agent._root_tools}
        subagents = {spec["name"]: spec for spec in agent._subagents}
    finally:
        await agent.close()

    assert graph is not None
    assert "boss_greet_jobs" not in root_names
    assert "send_application_email" not in root_names
    # State-DB tools stay on root; boss_verification mirrors them read-mostly.
    assert {"get_job_progress", "list_job_records"} <= root_names
    # Shell execution is delegated: root has no execute tool (the 2026-09-22
    # field bypass ran a python script straight through root execute), and
    # code_runner carries the wants_shell marker consumed in _ensure_deep_agent
    # to inject FilesystemMiddleware(tools=["execute"]) + the bypass guard.
    from jobagent.agent import _ROOT_FILESYSTEM_TOOLS

    assert "execute" not in _ROOT_FILESYSTEM_TOOLS
    assert subagents["code_runner"]["tools"] == []
    assert subagents["code_runner"]["wants_shell"] is True
    boss_split = {
        "boss_discovery": {"discover_boss_jobs"},
        "boss_greeting": {"boss_greet_jobs", "list_boss_greetings"},
        "boss_engagement": {
            "read_boss_conversation",
            "reply_boss_greeting",
            "prepare_boss_resume_after_hr_reply",
            "send_boss_resume_after_hr_reply",
            "upload_boss_resume_pdf",
        },
        "boss_verification": {
            "read_boss_conversation",
            "confirm_greeting_delivered",
            "get_job_progress",
            "list_job_records",
        },
    }
    for name, expected_tools in boss_split.items():
        assert name in subagents, name
        actual_tools = {tool.name for tool in subagents[name]["tools"]}
        assert actual_tools == expected_tools, name
        assert "list_available_resume_pdfs" not in actual_tools, name
        # A prompt may only reference tools its own subagent holds — drift
        # here once produced an empty-tool and an unusable-verification subagent.
        referenced = set(
            re.findall(r"`([a-z_]+)`", str(subagents[name]["system_prompt"]))
        )
        assert referenced <= actual_tools, f"{name} prompt references unheld tools"
    # Every platform write tool is HITL-gated inside its owning subagent.
    assert "boss_greet_jobs" in subagents["boss_greeting"]["interrupt_on"]
    assert {
        "reply_boss_greeting",
        "send_boss_resume_after_hr_reply",
        "upload_boss_resume_pdf",
    } == set(subagents["boss_engagement"]["interrupt_on"])
    # Read-only subagents carry no HITL gates.
    assert not subagents["boss_discovery"].get("interrupt_on")
    assert not subagents["boss_verification"].get("interrupt_on")
    # The two outbound-facing prompts share the frozen channel-facts block.
    engagement_prompt = str(subagents["boss_engagement"]["system_prompt"])
    greeting_prompt = str(subagents["boss_greeting"]["system_prompt"])
    assert "渠道事实（实测 2026-09-19）" in engagement_prompt
    assert "渠道事实（实测 2026-09-19）" in greeting_prompt
    # First-round P0 vector guard: every platform write tool must end up
    # HITL-gated in whichever subagent holds it. A write tool name missing
    # from _HITL_TOOLS would be silently dropped by _boss_interrupts and every
    # assertion above would stay green.
    gated_union = set().union(
        *(spec.get("interrupt_on", {}) for spec in subagents.values())
    )
    assert _PLATFORM_WRITE_TOOL_NAMES <= gated_union
    xhs_names = {tool.name for tool in subagents["xhs_recruiting"]["tools"]}
    assert xhs_names >= {
        "send_recruitment_email",
        "search_xhs_notes",
        "analyze_recruitment_note",
        "find_recruitment_posts",
        "select_recruitment_position",
    }
    assert "list_available_resume_pdfs" in xhs_names
    assert {"list_skills", "read_skill"} <= xhs_names
    assert "install_skill" not in xhs_names
    assert "register_resume_pdf" not in xhs_names
    assert "list_available_resume_pdfs" not in root_names
    assert "register_resume_pdf" in root_names
    assert {"list_skills", "read_skill", "install_skill"} <= root_names
    assert "send_recruitment_email" in subagents["xhs_recruiting"]["interrupt_on"]
    xhs_prompt = str(subagents["xhs_recruiting"]["system_prompt"])
    assert "<xhs_recruitment_skill>" in xhs_prompt
    assert "read_skill 读取最新内容" in xhs_prompt
    assert "失败恢复表" in xhs_prompt
    assert "already_submitted" in xhs_prompt
    boss_prompt = engagement_prompt
    assert "不得查询、导入或使用本地 PDF 简历库" in boss_prompt
    # boss-delivery channel skill is deterministically injected at assembly
    # time (boss_engagement holds no read_skill; injection is the only path).
    assert "<boss_delivery_skill>" in boss_prompt
    assert "resume_filename_mismatch" in boss_prompt
    assert "ACK 丢失铁律" in boss_prompt
