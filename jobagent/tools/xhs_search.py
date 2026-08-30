"""Business Tool for Xiaohongshu keyword note search."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.config import Settings
from jobagent.scraper.xhs_backend import (
    SpiderXhsDependencyError,
    XhsAuthenticationError,
    XhsNoteReference,
    strip_xsec_token,
)

logger = logging.getLogger(__name__)

_MAX_RESULTS = 30


class XhsNoteSearchRequest(BaseModel):
    """Public Xiaohongshu note keyword search criteria."""

    query: str = Field(
        min_length=1,
        max_length=60,
        description="搜索关键词，例如「Agent 开发 招聘」「内推 简历投递」。",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=_MAX_RESULTS,
        description="返回笔记数量上限。",
    )
    sort: int = Field(
        default=0,
        ge=0,
        le=4,
        description="排序方式：0 综合，1 最新，2 最多点赞，3 最多评论，4 最多收藏。",
    )


def _pick(container: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Return the first nested dict found under any of the given keys."""
    for key in keys:
        nested = container.get(key)
        if isinstance(nested, dict):
            return nested
    return {}


def _display_fields(raw: dict[str, Any]) -> dict[str, str]:
    """Extract title/author/likes from a search item (v1 flat or v2 note_card)."""

    card = _pick(raw, "note_card", "note")
    title_source = card or raw
    title = str(title_source.get("display_title") or title_source.get("title") or "").strip()

    user = _pick(title_source, "user")
    author = str(user.get("nickname") or user.get("nick_name") or "").strip()

    interact = _pick(title_source, "interact_info")
    liked = str(interact.get("liked_count") or "").strip()

    return {"title": title, "author": author, "liked_count": liked}


class XhsNoteSearcher:
    """Search public XHS notes by keyword behind one bounded Tool."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend_factory: Any = None,
    ) -> None:
        self._settings = settings
        self._backend_factory = backend_factory

    async def search(self, request: XhsNoteSearchRequest) -> dict[str, Any]:
        """Run one keyword search and return display-ready note references."""

        if self._backend_factory is None:
            from jobagent.scraper.xhs_backend import SpiderXhsBackend

            def default_factory(s: Settings) -> Any:
                return SpiderXhsBackend(s)

            factory: Any = default_factory
        else:
            factory = self._backend_factory

        try:
            async with factory(self._settings) as backend:
                references: list[XhsNoteReference] = await backend.search_notes(
                    request.query,
                    limit=request.limit,
                    sort=request.sort,
                )
        except XhsAuthenticationError:
            return {
                "status": "blocked",
                "platform": "xiaohongshu",
                "error_type": "login_required",
                "message": "小红书登录态不可用；请先运行 jobagent login --platform xhs。",
            }
        except SpiderXhsDependencyError:
            return {
                "status": "failed",
                "platform": "xiaohongshu",
                "error_type": "dependency_unavailable",
                "message": "Spider_XHS 依赖不可用，无法执行搜索。",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("xhs note search failed", exc_info=True)
            return {
                "status": "failed",
                "platform": "xiaohongshu",
                "error_type": "search_error",
                "message": f"搜索失败：{exc}",
            }

        notes = [
            {
                "note_id": ref.note_id,
                "url": strip_xsec_token(ref.url),
                **_display_fields(ref.raw),
            }
            for ref in references[: request.limit]
        ]

        return {
            "status": "ok",
            "query": request.query,
            "sort": request.sort,
            "count": len(notes),
            "notes": notes,
        }


def build_xhs_note_search_tool(searcher: XhsNoteSearcher) -> BaseTool:
    """Expose bounded keyword search without crawler pagination controls."""

    return StructuredTool.from_function(
        coroutine=searcher.search,
        name="search_xhs_notes",
        description=(
            "按关键词搜索小红书公开笔记（招聘帖/内推/面经等）。返回笔记标题、作者、"
            "点赞数与链接。选中感兴趣的笔记后，用 save_shared_url 拉取正文与图片。"
            "sort：0 综合（默认），1 最新，2 最多点赞。"
            "搜索是读操作，无需用户确认。"
        ),
        args_schema=XhsNoteSearchRequest,
    )
