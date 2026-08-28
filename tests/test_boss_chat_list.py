"""Tests for BossChatReader / list_boss_greetings (TR-5, read-only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobagent.applier.boss_chat import BossChatReader
from jobagent.config import Settings
from jobagent.tools.boss_chat import build_boss_chat_list_tool


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        jobagent_state_db=tmp_path / "state.db",
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
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
async def test_read_conversation_chains_list_then_history() -> None:
    """read_conversation: finds the HR in the list (needs securityId),
    then calls historyMsg with the live-verified query credentials."""
    import jobagent.applier.boss_chat as chat_mod

    chat_mod._parked_page = None
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/chat"
    page.is_closed = lambda: False
    page.goto = AsyncMock()
    history_payloads: list[dict] = []

    async def evaluate(script: str, payload: dict | None = None):
        if payload is None:
            return True
        if "getGeekFriendList" in script:
            # stage 2: securityId comes from the POST, not the label GET
            return {
                "code": 0,
                "creds": [
                    {
                        "friendId": 20001,
                        "friendSource": 1,
                        "encryptBossId": "enc-boss-1",
                        "securityId": "sec-token-1",
                    }
                ],
            }
        if "geekFilterByLabel" in script:
            return {
                "code": 0,
                "friends": [
                    {
                        "friendId": 20001,
                        "friendSource": 1,
                        "name": "张HR",
                        # securityId absent: forces the two-stage fetch
                        "lastMessage": None,
                    }
                ],
            }
        if "historyMsg" in script:
            history_payloads.append(payload)
            return {
                "code": 0,
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
    reader = _reader_with_pool(pool)

    result = await reader.read_conversation(hr_name="张HR")

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["messages"][0]["text"].startswith("你好")
    assert history_payloads == [
        {"bossId": "enc-boss-1", "securityId": "sec-token-1", "page": 1}
    ]


@pytest.mark.asyncio
async def test_read_conversation_without_security_id_fails_loudly() -> None:
    import jobagent.applier.boss_chat as chat_mod

    chat_mod._parked_page = None
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/chat"
    page.is_closed = lambda: False
    page.goto = AsyncMock()

    async def evaluate(script: str, payload: dict | None = None):
        if payload is None:
            return True
        if "geekFilterByLabel" in script:
            return {
                "code": 0,
                "friends": [{"friendId": 91001, "friendSource": 0, "name": "旧会话",
                             "securityId": "", "lastMessage": None}],
            }
        if "getGeekFriendList" in script:
            return {"code": 0, "creds": []}  # stage 2 finds nothing
        return True

    page.evaluate = evaluate
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=page)
    pool.detach = AsyncMock()
    reader = _reader_with_pool(pool)

    result = await reader.read_conversation(hr_name="旧会话")

    assert result["status"] == "failed"
    assert result["error_type"] == "security_id_unavailable"
