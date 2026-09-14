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

HITL: the agent's HumanInTheLoopMiddleware physically pauses the tool call
before this sender runs; the message text itself is the approved artefact.
"""

from __future__ import annotations

import asyncio
import inspect
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
    "#chat-input",  # live DOM (user-supplied, chat-new v5535):
    # <div contenteditable="true" id="chat-input" class="chat-input">
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
        from jobagent.auth.boss_debug_chrome import BossDebugChromeError, ensure_boss_debug_chrome

        self._playwright = await async_playwright().start()
        try:
            await ensure_boss_debug_chrome(self._settings)
            browser = await self._playwright.chromium.connect_over_cdp(
                self._settings.debug_chrome_cdp_endpoint,
                timeout=10_000,
            )
        except BossDebugChromeError as exc:
            await self._playwright.stop()
            raise ConnectionError(str(exc)) from exc
        except Exception as exc:
            await self._playwright.stop()
            raise ConnectionError(
                "无法连接调试 Chrome（CDP）。请先启动带 --remote-debugging-port 的 Chrome。"
            ) from exc
        self._context = next((c for c in browser.contexts if c.pages), None)
        if self._context is None:
            await self._playwright.stop()
            raise ConnectionError("调试 Chrome 中没有可用的浏览器上下文。")
        # Keep the chat work page parked for reuse across replies.  The pool
        # owns its lifetime and closes it when the JobAgent shuts down.
        self._tab_pool = CdpTabPool(self._context, max_reuses=10_000)
        return self

    async def __aexit__(self, *exc: object) -> None:
        # The debug Chrome page is intentionally left open for the next
        # operation; JobAgent.close() owns final tab cleanup.
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
        # Park the page after sending. The next reply reuses this live chat
        # page, avoiding another navigation and preserving Boss session state.
        pruning = pool.prune_excess_blank_tabs()
        if inspect.isawaitable(pruning):
            await pruning
        page = await pool.acquire()
        try:
            result = await self._send_flow(page, hr_name=hr_name, message=message)
        finally:
            await pool.release(page)
        return result

    async def _send_flow(
        self, page: Any, *, hr_name: str, message: str
    ) -> dict[str, Any]:
        try:
            await page.goto(
                f"https://www.zhipin.com{_CHAT_PAGE_PATH}",
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
            # Page-native write: selectAll + insertText in ONE evaluate.
            # Live finding (88-char send, 2026-08-28): element.type's 88
            # synthetic key events on the contenteditable editor lost the
            # text entirely (editor read back EMPTY after "typing"), so
            # Enter shipped nothing. execCommand('insertText') is the
            # editor's real input path (fires the input event Vue binds
            # to) with ZERO synthetic keystrokes.
            inserted = await page.evaluate(
                """(text) => {
                    const ed = document.querySelector('#chat-input');
                    if (!ed) return false;
                    ed.focus();
                    document.execCommand('selectAll', false, null);
                    document.execCommand('insertText', false, text);
                    return (ed.innerText || '').includes(
                        text.replace(/\n/g, '').slice(0, 10)
                    );
                }""",
                message,
            )
            if inserted is not True:
                return {
                    "status": "failed",
                    "error_type": "typing_failed",
                    "message": "写入输入框后未能读到文案（编辑器可能未激活）。",
                }
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": "typing_failed",
                "message": f"输入回复失败: {exc}",
            }
        # 4) Send: keyboard-only (no button exists - see input hint).
        # Element press FIRST (focuses the editor, key lands on it for
        # sure); page-level keyboard as fallback. Live finding: a bare
        # page.keyboard Enter sometimes missed the editor when focus
        # drifted after typing (message typed, never sent).
        for send_attempt in range(2):
            try:
                if send_attempt == 0:
                    await input_area.press(_ENTER_SEND, timeout=3_000)
                else:
                    await page.keyboard.press(_ENTER_SEND)
                break
            except Exception as exc:
                if send_attempt == 1:
                    return {
                        "status": "failed",
                        "error_type": "send_failed",
                        "message": (
                            f"回车发送失败（内容仍在输入框，可人工按回车发出）: {exc}"
                        ),
                        "page_diag": await self._page_diag(page),
                    }
        # 5) Verification with honest ambiguity handling.
        # - live lie #1 (2026-08-28): "input cleared" misread a
        #   contenteditable editor -> false success. Hence panel echo.
        # - live lie #2 (same day): the send WORKED (message showed
        #   [送达] in the list) but the panel check itself failed ->
        #   false failure, which invites a duplicate resend. An editor
        #   that no longer holds the text IS evidence of sending; a
        #   missing panel echo with an empty editor is UNVERIFIED, not
        #   failed.
        tail = message.strip().replace("\n", "")[-24:]
        await asyncio.sleep(1.5)
        try:
            editor_text = (await input_area.inner_text()) or ""
        except Exception:
            editor_text = ""
        editor_holds_it = tail in editor_text.replace("\n", "")

        async def _panel_echo() -> bool | None:
            """True/False when the check runs; None when it cannot."""
            try:
                return bool(
                    await page.evaluate(
                        "t => document.body.innerText"
                        ".replace(/\\s+/g, '').includes(t)",
                        tail,
                    )
                )
            except Exception:
                return None

        panel_has_it = await _panel_echo()
        if panel_has_it is None:
            await asyncio.sleep(1.5)  # render grace, then one retry
            panel_has_it = await _panel_echo()

        # A page-level evaluate can be unavailable after Boss's anti-debug
        # guard tears down the execution context.  A normal locator query is
        # a second, narrower confirmation seam and does not require reading
        # the whole document body.  It is only positive evidence when the
        # exact tail is visible in a rendered message node.
        if panel_has_it is not True:
            try:
                echo = page.get_by_text(tail, exact=False).last
                if await echo.is_visible():
                    panel_has_it = True
            except Exception:
                pass

        if editor_holds_it:
            # One self-heal Enter: the first keypress can miss the editor
            # when focus drifted (live case 2026-08-28: text typed, Enter
            # consumed, message stuck in the editor).
            try:
                await input_area.press(_ENTER_SEND, timeout=3_000)
                await asyncio.sleep(1.0)
                editor_text = (await input_area.inner_text()) or ""
                editor_holds_it = tail in editor_text.replace("\n", "")
            except Exception:
                pass
        if editor_holds_it:
            return {
                "status": "failed",
                "error_type": "send_unconfirmed",
                "message": "文案仍在输入框，未发出。人工按回车即可发出。",
                "editor_residual": True,
                "panel_echo": panel_has_it,
            }
        if panel_has_it is True:
            logger.info("Boss chat reply verified in panel: %s", hr_name)
            return {"status": "ok", "to": hr_name, "chars": len(message)}
        # editor cleared + echo unavailable: probably sent, cannot prove
        return {
            "status": "unverified",
            "to": hr_name,
            "chars": len(message),
            "message": (
                "消息很可能已发出（输入框已清空），但面板确认失败。"
                "请人工查看会话列表最后一条消息后再决定是否重发，避免重复。"
            ),
            "editor_residual": False,
            "panel_echo": panel_has_it,
        }

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
