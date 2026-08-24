"""Business Tool for browsing a user's public Xiaohongshu note list."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, field_validator

from jobagent.config import Settings
from jobagent.scraper.xhs_backend import (
    SpiderXhsBackend,
    SpiderXhsError,
    XhsAuthenticationError,
    XhsFetchedNote,
)


class XhsAuthorPostsRequest(BaseModel):
    """Public Xiaohongshu author profile and bounded browsing criteria."""

    profile_url: str = Field(description="Public Xiaohongshu user/profile URL")
    keyword: str | None = Field(
        default=None,
        description="Optional topic filter such as LangChain, LangGraph, or 面试",
    )
    limit: int = Field(default=20, ge=1, le=50)

    @field_validator("profile_url")
    @classmethod
    def validate_profile_url(cls, value: str) -> str:
        cleaned = value.strip()
        parsed = urlsplit(cleaned)
        parts = tuple(part for part in parsed.path.split("/") if part)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower()
            not in {"xiaohongshu.com", "www.xiaohongshu.com"}
            or len(parts) != 3
            or parts[:2] != ("user", "profile")
            or not parts[2]
        ):
            raise ValueError("Provide a public Xiaohongshu /user/profile/{id} URL")
        return cleaned

    @property
    def author_id(self) -> str:
        return tuple(part for part in urlsplit(self.profile_url).path.split("/") if part)[2]


class XhsAuthorPostsBrowser:
    """Reuse Spider_XHS author pagination and note normalization behind one Tool."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend_factory: Callable[[Settings], Any] = SpiderXhsBackend,
    ) -> None:
        self._settings = settings
        self._backend_factory = backend_factory

    async def browse(self, request: XhsAuthorPostsRequest) -> dict[str, Any]:
        try:
            async with self._backend_factory(self._settings) as backend:
                references = await backend.list_user_notes(
                    request.author_id,
                    limit=request.limit,
                )
        except XhsAuthenticationError:
            return {
                "status": "blocked",
                "error_type": "login_required",
                "message": "小红书登录态不可用；请先运行 jobagent login --platform xhs。",
            }
        except SpiderXhsError:
            return {
                "status": "blocked",
                "error_type": "source_access_control",
                "message": "小红书作者主页读取被拒绝或暂时不可用，未尝试绕过。",
            }

        # 逐篇 fetch_note，单篇失败不阻塞整批
        successful: list[XhsFetchedNote] = []
        failed_details: list[dict[str, str]] = []
        for ref in references:
            try:
                note = await backend.fetch_note(ref.url)
                successful.append(note)
            except Exception as exc:
                failed_details.append(
                    {
                        "note_id": ref.note_id,
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:200],
                    }
                )

        keyword = request.keyword.strip().lower() if request.keyword else ""
        selected = [
            note for note in successful if not keyword or _contains_keyword(note, keyword)
        ]
        result: dict[str, Any] = {
            "status": "completed" if successful else "failed",
            "author_id": request.author_id,
            "profile_url": request.profile_url,
            "total_found": len(references),
            "fetched": len(successful),
            "failed": len(failed_details),
            "count": len(selected),
            "posts": [_post_payload(note) for note in selected],
            "next_action": "Choose valuable post URLs and call save_shared_url for download/OCR.",
        }
        if failed_details:
            result["failed_details"] = failed_details
        if successful:
            result["status"] = (
                "partial" if failed_details else "completed"
            )
        return result


def build_xhs_author_posts_tool(browser: XhsAuthorPostsBrowser) -> BaseTool:
    """Expose bounded author browsing without crawler pagination controls."""

    async def browse_xhs_author_posts(
        profile_url: str,
        keyword: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List an author's public posts so the Agent can select valuable ones."""

        return await browser.browse(
            XhsAuthorPostsRequest(profile_url=profile_url, keyword=keyword, limit=limit)
        )

    return StructuredTool.from_function(
        coroutine=browse_xhs_author_posts,
        name="browse_xhs_author_posts",
        description=(
            "Browse a bounded list of posts from a user-supplied Xiaohongshu author profile URL. "
            "Use this when the user wants to find other valuable posts by the same author. "
            "Review titles and content, then call save_shared_url only for selected note URLs."
        ),
        args_schema=XhsAuthorPostsRequest,
    )


def _contains_keyword(note: XhsFetchedNote, keyword: str) -> bool:
    text = "\n".join((note.title, note.body, *note.tags)).lower()
    return keyword in text


def _post_payload(note: XhsFetchedNote) -> dict[str, Any]:
    return {
        "note_id": note.note_id,
        "url": note.url,
        "title": note.title,
        "body_preview": note.body[:2_000],
        "tags": list(note.tags),
        "published_at": note.published_at,
    }
