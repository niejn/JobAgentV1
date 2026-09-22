"""Agent tool: reply to a Boss HR greeting (TR-6) - middleware-approved send."""


import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from jobagent.boss_outbound_guard import guard_refusal, screen_outbound_text
from jobagent.config import Settings

logger = logging.getLogger(__name__)


class BossChatReplyRequest(BaseModel):
    """Send one chat reply to a Boss HR (approved via HITL middleware)."""

    hr_name: str = Field(description="HR 姓名（来自任务描述，或与 read_boss_conversation 读到的 HR 名一致）。")
    message: str = Field(
        description="要发送的回复全文。",
        max_length=500,
    )


def build_boss_chat_reply_tool(settings: Settings) -> StructuredTool:
    """Wrap BossChatSender.send_reply; approval handled by the HITL middleware."""

    async def _run(hr_name: str, message: str) -> dict[str, Any]:
        from jobagent.applier.boss_circuit import BossCircuit, boss_circuit_path

        circuit = BossCircuit(boss_circuit_path(settings.jobagent_state_db))
        if (refusal := circuit.check()) is not None:
            return refusal
        if violations := screen_outbound_text(message):
            return guard_refusal(violations)
        from jobagent.applier.boss_chat_send import BossChatSender

        async with BossChatSender(settings) as sender:
            result = await sender.send_reply(hr_name=hr_name, message=message)
        circuit.record(result)
        if result.get("status") == "ok":
            try:
                from jobagent.journey.chat_archive import BossChatArchive

                with BossChatArchive(settings.jobagent_state_db) as archive:
                    archive.append_sent(
                        friend_id=0,  # UI path has no friend id handy
                        friend_name=hr_name,
                        text=message,
                        source="ui_sent",
                    )
            except Exception:
                logger.warning("chat archive write failed", exc_info=True)
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="reply_boss_greeting",
        description=(
            "回复 Boss 直聘聊天：向指定 HR 发送一条文字消息（执行前暂停等待人工批准。）"
            "hr_name 来自任务描述或 read_boss_conversation 读到的 HR 名。消息必须以求职者本人第一人称书写；"
            "含测试/请忽略话术或暴露 AI/自动化身份的文本会被出站护栏拒发。"
        ),
        args_schema=BossChatReplyRequest,
    )


__all__ = ["BossChatReplyRequest", "build_boss_chat_reply_tool"]
