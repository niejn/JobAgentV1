"""Agent tool: read Boss chat lists - which HRs greeted us (TR-5).

Read-only: no confirmation gate (nothing is sent). Send/reply (TR-6) is a
separate, HITL-gated tool.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from jobagent.config import Settings

logger = logging.getLogger(__name__)


def _as_ms(value: Any) -> int:
    """Boss timestamps arrive as s or ms epoch depending on endpoint; normalize."""
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return ts if ts >= 1_000_000_000_000 else ts * 1000





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
        from jobagent.applier.boss_chat import BossChatReader, list_boss_greetings_http

        if settings.boss_chat_transport == "http":
            resolved_label = 0 if label_id is None and filter_name == "全部" else label_id
            if resolved_label is None:
                return {
                    "status": "failed",
                    "error_type": "unknown_filter",
                    "message": f"过滤器「{filter_name}」的 labelId 尚未校准",
                }
            result = await list_boss_greetings_http(
                settings, label_id=resolved_label, limit=limit
            )
        else:
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
    "BossChatListRequest",
    "BossChatScanRequest",
    "build_boss_chat_history_tool",
    "build_boss_chat_list_tool",
    "build_boss_chat_scan_tool",
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


class BossChatScanRequest(BaseModel):
    """Scan recent Boss conversations for HR replies awaiting our response."""

    hours_back: float = Field(
        default=24.0, ge=0.5, le=168.0, description="只扫描这段时间内活跃的会话。"
    )
    max_reads: int = Field(
        default=8, ge=1, le=20, description="最多逐个读取多少个候选会话（按活跃时间倒序）。"
    )


def build_boss_chat_scan_tool(settings: Settings) -> StructuredTool:
    """Incremental HR-reply sweep as a registered read-only tool.

    Replaces the recurring workspace scan_hr_round*.py pattern: ONE
    conversation-list call, then bounded history reads (max_reads, newest
    first) only for conversations active since the cutoff, reporting the
    ones whose last message is inbound — the HR is waiting on us.
    """

    async def _read_messages(reader: Any, hr_name: str) -> tuple[list[Any], str]:
        result = await reader.read_conversation(hr_name=hr_name)
        if result.get("status") != "ok":
            return [], str(result.get("status") or "failed")
        messages = list(result.get("messages") or [])
        if not messages:
            # Live finding (workspace reread_empty.py): the first read after a
            # fresh activity burst can return empty; one retry settles it.
            await asyncio.sleep(1.5)
            result = await reader.read_conversation(hr_name=hr_name)
            if result.get("status") != "ok":
                return [], str(result.get("status") or "failed")
            messages = list(result.get("messages") or [])
        try:
            from jobagent.journey.chat_archive import BossChatArchive

            with BossChatArchive(settings.jobagent_state_db) as archive:
                archive.upsert_history(
                    friend_id=int(result.get("friend_id") or 0),
                    friend_name=hr_name,
                    messages=messages,
                )
        except Exception:
            logger.warning("chat archive write failed", exc_info=True)
        return messages, "ok"

    async def _run(hours_back: float = 24.0, max_reads: int = 8) -> dict[str, Any]:
        from jobagent.applier.boss_circuit import BossCircuit, boss_circuit_path

        circuit = BossCircuit(boss_circuit_path(settings.jobagent_state_db))
        if (refusal := circuit.check()) is not None:
            return refusal
        from jobagent.applier.boss_chat import BossChatReader, list_boss_greetings_http

        if settings.boss_chat_transport == "http":
            listed = await list_boss_greetings_http(settings, label_id=0, limit=50)
        else:
            async with BossChatReader(settings) as reader:
                listed = await reader.list_greetings(
                    filter_name="全部", label_id=0, limit=50
                )
        if listed.get("status") != "ok":
            circuit.record(listed)
            return listed

        since_ms = int(time.time() * 1000 - hours_back * 3_600_000)
        candidates: list[tuple[int, dict[str, Any]]] = []
        for greeting in listed.get("greetings") or []:
            if not isinstance(greeting, dict):
                continue
            updated_ms = _as_ms(greeting.get("updateTime"))
            if updated_ms >= since_ms:
                candidates.append((updated_ms, greeting))
        candidates.sort(key=lambda item: item[0], reverse=True)

        awaiting: list[dict[str, Any]] = []
        unread: list[dict[str, Any]] = []
        async with BossChatReader(settings) as reader:
            for updated_ms, greeting in candidates[:max_reads]:
                hr_name = str(greeting.get("name") or "")
                if not hr_name:
                    continue
                messages, read_status = await _read_messages(reader, hr_name)
                entry = {
                    "hr_name": hr_name,
                    "friend_id": greeting.get("friendId"),
                    "company": str(greeting.get("brandName") or ""),
                    "job_title": str(
                        greeting.get("jobName") or greeting.get("positionName") or ""
                    ),
                    "last_active": datetime.fromtimestamp(updated_ms / 1000).strftime(
                        "%m-%d %H:%M"
                    ),
                }
                if read_status != "ok":
                    unread.append({**entry, "error_type": read_status})
                    continue
                if not messages:
                    unread.append({**entry, "error_type": "empty_history"})
                    continue
                last = messages[-1]
                if str(last.get("direction") or "").lower() != "boss":
                    continue  # our side sent the last word; nobody is waiting
                last_ms = _as_ms(last.get("time"))
                if last_ms < since_ms:
                    continue
                metadata = greeting.get("job_metadata") or {}
                awaiting.append(
                    {
                        **entry,
                        "last_hr_time": datetime.fromtimestamp(last_ms / 1000).strftime(
                            "%m-%d %H:%M"
                        ),
                        "last_hr_text": str(last.get("text") or "")[:120],
                        "job_id": metadata.get("job_id") or "",
                        "job_url": metadata.get("job_url") or "",
                    }
                )
        result = {
            "status": "ok",
            "since": datetime.fromtimestamp(since_ms / 1000).strftime("%Y-%m-%d %H:%M"),
            "conversations_listed": listed.get("count"),
            "candidates_read": min(len(candidates), max_reads),
            "awaiting_reply": awaiting,
            "unread": unread,
            "next": ("对 awaiting_reply 中的 HR 用 read_boss_conversation 读全文，"
                     "再按回复/投递流程处理。"),
        }
        circuit.record(result)
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="scan_boss_hr_replies",
        description=(
            "扫描 Boss 会话，找出近期有 HR 回复、正在等我们响应的会话（只读）。"
            "一次列表 + 按活跃时间倒序读取候选会话，返回 HR 名、公司、岗位、最后一条 "
            "HR 消息与时间。发打招呼后的批量回执核验、找出该回复谁，都用本工具，"
            "不要写脚本扫描。"
        ),
        args_schema=BossChatScanRequest,
    )
