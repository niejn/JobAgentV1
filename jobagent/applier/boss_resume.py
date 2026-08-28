"""Upload a PDF resume attachment to Boss直聘 via the user's Chrome (CDP).

Flow verified from a chrome://net-export capture + manual walk-through
(2026-08-27):

    homepage -> click header link 简历 (a[ka='header-resume'])
      -> resume page -> click 附件上传 button (<span>附件上传</span>)
      -> filechooser -> JS POSTs
           /wapi/zpupload/resume/uploadFile.json   (multipart file body)
           /wapi/zpgeek/resume/attachment/save.json (attach record)
      -> success = save.json returns code == 0

Platform constraint: at most THREE attachment resumes exist at once; a
fourth upload is rejected until an old one is deleted
(/wapi/zpgeek/resume/attachment/delete.json).

We never call those APIs directly (they sit behind warlock device
checks); the page's own JS uploads with its native fingerprint after we
hand it the file. The filechooser is intercepted via Playwright's
expect_file_chooser - no OS dialog opens, and no Runtime.enable is
attached to already-loaded pages (which is what trips warlock's
console-getter anti-debug trap; verified: F12 or a CDP listener attach
both blank the page).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jobagent.config import Settings
from jobagent.crawl import CrawlGate
from jobagent.scraper.boss import BossAccessError, get_boss_cooldown
from jobagent.scraper.cdp_tab_pool import CdpTabPool

logger = logging.getLogger(__name__)

_HEADER_RESUME_LINK = "a[ka='header-resume']"
_RESUME_PAGE_PATH = "/web/geek/resume"
_UPLOAD_BUTTONS = [
    "span:has-text('附件上传')",
    "button:has-text('附件上传')",
    "a:has-text('附件上传')",
]
_DELETE_BUTTONS = [
    "span:has-text('删除')",
    "a:has-text('删除')",
    "button:has-text('删除')",
    "text=删除",
]
_FILE_INPUT = "input[type=file]"
_SAVE_API = "/wapi/zpgeek/resume/attachment/save.json"
_DELETE_API = "/wapi/zpgeek/resume/attachment/delete.json"
_UPLOAD_API = "/wapi/zpupload/resume/uploadFile.json"
_MAX_ATTACHMENTS = 3
_NAV_TIMEOUT_MS = 30_000
_UPLOAD_TIMEOUT_S = 90.0
_LIMIT_MARKERS = ("最多", "上限", "三个", "3个", "不能超过")


class BossResumeUploader:
    """Attach one PDF resume on Boss via the user's logged-in Chrome."""

    def __init__(self, settings: Settings, *, crawl_gate: CrawlGate | None = None) -> None:
        self._settings = settings
        self._crawl_gate = crawl_gate
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._tab_pool: CdpTabPool | None = None

    async def __aenter__(self) -> BossResumeUploader:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            browser = await self._playwright.chromium.connect_over_cdp(
                self._settings.xhs_cdp_endpoint,
                timeout=10_000,
            )
        except Exception as exc:
            await self._playwright.stop()
            raise BossAccessError(
                "Boss CDP: 无法连接 Chrome--Chrome 调试端口未就绪。"
                "请按 skills/ChromeCDP-setup/SKILL.md 启动调试模式后重试。",
                code="cdp_not_ready",
            ) from exc
        context = next((c for c in browser.contexts if c.pages), None)
        if context is None:
            try:
                await browser.close()
            finally:
                await self._playwright.stop()
            raise BossAccessError(
                "Boss CDP: 未找到已打开页面的浏览器上下文（登录态可能丢失），"
                "请在 Chrome 窗口中确认 Boss 已登录。",
                code="boss_access_denied",
            )
        self._context = context
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._tab_pool is not None:
            await self._tab_pool.close()
            self._tab_pool = None
        self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def upload_pdf(
        self,
        pdf_path: str | Path,
        *,
        allow_delete: bool = False,
    ) -> dict[str, Any]:
        """Upload one PDF as the Boss attachment resume.

        ``allow_delete``: when the account already holds the maximum of
        three attachments and this flag is set (the tool layer only sets
        it after explicit user confirmation), the oldest-looking
        attachment is deleted via the page's own UI before retrying.

        Boss-side failures come back as ``{"status": "failed", ...}``
        instead of raising, so the agent can report them to the user.
        """

        allowed, remaining_min, _ = get_boss_cooldown().check()
        if not allowed:
            return {
                "status": "failed",
                "error_type": "cooldown_active",
                "message": f"Boss 风控冷却中（约剩 {remaining_min} 分钟），未发出请求。",
            }

        path = Path(pdf_path).expanduser().resolve()
        if not path.is_file():
            return {
                "status": "failed",
                "error_type": "file_not_found",
                "message": f"简历文件不存在: {path}",
            }
        if path.suffix.lower() != ".pdf":
            return {
                "status": "failed",
                "error_type": "not_a_pdf",
                "message": "Boss 附件简历只接受 PDF 文件。",
            }
        if self._context is None:
            raise RuntimeError("BossResumeUploader not initialised — use 'async with'.")

        if self._tab_pool is None:
            self._tab_pool = CdpTabPool(self._context)
        if self._crawl_gate is not None:
            await self._crawl_gate.acquire("boss-cdp")
        try:
            page = await self._tab_pool.acquire()
        except TimeoutError as exc:
            return {
                "status": "failed",
                "error_type": "tab_pool_timeout",
                "message": f"浏览器 tab 池超时: {exc}",
            }
        try:
            result = await self._upload_on_page(page, path)
            if (
                result.get("error_type") == "attachment_limit"
                and allow_delete
                and await self._delete_one_attachment(page)
            ):
                logger.info("Boss resume: attachment slot freed, retrying upload")
                result = await self._upload_on_page(page, path)
            return result
        finally:
            await self._tab_pool.release(page)

    # -- page flow --------------------------------------------------------------

    async def _upload_on_page(self, page: Any, path: Path) -> dict[str, Any]:
        # 1) Land on the homepage, then click into 简历 like a human; a
        # bare URL jump to the resume page draws more risk scrutiny.
        try:
            await page.goto(
                "https://www.zhipin.com/",
                wait_until="domcontentloaded",
                timeout=_NAV_TIMEOUT_MS,
            )
        except Exception:
            logger.info("Boss resume: homepage nav interrupted, continuing", exc_info=True)
        try:
            await page.locator(_HEADER_RESUME_LINK).first.click(timeout=8_000)
        except Exception:
            logger.info("Boss resume: header link not clickable, direct goto", exc_info=True)
            try:
                await page.goto(
                    f"https://www.zhipin.com{_RESUME_PAGE_PATH}",
                    wait_until="domcontentloaded",
                    timeout=_NAV_TIMEOUT_MS,
                )
            except Exception:
                logger.info("Boss resume: direct nav interrupted", exc_info=True)

        # 2) Wait until the resume page has rendered its upload controls.
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            if urlsplit(str(page.url or "")).path == _RESUME_PAGE_PATH:
                if await page.query_selector(_FILE_INPUT) is not None:
                    break
            await asyncio.sleep(0.5)
        else:
            if (
                urlsplit(str(page.url or "")).path != _RESUME_PAGE_PATH
                or await page.query_selector(_FILE_INPUT) is None
            ):
                return {
                    "status": "failed",
                    "error_type": "resume_page_blocked",
                    "message": (
                        "未能在简历页找到上传控件（页面可能被反爬跳转到空白页）。"
                        "建议稍后重试；若反复出现请先在 Chrome 中人工打开一次简历页。"
                    ),
                }

        # 3) Hand the file over. Prefer intercepting the filechooser the
        # 附件上传 button opens; fall back to feeding the hidden input.
        outcome: dict[str, Any] | None = None

        async def on_response(response: Any) -> None:
            nonlocal outcome
            try:
                path_part = urlsplit(str(getattr(response, "url", "") or "")).path
                if path_part == _SAVE_API:
                    body = await response.json()
                    if isinstance(body, dict):
                        failed = body.get("code") != 0
                        outcome = {
                            "status": "failed" if failed else "ok",
                            "api": "attachment/save.json",
                            "code": body.get("code"),
                            "message": str(body.get("message", ""))[:200],
                        }
                        if failed and _looks_like_limit(outcome["message"]):
                            outcome["error_type"] = "attachment_limit"
                elif path_part == _UPLOAD_API:
                    body = await response.json()
                    if isinstance(body, dict) and body.get("code") not in (0, None):
                        message = str(body.get("message", ""))[:200]
                        outcome = outcome or {
                            "status": "failed",
                            "api": "uploadFile.json",
                            "code": body.get("code"),
                            "message": message,
                        }
                        if _looks_like_limit(message):
                            outcome.setdefault("error_type", "attachment_limit")
            except Exception:
                pass  # body races are fine; the final poll decides

        page.on("response", on_response)
        try:
            await self._hand_file_over(page, path)
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": "input_rejected",
                "message": f"向简历页提交文件失败: {exc}",
            }

        # 4) Await the save/upload API verdict.
        deadline = asyncio.get_event_loop().time() + _UPLOAD_TIMEOUT_S
        while outcome is None and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
        if outcome is None:
            return {
                "status": "failed",
                "error_type": "no_upload_response",
                "message": "文件已提交但未捕获到上传结果（网络慢或页面被拦截）。",
            }
        outcome.setdefault("file", str(path))
        if outcome["status"] == "ok":
            logger.info("Boss resume upload SUCCESS: %s", path.name)
        else:
            logger.warning("Boss resume upload rejected: %s", outcome)
        return outcome

    async def _hand_file_over(self, page: Any, path: Path) -> None:
        """Click 附件上传 and feed the filechooser; fallback to the input."""

        for selector in _UPLOAD_BUTTONS:
            button = page.locator(selector).first
            try:
                if not await button.is_visible():
                    continue
                async with page.expect_file_chooser(timeout=6_000) as fc_info:
                    await button.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(str(path))
                return
            except Exception:
                logger.debug(
                    "Boss resume: filechooser path failed for %s", selector, exc_info=True
                )
        # Fallback: the hidden <input type=file> accepts files directly.
        file_input = await page.query_selector(_FILE_INPUT)
        if file_input is None:
            raise RuntimeError("简历页没有可用的文件上传控件")
        await file_input.set_input_files(str(path))

    async def _delete_one_attachment(self, page: Any) -> bool:
        """Delete one existing attachment through the page's own UI.

        Best-effort (selectors were provided from a manual walk-through,
        not yet machine-verified): click the first 删除 control and
        confirm. Returns True only when the delete API fired with code 0.
        """

        deleted: dict[str, Any] | None = None

        async def on_response(response: Any) -> None:
            nonlocal deleted
            try:
                if urlsplit(str(getattr(response, "url", "") or "")).path == _DELETE_API:
                    body = await response.json()
                    if isinstance(body, dict):
                        deleted = body
            except Exception:
                pass

        page.on("response", on_response)
        try:
            for selector in _DELETE_BUTTONS:
                button = page.locator(selector).first
                try:
                    if not await button.is_visible():
                        continue
                    await button.click()
                    # Boss asks for confirmation in a small dialog.
                    confirm = page.locator("button:has-text('确定'), button:has-text('确认')").first
                    try:
                        await confirm.click(timeout=3_000)
                    except Exception:
                        pass  # some variants delete without a dialog
                    deadline = asyncio.get_event_loop().time() + 15.0
                    while deleted is None and asyncio.get_event_loop().time() < deadline:
                        await asyncio.sleep(0.5)
                    return deleted is not None and deleted.get("code") == 0
                except Exception:
                    logger.debug("Boss resume: delete path failed for %s", selector, exc_info=True)
            return False
        finally:
            page.remove_listener("response", on_response)


def _looks_like_limit(message: str) -> bool:
    return any(marker in message for marker in _LIMIT_MARKERS)
