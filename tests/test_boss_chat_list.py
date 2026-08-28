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
    pool.release = AsyncMock()
    reader._tab_pool = pool
    return reader


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
