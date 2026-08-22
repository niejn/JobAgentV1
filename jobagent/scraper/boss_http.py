"""Read-only Boss job-list Adapter using the site's JSON transport."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from curl_cffi import requests
from pydantic import HttpUrl

from jobagent.auth.cookie_manager import get_cookies
from jobagent.models import Job, JobSource
from jobagent.scraper.boss import _CITY_CODES, BossDiscoveryRequest, _parse_boss_salary

_JOB_LIST_URL = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
_SEARCH_PAGE_URL = "https://www.zhipin.com/web/geek/job"
_COMPANY_SCALE_CODES = {
    "0-20人": "301",
    "20-99人": "302",
    "100-499人": "303",
    "500-999人": "304",
    "1000-9999人": "305",
    "10000人以上": "306",
}


class BossAccessError(RuntimeError):
    """Boss rejected a read request; callers must not retry without cooldown."""


class BossHttpBackend:
    """Fetch one bounded Boss result page and normalize public Job Posting fields."""

    def __init__(
        self,
        settings: object,
        *,
        requester: Callable[..., Any] = requests.get,
    ) -> None:
        self._settings = settings
        self._requester = requester
        self._request_lock = asyncio.Lock()
        self._last_request_started = 0.0
        self._cooldown_until = 0.0

    async def discover(self, request: BossDiscoveryRequest) -> list[Job]:
        city_code = _CITY_CODES.get(request.city)
        if city_code is None:
            raise ValueError(f"Unsupported Boss city: {request.city}")
        unknown_sizes = set(request.company_sizes) - set(_COMPANY_SCALE_CODES)
        if unknown_sizes:
            raise ValueError("Unsupported Boss company-size band")
        cookies = await get_cookies("boss", self._settings)
        cookie_map = {
            str(cookie["name"]): str(cookie["value"])
            for cookie in cookies
            if cookie.get("name") and cookie.get("value")
        }
        params = {
            "scene": "1",
            "query": request.query,
            "city": city_code,
            "page": "1",
            "pageSize": "30",
        }
        if request.company_sizes:
            params["scale"] = ",".join(
                _COMPANY_SCALE_CODES[size] for size in request.company_sizes
            )
        async with self._request_lock:
            now = time.monotonic()
            if now < self._cooldown_until:
                raise BossAccessError("Boss risk control cooldown is active")
            minimum_interval = float(
                getattr(self._settings, "boss_api_rate_period_seconds", 1.0)
            )
            remaining = minimum_interval - (now - self._last_request_started)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_request_started = time.monotonic()
            response = await asyncio.to_thread(
                self._requester,
                _JOB_LIST_URL,
                params=params,
                cookies=cookie_map,
                impersonate="chrome",
                timeout=getattr(self._settings, "jobagent_request_timeout", 30),
                headers={"Referer": _SEARCH_PAGE_URL},
            )
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                cooldown = int(
                    getattr(self._settings, "boss_risk_cooldown_seconds", 900)
                )
                self._cooldown_until = time.monotonic() + cooldown
                raise BossAccessError("Boss returned an invalid response") from exc
            if not isinstance(payload, dict) or payload.get("code") != 0:
                cooldown = int(
                    getattr(self._settings, "boss_risk_cooldown_seconds", 900)
                )
                self._cooldown_until = time.monotonic() + cooldown
                raise BossAccessError(
                    "Boss risk control or authentication rejected the read request; "
                    "stop and wait for cooldown"
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


def _normalize_job(raw: dict[str, Any]) -> Job:
    external_id = str(raw.get("encryptJobId") or "").strip()
    if not external_id:
        raise ValueError("Boss job result is missing encryptJobId")
    city = str(raw.get("cityName") or "").strip()
    district = str(raw.get("areaDistrict") or "").strip()
    business = str(raw.get("businessDistrict") or "").strip()
    location = "·".join(value for value in (city, district, business) if value)
    labels = _string_list(raw.get("jobLabels"))
    skills = _string_list(raw.get("skills"))
    tags = list(dict.fromkeys([*labels, *skills]))
    title = str(raw.get("jobName") or "Unknown").strip()
    return Job(
        id=f"boss:{external_id}",
        source=JobSource.BOSS,
        title=title,
        company=str(raw.get("brandName") or "Unknown").strip(),
        location=location or city or "Unknown",
        url=HttpUrl(f"https://www.zhipin.com/job_detail/{external_id}.html"),
        description="；".join([title, *tags]),
        salary=_parse_boss_salary(str(raw.get("salaryDesc") or "")),
        tags=tags,
        metadata={
            "company_size": raw.get("brandScaleName"),
            "city": city,
            "district": district,
            "business_district": business,
            "listing_summary_only": True,
            "security_id": raw.get("securityId"),
        },
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
