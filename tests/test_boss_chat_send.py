"""Tests for BossChatSender / reply_boss_greeting (TR-6, HITL-gated)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jobagent.applier.boss_chat_send import BossChatSender
from jobagent.config import Settings
from jobagent.tools.boss_chat_send import build_boss_chat_reply_tool


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        jobagent_state_db=tmp_path / "state.db",
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
    )


def _loc(visible: bool = True) -> MagicMock:
    loc = MagicMock()
    loc.is_visible = AsyncMock(return_value=visible)
    loc.click = AsyncMock()
    loc.fill = AsyncMock()
    loc.type = AsyncMock()
    loc.press = AsyncMock()
    loc.input_value = AsyncMock(return_value="")  # cleared = sent
    loc.inner_text = AsyncMock(return_value="")
    return loc


def _happy_page() -> MagicMock:
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/chat"
    page.is_closed = lambda: False
    page.goto = AsyncMock()

    async def evaluate(script: str):
        return True  # settle probes

    page.evaluate = evaluate

    search, row, input_area, send = _loc(), _loc(), _loc(), _loc()

    def _wrap(loc: MagicMock) -> MagicMock:
        holder = MagicMock()
        holder.first = loc  # playwright Locator.first
        return holder

    def locator(selector: str):
        if "placeholder" in selector or "boss-search" in selector:
            return _wrap(search)
        if "user-list" in selector:
            return _wrap(row)
        if "textarea" in selector or "contenteditable" in selector:
            return _wrap(input_area)
        if "发送" in selector:
            return _wrap(send)
        return _wrap(_loc(visible=False))

    page.locator = locator
    return page


def _pool_with(page: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=page)
    pool.detach = AsyncMock()
    pool.release = AsyncMock()
    return pool


def _sender_with(page: MagicMock) -> BossChatSender:
    sender = BossChatSender.__new__(BossChatSender)
    sender._settings = _settings(Path("."))
    sender._playwright = MagicMock()
    sender._context = MagicMock()
    sender._tab_pool = _pool_with(page)
    return sender


@pytest.fixture(autouse=True)
def _reset_parked_chat_page() -> None:
    import jobagent.applier.boss_chat_session as session

    session._parked = None


@pytest.mark.asyncio
async def test_send_reply_happy_path(tmp_path: Path) -> None:
    sender = _sender_with(_happy_page())
    result = await sender.send_reply(hr_name="张HR", message="您好，感谢关注！")

    assert result == {"status": "ok", "to": "张HR", "chars": 8}


@pytest.mark.asyncio
async def test_send_reply_refuses_empty_and_long(tmp_path: Path) -> None:
    sender = _sender_with(_happy_page())
    assert (await sender.send_reply(hr_name="x", message="  "))["error_type"] == "empty_message"
    assert (
        await sender.send_reply(hr_name="x", message="字" * 501)
    )["error_type"] == "message_too_long"


@pytest.mark.asyncio
async def test_send_reply_conversation_not_found(tmp_path: Path) -> None:
    page = _happy_page()

    def locator(selector: str):
        holder = MagicMock()
        holder.first = _loc(visible=False)
        return holder

    page.locator = locator
    sender = _sender_with(page)
    result = await sender.send_reply(hr_name="不存在", message="hi")

    assert result["status"] == "failed"
    assert result["error_type"] in {"search_box_not_found", "conversation_not_found"}


@pytest.mark.asyncio
async def test_send_unconfirmed_residual_input(tmp_path: Path) -> None:
    page = _happy_page()

    def locator(selector: str):
        holder = MagicMock()
        loc = _loc()
        if "textarea" in selector or "contenteditable" in selector:
            loc.input_value = AsyncMock(return_value="您好，感谢关注！")  # still there
        holder.first = loc
        return holder

    page.locator = locator
    sender = _sender_with(page)
    result = await sender.send_reply(hr_name="张HR", message="您好，感谢关注！")

    assert result["status"] == "failed"
    assert result["error_type"] == "send_unconfirmed"


@pytest.mark.asyncio
async def test_tool_gates_on_user_confirmation(tmp_path: Path) -> None:
    tool = build_boss_chat_reply_tool(_settings(tmp_path))
    gated = await tool.coroutine(hr_name="张HR", message="您好", user_confirmed=False)
    assert gated["status"] == "waiting_user_confirmation"
    assert gated["message"] == "您好"


@pytest.mark.asyncio
async def test_tool_confirmed_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = build_boss_chat_reply_tool(_settings(tmp_path))
    sent: list[Any] = []

    class _FakeSender:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeSender:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def send_reply(self, **kwargs: Any) -> dict[str, Any]:
            sent.append(kwargs)
            return {"status": "ok", "to": kwargs["hr_name"], "chars": 2}

    monkeypatch.setattr("jobagent.applier.boss_chat_send.BossChatSender", _FakeSender)
    result = await tool.coroutine(hr_name="张HR", message="你好", user_confirmed=True)

    assert result["status"] == "ok"
    assert sent[0]["message"] == "你好"
