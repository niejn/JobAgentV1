"""Business Tool for browsing a user's public Xiaohongshu note list."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, field_validator, model_validator

from jobagent.config import Settings
from jobagent.scraper.xhs_backend import (
    SpiderXhsBackend,
    SpiderXhsError,
    XhsAuthenticationError,
    XhsFetchedNote,
    XhsNoteReference,
    strip_xsec_token,
)

#: fetch_all 模式的列表上限：游标循环翻到 has_more=false 为止，实际永远
#: 远小于此值；仅为防御性封顶（防止上游异常时无限翻页）。
_FETCH_ALL_LIST_CAP = 10_000


@dataclass(frozen=True, slots=True)
class XhsAuthorBrowseCheckpoint:
    """fetch_all 模式的断点：剩余待抓详情的帖子引用与累计计数。"""

    author_id: str
    keyword: str
    total_found: int
    completed_count: int
    failed_count: int
    remaining_refs: tuple[XhsNoteReference, ...]
    updated_at: str


class XhsAuthorBrowseCheckpoints:
    """SQLite 断点存储（进程级持久：中断/重启后可续抓）。

    与 SQLiteJobRegistry 同模式：同步 sqlite3 + WAL，工具内 with 块短事务
    使用。remaining_refs 只持久 note_id 与带 token 的 URL——详情重抓只需要
    这两样，raw 不入（避免膨胀）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS xhs_author_browse_checkpoints (
                author_id TEXT PRIMARY KEY,
                keyword TEXT NOT NULL DEFAULT '',
                total_found INTEGER NOT NULL,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                remaining_refs_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> XhsAuthorBrowseCheckpoints:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def load(self, author_id: str) -> XhsAuthorBrowseCheckpoint | None:
        row = self._connection.execute(
            "SELECT * FROM xhs_author_browse_checkpoints WHERE author_id = ?",
            (author_id,),
        ).fetchone()
        if row is None:
            return None
        raw_refs = json.loads(row["remaining_refs_json"])
        remaining = tuple(
            XhsNoteReference(
                note_id=str(item["note_id"]),
                url=str(item["url"]),
                raw={},
            )
            for item in raw_refs
        )
        return XhsAuthorBrowseCheckpoint(
            author_id=row["author_id"],
            keyword=row["keyword"],
            total_found=int(row["total_found"]),
            completed_count=int(row["completed_count"]),
            failed_count=int(row["failed_count"]),
            remaining_refs=remaining,
            updated_at=row["updated_at"],
        )

    def save(self, checkpoint: XhsAuthorBrowseCheckpoint) -> None:
        refs_json = json.dumps(
            [
                {"note_id": ref.note_id, "url": ref.url}
                for ref in checkpoint.remaining_refs
            ],
            ensure_ascii=False,
        )
        self._connection.execute(
            """
            INSERT INTO xhs_author_browse_checkpoints (
                author_id, keyword, total_found, completed_count,
                failed_count, remaining_refs_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(author_id) DO UPDATE SET
                keyword = excluded.keyword,
                total_found = excluded.total_found,
                completed_count = excluded.completed_count,
                failed_count = excluded.failed_count,
                remaining_refs_json = excluded.remaining_refs_json,
                updated_at = excluded.updated_at
            """,
            (
                checkpoint.author_id,
                checkpoint.keyword,
                checkpoint.total_found,
                checkpoint.completed_count,
                checkpoint.failed_count,
                refs_json,
                checkpoint.updated_at,
            ),
        )
        self._connection.commit()

    def delete(self, author_id: str) -> None:
        self._connection.execute(
            "DELETE FROM xhs_author_browse_checkpoints WHERE author_id = ?",
            (author_id,),
        )
        self._connection.commit()


class XhsAuthorPostsRequest(BaseModel):
    """Public Xiaohongshu author profile and bounded browsing criteria."""

    profile_url: str = Field(description="Public Xiaohongshu user/profile URL")
    keyword: str | None = Field(
        default=None,
        description="Optional topic filter such as LangChain, LangGraph, or 面试",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Bounded mode only: max posts returned in one call.",
    )
    fetch_all: bool = Field(
        default=False,
        description=(
            "Fetch ALL posts (may be hundreds). Details are fetched in batches "
            "of batch_limit per call; each call returns one batch plus a "
            "persisted checkpoint so an interrupted run can resume."
        ),
    )
    batch_limit: int = Field(
        default=50,
        ge=1,
        le=100,
        description="fetch_all only: how many post details to fetch per call.",
    )
    resume: bool = Field(
        default=False,
        description=(
            "fetch_all only: continue from this author's persisted checkpoint. "
            "Without a checkpoint this starts fresh."
        ),
    )

    @model_validator(mode="after")
    def _reject_resume_without_fetch_all(self) -> XhsAuthorPostsRequest:
        if self.resume and not self.fetch_all:
            raise ValueError("resume requires fetch_all=true")
        return self

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
        if request.fetch_all:
            return await self._browse_fetch_all(request)
        try:
            # 列表与逐篇详情都必须在同一个 async with 块内：backend 的
            # __aexit__ 会 close() 并置空 _api，块外再调 fetch_note 只会得到
            # "SpiderXhsBackend is not started"（曾致 26/26 详情全败）。
            async with self._backend_factory(self._settings) as backend:
                references = await backend.list_user_notes(
                    request.author_id,
                    limit=request.limit,
                )
                # 逐篇 fetch_note，单篇失败不阻塞整批；每篇 URL 自带
                # 列表接口返回的 per-note xsec_token，无需用户逐帖提供。
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

    async def _browse_fetch_all(self, request: XhsAuthorPostsRequest) -> dict[str, Any]:
        """全量抓取：列表到底 + 分批抓详情 + checkpoint 断点续抓。

        中断恢复是进程级的：剩余引用持久化在 state.db，即使进程重启也能用
        resume=true 续抓（列表阶段很快，不需要断点；耗时的是详情抓取，断点
        在详情批次粒度）。每批返回本批 posts，Agent 拿到足够内容后也可选择
        不再续抓（自然早退，残留 checkpoint 会被下次 fresh 覆盖）。
        """

        state_db = self._settings.jobagent_state_db.expanduser().resolve()
        keyword = request.keyword.strip().lower() if request.keyword else ""

        checkpoint: XhsAuthorBrowseCheckpoint | None = None
        if request.resume:
            with XhsAuthorBrowseCheckpoints(state_db) as store:
                checkpoint = store.load(request.author_id)

        fresh_listing = checkpoint is None
        try:
            async with self._backend_factory(self._settings) as backend:
                if fresh_listing:
                    references = await backend.list_user_notes(
                        request.author_id,
                        limit=_FETCH_ALL_LIST_CAP,
                    )
                    now = datetime.now().astimezone().isoformat()
                    checkpoint = XhsAuthorBrowseCheckpoint(
                        author_id=request.author_id,
                        keyword=keyword,
                        total_found=len(references),
                        completed_count=0,
                        failed_count=0,
                        remaining_refs=tuple(references),
                        updated_at=now,
                    )

                assert checkpoint is not None
                batch = checkpoint.remaining_refs[: request.batch_limit]
                rest = checkpoint.remaining_refs[len(batch) :]
                successful: list[XhsFetchedNote] = []
                failed_details: list[dict[str, str]] = []
                for ref in batch:
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

        assert checkpoint is not None
        completed_total = checkpoint.completed_count + len(successful)
        failed_total = checkpoint.failed_count + len(failed_details)

        if rest:
            updated = XhsAuthorBrowseCheckpoint(
                author_id=checkpoint.author_id,
                keyword=checkpoint.keyword,
                total_found=checkpoint.total_found,
                completed_count=completed_total,
                failed_count=failed_total,
                remaining_refs=rest,
                updated_at=datetime.now().astimezone().isoformat(),
            )
            with XhsAuthorBrowseCheckpoints(state_db) as store:
                store.save(updated)
            status = "resumable"
            next_action = (
                "More batches remain; call browse_xhs_author_posts again with "
                "fetch_all=true, resume=true to continue."
            )
        else:
            with XhsAuthorBrowseCheckpoints(state_db) as store:
                store.delete(request.author_id)
            status = "partial" if failed_details else "completed"
            next_action = (
                "All posts fetched; select valuable post URLs and call "
                "save_shared_url for download/OCR."
            )

        selected = [
            note for note in successful if not keyword or _contains_keyword(note, keyword)
        ]
        result: dict[str, Any] = {
            "status": status,
            "mode": "fetch_all",
            "resumed": not fresh_listing,
            "author_id": request.author_id,
            "profile_url": request.profile_url,
            "total_found": checkpoint.total_found,
            "fetched_this_batch": len(successful),
            "failed_this_batch": len(failed_details),
            "completed_total": completed_total,
            "failed_total": failed_total,
            "remaining": len(rest),
            "count": len(selected),
            "posts": [_post_payload(note) for note in selected],
            "next_action": next_action,
        }
        if failed_details:
            result["failed_details"] = failed_details
        if not checkpoint.total_found:
            result["status"] = "failed"
            result["next_action"] = "No posts found for this author."
        return result


def build_xhs_author_posts_tool(browser: XhsAuthorPostsBrowser) -> BaseTool:
    """Expose bounded author browsing without crawler pagination controls."""

    async def browse_xhs_author_posts(
        profile_url: str,
        keyword: str | None = None,
        limit: int = 20,
        fetch_all: bool = False,
        batch_limit: int = 50,
        resume: bool = False,
    ) -> dict[str, Any]:
        """List an author's public posts so the Agent can select valuable ones.

        Tracking note: rebuild the profile URL from a tracked author_id any
        time - the share token is unused and per-note tokens refresh with
        every listing. Only cookie expiry needs user action (login command).
        """

        return await browser.browse(
            XhsAuthorPostsRequest(
                profile_url=profile_url,
                keyword=keyword,
                limit=limit,
                fetch_all=fetch_all,
                batch_limit=batch_limit,
                resume=resume,
            )
        )

    return StructuredTool.from_function(
        coroutine=browse_xhs_author_posts,
        name="browse_xhs_author_posts",
        description=(
            "Browse posts from a Xiaohongshu author profile URL. "
            "Bounded mode (default) returns up to `limit` posts in one call; "
            "fetch_all=true lists every post and fetches details in batches of "
            "batch_limit, persisting a checkpoint so interrupted runs resume "
            "with fetch_all=true, resume=true. "
            "Token model: the profile share token in the URL is NOT used - only "
            "the stable author_id path segment matters, so you can rebuild the "
            "profile URL from a tracked author_id at any time (e.g. to check an "
            "author for updates) without asking the user for a fresh share "
            "link; per-note tokens come back fresh from every listing response. "
            "The only credential involved is the XHS login cookie (re-run "
            "jobagent login --platform xhs if it expires). "
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
        "url": strip_xsec_token(note.url),
        "title": note.title,
        "body_preview": note.body[:2_000],
        "tags": list(note.tags),
        "published_at": note.published_at,
    }
