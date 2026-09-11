"""Tests for BossChatReader / list_boss_greetings (TR-5, read-only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobagent.applier.boss_chat import (
    BossChatReader,
    _normalize_job_metadata,
    _resolve_registry_job_metadata,
    list_boss_greetings_http,
)
from jobagent.config import Settings
from jobagent.journey.job_registry import SQLiteJobRegistry
from jobagent.tools.boss_chat import build_boss_chat_list_tool


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        jobagent_state_db=tmp_path / "state.db",
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
        boss_chat_transport="cdp",
    )


def _chat_page() -> MagicMock:
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/chat"
    page.is_closed = lambda: False
    page.goto = AsyncMock()

    async def evaluate(script: str, payload: dict | None = None):
        if payload is None:
            return True  # settle probe
        assert "geekFilterByLabel" in script
        assert "labelId=" in script
        # payload carries the raw label id; assert covers both call shapes
        assert payload["labelId"] in (0, 7)
        return {
            "code": 0,
            "friends": [
                {
                    "friendId": 20001,
                    "friendSource": 1,
                    "encryptFriendId": "enc-1",
                    "name": "张HR",
                    "brandName": "字节跳动",
                    "jobName": "后端开发",
                    "positionName": "后端",
                    "bossTitle": "招聘者",
                    "jobCity": "上海",
                    "jobMetadata": {
                        "jobId": "job-123",
                        "jobUrl": "https://www.zhipin.com/job_detail/job-123.html?securityId=temporary",
                        "title": "后端开发",
                        "company": "字节跳动",
                        "city": "上海",
                        "source": "conversation_friend",
                    },
                    "updateTime": 1787900000,
                    "lastMessage": {"text": "你好，我们团队在招后端", "fromId": 20001},
                },
                {  # system account - must be filtered out
                    "friendId": 100,
                    "friendSource": 0,
                    "name": "职位助理",
                    "lastMessage": None,
                },
            ],
        }

    page.evaluate = evaluate
    return page


def _reader_with(page: MagicMock) -> BossChatReader:
    reader = BossChatReader.__new__(BossChatReader)
    reader._settings = _settings(Path("."))
    reader._playwright = MagicMock()
    reader._context = MagicMock()
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=page)
    pool.detach = AsyncMock()
    pool.prune_blank_tabs = AsyncMock()
    pool.release = AsyncMock()
    reader._tab_pool = pool
    return reader


@pytest.fixture(autouse=True)
def _reset_parked_chat_page() -> None:
    import jobagent.applier.boss_chat_session as session

    session._parked = None


@pytest.mark.asyncio
async def test_list_greetings_happy_path(tmp_path: Path) -> None:
    reader = _reader_with(_chat_page())
    result = await reader.list_greetings(filter_name="全部")

    assert result["status"] == "ok"
    assert result["filter_label_id"] == 0
    assert result["count"] == 1  # system account filtered
    hr = result["greetings"][0]
    assert hr["name"] == "张HR"
    assert hr["brandName"] == "字节跳动"
    assert hr["lastMessage"]["text"].startswith("你好")
    assert hr["encryptFriendId"] == "enc-1"
    assert hr["job_metadata"] == {
        "job_id": "job-123",
        "job_url": "https://www.zhipin.com/job_detail/job-123.html",
        "title": "后端开发",
        "company": "字节跳动",
        "city": "上海",
        "source": "conversation_friend",
    }


def test_job_metadata_uses_stable_job_url_without_security_id() -> None:
    result = _normalize_job_metadata(
        {
            "jobUrl": "https://www.zhipin.com/job_detail/stable-id.html?securityId=temporary",
            "title": "AI Agent 工程师",
        },
        source="conversation_message",
    )

    assert result is not None
    assert result["job_id"] == "stable-id"
    assert result["job_url"] == "https://www.zhipin.com/job_detail/stable-id.html"
    assert result["source"] == "conversation_message"


def test_job_metadata_rejects_non_boss_urls() -> None:
    result = _normalize_job_metadata(
        {"jobUrl": "https://evilzhipin.com/job_detail/not-a-boss-job.html"},
        source="conversation_message",
    )

    assert result is None


def test_job_metadata_falls_back_only_to_one_exact_registry_match(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with SQLiteJobRegistry(settings.jobagent_state_db) as registry:
        registry.upsert_discovered(
            job_id="boss:stable-id",
            source="boss",
            company="四川影目",
            title="AI agent 工程师",
            location="上海",
            url="https://www.zhipin.com/job_detail/stable-id.html?securityId=temporary",
        )

    result = _resolve_registry_job_metadata(
        settings, company="四川影目", title="AI Agent 工程师"
    )

    assert result is not None
    assert result["job_id"] == "stable-id"
    assert result["job_url"] == "https://www.zhipin.com/job_detail/stable-id.html"
    assert result["source"] == "job_registry_exact_match"


@pytest.mark.asyncio
async def test_uncalibrated_filter_rejected_with_hint(tmp_path: Path) -> None:
    reader = _reader_with(_chat_page())
    result = await reader.list_greetings(filter_name="新招呼")

    assert result["status"] == "failed"
    assert result["error_type"] == "unknown_filter"
    assert "label_id" in result["message"]


@pytest.mark.asyncio
async def test_label_id_bypasses_name_map(tmp_path: Path) -> None:
    reader = _reader_with(_chat_page())
    result = await reader.list_greetings(label_id=7)

    assert result["status"] == "ok"
    assert result["filter_label_id"] == 7


@pytest.mark.asyncio
async def test_api_rejection_reports_stoken(tmp_path: Path) -> None:
    page = _chat_page()

    async def evaluate(script: str, payload: dict | None = None):
        if payload is None:
            return True
        return {"code": 121, "message": "请求不合法(121).", "stokenPresent": False}

    page.evaluate = evaluate
    reader = _reader_with(page)
    result = await reader.list_greetings()

    assert result["status"] == "failed"
    assert result["error_type"] == "api_rejected"
    assert result["stoken_present"] is False


@pytest.mark.asyncio
async def test_tool_runs_read_only(tmp_path: Path) -> None:
    tool = build_boss_chat_list_tool(_settings(tmp_path))
    assert tool.name == "list_boss_greetings"

    async def _fake_run(**kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "count": 0, "greetings": []}

    with patch("jobagent.applier.boss_chat.BossChatReader") as reader_cls:
        reader = MagicMock()
        reader.__aenter__ = AsyncMock(return_value=reader)
        reader.__aexit__ = AsyncMock(return_value=False)
        reader.list_greetings = _fake_run
        reader_cls.return_value = reader
        result = await tool.coroutine(filter_name="全部")

    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_http_list_does_not_require_chat_page(tmp_path: Path) -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "code": 0,
        "zpData": {
            "friendList": [
                {
                    "friendId": 20001,
                    "friendSource": 1,
                    "encryptFriendId": "enc-1",
                    "name": "张HR",
                    "brandName": "字节跳动",
                    "jobName": "后端开发",
                    "jobCity": "上海",
                },
                {"friendId": 100, "name": "职位助理"},
            ]
        },
    }
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)
    settings = _settings(tmp_path)
    with (
        patch("jobagent.auth.cookie_manager.get_cookies", new=AsyncMock(return_value=[
            {"name": "bst", "value": "b"}
        ])),
        patch("httpx.AsyncClient", return_value=client),
    ):
        result = await list_boss_greetings_http(settings)
    assert result["status"] == "ok"
    assert result["transport"] == "http"
    assert result["count"] == 1
    assert result["greetings"][0]["name"] == "张HR"


@pytest.mark.asyncio
async def test_parked_chat_page_is_reused_without_reload() -> None:
    """Regression (2026-08-28 live lesson): rapid chat-page loads trip
    warlock. The second list call in one process must NOT navigate again -
    it reuses the parked tab and only runs the fetch."""
    import jobagent.applier.boss_chat_session as session

    session._parked = None
    page = _chat_page()
    nav_calls: list[str] = []

    async def goto(url: str, **kwargs: object) -> None:
        nav_calls.append(url)

    page.goto = goto
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=page)
    pool.detach = AsyncMock()
    pool.prune_blank_tabs = AsyncMock()
    reader = _reader_with_pool(pool)

    first = await reader.list_greetings(filter_name="全部")
    second = await reader.list_greetings(filter_name="全部")

    assert first["status"] == "ok" and second["status"] == "ok"
    assert len(nav_calls) == 1  # parked: loaded once, never reloaded
    assert pool.acquire.await_count == 1  # single tab for both calls


def _reader_with_pool(pool: MagicMock) -> BossChatReader:
    reader = BossChatReader.__new__(BossChatReader)
    reader._settings = _settings(Path("."))
    reader._playwright = MagicMock()
    reader._context = MagicMock()
    reader._tab_pool = pool
    return reader


@pytest.mark.asyncio
async def test_read_conversation_single_evaluate_chain() -> None:
    """read_conversation runs list -> creds -> history as ONE page
    evaluate (warlock tolerates only ~2-3 automated fetch rounds per page
    load; the multi-stage Python flow burned the budget)."""
    import jobagent.applier.boss_chat as chat_mod

    chat_mod._parked_page = None
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/chat"
    page.is_closed = lambda: False
    page.goto = AsyncMock()
    evaluate_payloads: list[dict] = []

    async def evaluate(script: str, payload: dict | None = None):
        if payload is None:
            return True
        if "geekFilterByLabel" in script and "historyMsg" in script:
            # the merged full-chain script
            evaluate_payloads.append(payload)
            return {
                "step": "done",
                "job": {
                    "jobId": "job-123",
                    "jobUrl": "https://www.zhipin.com/job_detail/job-123.html?securityId=temporary",
                    "title": "后端开发",
                    "company": "字节跳动",
                    "source": "conversation_friend",
                },
                "jobCandidates": [
                    {
                        "jobId": "card-job-456",
                        "jobUrl": "https://www.zhipin.com/job_detail/card-job-456.html?securityId=temporary",
                        "source": "conversation_message",
                    }
                ],
                "messages": [
                    {"fromId": 20001, "toId": 1, "time": 1787900100,
                     "type": 1, "text": "你好，我们团队在招后端"},
                    {"fromId": 1, "toId": 20001, "time": 1787900200,
                     "type": 1, "text": "您好，感谢关注"},
                ],
            }
        return True

    page.evaluate = evaluate
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=page)
    pool.detach = AsyncMock()
    pool.prune_blank_tabs = AsyncMock()
    reader = _reader_with_pool(pool)

    result = await reader.read_conversation(hr_name="张HR")

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["messages"][0]["text"].startswith("你好")
    assert result["job_metadata"]["job_url"] == "https://www.zhipin.com/job_detail/job-123.html"
    assert result["job_candidates"][0]["job_id"] == "card-job-456"
    # exactly ONE automated fetch round, with the merged script
    assert evaluate_payloads == [{"hrName": "张HR", "page": 1}]


@pytest.mark.asyncio
async def test_read_conversation_step_failures_are_typed() -> None:
    import jobagent.applier.boss_chat as chat_mod

    chat_mod._parked_page = None
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/chat"
    page.is_closed = lambda: False
    page.goto = AsyncMock()

    outcomes = iter(
        [
            {"step": "match", "code": 0, "message": "conversation not found"},
            {"step": "creds", "code": 0, "message": "no securityId returned"},
            {"step": "history", "code": 121, "message": "请求不合法"},
        ]
    )

    async def evaluate(script: str, payload: dict | None = None):
        if payload is None:
            return True
        return next(outcomes)

    page.evaluate = evaluate
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=page)
    pool.detach = AsyncMock()
    pool.prune_blank_tabs = AsyncMock()
    reader = _reader_with_pool(pool)

    r1 = await reader.read_conversation(hr_name="无此人")
    r2 = await reader.read_conversation(hr_name="张HR")
    r3 = await reader.read_conversation(hr_name="张HR")

    assert (r1["error_type"], r2["error_type"], r3["error_type"]) == (
        "conversation_not_found",
        "security_id_unavailable",
        "api_rejected",
    )
    assert r3["api_step"] == "history"


@pytest.mark.asyncio
async def test_read_conversation_self_heals_on_dead_page() -> None:
    """page_lost on the first evaluate (warlock's delayed kill) gets ONE
    fresh-tab retry - the new page lands inside a fresh fetch budget."""
    import jobagent.applier.boss_chat_session as session_mod

    session_mod._parked = None
    calls = {"n": 0}

    def make_page():
        page = MagicMock()
        page.url = "https://www.zhipin.com/web/geek/chat"
        page.is_closed = lambda: False
        page.goto = AsyncMock()

        async def evaluate(script: str, payload: dict | None = None):
            if payload is None:
                return True
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Execution context was destroyed")
            return {
                "step": "done",
                "messages": [{"fromId": 1, "toId": 2, "time": 1,
                              "type": 1, "text": "hi"}],
            }

        page.evaluate = evaluate
        return page

    pages = [make_page(), make_page()]
    pool = MagicMock()
    pool.acquire = AsyncMock(side_effect=pages)
    pool.detach = AsyncMock()
    pool.prune_blank_tabs = AsyncMock()
    reader = _reader_with_pool(pool)

    result = await reader.read_conversation(hr_name="张HR")

    assert result["status"] == "ok"
    assert calls["n"] == 2  # died once, healed once
    assert pool.acquire.await_count == 2
