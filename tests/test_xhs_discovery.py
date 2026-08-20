"""Tests for XHS interview-experience discovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobclaw.scraper.xhs_backend import DownloadedXhsNote, XhsFetchedNote, XhsNoteReference
from jobclaw.scraper.xhs_discovery import XhsDiscoveryRequest, discover_xhs_notes

SHANGHAI = timezone(timedelta(hours=8))


class FakeDiscoveryBackend:
    def __init__(self, notes: dict[str, XhsFetchedNote]) -> None:
        self.notes = notes
        self.search_call: tuple[str, int | None, int] | None = None
        self.user_call: tuple[str, int | None] | None = None
        self.downloaded: list[str] = []

    async def search_notes(
        self, query: str, *, limit: int | None = None, sort: int = 0
    ) -> list[XhsNoteReference]:
        self.search_call = (query, limit, sort)
        return [XhsNoteReference(key, key, {}) for key in self.notes]

    async def list_user_notes(
        self, user_id: str, *, limit: int | None = None
    ) -> list[XhsNoteReference]:
        self.user_call = (user_id, limit)
        return [XhsNoteReference(key, key, {}) for key in self.notes]

    async def fetch_note(self, url: str) -> XhsFetchedNote:
        return self.notes[url]

    async def download_note(
        self, url: str, *, output_dir: Path | None = None
    ) -> DownloadedXhsNote:
        self.downloaded.append(url)
        note = self.notes[url]
        directory = (output_dir or Path("data/xhs")) / note.note_id
        return DownloadedXhsNote(
            note,
            directory,
            directory / "detail.txt",
            (),
            directory / "raw.json",
        )


def make_note(
    note_id: str,
    *,
    title: str,
    body: str,
    published_at: str | None,
    author_id: str | None = None,
) -> XhsFetchedNote:
    return XhsFetchedNote(
        note_id=note_id,
        url=note_id,
        title=title,
        body=body,
        author_id=author_id or f"author-{note_id}",
        author_name="普通用户",
        image_urls=(),
        tags=("面经",),
        published_at=published_at,
        normalized={},
        raw_response={},
    )


@pytest.mark.asyncio
async def test_keyword_discovery_searches_latest_and_filters_old_and_seller_posts() -> None:
    backend = FakeDiscoveryBackend(
        {
            "good": make_note(
                "good",
                title="字节后端面经",
                body="一面问了 Redis",
                published_at="2026-08-01 12:00:00",
            ),
            "seller": make_note(
                "seller",
                title="大厂面试资料包",
                body="评论区扣1，私信领取",
                published_at="2026-08-10 12:00:00",
            ),
            "old": make_note(
                "old", title="字节面经", body="真实复盘", published_at="2025-01-01 12:00:00"
            ),
        }
    )

    result = await discover_xhs_notes(
        backend,
        XhsDiscoveryRequest(company="字节跳动", role="后端", city="北京", days=90, limit=10),
        now=datetime(2026, 8, 20, 12, tzinfo=SHANGHAI),
    )

    assert backend.search_call == ("字节跳动 后端 北京 面经", 10, 1)
    assert [item.note.note_id for item in result.accepted] == ["good"]
    rejected = {item.note.note_id: item for item in result.rejected}
    assert rejected["old"].is_fresh is False
    assert rejected["seller"].seller_risk is True
    assert "私信领取" in rejected["seller"].risk_flags


@pytest.mark.asyncio
async def test_user_id_discovery_and_download_use_user_posts() -> None:
    backend = FakeDiscoveryBackend(
        {
            "good": make_note(
                "good",
                title="后端面经",
                body="二面复盘",
                published_at="2026-08-01 12:00:00",
            )
        }
    )

    result = await discover_xhs_notes(
        backend,
        XhsDiscoveryRequest(user_id="user-123", days=90, limit=5, download_limit=1),
        now=datetime(2026, 8, 20, 12, tzinfo=SHANGHAI),
    )

    assert backend.user_call == ("user-123", 5)
    assert backend.search_call is None
    assert backend.downloaded == ["good"]
    assert len(result.downloads) == 1


@pytest.mark.asyncio
async def test_user_posts_sort_newest_first_and_propagate_account_seller_risk() -> None:
    backend = FakeDiscoveryBackend(
        {
            "old-normal": make_note(
                "old-normal",
                title="面试复盘",
                body="记录问题",
                published_at="2026-07-01 12:00:00",
                author_id="same-author",
            ),
            "new-seller": make_note(
                "new-seller",
                title="学员拿到 offer",
                body="成果展示",
                published_at="2026-08-18 12:00:00",
                author_id="same-author",
            ),
        }
    )

    result = await discover_xhs_notes(
        backend,
        XhsDiscoveryRequest(user_id="same-author", days=90, limit=5),
        now=datetime(2026, 8, 20, 12, tzinfo=SHANGHAI),
    )

    assert [item.note.note_id for item in result.candidates] == [
        "new-seller",
        "old-normal",
    ]
    assert result.accepted == ()
    assert all("学员" in item.risk_flags for item in result.rejected)


def test_discovery_request_requires_search_terms_or_user_id() -> None:
    with pytest.raises(ValueError, match="company, role, city, keyword, or user_id"):
        XhsDiscoveryRequest()
