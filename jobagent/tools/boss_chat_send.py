"""Agent tool: reply to a Boss HR greeting (TR-6) - HITL-gated send."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from jobagent.config import Settings


class BossChatReplyRequest(BaseModel):
    """Send one chat reply to a Boss HR. Requires explicit confirmation."""

    hr_name: str = Field(description="HR 姓名（须与 list_boss_greetings 返回的 name 一致）。")
    message: str = Field(
        description="要发送的回复全文。发送前必须已获得用户对这段文字的确认。",
        max_length=500,
    )
    user_confirmed: bool = Field(
        default=False,
        description="用户已确认发送这段文案。未确认时工具只返回待确认状态，不发送。",
    )


def build_boss_chat_reply_tool(settings: Settings) -> StructuredTool:
    """Wrap BossChatSender.send_reply as an HITL-gated agent tool."""

    async def _run(hr_name: str, message: str, user_confirmed: bool = False) -> dict[str, Any]:
        if not user_confirmed:
            return {
                "status": "waiting_user_confirmation",
                "hr_name": hr_name,
                "message": message,
                "hint": "向用户展示完整文案，确认后携带 user_confirmed=true 重试。",
            }
        from jobagent.applier.boss_chat_send import BossChatSender

        async with BossChatSender(settings) as sender:
            return await sender.send_reply(hr_name=hr_name, message=message)

    return StructuredTool.from_function(
        coroutine=_run,
        name="reply_boss_greeting",
        description=(
            "回复 Boss 直聘聊天：向指定 HR 发送一条文字消息（HITL：必须先让用户确认文案，"
            "user_confirmed=true 才会真正发送）。hr_name 用 list_boss_greetings 查到的 name。"
        ),
        args_schema=BossChatReplyRequest,
    )


__all__ = ["BossChatReplyRequest", "build_boss_chat_reply_tool"]
