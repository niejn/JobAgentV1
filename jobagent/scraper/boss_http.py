"""Direct HTTP adapter for read-only Boss job discovery."""

from __future__ import annotations

import time
from typing import Any

from jobagent.auth.cookie_manager import CookieNotFoundError, get_cookies
from jobagent.config import Settings
from jobagent.models import Job
from jobagent.scraper.boss import (
    _CITY_CODES,
    BossAccessError,
    BossDiscoveryRequest,
    _normalize_job,
    get_boss_cooldown,
)

_JOB_LIST_PATH = "/wapi/zpgeek/search/joblist.json"
_RISK_CODES = {31, 32, 35, 36, 37, 5002, 5003, 5004}


class BossHttpBackend:
    """Search Boss through its read-only job-list HTTP endpoint.

    The adapter accepts an injected HTTP client for tests. In production it
    loads the persisted Boss session cookies and uses ``httpx`` only; it does
    not use TLS-fingerprint impersonation libraries.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._cookies = cookies

    async def discover(self, request: BossDiscoveryRequest) -> list[Job]:
        allowed, remaining_min, reason = get_boss_cooldown().check()
        if not allowed:
            raise BossAccessError(
                f"Boss 风控冷却中（约剩 {remaining_min} 分钟），本次请求未发出。",
                code="cooldown_active",
            )
        city_code = _CITY_CODES.get(request.city)
        if city_code is None:
            raise ValueError(f"Unsupported Boss city: {request.city}")

        cookies = await self._load_cookies()
        params: dict[str, str | int] = {
            "query": request.query,
            "city": city_code,
            "page": 1,
            "pageSize": request.limit,
            "_": int(time.time() * 1000),
        }
        params.update(request._filter_query_params())
        headers = self._headers(cookies)
        response = await self._get(
            f"https://www.zhipin.com{_JOB_LIST_PATH}", params=params, headers=headers
        )
        if response.status_code != 200:
            raise BossAccessError(
                f"Boss job search HTTP error: {response.status_code}",
                code="boss_http_error",
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise BossAccessError(
                "Boss job search returned malformed JSON", code="boss_bad_response"
            )
        code = int(payload.get("code") or 0)
        if code != 0:
            if code in _RISK_CODES:
                get_boss_cooldown().trigger("risk_control")
                raise BossAccessError(
                    str(payload.get("message") or "Boss rejected the job search"),
                    code="boss_risk_control",
                )
            raise BossAccessError(
                str(payload.get("message") or "Boss job search failed"),
                code="boss_api_error",
            )
        raw_jobs = ((payload.get("zpData") or {}).get("jobList") or [])
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
            if request.company_sizes:
                size = raw.get("brandScaleName")
                if size not in request.company_sizes:
                    continue
            jobs.append(job)
            if len(jobs) >= request.limit:
                break
        return jobs

    async def _load_cookies(self) -> dict[str, str]:
        if self._cookies is not None:
            return dict(self._cookies)
        try:
            persisted = await get_cookies("boss", self._settings)
        except CookieNotFoundError:
            raise
        return {
            str(item["name"]): str(item["value"])
            for item in persisted
            if item.get("name") and item.get("value")
        }

    async def _get(self, url: str, *, params: dict[str, str | int], headers: dict[str, str]) -> Any:
        if self._client is not None:
            return await self._client.get(url, params=params, headers=headers)
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx dependency is required") from exc
        async with httpx.AsyncClient(
            cookies=await self._load_cookies(), headers=headers, timeout=20, follow_redirects=True
        ) as client:
            return await client.get(url, params=params)

    @staticmethod
    def _headers(cookies: dict[str, str]) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.zhipin.com",
            "Referer": "https://www.zhipin.com/web/geek/job",
            "X-Requested-With": "XMLHttpRequest",
            "traceId": f"F-{int(time.time() * 1000)}",
            "zp_token": cookies.get("bst", ""),
        }
