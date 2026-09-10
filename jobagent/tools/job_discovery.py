"""Read-only Boss job discovery application module and Agent Tool adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from langchain_core.tools import BaseTool, StructuredTool

from jobagent.auth.cookie_manager import CookieNotFoundError
from jobagent.config import Settings
from jobagent.crawl import CrawlGate
from jobagent.journey.job_registry import SQLiteJobRegistry
from jobagent.models import Job
from jobagent.profile import CandidateContext
from jobagent.scraper.boss import BossAccessError, BossDiscoveryRequest


class BossDiscovery(Protocol):
    async def discover(self, request: BossDiscoveryRequest) -> list[Job]: ...


class BossJobDiscovery:
    """Boss search via CDP (connected to your real logged-in Chrome)."""

    def __init__(self, settings: Settings, *, crawl_gate: CrawlGate | None = None) -> None:
        self._settings = settings
        self._crawl_gate = crawl_gate

    async def discover(self, request: BossDiscoveryRequest) -> list[Job]:
        if self._settings.boss_search_transport == "http":
            from jobagent.scraper.boss_http import BossHttpBackend

            return await BossHttpBackend(self._settings).discover(request)
        from jobagent.scraper.boss_cdp import BossCdpBackend

        backend = BossCdpBackend(self._settings, crawl_gate=self._crawl_gate)
        try:
            return await backend.discover(request)
        finally:
            await backend.dispose()


def _missing_candidate_data(context: CandidateContext | None) -> list[str]:
    """Return the missing global inputs job discovery needs before crawling.

    Job discovery derives its search keywords, city and filters from the
    confirmed Job Search Profile, and judges postings against the candidate
    background; crawling before both exist produces recommendations the user
    cannot act on. The Agent must collect these first.
    """

    missing: list[str] = []
    if context is None or context.search_profile is None:
        missing.append("job_search_profile")
    if context is None or (
        context.background is None and context.resume_text is None
    ):
        missing.append("candidate_background_or_resume")
    return missing


_MISSING_DATA_LABELS = {
    "job_search_profile": (
        "全局求职意向（期望岗位、城市、薪资、公司特征、职位特征、排除条件；"
        "确认后用 save_job_search_profile 保存）"
    ),
    "candidate_background_or_resume": (
        "基础简历或已确认候选人背景（用 import_candidate_resume 导入，"
        "确认事实后用 save_candidate_background 保存）"
    ),
}


def build_boss_job_discovery_tool(
    discovery: BossDiscovery,
    *,
    context_loader: Callable[[], CandidateContext | None] | None = None,
    registry_path: Path | None = None,
) -> BaseTool:
    """Expose safe read-only Boss discovery to JobAgent.

    ``context_loader`` supplies the latest persisted candidate context. When
    the Job Search Profile or the resume/confirmed background is missing, the
    tool refuses to crawl and returns ``missing_candidate_data`` so the Agent
    collects the data first instead of producing useless recommendations.

    ``registry_path`` points at the job progress registry: every returned job
    is recorded (new ones as ``discovered``), and known jobs come back with
    their live ``progress_status`` so already-greeted or interviewing jobs are
    never re-recommended.
    """

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

        if context_loader is not None:
            missing = _missing_candidate_data(context_loader())
            if missing:
                return {
                    "status": "missing_candidate_data",
                    "missing": missing,
                    "message": (
                        "岗位发现前需先收集: "
                        + "；".join(
                            _MISSING_DATA_LABELS[item] for item in missing
                        )
                        + "。请先引导用户补齐资料，资料就绪后再重新发现岗位；"
                        "不要绕过本检查。"
                    ),
                }

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
        except BossAccessError as exc:
            # Pass the real reason through so the Agent can act on it:
            # cdp_not_ready -> follow skills/ChromeCDP-setup/SKILL.md;
            # cooldown_active / boss_risk_control -> stop retrying and wait.
            result: dict[str, Any] = {
                "status": "blocked",
                "error_type": getattr(exc, "code", "boss_access_error"),
                "message": str(exc),
            }
            details = getattr(exc, "details", None)
            if isinstance(details, dict):
                result["diagnostic"] = details
            return result
        except CookieNotFoundError:
            return {
                "status": "blocked",
                "error_type": "boss_cookie_missing",
                "message": "No matching Boss cookie is available.",
            }
        payloads, new_count = _record_jobs(jobs, registry_path)
        return {
            "status": "completed",
            "count": len(jobs),
            "new_count": new_count,
            "already_known": len(jobs) - new_count,
            "jobs": payloads,
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


def _record_jobs(
    jobs: list[Job], registry_path: Path | None
) -> tuple[list[dict[str, Any]], int]:
    """Persist discovered jobs and annotate payloads with journey status.

    New jobs enter the registry as ``discovered``; known jobs keep their
    current progress status (greeted / interviewing / closed ...) plus
    ``greeted_at`` when present, so the Agent never re-recommends a job the
    user already pursued.
    """

    if registry_path is None:
        return [_job_payload(job) for job in jobs], len(jobs)
    from jobagent.journey.boss_contact import BossContactRegistry

    payloads: list[dict[str, Any]] = []
    new_count = 0
    with (
        SQLiteJobRegistry(registry_path) as registry,
        BossContactRegistry(registry_path) as contact_registry,
    ):
        for job in jobs:
            payload = _job_payload(job)
            contact_registry.save_job_transport(job_id=job.id, metadata=job.metadata)
            created = registry.upsert_discovered(
                job_id=job.id,
                source=job.source.value,
                company=job.company,
                title=job.title,
                location=job.location,
                url=str(job.url),
            )
            if created is not None:
                new_count += 1
                payload["is_new"] = True
                payload["progress_status"] = created.status.value
            else:
                record = registry.get(job.id)
                payload["is_new"] = False
                payload["progress_status"] = (
                    record.status.value if record is not None else "unknown"
                )
                if record is not None and record.greeted_at is not None:
                    payload["greeted_at"] = record.greeted_at.isoformat()
            payloads.append(payload)
    return payloads, new_count


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
