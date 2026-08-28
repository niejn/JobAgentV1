"""Agent tool: read Boss chat lists - which HRs greeted us (TR-5).

Read-only: no confirmation gate (nothing is sent). Send/reply (TR-6) is a
separate, HITL-gated tool.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from jobagent.config import Settings


class BossChatListRequest(BaseModel):
    """List Boss chat conversations (who greeted us). Read-only."""

    filter_name: str = Field(
        default="全部",
        description="过滤器名称。已校准：全部。其余（未读/新招呼/仅沟通/有交换/有面试/不感兴趣）待真机校准。",
    )
    label_id: int | None = Field(
        default=None,
        description="直接指定 Boss 的 labelId（校准后使用；优先于 filter_name）。",
    )
    limit: int = Field(default=50, ge=1, le=100, description="最多返回多少条会话。")


def build_boss_chat_list_tool(settings: Settings) -> StructuredTool:
    """Wrap BossChatReader.list_greetings as an agent tool."""

    async def _run(
        filter_name: str = "全部",
        label_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        from jobagent.applier.boss_chat import BossChatReader

        async with BossChatReader(settings) as reader:
            return await reader.list_greetings(
                filter_name=filter_name, label_id=label_id, limit=limit
            )

    return StructuredTool.from_function(
        coroutine=_run,
        name="list_boss_greetings",
        description=(
            "查看 Boss 直聘的聊天会话列表：哪些 HR 打过招呼、公司、职位、最后一条消息。"
            "只读操作。filter_name 目前支持「全部」，其余过滤器待校准。"
            "返回的 friendSource 区分来源（如对方主动打招呼）。"
        ),
        args_schema=BossChatListRequest,
    )


__all__ = ["BossChatListRequest", "build_boss_chat_list_tool"]
