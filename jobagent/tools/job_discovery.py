"""Read-only Boss job discovery application module and Agent Tool adapter."""

from __future__ import annotations

from typing import Any, Protocol

from langchain_core.tools import BaseTool, StructuredTool

from jobagent.auth.cookie_manager import CookieNotFoundError
from jobagent.config import Settings
from jobagent.models import Job
from jobagent.scraper.boss import BossAccessError, BossDiscoveryRequest


class BossDiscovery(Protocol):
    async def discover(self, request: BossDiscoveryRequest) -> list[Job]: ...


class BossJobDiscovery:
    """Boss search via CDP (connected to your real logged-in Chrome)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def discover(self, request: BossDiscoveryRequest) -> list[Job]:
        from jobagent.scraper.boss_cdp import BossCdpBackend

        backend = BossCdpBackend(self._settings)
        try:
            return await backend.discover(request)
        finally:
            await backend.dispose()
        return await backend.discover(request)


def build_boss_job_discovery_tool(discovery: BossDiscovery) -> BaseTool:
    """Expose safe read-only Boss discovery to JobAgent."""

    async def discover_boss_jobs(
        query: str,
        city: str = "全国",
        area: str | None = None,
        company_sizes: list[str] | None = None,
        job_type: str | None = None,
        salary: str | None = None,
        experience: str | None = None,
        degree: str | None = None,
        industry: str | None = None,
        stage: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Find Boss jobs; never contact HR or submit applications."""

        try:
            jobs = await discovery.discover(
                BossDiscoveryRequest(
                    query=query,
                    city=city,
                    area=area,
                    company_sizes=company_sizes or [],
                    job_type=job_type,
                    salary=salary,
                    experience=experience,
                    degree=degree,
                    industry=industry,
                    stage=stage,
                    limit=limit,
                )
            )
        except BossAccessError:
            return {
                "status": "blocked",
                "error_type": "boss_risk_control",
                "message": "Boss read access is cooling down; no retry was attempted.",
            }
        except CookieNotFoundError:
            return {
                "status": "blocked",
                "error_type": "boss_cookie_missing",
                "message": "No matching Boss cookie is available.",
            }
        return {
            "status": "completed",
            "count": len(jobs),
            "jobs": [_job_payload(job) for job in jobs],
        }

    return StructuredTool.from_function(
        coroutine=discover_boss_jobs,
        name="discover_boss_jobs",
        description=(
            "Read Boss job postings by role keywords, city, area/business-district text, "
            "company-size bands (0-20人, 20-99人, 100-499人, 500-999人, 1000-9999人, 10000人以上), "
            "job type (1901=全职, 1902=兼职, 1903=实习, 1905=校招), "
            "salary range (402=3K以下, 403=3-5K, 404=5-10K, 405=10-20K, 406=20-50K, 407=50K+), "
            "experience (108=在校生, 102=应届生, 104=1-3年, 105=3-5年, 106=5-10年), "
            "degree (202=大专, 203=本科, 204=硕士, 205=博士), "
            "industry code (e.g. 1001=互联网), and funding stage "
            "(801=未融资, 802=天使轮, 803=A轮, 804=B轮, 805=C轮, "
            "806=D轮, 807=已上市, 808=不需要融资). "
            "This is read-only and does not greet HR or apply."
        ),
        args_schema=BossDiscoveryRequest,
    )


def _job_payload(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "source": job.source.value,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "url": str(job.url),
        "description": job.description,
        "tags": job.tags,
        "salary": job.salary.model_dump(mode="json") if job.salary else None,
        "company_size": job.metadata.get("company_size"),
        "company_tags": job.metadata.get("company_tags", []),
    }
