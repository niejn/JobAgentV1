"""Boss直聘 discovery domain models and shared parsing helpers."""

from __future__ import annotations

import re
import time
from typing import Any

from pydantic import BaseModel, Field, HttpUrl

from jobagent.models import Job, JobSource, SalaryRange

_CITY_CODES = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
}


class BossDiscoveryRequest(BaseModel):
    """User-level filters for read-only Boss job discovery.

    Filter parameters mirror Boss's own ``/web/geek/job`` URL query string.
    """

    query: str = Field(min_length=1, description="Search keyword(s)")
    city: str = Field(default="全国", description="City name (e.g. 上海, 北京)")
    area: str | None = Field(
        default=None,
        description="Business district filter (e.g. 五角场, 漕河泾)",
    )
    company_sizes: list[str] = Field(
        default_factory=list,
        description=(
            "Company size bands: 0-20人, 20-99人, 100-499人, "
            "500-999人, 1000-9999人, 10000人以上"
        ),
    )
    job_type: str | None = Field(
        default=None,
        description=(
            "Job type: 1901=全职, 1902=兼职, 1903=实习, "
            "1904=全职/兼职, 1905=校招"
        ),
    )
    salary: str | None = Field(
        default=None,
        description=(
            "Salary range code: 402=3K以下, 403=3-5K, 404=5-10K, "
            "405=10-20K, 406=20-50K, 407=50K+"
        ),
    )
    experience: str | None = Field(
        default=None,
        description=(
            "Experience code: 108=在校生, 102=应届生, 101=经验不限, "
            "103=1年以内, 104=1-3年, 105=3-5年, 106=5-10年, 107=10年+"
        ),
    )
    degree: str | None = Field(
        default=None,
        description=(
            "Education code: 209=初中及以下, 208=中专/中技, 206=高中, "
            "202=大专, 203=本科, 204=硕士, 205=博士"
        ),
    )
    industry: str | None = Field(
        default=None,
        description="Industry code (e.g. 1001=互联网, 1002=电商, 1003=金融)",
    )
    stage: str | None = Field(
        default=None,
        description=(
            "Funding stage code: 801=未融资, 802=天使轮, 803=A轮, "
            "804=B轮, 805=C轮, 806=D轮及以上, 807=已上市, 808=不需要融资"
        ),
    )
    limit: int = Field(default=20, ge=1, le=100)

    def _filter_query_params(self) -> dict[str, str]:
        """Map filter fields to Boss URL query parameters (skip empty)."""

        params: dict[str, str] = {}
        if self.job_type:
            params["jobType"] = self.job_type
        if self.salary:
            params["salary"] = self.salary
        if self.experience:
            params["experience"] = self.experience
        if self.degree:
            params["degree"] = self.degree
        if self.industry:
            params["industry"] = self.industry
        if self.stage:
            params["stage"] = self.stage
        if self.company_sizes:
            params["scale"] = ",".join(
                _COMPANY_SCALE_CODES.get(size, size) for size in self.company_sizes
            )
        return params


class BossAccessError(RuntimeError):
    """Boss access failed; ``code`` tells the Agent what to do next.

    Codes:
    - ``cdp_not_ready``  - Chrome debug port is down -> follow skills/ChromeCDP-setup/SKILL.md
    - ``cooldown_active`` - rate-limit cooldown running -> wait, do not retry
    - ``boss_risk_control`` - server rejected the request -> cooldown started
    - ``boss_access_denied`` - login expired or automation detected -> check the Chrome window
    """

    def __init__(self, message: str, *, code: str = "boss_access_denied") -> None:
        super().__init__(message)
        self.code = code


class BossCooldownManager:
    """In-memory rate-limit cooldown shared across Boss operations.

    When Boss rejects a read with risk control, all Boss tools should refuse
    to send further requests until the cooldown expires. Without this the
    Agent retries the tool, each retry is a real request, and the account
    gets blocked harder.
    """

    def __init__(self, cooldown_seconds: int = 3600) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._blocked_until = 0.0
        self._reason = ""

    def trigger(self, reason: str = "risk_control") -> None:
        self._blocked_until = time.monotonic() + self._cooldown_seconds
        self._reason = reason

    def check(self) -> tuple[bool, int, str]:
        """Return ``(allowed, remaining_minutes, reason)``."""

        remaining = self._blocked_until - time.monotonic()
        if remaining <= 0:
            return True, 0, ""
        return False, max(1, int(remaining // 60)), self._reason

    def reset(self) -> None:
        self._blocked_until = 0.0
        self._reason = ""


_cooldown_manager = BossCooldownManager()


def get_boss_cooldown() -> BossCooldownManager:
    """Process-wide cooldown shared by search and greeting tools."""

    return _cooldown_manager


def _normalize_job(raw: dict[str, Any]) -> Job:
    """Map a raw Boss API joblist item to a normalized ``Job``."""

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
            "company_stage": raw.get("brandStageName"),
            "company_industry": raw.get("brandIndustry"),
            "city": city,
            "district": district,
            "business_district": business,
            "experience": raw.get("jobExperience"),
            "degree": raw.get("jobDegree"),
            "listing_summary_only": True,
            "security_id": raw.get("securityId"),
            # Internal-only transport identity. _job_payload deliberately
            # does not expose these values to the LLM/tool response.
            "encrypt_boss_id": raw.get("encryptBossId"),
            "boss_name": raw.get("bossName"),
            "boss_title": raw.get("bossTitle"),
            "lid": raw.get("lid"),
            "job_source": raw.get("jobSource") or 0,
        },
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_boss_salary(text: str) -> SalaryRange | None:
    """Parse Boss salary text like '25-50K·16薪' into SalaryRange."""

    match = re.search(r"(\d+)-(\d+)K", text)
    if not match:
        return None

    low = int(match.group(1)) * 1000
    high = int(match.group(2)) * 1000

    months = 12
    bonus_match = re.search(r"(\d+)薪", text)
    if bonus_match:
        months = int(bonus_match.group(1))

    return SalaryRange(
        min_annual=low * months,
        max_annual=high * months,
        currency="CNY",
    )


_COMPANY_SCALE_CODES = {
    "0-20人": "301",
    "20-99人": "302",
    "100-499人": "303",
    "500-999人": "304",
    "1000-9999人": "305",
    "10000人以上": "306",
}
