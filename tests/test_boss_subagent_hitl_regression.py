"""Native DeepAgents HITL regression for the Boss channel subagent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import PrivateAttr

from jobagent.agent import JobAgent


class BossHITLModel(BaseChatModel):
    """Returns a root task call or a Boss write call based on bound tools."""

    _tool_names: set[str] = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        return "boss-hitl-regression-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        clone = self.model_copy(deep=True)
        clone._tool_names = {str(getattr(tool, "name", "")) for tool in tools}
        return clone

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,  # noqa: ARG002
        run_manager: Any | None = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> ChatResult:
        if "task" in self._tool_names:
            if any(message.type == "tool" for message in messages):
                answer = AIMessage(content="Boss 子任务已完成并取得回执。")
            else:
                answer = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "subagent_type": "boss_recruiting",
                                "description": "向测试 HR 打招呼",
                            },
                            "id": "delegate-boss",
                            "type": "tool_call",
                        }
                    ],
                )
        elif any(message.type == "tool" for message in messages):
            answer = AIMessage(content="打招呼已确认发送。")
        else:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "boss_greet_jobs",
                        "args": {"company": "测试公司", "title": "测试岗位"},
                        "id": "boss-greet-1",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


@pytest.mark.asyncio
async def test_boss_subagent_write_pauses_then_executes_once_after_resume(tmp_path: Path) -> None:
    calls: list[dict[str, str]] = []

    async def greet(company: str, title: str) -> dict[str, str]:
        calls.append({"company": company, "title": title})
        return {"status": "confirmed", "receipt": "fake-boss-receipt"}

    boss_tool = StructuredTool.from_function(
        coroutine=greet,
        name="boss_greet_jobs",
        description="向 Boss HR 发送打招呼。",
    )
    agent = JobAgent(
        model=BossHITLModel(),
        tools=[],
        subagents=[
            {
                "name": "boss_recruiting",
                "description": "处理 Boss 招聘动作。",
                "system_prompt": "只完成被委派的 Boss 操作。",
                "model": BossHITLModel(),
                "tools": [boss_tool],
                "interrupt_on": {
                    "boss_greet_jobs": {
                        "allowed_decisions": ["approve", "reject"],
                        "description": "向测试 HR 发送 Boss 打招呼",
                    }
                },
            }
        ],
        system_prompt="把 Boss 操作委派给 boss_recruiting。",
        checkpoint_db=tmp_path / "checkpoints.db",
        filesystem_root=tmp_path,
    )
    try:
        first = [
            event
            async for event in agent.stream_reply("联系测试 HR", session_id="boss-regression")
        ]
        interrupts = [event for event in first if event.kind == "interrupt"]

        assert len(interrupts) == 1
        assert "boss_greet_jobs" in interrupts[0].text
        assert calls == [], "the Boss write must not execute before approval"

        resumed = [
            event
            async for event in agent.resume_reply(True, session_id="boss-regression")
        ]

        assert calls == [{"company": "测试公司", "title": "测试岗位"}]
        assert any("完成" in event.text for event in resumed if event.kind == "token")
    finally:
        await agent.close()
