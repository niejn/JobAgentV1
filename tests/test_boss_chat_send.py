"""Tests for BossChatSender / reply_boss_greeting (TR-6, HITL middleware-gated)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from jobagent.applier.boss_chat_send import BossChatSender
from jobagent.config import Settings
from jobagent.tools.boss_chat_send import (
    BossChatReplyRequest,
    build_boss_chat_reply_tool,
)


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
    loc.input_value = AsyncMock(return_value="")
    loc.inner_text = AsyncMock(return_value="")
    return loc


def _wrap(loc: MagicMock) -> MagicMock:
    holder = MagicMock()
    holder.first = loc
    return holder


def _pool_with(page: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=page)
    pool.detach = AsyncMock()
    pool.release = AsyncMock()
    pool.prune_blank_tabs = AsyncMock()
    return pool


def _sender_with(page: MagicMock, settings: Settings) -> BossChatSender:
    sender = BossChatSender.__new__(BossChatSender)
    sender._settings = settings
    sender._playwright = MagicMock()
    sender._context = MagicMock()
    sender._tab_pool = _pool_with(page)
    return sender


@pytest.fixture(autouse=True)
def _reset_parked_chat_page() -> None:
    import jobagent.applier.boss_chat_session as session

    session._parked = None


def _happy_page() -> MagicMock:
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/chat"
    page.is_closed = lambda: False
    page.goto = AsyncMock()
    keyboard = MagicMock()
    keyboard.press = AsyncMock()
    page.keyboard = keyboard

    async def evaluate(script: str, payload: dict | None = None):
        return True

    page.evaluate = evaluate

    search, row, input_area, send = _loc(), _loc(), _loc(), _loc()

    def locator(selector: str):
        if "placeholder" in selector or "boss-search" in selector:
            return _wrap(search)
        if "user-list" in selector:
            return _wrap(row)
        if "chat-input" in selector or "textarea" in selector or "contenteditable" in selector:
            return _wrap(input_area)
        if "发送" in selector:
            return _wrap(send)
        return _wrap(_loc(visible=False))

    page.locator = locator
    return page


@pytest.mark.asyncio
async def test_send_reply_happy_path(tmp_path: Path) -> None:
    sender = _sender_with(_happy_page(), _settings(tmp_path))
    result = await sender.send_reply(hr_name="张HR", message="您好，感谢关注！")

    assert result == {"status": "ok", "to": "张HR", "chars": 8}


@pytest.mark.asyncio
async def test_send_reply_refuses_empty_and_long(tmp_path: Path) -> None:
    sender = _sender_with(_happy_page(), _settings(tmp_path))
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
    sender = _sender_with(page, _settings(tmp_path))
    result = await sender.send_reply(hr_name="不存在", message="hi")

    assert result["status"] == "failed"
    assert result["error_type"] in {"search_box_not_found", "conversation_not_found"}


@pytest.mark.asyncio
async def test_send_unconfirmed_residual_input(tmp_path: Path) -> None:
    page = _happy_page()

    def locator(selector: str):
        holder = MagicMock()
        loc = _loc()
        if "chat-input" in selector or "textarea" in selector or "contenteditable" in selector:
            loc.inner_text = AsyncMock(return_value="您好，感谢关注！")
        holder.first = loc
        return holder

    page.locator = locator
    sender = _sender_with(page, _settings(tmp_path))
    result = await sender.send_reply(hr_name="张HR", message="您好，感谢关注！")

    assert result["status"] == "failed"
    assert result["error_type"] == "send_unconfirmed"


@pytest.mark.asyncio
async def test_send_reply_search_box_slow_mount_is_retried(tmp_path: Path) -> None:
    """Regression (live failure 2026-08-28): the chat SPA mounts after
    readyState; probing the search box once right after nav missed it.
    The sender must retry until the anchor appears, then send normally."""
    page = _happy_page()
    probes = {"count": 0}
    original_locator = page.locator

    def locator(selector: str):
        if "placeholder" in selector or "boss-search" in selector:
            probes["count"] += 1
            if probes["count"] <= 6:  # first two full sweeps: SPA not mounted yet
                return _wrap(_loc(visible=False))
        return original_locator(selector)

    page.locator = locator
    sender = _sender_with(page, _settings(tmp_path))
    with patch(
        "jobagent.applier.boss_chat_send.asyncio.sleep", new_callable=AsyncMock
    ):
        result = await sender.send_reply(hr_name="张HR", message="您好，感谢关注！")

    assert probes["count"] > 6, "search box must be probed more than once"
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_send_unverified_when_editor_clears_but_echo_fails(tmp_path: Path) -> None:
    """Tri-state verification (live lie #2, 2026-08-28): the message went
    out (editor cleared) but the panel echo check cannot run - the honest
    result is `unverified`, never a false `failed` inviting a duplicate send."""
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/chat"
    page.is_closed = lambda: False
    page.goto = AsyncMock()
    keyboard = MagicMock()
    keyboard.press = AsyncMock()
    page.keyboard = keyboard

    async def evaluate(script: str, payload: dict | None = None):
        if "insertText" in script:
            return True  # editor accepted the message
        raise RuntimeError("execution context gone")  # panel echo unavailable

    page.evaluate = evaluate

    message = "您好，感谢关注！"
    input_area = _loc()
    reads = {"count": 0}

    async def inner_text() -> str:
        reads["count"] += 1
        return message if reads["count"] == 1 else ""  # cleared after Enter

    input_area.inner_text = inner_text

    search, row = _loc(), _loc()

    def locator(selector: str):
        if "placeholder" in selector or "boss-search" in selector:
            return _wrap(search)
        if "user-list" in selector:
            return _wrap(row)
        if "chat-input" in selector or "textarea" in selector or "contenteditable" in selector:
            return _wrap(input_area)
        return _wrap(_loc(visible=False))

    page.locator = locator
    sender = _sender_with(page, _settings(tmp_path))
    with patch(
        "jobagent.applier.boss_chat_send.asyncio.sleep", new_callable=AsyncMock
    ):
        result = await sender.send_reply(hr_name="张HR", message=message)

    assert result["status"] == "unverified"
    assert result["editor_residual"] is False
    assert result["panel_echo"] is None


# ===== Schema validation tests (tool schema level) =====


@pytest.mark.asyncio
async def test_schema_validates_required_fields(tmp_path: Path) -> None:
    """工具 schema 要求 hr_name 和 message 为必填字段。"""
    # 缺少 hr_name
    with pytest.raises(ValidationError):
        BossChatReplyRequest(message="hello")
    # 缺少 message
    with pytest.raises(ValidationError):
        BossChatReplyRequest(hr_name="张HR")
    # 正常情况
    req = BossChatReplyRequest(hr_name="张HR", message="你好")
    assert req.hr_name == "张HR"
    assert req.message == "你好"


@pytest.mark.asyncio
async def test_message_length_validation(tmp_path: Path) -> None:
    """message 字段有长度限制 (max 500)。"""
    with pytest.raises(ValidationError):
        BossChatReplyRequest(hr_name="x", message="x" * 501)

    # 正常长度
    req = BossChatReplyRequest(hr_name="张HR", message="你好")
    assert req.message == "你好"


def test_tool_schema_has_no_user_confirmed_field(tmp_path: Path) -> None:
    """The old parameter gate is gone: approval is the HITL middleware
    interrupt, not a flag the model can set itself."""
    tool = build_boss_chat_reply_tool(_settings(tmp_path))
    assert "user_confirmed" not in tool.args_schema.model_fields
