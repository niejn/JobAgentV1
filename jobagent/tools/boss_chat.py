"""Agent tool: read Boss chat lists - which HRs greeted us (TR-5).

Read-only: no confirmation gate (nothing is sent). Send/reply (TR-6) is a
separate, HITL-gated tool.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from jobagent.config import Settings

logger = logging.getLogger(__name__)


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
        from jobagent.applier.boss_circuit import BossCircuit, boss_circuit_path

        circuit = BossCircuit(boss_circuit_path(settings.jobagent_state_db))
        if (refusal := circuit.check()) is not None:
            return refusal
        from jobagent.applier.boss_chat import BossChatReader

        async with BossChatReader(settings) as reader:
            result = await reader.list_greetings(
                filter_name=filter_name, label_id=label_id, limit=limit
            )
        circuit.record(result)
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="list_boss_greetings",
        description=(
            "查看 Boss 直聘的聊天会话列表：哪些 HR 打过招呼、公司、职位、最后一条消息，"
            "以及可用时的 job_metadata（稳定 job_id、无 securityId 的职位 URL、来源）。"
            "只读操作。filter_name 目前支持「全部」，其余过滤器待校准。"
            "返回的 friendSource 区分来源（如对方主动打招呼）。"
        ),
        args_schema=BossChatListRequest,
    )


__all__ = [
    "BossChatHistoryRequest",
    "build_boss_chat_history_tool",
    "BossChatListRequest",
    "build_boss_chat_list_tool",
]


class BossChatHistoryRequest(BaseModel):
    """Read the chat history with one HR. Read-only."""

    hr_name: str = Field(description="HR 姓名（与 list_boss_greetings 返回的 name 一致）。")
    page: int = Field(default=1, ge=1, le=10, description="消息页码（每页 20 条，从最新往回）。")


def build_boss_chat_history_tool(settings: Settings) -> StructuredTool:
    """Expose BossChatReader.read_conversation as an agent tool."""

    async def _run(hr_name: str, page: int = 1) -> dict[str, Any]:
        from jobagent.applier.boss_circuit import BossCircuit, boss_circuit_path

        circuit = BossCircuit(boss_circuit_path(settings.jobagent_state_db))
        if (refusal := circuit.check()) is not None:
            return refusal
        from jobagent.applier.boss_chat import BossChatReader
        from jobagent.journey.chat_archive import BossChatArchive

        async with BossChatReader(settings) as reader:
            result = await reader.read_conversation(hr_name=hr_name, page=page)
        circuit.record(result)
        if result.get("status") == "ok" and result.get("messages"):
            # Dual-write: every history pull archives idempotently
            # (msg_id dedupe) - funnel analytics ground truth.
            try:
                with BossChatArchive(settings.jobagent_state_db) as archive:
                    archive.upsert_history(
                        friend_id=int(result.get("friend_id") or 0),
                        friend_name=hr_name,
                        messages=result["messages"],
                    )
            except Exception:
                logger.warning("chat archive write failed", exc_info=True)
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="read_boss_conversation",
        description=(
            "读取与指定 HR 的 Boss 聊天记录（REST，只读）。**直接用 hr_name 调用，"
            "不要先调 list_boss_greetings**——本工具会在页内自行匹配名单，"
            "省下的页面预算能显著降低被反爬拦截的概率。"
            "返回按时间排列的消息（发送方向/时间/文本），并从会话对象和职位卡片"
            "恢复 job_metadata / job_candidates；没有唯一证据时不会猜测职位 URL。"
        ),
        args_schema=BossChatHistoryRequest,
    )
