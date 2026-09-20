"""Direct HTTP creation/entry of a Boss HR conversation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jobagent.applier.boss_ws import BossConversationTarget, find_conversation_target
from jobagent.auth.cookie_manager import get_cookies
from jobagent.config import Settings
from jobagent.models import Job

_ADD_FRIEND_PATH = "/wapi/zpgeek/friend/add.json"
_USER_INFO_PATH = "/wapi/zpuser/wap/getUserInfo.json"

# Keep the CDP attachment and the chat page alive across tool invocations.
# Boss's page token is owned by the live page; closing it after every greeting
# forces the next invocation through a fresh, fragile navigation.
_CDP_RUNTIMES: dict[str, tuple[Any, Any, Any, Any]] = {}


class BossPageTokenError(RuntimeError):
    """The current logged-in Boss session did not yield an action token."""


@dataclass(frozen=True, slots=True)
class BossDirectContactResult:
    status: str
    target: BossConversationTarget | None = None
    chat_status: int | None = None
    error_type: str | None = None
    platform_code: int | None = None
    platform_message: str = ""
    default_greeting: str | None = None
    show_greeting: bool = False


class BossDirectContactAdapter:
    """Create/enter one HR Conversation and verify it through the friend list."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        cookies: dict[str, str] | None = None,
        page_token: str | None = None,
        target_finder: Callable[..., Awaitable[BossConversationTarget]] = (
            find_conversation_target
        ),
    ) -> None:
        self._settings = settings
        self._client = client
        self._cookies = cookies
        self._page_token = page_token
        self._target_finder = target_finder

    @property
    def page_token(self) -> str:
        """In-memory only; callers may pass it to the WebSocket credential fetch."""

        return self._page_token or ""

    async def enter(self, job: Job) -> BossDirectContactResult:
        security_id = str(job.metadata.get("security_id") or "")
        if not security_id:
            return BossDirectContactResult("failed", error_type="security_id_missing")
        lid = str(job.metadata.get("lid") or "")
        if not lid:
            return BossDirectContactResult("failed", error_type="lid_missing")
        cookies = await self._load_cookies()
        try:
            page_token = self._page_token or await self._fetch_page_token(cookies)
        except BossPageTokenError as exc:
            return BossDirectContactResult(
                "failed",
                error_type="page_token_missing",
                platform_message=str(exc)[:200],
            )
        response = await self._post(
            f"https://www.zhipin.com{_ADD_FRIEND_PATH}",
            params={
                "securityId": security_id,
                "jobId": job.id.removeprefix("boss:"),
                "lid": lid,
                "_": int(time.time() * 1000),
            },
            data={"expectId": "0"},
            headers={**self._headers(cookies), "token": page_token},
            cookies=cookies,
        )
        if response.status_code != 200:
            return BossDirectContactResult(
                "failed", error_type="friend_add_http_error", platform_code=response.status_code
            )
        body = response.json()
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(body, dict) or not isinstance(code, int) or code != 0:
            message = str(body.get("message") or "")[:200] if isinstance(body, dict) else ""
            return BossDirectContactResult(
                "failed",
                error_type="friend_add_rejected",
                platform_code=code if isinstance(code, int) else None,
                platform_message=message,
            )
        data = body.get("zpData") or {}
        response_boss_id = (
            str(data.get("encBossId") or "") if isinstance(data, dict) else ""
        )
        default_greeting = (
            str(data.get("greeting") or "") if isinstance(data, dict) else ""
        ) or None
        show_greeting = bool(data.get("showGreeting")) if isinstance(data, dict) else False

        for attempt in range(3):
            try:
                target = await self._target_finder(
                    cookies=cookies,
                    company=job.company,
                    job_title=job.title,
                    friend_name=str(job.metadata.get("boss_name") or "") or None,
                    expected_encrypt_boss_id=(
                        response_boss_id
                        or str(job.metadata.get("encrypt_boss_id") or "")
                        or None
                    ),
                )
                return BossDirectContactResult(
                    "confirmed",
                    target=target,
                    chat_status=1,
                    default_greeting=default_greeting,
                    show_greeting=show_greeting,
                )
            except LookupError:
                if attempt < 2:
                    await asyncio.sleep(1)
        return BossDirectContactResult(
            "unverified",
            chat_status=0,
            error_type="conversation_not_visible",
            default_greeting=default_greeting,
            show_greeting=show_greeting,
        )

    async def _load_cookies(self) -> dict[str, str]:
        if self._cookies is not None:
            return dict(self._cookies)
        items = await get_cookies("boss", self._settings)
        return {
            str(item["name"]): str(item["value"])
            for item in items
            if item.get("name") and item.get("value")
        }

    async def _post(
        self,
        url: str,
        *,
        params: dict[str, str | int],
        data: dict[str, str],
        headers: dict[str, str],
        cookies: dict[str, str],
    ) -> Any:
        if self._client is not None:
            return await self._client.post(
                url, params=params, data=data, headers=headers
            )
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx dependency is required") from exc
        async with httpx.AsyncClient(
            cookies=cookies, headers=headers, timeout=20, follow_redirects=True
        ) as client:
            return await client.post(url, params=params, data=data)

    async def _fetch_page_token(self, cookies: dict[str, str]) -> str:
        headers = self._headers(cookies)
        if self._client is not None:
            response = await self._client.get(
                f"https://www.zhipin.com{_USER_INFO_PATH}", headers=headers
            )
        else:
            try:
                import httpx
            except ImportError as exc:
                raise RuntimeError("httpx dependency is required") from exc
            async with httpx.AsyncClient(
                cookies=cookies, headers=headers, timeout=20, follow_redirects=True
            ) as client:
                response = await client.get(
                    f"https://www.zhipin.com{_USER_INFO_PATH}"
                )
        body = response.json()
        data = body.get("zpData") if isinstance(body, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        if response.status_code == 200 and isinstance(token, str) and token:
            self._page_token = token
            return token
        # Some valid browser sessions no longer expose this short-lived value
        # to a standalone HTTP call. Read it from an existing Boss page, or a
        # single short-lived chat page when no Boss page is open, then continue
        # the direct HTTP + WebSocket contact flow without touching a job page.
        return await self._fetch_page_token_from_cdp()

    async def _fetch_page_token_from_cdp(self) -> str:
        from playwright.async_api import async_playwright

        from jobagent.auth.boss_debug_chrome import BossDebugChromeError, ensure_boss_debug_chrome

        endpoint = str(self._settings.debug_chrome_cdp_endpoint)
        try:
            await ensure_boss_debug_chrome(self._settings)
        except BossDebugChromeError as exc:
            raise BossPageTokenError(str(exc)) from exc
        runtime = _CDP_RUNTIMES.get(endpoint)
        if runtime is not None:
            try:
                _ = runtime[2].pages
            except Exception:
                _CDP_RUNTIMES.pop(endpoint, None)
                try:
                    await runtime[1].close()
                except Exception:
                    pass
                try:
                    await runtime[0].stop()
                except Exception:
                    pass
                runtime = None
        if runtime is None:
            driver = await async_playwright().start()
            browser = await driver.chromium.connect_over_cdp(endpoint, timeout=10_000)
            context = next((item for item in browser.contexts if item.pages), None)
            if context is None:
                await browser.close()
                await driver.stop()
                raise BossPageTokenError("Boss page token missing: no Chrome context")
            runtime = (driver, browser, context, None)
            _CDP_RUNTIMES[endpoint] = runtime
        driver, browser, context, cached_page = runtime
        try:
            if cached_page is not None and not cached_page.is_closed():
                try:
                    return await self._wait_page_token(cached_page, "cached Boss page")
                except BossPageTokenError:
                    pass
            pages = [
                candidate
                for candidate in context.pages
                if not candidate.is_closed() and "zhipin.com" in str(candidate.url or "")
            ]
            for page in pages:
                try:
                    token = await self._wait_page_token(page, "existing Boss page")
                    _CDP_RUNTIMES[endpoint] = (driver, browser, context, page)
                    return token
                except BossPageTokenError:
                    continue
            page = await context.new_page()
            await page.goto(
                "https://www.zhipin.com/web/geek/chat",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            token = await self._wait_page_token(page, "new Boss chat page")
            _CDP_RUNTIMES[endpoint] = (driver, browser, context, page)
            return token
        except Exception as exc:
            if isinstance(exc, BossPageTokenError):
                raise
            raise BossPageTokenError("Boss page token unavailable from Chrome") from exc
    async def _wait_page_token(self, page: Any, source: str) -> str:
        deadline = time.monotonic() + 15.0
        last_error = ""
        while time.monotonic() < deadline:
            try:
                raw = await page.evaluate(
                    "() => String((window._PAGE || {}).token || '').split('|')[0]"
                )
                if isinstance(raw, str) and raw:
                    self._page_token = raw
                    return raw
                last_error = "window._PAGE.token_empty"
            except Exception as exc:
                last_error = type(exc).__name__
            await asyncio.sleep(0.5)
        raise BossPageTokenError(f"{source}: page token unavailable ({last_error})")

    @staticmethod
    def _headers(cookies: dict[str, str]) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.zhipin.com",
            "Referer": "https://www.zhipin.com/web/geek/jobs",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
            "traceId": f"F-{int(time.time() * 1000)}",
            "zp_token": cookies.get("bst", ""),
        }


async def close_boss_cdp_runtimes() -> None:
    """Close retained Boss CDP pages/connections during JobAgent shutdown."""

    runtimes = list(_CDP_RUNTIMES.values())
    _CDP_RUNTIMES.clear()
    for driver, browser, _context, _page in runtimes:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await driver.stop()
        except Exception:
            pass
