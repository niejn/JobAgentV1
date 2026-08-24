"""Agent Tool for batch-sending Boss greetings with HITL confirmation."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, field_validator

from jobagent.applier.boss import BossApplier
from jobagent.config import Settings
from jobagent.domain import HttpUrl, Job, JobSource, Profile
from jobagent.profile import SQLiteCandidateContextProvider

logger = logging.getLogger(__name__)


class GreetingTarget(BaseModel):
    """One Boss job to greet — minimal fields the agent already has."""

    url: str = Field(min_length=1, description="Full Boss job-detail URL")
    company: str = Field(min_length=1, description="Company name")
    title: str = Field(min_length=1, description="Job title")
    job_id: str = Field(default="", description="Unique job ID if known")

    @field_validator("url")
    @classmethod
    def validate_boss_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        hostname = parsed.hostname or ""
        if "zhipin.com" not in hostname or not parsed.path:
            raise ValueError("Provide a valid Boss job detail URL (zhipin.com)")
        return value.strip()


class BossGreetJobsRequest(BaseModel):
    """Jobs to greet and batch controls."""

    jobs: list[GreetingTarget] = Field(
        min_length=1,
        max_length=20,
        description=(
            "List of Boss jobs to greet. The Agent must present these to the user "
            "and obtain explicit confirmation before calling this tool."
        ),
    )
    max_greetings: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum greetings to send in this batch",
    )


class BossGreetingsManager:
    """Batch-send greetings on Boss using the existing Playwright applier."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def greet(self, request: BossGreetJobsRequest) -> dict[str, Any]:
        """Execute greetings for up to ``max_greetings`` jobs."""

        profile = self._load_profile()
        if profile is None:
            return {
                "status": "failed",
                "message": "候选人背景未就绪；请先导入简历并确认候选背景。",
            }

        targets = request.jobs[: request.max_greetings]
        results: list[dict[str, Any]] = []

        async with BossApplier(self._settings) as applier:
            for target in targets:
                job = Job(
                    id=target.job_id or target.url,
                    source=JobSource.BOSS,
                    title=target.title,
                    company=target.company,
                    location="",
                    url=HttpUrl(target.url),
                    description="",
                )
                application = await applier.apply(job, profile)
                status = application.status.value
                extra = application.extra or {}
                results.append(
                    {
                        "job_id": target.job_id or target.url,
                        "company": target.company,
                        "title": target.title,
                        "status": status,
                        "reason": extra.get("reason", ""),
                        "greeting_sent": extra.get("greeting_sent"),
                    }
                )
                # Stop on daily limit or captcha — further jobs won't work.
                if status in ("failed", "captcha_blocked"):
                    reason = extra.get("reason", "")
                    if reason in ("daily_limit", "captcha"):
                        break

        succeeded = sum(1 for r in results if r["status"] == "submitted")
        result: dict[str, Any] = {
            "status": "completed" if succeeded > 0 else "failed",
            "total": len(targets),
            "succeeded": succeeded,
            "results": results,
        }
        # Read interview-mode preference to guide HR conversation.
        interview_mode = self._load_interview_preference()
        if interview_mode:
            result["interview_mode_note"] = interview_mode
        return result

    def _load_interview_preference(self) -> str | None:
        """Return a note about the user's interview-mode preference, if set."""

        provider = SQLiteCandidateContextProvider(
            self._settings.jobagent_state_db
        )
        context = provider.load()
        if context is None or context.search_profile is None:
            return None
        sp = context.search_profile
        if sp.prefer_online_interview:
            return (
                "候选人优先在线面试（视频/电话），"
                "如果 HR 回复了招呼，请确认面试方式能否在线上进行。"
            )
        return None

    def _load_profile(self) -> Profile | None:
        """Build a ``Profile`` from the candidate background stored in SQLite."""

        provider = SQLiteCandidateContextProvider(
            self._settings.jobagent_state_db
        )
        context = provider.load()
        if context is None:
            return None
        bg = context.background
        if bg is None:
            return None
        return Profile(
            name=bg.name,
            skills=bg.skills,
            years_experience=bg.years_experience,
            summary=bg.summary,
            desired_roles=(
                context.search_profile.desired_roles
                if context.search_profile
                else []
            ),
            preferred_locations=(
                context.search_profile.preferred_locations
                if context.search_profile
                else []
            ),
        )


def build_boss_greet_jobs_tool(manager: BossGreetingsManager) -> BaseTool:
    """Expose batch greeting to the Agent with HITL expectation."""

    async def boss_greet_jobs(
        jobs: list[dict[str, str]],
        max_greetings: int = 5,
    ) -> dict[str, Any]:
        """批量向 Boss 招聘方发送打招呼消息，需要用户先确认。

        使用说明：
        1. 先用 discover_boss_jobs 找到合适的岗位
        2. 向用户展示岗位列表并询问是否要打招呼
        3. 用户确认后，调用此工具传入选中的岗位
        4. 工具会在后台逐一点击「立即沟通」并发送招呼语
        5. 招呼语使用 BOSS_GREETING 模板（支持 $company / $title / $name 变量）
        """

        return await manager.greet(
            BossGreetJobsRequest(
                jobs=[GreetingTarget(**j) for j in jobs],
                max_greetings=max_greetings,
            )
        )

    return StructuredTool.from_function(
        coroutine=boss_greet_jobs,
        name="boss_greet_jobs",
        description=(
            "Send greetings to Boss HR for listed jobs (batch). "
            "Uses an existing logged-in Boss session with Playwright. "
            "This tool REQUIRES the user to confirm before calling it. "
            "The greeter logs and enforces daily Boss limits. "
            "Call update_job_application_state after to track the result."
        ),
        args_schema=BossGreetJobsRequest,
    )