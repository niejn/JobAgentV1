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
    ".boss-search-input",  # live-verified class (2026-08-28 DOM probe)
    "input[placeholder*='联系人']",
    "input[placeholder*='搜索']",
]
_INPUT_AREA_ANCHORS = [
    "textarea[placeholder*='输入']",
    "textarea",
    "[contenteditable='true']",
]
# Live-verified (user, 2026-08-28): the chat input shows
# "按 Enter 键发送，按 Ctrl+Enter 键换行" - there is NO send button.
# Sending is keyboard-only; multi-line text must use Ctrl+Enter for
# newlines because a bare \n inside type() would SEND each line as a
# separate half-message.
_ENTER_SEND = "Enter"
_NEWLINE_KEYS = "Control+Enter"


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
            # SPA mount: readyState completes before Vue mounts the chat
            # UI - the original bug probed the search box once, instantly,
            # and missed it (live failure 2026-08-28: search_box_not_found
            # on a healthy page).
            settle_deadline = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < settle_deadline:
                try:
                    if await page.evaluate("document.readyState === 'complete'"):
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
        # Parked page: drive the conversation without reloading.

        # 1) Find the search box and narrow the list to the target HR.
        # Retry: the chat SPA mounts asynchronously (this miss was the
        # live bug - probing once right after nav found nothing).
        search = None
        search_deadline = asyncio.get_event_loop().time() + 15.0
        while asyncio.get_event_loop().time() < search_deadline:
            search = await self._first_visible(page, _SEARCH_INPUT_ANCHORS)
            if search is not None:
                break
            await asyncio.sleep(0.5)
        if search is None:
            diag = await self._page_diag(page)
            return {
                "status": "failed",
                "error_type": "search_box_not_found",
                "message": "未找到会话搜索框（聊天页结构可能已变化）。",
                "page_diag": diag,
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
        # Live DOM (user-provided 2026-08-28, chat-new v5535): rows are
        # DIVs, not <li>; the HR name sits in <span class="name-text">.
        row = None
        row_deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < row_deadline:
            for sel in (
                f".user-list .name-text:text-is('{hr_name}')",
                f".user-list :text('{hr_name}')",
            ):
                candidate = page.locator(sel).first
                try:
                    if await candidate.is_visible():
                        row = candidate
                        break
                except Exception:
                    continue
            if row is not None:
                break
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
            # ~33 chars/sec, human-ish. Newlines are Ctrl+Enter - typing a
            # bare \n would hit Enter and SEND a half-written message.
            segments = message.split("\n")
            for index, segment in enumerate(segments):
                if segment:
                    await input_area.type(segment, delay=30)
                if index < len(segments) - 1:
                    await page.keyboard.press(_NEWLINE_KEYS)
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": "typing_failed",
                "message": f"输入回复失败: {exc}",
            }
        # 4) Send: keyboard-only (no button exists - see input hint).
        # Page-level keypress: focus is on the input right after typing,
        # and page.keyboard skips the element actionability checks that
        # stalled element.press for 30s in the live failure.
        try:
            await page.keyboard.press(_ENTER_SEND)
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": "send_failed",
                "message": (
                    f"回车发送失败（内容仍在输入框，可人工按回车发出）: {exc}"
                ),
                "page_diag": await self._page_diag(page),
            }
        # 5) STRONG verification (live lie, 2026-08-28: "input cleared"
        # misread a contenteditable editor - input_value() THROWS on
        # contenteditable, the except-branch read empty text and reported
        # success while nothing was sent). Sent only if the message tail
        # renders in the conversation panel AND the editor no longer
        # holds it.
        tail = message.strip().replace("\n", "")[-24:]
        await asyncio.sleep(1.5)
        try:
            editor_text = (await input_area.inner_text()) or ""
        except Exception:
            editor_text = ""
        try:
            panel_has_it = bool(
                await page.evaluate(
                    "t => document.body.innerText.replace(/\\s+/g, '').includes(t)",
                    tail,
                )
            )
        except Exception:
            panel_has_it = False
        editor_holds_it = tail in editor_text.replace("\n", "")
        if editor_holds_it or not panel_has_it:
            return {
                "status": "failed",
                "error_type": "send_unconfirmed",
                "message": (
                    "消息未确认发出：输入框残留="
                    f"{editor_holds_it}，聊天面板出现文案={panel_has_it}。"
                    "若文案仍在输入框，人工按回车即可发出。"
                ),
                "editor_residual": editor_holds_it,
                "panel_echo": panel_has_it,
            }
        logger.info("Boss chat reply verified in panel: %s", hr_name)
        return {"status": "ok", "to": hr_name, "chars": len(message)}

    async def _page_diag(self, page: Any) -> dict[str, Any]:
        """Snapshot page health for structured failures (blank vs restyle)."""

        try:
            return {
                "url": str(page.url or "")[:80],
                "title": await page.title(),
                "body_chars": await page.evaluate("document.body.innerText.length"),
            }
        except Exception as exc:
            return {"url": str(page.url or "")[:80], "error": str(exc)[:120]}

    async def _first_visible(self, page: Any, selectors: list[str]) -> Any:
        for selector in selectors:
            loc = page.locator(selector).first
            try:
                if await loc.is_visible():
                    return loc
            except Exception:
                continue
        return None
