"""Boss job search via CDP — borrows the proven pattern from boss-zhipin-scraper.

Keeps the search tab open in the user's Chrome (no create/close lifecycle that
Boss flags as automation). Reuses the existing CDP connection from XHS.
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

logger = logging.getLogger(__name__)

_JOB_LIST_PATH = "/wapi/zpgeek/search/joblist.json"
_BOSS_HOSTS = frozenset({"zhipin.com", "www.zhipin.com"})


class BossCdpBackend:
    """Boss job search through the user's already-logged-in Chrome via CDP.

    Pattern borrowed from ``boss-zhipin-scraper`` (``NetworkJoblistCapture``):
    navigate the real Boss search page, passively capture the page's own API
    response, keep the tab open for reuse. Never create-then-close a page.
    """

    def __init__(self, settings: Settings, *, crawl_gate: CrawlGate | None = None) -> None:
        self._settings = settings
        self._crawl_gate = crawl_gate
        self._connection: Any = None
        self._connection_lock = asyncio.Lock()
        self._page: Any = None

    async def _ensure_connection(self) -> tuple[Any, Any, Any]:
        if self._connection is not None:
            return cast("tuple[Any, Any, Any]", self._connection)
        async with self._connection_lock:
            if self._connection is not None:
                return cast("tuple[Any, Any, Any]", self._connection)
            from playwright.async_api import async_playwright

            driver = await async_playwright().start()
            try:
                browser = await driver.chromium.connect_over_cdp(
                    self._settings.xhs_cdp_endpoint,
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
        """Create a fresh page for each search; keep it alive (don't close)."""
        # Use a fresh page - Boss detects reused pages that went to blank
        return await context.new_page()

    async def dispose(self) -> None:
        """Let the connection go; search tabs stay open in the user's Chrome."""
        self._page = None
        self._connection = None

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

        await asyncio.sleep(1.0)

        # Poll for the API response (boss-zhipin-scraper pattern)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not captured:
            await asyncio.sleep(0.3)

        if not captured:
            current_url = str(page.url or "")
            logger.warning("Boss CDP: no joblist API (URL: %s)", current_url[:100])
            raise BossAccessError(
                "Boss CDP: 页面试图加载但未捕获到 API 响应。\n"
                "可能原因：Boss 反爬校验或登录态过期。\n"
                "建议：检查 Chrome 窗口中 Boss 直聘是否已登录，是否触发验证码。",
                code="boss_access_denied",
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