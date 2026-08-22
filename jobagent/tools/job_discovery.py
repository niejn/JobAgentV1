"""Read-only Boss job discovery application module and Agent Tool adapter."""

from __future__ import annotations

from typing import Any, Protocol

from langchain_core.tools import BaseTool, StructuredTool

from jobagent.auth.cookie_manager import CookieNotFoundError
from jobagent.models import Job
from jobagent.scraper.boss import BossDiscoveryRequest
from jobagent.scraper.boss_http import BossAccessError, BossHttpBackend


class BossDiscovery(Protocol):
    async def discover(self, request: BossDiscoveryRequest) -> list[Job]: ...


class BossJobDiscovery:
    """Hide browser and Boss transport details behind one business interface."""

    def __init__(self, settings: object) -> None:
        self._backend = BossHttpBackend(settings)

    async def discover(self, request: BossDiscoveryRequest) -> list[Job]:
        return await self._backend.discover(request)


def build_boss_job_discovery_tool(discovery: BossDiscovery) -> BaseTool:
    """Expose safe read-only Boss discovery to JobAgent."""

    async def discover_boss_jobs(
        query: str,
        city: str = "全国",
        area: str | None = None,
        company_sizes: list[str] | None = None,
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
            "and explicit company-size bands such as 0-20人 or 20-99人. This is read-only "
            "and does not greet HR or apply."
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
