"""Agent Tool tests for read-only Boss job discovery."""

from __future__ import annotations

import pytest

from jobagent.models import Job, JobSource
from jobagent.scraper.boss import BossDiscoveryRequest
from jobagent.scraper.boss_http import BossAccessError
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


@pytest.mark.asyncio
async def test_tool_returns_safe_blocked_result_instead_of_crashing_agent() -> None:
    tool = build_boss_job_discovery_tool(RiskBlockedBossDiscovery())

    result = await tool.ainvoke({"query": "AI Agent", "city": "上海"})

    assert result == {
        "status": "blocked",
        "error_type": "boss_risk_control",
        "message": "Boss read access is cooling down; no retry was attempted.",
    }
