"""Boss job search via CDP — borrows the proven pattern from boss-zhipin-scraper.

Keeps the search tab open in the user's Chrome (no create/close lifecycle that
Boss flags as automation). Each backend instance owns its own CDP connection;
``dispose()`` must run after use to release the driver and websocket.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast
from urllib.parse import urlencode

from jobagent.config import Settings
from jobagent.crawl import CrawlGate
from jobagent.models import Job
from jobagent.scraper.boss import (
    _CITY_CODES,
    BossAccessError,
    BossDiscoveryRequest,
    _normalize_job,
    get_boss_cooldown,
)
from jobagent.scraper.cdp_tab_pool import CdpTabPool

logger = logging.getLogger(__name__)

_JOB_LIST_PATH = "/wapi/zpgeek/search/joblist.json"
_BOSS_HOSTS = frozenset({"zhipin.com", "www.zhipin.com"})
_PAGE_READY_TIMEOUT_S = 10.0
_PAGE_READY_POLL_S = 0.5
_HEALTH_SCRIPT = """() => {
  const text = (document.body?.innerText || '').trim();
  const visible = (el) => !!el && !!(
    el.offsetWidth || el.offsetHeight || el.getClientRects().length
  );
  const anyVisible = (selectors) => selectors.some(
    (selector) => visible(document.querySelector(selector))
  );
  const cards = Array.from(document.querySelectorAll('.job-card-box'));
  const realJobCards = cards.filter((card) => (card.innerText || '').trim().length >= 10).length;
  const loginWall = anyVisible([
    '[class*="login-dialog"]', '[class*="boss-login"]', '[class*="loginDialog"]',
    '[class*="login-wrap"]', '.header-login-btn', '[ka="guide_login_btn_click"]'
  ]);
  const captcha = anyVisible([
    '[class*="captcha"]', '[class*="verify"]', 'iframe[src*="captcha"]', 'iframe[src*="verify"]'
  ]);
  const lower = text.toLowerCase();
  const riskControl = /访问受限|操作过于频繁|安全验证|请完成验证|风险控制/.test(text)
    || lower.includes('captcha') || lower.includes('verify you are human');
  const emptyResult = /暂无相关职位|没有找到相关职位|未找到相关职位/.test(text);
  const url = location.href;
  return {
    url, title: document.title || '', body_chars: text.length,
    real_job_cards: realJobCards, login_wall: loginWall, captcha,
    risk_control: riskControl, empty_result: emptyResult,
    blank_or_data_url: url === 'about:blank' || url.startsWith('data:')
  };
}"""


class BossCdpBackend:
    """Boss job search through the user's already-logged-in Chrome via CDP.

    Pattern borrowed from ``boss-zhipin-scraper`` (``NetworkJoblistCapture``):
    navigate the real Boss search page, passively capture the page's own API
    response, keep the tab open for reuse. Never create-then-close a page.
    """

    def __init__(self, settings: Settings, *, crawl_gate: CrawlGate | None = None) -> None:
        self._settings = settings
        self._crawl_gate = crawl_gate
        self._connection: Any | None = None
        self._connection_lock = asyncio.Lock()
        self._tab_pool: CdpTabPool | None = None

    async def _ensure_connection(self) -> tuple[Any, Any, Any]:
        if self._connection is not None:
            return cast("tuple[Any, Any, Any]", self._connection)
        async with self._connection_lock:
            if self._connection is not None:
                return cast("tuple[Any, Any, Any]", self._connection)
            from playwright.async_api import async_playwright

            from jobagent.auth.boss_debug_chrome import (
                BossDebugChromeError,
                ensure_boss_debug_chrome,
            )

            try:
                started = await ensure_boss_debug_chrome(self._settings)
                if started:
                    logger.info("Boss CDP: started dedicated visible debug Chrome")
            except BossDebugChromeError as exc:
                raise BossAccessError(str(exc), code="cdp_not_ready") from exc

            driver = await async_playwright().start()
            try:
                browser = await driver.chromium.connect_over_cdp(
                    self._settings.debug_chrome_cdp_endpoint,
                    timeout=10_000,
                )
            except Exception as exc:
                await driver.stop()
                raise BossAccessError(
                    "Boss CDP: 无法连接 Chrome--Chrome 调试端口未就绪。请按以下步骤设置:\n"
                    "1. 读取 skills/ChromeCDP-setup/SKILL.md\n"
                    "2. 按 SKILL.md 中的 4 个步骤启动 Chrome 调试模式\n"
                    "3. 重试本次操作",
                    code="cdp_not_ready",
                ) from exc

            # Find a context that already has pages (has persistent cookies)
            context = None
            for ctx in browser.contexts:
                if list(ctx.pages):
                    context = ctx
                    break
            if context is None:
                try:
                    await browser.close()
                finally:
                    await driver.stop()
                raise BossAccessError(
                    "Boss CDP: 未找到已打开页面的浏览器上下文（登录态可能丢失），"
                    "请在 Chrome 窗口中确认 Boss 已登录。",
                    code="boss_access_denied",
                )

            self._connection = (browser, context, driver)
            return cast("tuple[Any, Any, Any]", self._connection)

    async def _get_page(self, context: Any) -> Any:
        """Acquire a tab from the bounded pool (keeper + cap + recycling)."""
        if self._tab_pool is None:
            self._tab_pool = CdpTabPool(context)
        return await self._tab_pool.acquire()

    async def _release_page(self, page: Any) -> None:
        if self._tab_pool is not None:
            await self._tab_pool.release(page)

    async def dispose(self) -> None:
        """Tear down this backend's CDP connection and Playwright driver.

        Search tabs stay open in the user's Chrome: on a connect_over_cdp()
        browser, close() only detaches the connection (verified against
        playwright 1.62.0 — the externally-owned Chrome keeps running).
        Stopping the driver releases the node process and websocket this
        backend started; without it every discovery call leaks both.
        """
        connection, self._connection = self._connection, None
        if self._tab_pool is not None:
            await self._tab_pool.close()
            self._tab_pool = None
        if connection is None:
            return
        browser, _, driver = connection
        try:
            await browser.close()
        except Exception:
            logger.debug("Boss CDP: browser close failed during dispose", exc_info=True)
        finally:
            try:
                await driver.stop()
            except Exception:
                logger.debug("Boss CDP: driver stop failed during dispose", exc_info=True)

    async def discover(self, request: BossDiscoveryRequest) -> list[Job]:
        # Refuse immediately while rate-limit cooldown is active - do NOT
        # send a real request that would deepen the block.
        allowed, remaining_min, _ = get_boss_cooldown().check()
        if not allowed:
            raise BossAccessError(
                f"Boss 风控冷却中（约剩 {remaining_min} 分钟），本次请求未发出。"
                "请勿继续重试 Boss 操作；可先处理其他任务，稍后再试。",
                code="cooldown_active",
            )

        city_code = _CITY_CODES.get(request.city)
        if city_code is None:
            raise ValueError(f"Unsupported Boss city: {request.city}")

        _, context, _ = await self._ensure_connection()
        page = await self._get_page(context)
        try:
            return await self._discover_on_page(page, context, request, city_code)
        finally:
            await self._release_page(page)

    async def _discover_on_page(
        self,
        page: Any,
        context: Any,
        request: BossDiscoveryRequest,
        city_code: str,
    ) -> list[Job]:
        captured: list[dict] = []

        async def on_response(response: Any) -> None:
            try:
                url = str(getattr(response, "url", "") or "")
                if _JOB_LIST_PATH in url:
                    body = await response.json()
                    if isinstance(body, dict):
                        captured.append(body)
                        logger.info("Boss CDP: captured joblist API")
            except Exception:
                pass

        page.on("response", on_response)

        search_params: dict[str, str] = {"query": request.query, "city": city_code}
        search_params.update(request._filter_query_params())
        search_url = f"https://www.zhipin.com/web/geek/job?{urlencode(search_params)}"

        if self._crawl_gate is not None:
            # Boss 站桶：搜索页导航也计入该账号的出站请求节奏。
            await self._crawl_gate.acquire("boss-cdp")
        logger.info("Boss CDP: navigating to search page")
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            # Boss redirects to about:blank after the page loads; the API
            # response fires before the redirect, so continue waiting.
            logger.info("Boss CDP: navigation interrupted (expected): %s", exc)

        # A DOMContentLoaded event only proves the shell arrived. Before
        # trusting the page's network responses, require a usable SPA: real
        # job text (or an explicit empty result), a live login state, and no
        # risk/captcha/blank-page signal. One reload is enough to clear a
        # transient SPA boot failure; repeated reloads feed the risk system.
        await self._wait_for_search_content(page)
        from jobagent.auth.browser_login import sync_boss_cookies_from_cdp_context

        try:
            saved = await sync_boss_cookies_from_cdp_context(context)
            logger.info("Boss CDP: synchronized %d session cookies after login check", saved)
        except Exception as exc:
            raise BossAccessError(
                "Boss CDP: 已确认页面登录，但本地 Cookie 同步失败；未继续搜索或联系。",
                code="boss_cookie_sync_failed",
                details={"reason": "cookie_sync_failed"},
            ) from exc

        # Poll for the API response (boss-zhipin-scraper pattern)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not captured:
            await asyncio.sleep(0.3)

        if not captured:
            diagnostic = await self._page_diagnostic(page)
            logger.warning("Boss CDP: no joblist API: %s", diagnostic)
            raise BossAccessError(
                "Boss CDP: 页面试图加载但未捕获到 API 响应。\n"
                "可能原因：Boss 反爬校验或登录态过期。\n"
                "建议：检查 Chrome 窗口中 Boss 直聘是否已登录，是否触发验证码。",
                code="boss_access_denied",
                details=diagnostic,
            )

        payload = captured[0]
        if not isinstance(payload, dict) or payload.get("code") != 0:
            # Server confirmed risk control - start the shared cooldown so
            # subsequent tool calls fail fast without hitting Boss again.
            get_boss_cooldown().trigger("risk_control")
            raise BossAccessError(
                "Boss 反爬拒绝了本次请求，已进入 60 分钟冷却期。"
                "冷却期内所有 Boss 请求会被直接拒绝，请稍后再试或改用其他渠道。",
                code="boss_risk_control",
            )

        zp_data = payload.get("zpData")
        raw_jobs = zp_data.get("jobList", []) if isinstance(zp_data, dict) else []
        jobs: list[Job] = []
        for raw in raw_jobs:
            if not isinstance(raw, dict):
                continue
            try:
                job = _normalize_job(raw)
            except ValueError:
                continue
            if request.area and request.area not in job.location:
                continue
            if (
                request.company_sizes
                and job.metadata.get("company_size") not in request.company_sizes
            ):
                continue
            jobs.append(job)
            if len(jobs) >= request.limit:
                break
        return jobs

    async def _wait_for_search_content(self, page: Any) -> dict[str, object]:
        """Wait for a real Boss SPA once, with one bounded reload recovery."""

        for attempt in range(2):
            deadline = time.monotonic() + _PAGE_READY_TIMEOUT_S
            latest: dict[str, object] = {}
            while time.monotonic() < deadline:
                latest = await self._page_diagnostic(page)
                failure = self._health_failure(latest)
                if failure is not None:
                    raise failure
                if bool(latest.get("real_job_cards")) or bool(latest.get("empty_result")):
                    return latest
                await asyncio.sleep(_PAGE_READY_POLL_S)
            if attempt == 0:
                logger.info("Boss CDP: SPA content absent; reloading once")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    # The next diagnostic distinguishes renderer loss from a
                    # slow SPA without treating reload itself as success.
                    logger.info("Boss CDP: reload interrupted", exc_info=True)
                continue
            raise BossAccessError(
                "Boss CDP: 页面壳已加载，但岗位内容没有渲染；已刷新一次后停止。"
                "请在 Chrome 中检查 Boss 页面、登录态或安全验证，不会自动继续刷新。",
                code="boss_access_denied",
                details={**latest, "reason": "spa_content_not_rendered", "reload_attempted": True},
            )
        raise AssertionError("two bounded SPA attempts must return or raise")

    async def _page_diagnostic(self, page: Any) -> dict[str, object]:
        """Return only safe health facts; a lost execution context is diagnostic data."""

        fallback_url = str(getattr(page, "url", "") or "")[:200]
        try:
            raw = await page.evaluate(_HEALTH_SCRIPT)
        except Exception as exc:
            return {
                "url": fallback_url,
                "title": "",
                "body_chars": 0,
                "real_job_cards": 0,
                "login_wall": False,
                "captcha": False,
                "risk_control": False,
                "blank_or_data_url": (
                    fallback_url == "about:blank" or fallback_url.startswith("data:")
                ),
                "probe_error": type(exc).__name__,
            }
        if not isinstance(raw, dict):
            return {"url": fallback_url, "probe_error": "invalid_health_payload"}
        return {
            key: value
            for key, value in raw.items()
            if key
            in {
                "url", "title", "body_chars", "real_job_cards", "login_wall", "captcha",
                "risk_control", "empty_result", "blank_or_data_url",
            }
            and isinstance(value, (str, int, bool))
        }

    @staticmethod
    def _health_failure(diagnostic: dict[str, object]) -> BossAccessError | None:
        if diagnostic.get("probe_error"):
            return BossAccessError(
                "Boss CDP: 页面执行上下文已丢失，未继续请求。",
                code="page_lost",
                details=diagnostic,
            )
        if diagnostic.get("blank_or_data_url"):
            return BossAccessError(
                "Boss CDP: 页面被跳转到空白页，未继续请求。",
                code="page_lost",
                details=diagnostic,
            )
        if diagnostic.get("login_wall"):
            return BossAccessError(
                "Boss CDP: 检测到未登录状态。请在已打开的调试 Chrome 中完成 Boss 登录，"
                "然后回复“已登录”；系统会再次验证并同步本地 Cookie 后继续。",
                code="boss_login_required",
                details=diagnostic,
            )
        if diagnostic.get("captcha") or diagnostic.get("risk_control"):
            get_boss_cooldown().trigger("page_risk_control")
            return BossAccessError(
                "Boss CDP: 检测到安全验证或风控提示，已停止本次搜索并进入冷却。",
                code="boss_risk_control",
                details=diagnostic,
            )
        return None
