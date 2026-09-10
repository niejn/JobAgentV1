"""Root middleware keeps native DeepAgents task delegation serial."""

from __future__ import annotations

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from jobagent.agent import build_job_agent
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
    assert {tool.name for tool in subagents["boss_recruiting"]["tools"]} >= {
        "boss_greet_jobs",
        "send_boss_resume_after_hr_reply",
    }
    xhs_names = {tool.name for tool in subagents["xhs_recruiting"]["tools"]}
    assert xhs_names >= {
        "send_recruitment_email",
        "search_xhs_notes",
        "analyze_recruitment_note",
        "find_recruitment_posts",
        "select_recruitment_position",
    }
    assert "list_available_resume_pdfs" in xhs_names
    boss_names = {tool.name for tool in subagents["boss_recruiting"]["tools"]}
    assert "list_available_resume_pdfs" not in boss_names
    assert {"list_skills", "read_skill"} <= xhs_names
    assert "install_skill" not in xhs_names
    assert "register_resume_pdf" not in xhs_names
    assert "list_available_resume_pdfs" not in root_names
    assert "register_resume_pdf" in root_names
    assert {"list_skills", "read_skill", "install_skill"} <= root_names
    assert "boss_greet_jobs" in subagents["boss_recruiting"]["interrupt_on"]
    assert "send_recruitment_email" in subagents["xhs_recruiting"]["interrupt_on"]
    xhs_prompt = str(subagents["xhs_recruiting"]["system_prompt"])
    assert "<xhs_recruitment_skill>" in xhs_prompt
    assert "read_skill 读取最新内容" in xhs_prompt
    assert "失败恢复表" in xhs_prompt
    assert "already_submitted" in xhs_prompt
    boss_prompt = str(subagents["boss_recruiting"]["system_prompt"])
    assert "不得查询、导入或使用本地 PDF 简历库" in boss_prompt
