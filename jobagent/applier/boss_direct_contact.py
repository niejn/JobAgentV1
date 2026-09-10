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


class BossPageTokenError(RuntimeError):
    """The current logged-in Boss session did not yield an action token."""


@dataclass(frozen=True, slots=True)
class BossDirectContactResult:
    status: str
    target: BossConversationTarget | None = None
    chat_status: int | None = None
    error_type: str | None = None
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
        except BossPageTokenError:
            return BossDirectContactResult("failed", error_type="page_token_missing")
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
            return BossDirectContactResult("failed", error_type="friend_add_http_error")
        body = response.json()
        if not isinstance(body, dict) or int(body.get("code") or 0) != 0:
            return BossDirectContactResult("failed", error_type="friend_add_rejected")
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

        driver = await async_playwright().start()
        browser: Any | None = None
        try:
            browser = await driver.chromium.connect_over_cdp(
                self._settings.debug_chrome_cdp_endpoint, timeout=10_000
            )
            page = next(
                (
                    candidate
                    for context in browser.contexts
                    for candidate in context.pages
                    if "zhipin.com" in str(candidate.url or "")
                ),
                None,
            )
            opened_page = page is None
            if page is None:
                context = next((item for item in browser.contexts if item.pages), None)
                if context is None:
                    raise BossPageTokenError("Boss page token missing: no Chrome context")
                page = await context.new_page()
                await page.goto(
                    "https://www.zhipin.com/web/geek/chat",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            try:
                raw = await page.evaluate(
                    "() => String((window._PAGE || {}).token || '').split('|')[0]"
                )
            finally:
                if opened_page:
                    try:
                        await page.close()
                    except Exception:
                        pass
            if not isinstance(raw, str) or not raw:
                raise BossPageTokenError("Boss page token missing in current Chrome page")
            self._page_token = raw
            return raw
        except BossPageTokenError:
            raise
        except Exception as exc:
            raise BossPageTokenError("Boss page token unavailable from Chrome") from exc
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            await driver.stop()

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
