"""Agent Tool tests for read-only Boss job discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.models import Job, JobSource
from jobagent.profile.context import (
    CandidateBackground,
    CandidateContext,
    JobSearchProfile,
)
from jobagent.scraper.boss import BossAccessError, BossDiscoveryRequest
from jobagent.tools.job_discovery import build_boss_job_discovery_tool


class FakeBossDiscovery:
    async def discover(self, request: BossDiscoveryRequest) -> list[Job]:
        assert request.city == "上海"
        assert request.area == "五角场"
        assert request.company_sizes == ["0-20人", "20-99人"]
        return [
            Job(
                id="boss:stable-id",
                source=JobSource.BOSS,
                title="AI Agent 工程师",
                company="小而美科技",
                location="上海·杨浦区·五角场",
                url="https://www.zhipin.com/job_detail/example.html",
                description="负责 AI Agent 平台开发",
                tags=["Python", "LangChain"],
                metadata={"company_size": "0-20人"},
            )
        ]


@pytest.mark.asyncio
async def test_tool_returns_structured_jobs_without_platform_parameters() -> None:
    tool = build_boss_job_discovery_tool(FakeBossDiscovery())

    result = await tool.ainvoke(
        {
            "query": "AI Agent",
            "city": "上海",
            "area": "五角场",
            "company_sizes": ["0-20人", "20-99人"],
            "limit": 10,
        }
    )

    assert result["status"] == "completed"
    assert result["count"] == 1
    assert result["jobs"][0]["company"] == "小而美科技"
    assert result["jobs"][0]["company_size"] == "0-20人"
    assert result["jobs"][0]["source"] == "boss"


class RiskBlockedBossDiscovery:
    async def discover(self, request: BossDiscoveryRequest) -> list[Job]:
        raise BossAccessError("Boss risk control")


class NeverCrawlDiscovery:
    """Fail the test if discovery runs while candidate data is incomplete."""

    async def discover(self, request: BossDiscoveryRequest) -> list[Job]:
        raise AssertionError("must not crawl when candidate data is missing")


def _context(
    *,
    with_profile: bool,
    with_background: bool,
) -> CandidateContext | None:
    if not with_profile and not with_background:
        return None
    return CandidateContext(
        search_profile=(
            JobSearchProfile(desired_roles=["后端开发"], preferred_locations=["上海"])
            if with_profile
            else None
        ),
        resume_path=Path("resume.txt") if with_background else None,
        background=(
            CandidateBackground(name="张三", skills=["Python"])
            if with_background
            else None
        ),
        resume_text="张三的简历" if with_background else None,
    )


@pytest.mark.asyncio
async def test_tool_refuses_to_crawl_when_no_candidate_data() -> None:
    tool = build_boss_job_discovery_tool(
        NeverCrawlDiscovery(), context_loader=lambda: None
    )

    result = await tool.ainvoke({"query": "AI Agent", "city": "上海"})

    assert result["status"] == "missing_candidate_data"
    assert result["missing"] == [
        "job_search_profile",
        "candidate_background_or_resume",
    ]
    assert "save_job_search_profile" in result["message"]
    assert "import_candidate_resume" in result["message"]


@pytest.mark.asyncio
async def test_tool_refuses_to_crawl_when_only_profile_saved() -> None:
    tool = build_boss_job_discovery_tool(
        NeverCrawlDiscovery(),
        context_loader=lambda: _context(with_profile=True, with_background=False),
    )

    result = await tool.ainvoke({"query": "AI Agent"})

    assert result["status"] == "missing_candidate_data"
    assert result["missing"] == ["candidate_background_or_resume"]


@pytest.mark.asyncio
async def test_tool_refuses_to_crawl_when_only_resume_saved() -> None:
    tool = build_boss_job_discovery_tool(
        NeverCrawlDiscovery(),
        context_loader=lambda: _context(with_profile=False, with_background=True),
    )

    result = await tool.ainvoke({"query": "AI Agent"})

    assert result["status"] == "missing_candidate_data"
    assert result["missing"] == ["job_search_profile"]


@pytest.mark.asyncio
async def test_tool_crawls_once_profile_and_background_are_saved() -> None:
    tool = build_boss_job_discovery_tool(
        FakeBossDiscovery(),
        context_loader=lambda: _context(with_profile=True, with_background=True),
    )

    result = await tool.ainvoke(
        {
            "query": "AI Agent",
            "city": "上海",
            "area": "五角场",
            "company_sizes": ["0-20人", "20-99人"],
            "limit": 10,
        }
    )

    assert result["status"] == "completed"
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_tool_returns_safe_blocked_result_instead_of_crashing_agent() -> None:
    tool = build_boss_job_discovery_tool(RiskBlockedBossDiscovery())

    result = await tool.ainvoke({"query": "AI Agent", "city": "上海"})

    # The tool layer now passes the backend's real code and message through
    # so the Agent can act on the failure reason.
    assert result["status"] == "blocked"
    assert result["error_type"] == "boss_access_denied"
    assert "Boss risk control" in result["message"]
