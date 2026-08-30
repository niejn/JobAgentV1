"""Tests for the Xiaohongshu keyword note search tool."""

from __future__ import annotations

import pytest

from jobagent.config import Settings
from jobagent.scraper.xhs_backend import (
    XhsAuthenticationError,
    XhsNoteReference,
)
from jobagent.tools.xhs_search import (
    XhsNoteSearcher,
    XhsNoteSearchRequest,
    build_xhs_note_search_tool,
)


class _FakeBackend:
    def __init__(self, references: list[XhsNoteReference]) -> None:
        self.references = references
        self.calls: list[tuple[str, int, int]] = []

    async def __aenter__(self) -> _FakeBackend:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def search_notes(
        self, query: str, *, limit: int | None = None, sort: int = 0
    ) -> list[XhsNoteReference]:
        self.calls.append((query, limit or 0, sort))
        return self.references


class _AuthFailBackend:
    async def __aenter__(self) -> _AuthFailBackend:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def search_notes(
        self, query: str, *, limit: int | None = None, sort: int = 0
    ) -> list[XhsNoteReference]:
        raise XhsAuthenticationError("no cookie")


def _v2_item(note_id: str, title: str, author: str, liked: str) -> dict:
    return {
        "id": note_id,
        "model_type": "note",
        "xsec_token": "tok",
        "note_card": {
            "display_title": title,
            "user": {"nickname": author},
            "interact_info": {"liked_count": liked},
        },
    }


def _factory(backend: object) -> object:
    def make(_: Settings) -> object:
        return backend

    return make


def _settings() -> Settings:
    return Settings(jobagent_env="test")


@pytest.mark.asyncio
async def test_search_returns_display_fields_and_strips_token() -> None:
    refs = [
        XhsNoteReference(
            note_id="abc",
            url="https://www.xiaohongshu.com/explore/abc?xsec_token=tok&xsec_source=pc_search",
            raw=_v2_item("abc", "招募 Agent 工程师", "HR小李", "233"),
        )
    ]
    backend = _FakeBackend(refs)
    searcher = XhsNoteSearcher(_settings(), backend_factory=_factory(backend))
    result = await searcher.search(XhsNoteSearchRequest(query="Agent 招聘", limit=5))
    assert result["status"] == "ok"
    assert result["count"] == 1
    note = result["notes"][0]
    assert note["note_id"] == "abc"
    assert note["title"] == "招募 Agent 工程师"
    assert note["author"] == "HR小李"
    assert note["liked_count"] == "233"
    assert "xsec_token" not in note["url"]
    assert backend.calls == [("Agent 招聘", 5, 0)]


@pytest.mark.asyncio
async def test_search_handles_v1_flat_items() -> None:
    refs = [
        XhsNoteReference(
            note_id="flat",
            url="https://www.xiaohongshu.com/explore/flat",
            raw={
                "id": "flat",
                "model_type": "note",
                "display_title": "内推帖",
                "user": {"nickname": "内推君"},
                "interact_info": {"liked_count": "42"},
            },
        )
    ]
    searcher = XhsNoteSearcher(_settings(), backend_factory=_factory(_FakeBackend(refs)))

    result = await searcher.search(XhsNoteSearchRequest(query="内推"))
    assert result["status"] == "ok"
    note = result["notes"][0]
    assert note["title"] == "内推帖"
    assert note["author"] == "内推君"


@pytest.mark.asyncio
async def test_search_auth_failure_returns_blocked() -> None:
    searcher = XhsNoteSearcher(_settings(), backend_factory=_factory(_AuthFailBackend()))

    result = await searcher.search(XhsNoteSearchRequest(query="任聘"))
    assert result["status"] == "blocked"
    assert result["error_type"] == "login_required"


def test_tool_metadata() -> None:
    tool = build_xhs_note_search_tool(
        XhsNoteSearcher(_settings(), backend_factory=_factory(_FakeBackend([])))
    )
    assert tool.name == "search_xhs_notes"
    assert "关键词" in str(tool.description)
