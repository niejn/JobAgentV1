"""Agent tools: save_user_fact + search_history (cross-session memory)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from jobagent.config import Settings
from jobagent.memory.conversation_log import ConversationLog
from jobagent.memory.store import CandidateMemoryStore


class SaveUserFactRequest(BaseModel):
    """Persist one cross-session fact into the candidate memory file."""

    kind: str = Field(
        description="事实类别：experience=经历事实 / preference=偏好 / "
        "status=最近状态（会随进展更新）"
    )
    content: str = Field(
        min_length=2,
        max_length=300,
        description="一条自然语言事实，例如「曾在中信期货做交易所行情爬虫」。",
    )
    supersedes: str | None = Field(
        default=None,
        description="可选：被本条取代的旧事实的开头（用户改主意时用，旧条目保留但标记过期）。",
    )


def build_save_user_fact_tool(settings: Settings) -> StructuredTool:
    """Expose CandidateMemoryStore.append_fact/supersede as a tool."""

    store = CandidateMemoryStore(
        settings.jobagent_state_db.parent / "memory" / "candidate_memory.md"
    )

    async def _run(kind: str, content: str, supersedes: str | None = None) -> dict[str, Any]:
        try:
            if supersedes:
                ok = store.supersede(kind, supersedes, content)
                if not ok:
                    return {
                        "status": "failed",
                        "error_type": "fact_not_found",
                        "message": f"没有找到要取代的旧事实：「{supersedes[:30]}」。",
                    }
                return {"status": "completed", "action": "superseded",
                        "kind": kind, "content": content}
            entry = store.append_fact(kind, content)
            return {"status": "completed", "action": "saved",
                    "kind": kind, "entry": entry}
        except ValueError as exc:
            return {"status": "failed", "error_type": "invalid_fact",
                    "message": str(exc)}

    return StructuredTool.from_function(
        coroutine=_run,
        name="save_user_fact",
        description=(
            "把用户的跨会话事实写入记忆（data/memory/candidate_memory.md，"
            "每次启动自动加载）。适用：经历事实(experience)、稳定偏好(preference)、"
            "进行中状态(status)。写入前先向用户复述内容确认；对话细节不要写——"
            "历史对话用 search_history 查。用户改主意时用 supersedes 参数取代旧事实。"
        ),
        args_schema=SaveUserFactRequest,
    )


class SearchHistoryRequest(BaseModel):
    """Search past conversation logs (JSONL, keyword based)."""

    query: str = Field(
        min_length=1,
        description="关键词，空格分隔多个词（任一命中即返回，结果含前后各2行上下文）。",
    )
    day: str | None = Field(
        default=None,
        description="可选 YYYY-MM-DD，只查该天；不填则查最近 7 天。"
        "「昨天」请自行换算成日期。",
    )
    limit: int = Field(default=15, ge=1, le=50, description="最多返回条数。")


def build_search_history_tool(settings: Settings) -> StructuredTool:
    """Expose ConversationLog.search as a tool."""

    log = ConversationLog(
        settings.jobagent_state_db.parent / "conversations"
    )

    async def _run(query: str, day: str | None = None, limit: int = 15) -> dict[str, Any]:
        return log.search(query, day=day, limit=limit)

    return StructuredTool.from_function(
        coroutine=_run,
        name="search_history",
        description=(
            "检索历史对话存档（按天 JSONL，只读）。用户问「之前说过/推荐过/提到过什么」"
            "时先用这个工具查原文，不要凭记忆猜。命中行带前后各 2 行上下文"
            "（提问和回答通常相邻）。"
        ),
        args_schema=SearchHistoryRequest,
    )


__all__ = [
    "SaveUserFactRequest",
    "SearchHistoryRequest",
    "build_save_user_fact_tool",
    "build_search_history_tool",
    "Path",
]
