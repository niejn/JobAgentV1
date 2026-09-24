"""Upload a PDF resume attachment to Boss直聘 via the user's Chrome (CDP).

Transport (v2, page-fetch - verified 2026-08-28): the filechooser route is
DEAD - warlock blanks the page the instant a file chooser opens, even for
manual clicks (net-export evidence: FILECHOOSER event and NAV -> about:blank
in the same second). Instead we run Boss's own upload calls from inside the
resume page via page.evaluate(fetch):

    POST /wapi/zpupload/resume/uploadFile.json
         multipart: file=<File>, fileType=1   -> zpData.previewUrl
    POST /wapi/zpgeek/resume/attachment/save.json
         ?previewUrl=<url>&annexType=0&from=8 -> zpData.resumeId

Both endpoints and field names extracted from Boss's public JS bundle
(resume~2.33dc2c5d.js): FormData.append("file", file), append("fileType", 1),
then save.json with previewUrl query params. A domain-internal fetch with
credentials carries the same cookies/origin as Boss's own axios calls - no
file chooser, no synthetic clicks, no upload-control interaction.

Platform constraint: at most THREE attachment resumes at once; a fourth is
rejected until one is deleted (attachment/delete.json - endpoint known,
request format not yet extracted).

We never touch input[type=file] or filechooser (verified warlock tripwires:
filechooser interception, F12, Runtime.enable listener attaches all blank
the page).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from jobagent.auth.boss_debug_chrome import BossDebugChromeError, ensure_boss_debug_chrome
from jobagent.config import Settings
from jobagent.crawl import CrawlGate
from jobagent.scraper.boss import BossAccessError, get_boss_cooldown
from jobagent.scraper.cdp_tab_pool import CdpTabPool

logger = logging.getLogger(__name__)

_HEADER_RESUME_LINK = "a[ka='header-resume']"
_RESUME_PAGE_PATH = "/web/geek/resume"
_DELETE_BUTTONS = [
    "span:has-text('删除')",
    "a:has-text('删除')",
    "button:has-text('删除')",
    "text=删除",
]
_DELETE_API = "/wapi/zpgeek/resume/attachment/delete.json"
_MAX_ATTACHMENTS = 3
_NAV_TIMEOUT_MS = 30_000
_UPLOAD_TIMEOUT_S = 90.0
_LIMIT_MARKERS = ("最多", "上限", "三个", "3个", "不能超过")

# Runs inside the resume page: POST uploadFile.json (multipart), then
# save.json (previewUrl from step 1). Field names and both endpoints
# extracted verbatim from Boss's public bundle resume~2.33dc2c5d.js.
_FETCH_UPLOAD_JS = """
async (payload) => {
  const bin = atob(payload.b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  const file = new File([buf], payload.name, {type: "application/pdf"});
  const fd = new FormData();
  fd.append("file", file);
  fd.append("fileType", "1");
  // Mirror Boss's axios request interceptor (app bundle): every wapi call
  // carries X-Requested-With, a traceId, and the logged-in page token
  // (window._PAGE.token) - the zpgeek gateway rejects saves without it
  // (code 121 "请求不合法"). The __zp_stoken__ proof rides in cookies,
  // which credentials:"include" already forwards.
  const pageToken = ((window._PAGE || {}).token || "").split("|")[0];
  const bstMatch = document.cookie.match(/(?:^|;\s*)bst=([^;]+)/);
  const zpToken = bstMatch ? decodeURIComponent(bstMatch[1]) : "";
  const base = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
    "traceId": "F-" + Math.random().toString(36).slice(2, 8)
      + Date.now().toString(36),
  };
  if (pageToken) base.token = pageToken;
  if (zpToken) base["zp_token"] = zpToken;
  // Boss's axios request interceptor stamps every call with a cache-buster
  // query param (Object.assign(params, {_: Date.now()})) - the zpgeek
  // gateway rejects saves without it (121 请求不合法, seen live even with
  // token+traceId headers present).
  const bust = "_=" + Date.now();
  const up = await fetch("/wapi/zpupload/resume/uploadFile.json", {
    method: "POST", body: fd, credentials: "include", headers: base,
  }).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
  if (up.code !== 0) return {step: "upload", code: up.code, message: up.message};
  const previewUrl = up.zpData && up.zpData.previewUrl;
  if (!previewUrl) return {step: "upload", code: up.code, message: "no previewUrl"};
  const save = await fetch(
    "/wapi/zpgeek/resume/attachment/save.json?previewUrl="
      + encodeURIComponent(previewUrl) + "&annexType=0&from=8&" + bust,
    {method: "POST", credentials: "include", headers: base},
  ).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
  if (save.code !== 0) return {
    step: "save", code: save.code, message: save.message,
    stokenPresent: document.cookie.indexOf("__zp_stoken__") !== -1,
  };
  return {step: "done",
          resumeId: save.zpData && save.zpData.resumeId,
          previewUrl};
}
"""


class BossResumeUploader:
    """Attach one PDF resume on Boss via the user's logged-in Chrome."""

    def __init__(self, settings: Settings, *, crawl_gate: CrawlGate | None = None) -> None:
        self._settings = settings
        self._crawl_gate = crawl_gate
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._tab_pool: CdpTabPool | None = None

    async def __aenter__(self) -> BossResumeUploader:
        self._playwright = await async_playwright().start()
        try:
            await ensure_boss_debug_chrome(self._settings)
            browser = await self._playwright.chromium.connect_over_cdp(
                self._settings.debug_chrome_cdp_endpoint,
                timeout=10_000,
            )
        except BossDebugChromeError as exc:
            await self._playwright.stop()
            raise BossAccessError(str(exc), code="cdp_not_ready") from exc
        except Exception as exc:
            await self._playwright.stop()
            raise BossAccessError(
                "Boss CDP: 无法连接 Chrome--Chrome 调试端口未就绪。"
                "请按 skills/chrome-cdp-setup/SKILL.md 启动调试模式后重试。",
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

        # 2) Wait until the resume page has settled (the fetch transport
        # does not need the upload widgets - only a live, logged-in page).
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            if urlsplit(str(page.url or "")).path == _RESUME_PAGE_PATH:
                try:
                    settled = await page.evaluate("document.readyState === 'complete'")
                except Exception:
                    settled = False
                if settled:
                    break
            await asyncio.sleep(0.5)
        else:
            return {
                "status": "failed",
                "error_type": "resume_page_blocked",
                "message": (
                    "简历页未能稳定加载（页面可能被反爬跳转到空白页）。"
                    "建议稍后重试；若反复出现请先在 Chrome 中人工打开一次简历页。"
                ),
            }

        # 3) Transport: run Boss's own upload chain via page-scope fetch.
        # (v1 used filechooser interception - warlock blanks the page the
        # instant a file chooser opens; see module docstring.)
        result = await self._upload_via_page_fetch(page, path)
        result.setdefault("file", str(path))
        if result["status"] == "ok":
            logger.info("Boss resume upload SUCCESS: %s", path.name)
        else:
            logger.warning("Boss resume upload rejected: %s", result)
        return result

    async def _upload_via_page_fetch(self, page: Any, path: Path) -> dict[str, Any]:
        """POST uploadFile.json + save.json from inside the resume page.

        The fetch runs with the page's own origin, cookies and headers -
        byte-identical surface to Boss's own axios calls. The PDF crosses
        the CDP boundary as base64 and is reassembled with the page's File
        constructor (no file chooser, no synthetic click on upload widgets).
        """

        try:
            payload = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            return {
                "status": "failed",
                "error_type": "file_not_found",
                "message": f"无法读取 PDF: {exc}",
            }
        try:
            raw = await asyncio.wait_for(
                page.evaluate(
                    _FETCH_UPLOAD_JS,
                    {"b64": payload, "name": path.name},
                ),
                timeout=_UPLOAD_TIMEOUT_S,
            )
        except Exception as exc:
            # evaluate dies when warlock navigates the page mid-call
            return {
                "status": "failed",
                "error_type": "page_lost",
                "message": f"页内上传被中断（页面可能被反爬跳转）: {exc}",
            }
        if not isinstance(raw, dict):
            return {
                "status": "failed",
                "error_type": "api_rejected",
                "message": f"上传接口返回了无法解析的结果: {raw!r:.200}",
            }
        step = str(raw.get("step", ""))
        if step == "done":
            return {
                "status": "ok",
                "api": "attachment/save.json",
                "resume_id": raw.get("resumeId"),
                "message": "附件简历上传成功。",
            }
        message = str(raw.get("message", ""))[:200]
        result: dict[str, Any] = {
            "status": "failed",
            "api": "uploadFile.json" if step == "upload" else "attachment/save.json",
            "code": raw.get("code"),
            "message": message,
        }
        if _looks_like_limit(message):
            result["error_type"] = "attachment_limit"
        return result

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
