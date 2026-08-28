"""Send a chat reply to a Boss直聘 HR via the user's Chrome (CDP) - TR-6.

Transport: chat-page UI automation with anchor probing (no hardcoded
fragile class names). Rationale (2026-08-28 live findings):

- Boss message sending is NOT REST - it rides a WebSocket driven by the
  page's chat-core (SharedWorker RPC); replicating that protocol is a
  much larger, riskier surface than letting the page send it.
- Manual chatting (typing + clicking) never trips warlock - only
  filechooser/F12/CDP-listener attaches do. UI automation replicates the
  user's own daily behaviour and the page attaches its own stoken/headers.
- Anchors: search box placeholder, HR name text in .user-list, the chat
  input area (textarea/contenteditable), and a 发送 button - each step
  probes several shapes and reports which failed, so live calibration is
  a one-run affair.

HITL: the tool layer refuses to send without user_confirmed=true; the
message text itself is the confirmed artefact.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from jobagent.config import Settings
from jobagent.scraper.cdp_tab_pool import CdpTabPool

logger = logging.getLogger(__name__)

_CHAT_PAGE_PATH = "/web/geek/chat"
_NAV_TIMEOUT_MS = 30_000
_STEP_TIMEOUT_S = 20.0
_MAX_MESSAGE_CHARS = 500

_SEARCH_INPUT_ANCHORS = [
    "input[placeholder*='联系人']",
    "input[placeholder*='搜索']",
    ".boss-search-input",
]
_INPUT_AREA_ANCHORS = [
    "textarea[placeholder*='输入']",
    "textarea",
    "[contenteditable='true']",
]
_SEND_BUTTON_ANCHORS = [
    "button:has-text('发送')",
    "[class*='send']:has-text('发送')",
    "a:has-text('发送')",
]


class BossChatSender:
    """Reply to one HR conversation through the chat page UI."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._tab_pool: CdpTabPool | None = None

    async def __aenter__(self) -> BossChatSender:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            browser = await self._playwright.chromium.connect_over_cdp(
                self._settings.xhs_cdp_endpoint,
                timeout=10_000,
            )
        except Exception as exc:
            await self._playwright.stop()
            raise ConnectionError(
                "无法连接调试 Chrome（CDP）。请先启动带 --remote-debugging-port 的 Chrome。"
            ) from exc
        self._context = next((c for c in browser.contexts if c.pages), None)
        if self._context is None:
            await self._playwright.stop()
            raise ConnectionError("调试 Chrome 中没有可用的浏览器上下文。")
        self._tab_pool = CdpTabPool(self._context)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._tab_pool is not None:
            await self._tab_pool.close()
        self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def send_reply(
        self, *, hr_name: str, message: str
    ) -> dict[str, Any]:
        """Send `message` in the conversation with `hr_name`."""

        if not message.strip():
            return {
                "status": "failed",
                "error_type": "empty_message",
                "message": "回复内容为空。",
            }
        if len(message) > _MAX_MESSAGE_CHARS:
            return {
                "status": "failed",
                "error_type": "message_too_long",
                "message": f"回复超过 {_MAX_MESSAGE_CHARS} 字（当前 {len(message)}）。",
            }
        assert self._tab_pool is not None
        try:
            return await self._send_on_page(
                self._tab_pool, hr_name=hr_name, message=message
            )
        except TimeoutError:
            return {
                "status": "failed",
                "error_type": "tab_pool_timeout",
                "message": "浏览器 tab 池超时。",
            }

    # -- page flow --------------------------------------------------------------

    async def _send_on_page(
        self, pool: Any, *, hr_name: str, message: str
    ) -> dict[str, Any]:
        from jobagent.applier.boss_chat_session import (
            chat_page_url_path,
            get_chat_page,
        )

        page, fresh = await get_chat_page(pool)
        if fresh:
            # First use this process: load and park the chat page once.
            try:
                await page.goto(
                    f"https://www.zhipin.com{chat_page_url_path()}",
                    wait_until="domcontentloaded",
                    timeout=_NAV_TIMEOUT_MS,
                )
            except Exception:
                logger.info("Boss chat reply: nav interrupted", exc_info=True)
            deadline = asyncio.get_event_loop().time() + 20.0
            while asyncio.get_event_loop().time() < deadline:
                if urlsplit(str(page.url or "")).path == _CHAT_PAGE_PATH:
                    break
                await asyncio.sleep(0.5)
            else:
                return {
                    "status": "failed",
                    "error_type": "chat_page_blocked",
                    "message": "聊天页被反爬拦截（跳转空白页）。建议暂停并稍后再试。",
                }
        # Parked page: drive the conversation without reloading.

        # 1) Find the search box and narrow the list to the target HR.
        search = await self._first_visible(page, _SEARCH_INPUT_ANCHORS)
        if search is None:
            return {
                "status": "failed",
                "error_type": "search_box_not_found",
                "message": "未找到会话搜索框（聊天页结构可能已变化）。",
            }
        try:
            await search.fill(hr_name)
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": "search_failed",
                "message": f"搜索会话失败: {exc}",
            }
        # 2) Click the conversation row containing the HR name.
        row = None
        row_deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < row_deadline:
            row = page.locator(f".user-list li:has-text('{hr_name}')").first
            try:
                if await row.is_visible():
                    break
            except Exception:
                pass
            row = None
            await asyncio.sleep(0.5)
        if row is None:
            return {
                "status": "failed",
                "error_type": "conversation_not_found",
                "message": f"会话列表中没有找到「{hr_name}」。",
            }
        try:
            await row.click(timeout=5_000)
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": "conversation_click_failed",
                "message": f"点击会话失败: {exc}",
            }
        # 3) Type the message into the chat input area, human-paced.
        input_area = None
        input_deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < input_deadline:
            input_area = await self._first_visible(page, _INPUT_AREA_ANCHORS)
            if input_area is not None:
                break
            await asyncio.sleep(0.5)
        if input_area is None:
            return {
                "status": "failed",
                "error_type": "input_area_not_found",
                "message": "未找到聊天输入框（会话可能未打开）。",
            }
        try:
            await input_area.click(timeout=3_000)
            await input_area.type(message, delay=30)  # ~33 chars/sec, human-ish
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": "typing_failed",
                "message": f"输入回复失败: {exc}",
            }
        # 4) Send: prefer the 发送 button; fall back to Enter.
        send_btn = await self._first_visible(page, _SEND_BUTTON_ANCHORS)
        try:
            if send_btn is not None:
                await send_btn.click(timeout=5_000)
            else:
                await input_area.press("Enter")
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": "send_failed",
                "message": f"发送失败（内容仍在输入框，未发出）: {exc}",
            }
        # 5) Verify the input area cleared = message accepted by the page.
        await asyncio.sleep(1.5)
        try:
            residual = await input_area.input_value()
        except Exception:
            try:
                residual = (await input_area.inner_text()).strip()
            except Exception:
                residual = ""
        if isinstance(residual, str) and residual.strip():
            return {
                "status": "failed",
                "error_type": "send_unconfirmed",
                "message": "输入框未清空，消息可能未发出。请人工确认。",
            }
        logger.info("Boss chat reply sent to %s (%d chars)", hr_name, len(message))
        return {"status": "ok", "to": hr_name, "chars": len(message)}

    async def _first_visible(self, page: Any, selectors: list[str]) -> Any:
        for selector in selectors:
            loc = page.locator(selector).first
            try:
                if await loc.is_visible():
                    return loc
            except Exception:
                continue
        return None
