"""Tests for job progress Agent Tools and discovery/greeting integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.journey.job_registry import JobProgressStatus, SQLiteJobRegistry
from jobagent.models import Job, JobSource
from jobagent.scraper.boss import BossDiscoveryRequest
from jobagent.tools.job_discovery import build_boss_job_discovery_tool
from jobagent.tools.job_progress import (
    build_get_job_progress_tool,
    build_list_job_records_tool,
    build_update_job_progress_tool,
)


def _job(job_id: str = "boss:known1") -> Job:
    return Job(
        id=job_id,
        source=JobSource.BOSS,
        title="AI Agent 工程师",
        company="小而美科技",
        location="上海·杨浦区",
        url="https://www.zhipin.com/job_detail/known1.html",
        description="负责 AI Agent 平台开发",
        tags=["Python"],
    )


class FakeDiscovery:
    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs

    async def discover(self, request: BossDiscoveryRequest) -> list[Job]:
        return self._jobs


class TestDiscoveryRegistryIntegration:
    @pytest.mark.asyncio
    async def test_new_jobs_recorded_and_annotated(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        tool = build_boss_job_discovery_tool(
            FakeDiscovery([_job()]), registry_path=db
        )

        result = await tool.ainvoke({"query": "AI Agent", "city": "上海"})

        assert result["status"] == "completed"
        assert result["new_count"] == 1
        assert result["already_known"] == 0
        assert result["jobs"][0]["is_new"] is True
        assert result["jobs"][0]["progress_status"] == "discovered"

    @pytest.mark.asyncio
    async def test_known_greeted_job_annotated_not_new(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        with SQLiteJobRegistry(db) as registry:
            registry.upsert_discovered(
                job_id="boss:known1",
                source="boss",
                company="小而美科技",
                title="AI Agent 工程师",
            )
            registry.mark("boss:known1", JobProgressStatus.GREETED)
        tool = build_boss_job_discovery_tool(
            FakeDiscovery([_job()]), registry_path=db
        )

        result = await tool.ainvoke({"query": "AI Agent", "city": "上海"})

        assert result["new_count"] == 0
        assert result["already_known"] == 1
        job = result["jobs"][0]
        assert job["is_new"] is False
        assert job["progress_status"] == "greeted"
        assert "greeted_at" in job

    @pytest.mark.asyncio
    async def test_without_registry_keeps_old_contract(self) -> None:
        tool = build_boss_job_discovery_tool(FakeDiscovery([_job()]))

        result = await tool.ainvoke({"query": "AI Agent", "city": "上海"})

        assert result["status"] == "completed"
        assert result["count"] == 1
        assert "progress_status" not in result["jobs"][0]


class TestProgressTools:
    @pytest.mark.asyncio
    async def test_update_records_transition_with_note(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        with SQLiteJobRegistry(db) as registry:
            registry.upsert_discovered(
                job_id="boss:j1", source="boss", company="某公司", title="后端"
            )
            registry.mark("boss:j1", JobProgressStatus.GREETED)
        tool = build_update_job_progress_tool(db)

        result = await tool.ainvoke(
            {"job_id": "boss:j1", "status": "hr_replied", "note": "HR问期望薪资"}
        )

        assert result["status"] == "completed"
        assert result["progress_status"] == "hr_replied"
        assert result["note"] == "HR问期望薪资"

    @pytest.mark.asyncio
    async def test_update_unknown_job_returns_not_found(self, tmp_path: Path) -> None:
        tool = build_update_job_progress_tool(tmp_path / "registry.db")

        result = await tool.ainvoke({"job_id": "boss:ghost", "status": "greeted"})

        assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_update_illegal_transition_reports_current_state(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "registry.db"
        with SQLiteJobRegistry(db) as registry:
            registry.upsert_discovered(
                job_id="boss:j1", source="boss", company="某公司", title="后端"
            )
        tool = build_update_job_progress_tool(db)

        result = await tool.ainvoke({"job_id": "boss:j1", "status": "interviewing"})

        assert result["status"] == "invalid_transition"
        assert "discovered" in result["message"]

    @pytest.mark.asyncio
    async def test_get_returns_full_history(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        with SQLiteJobRegistry(db) as registry:
            registry.upsert_discovered(
                job_id="boss:j1", source="boss", company="某公司", title="后端"
            )
            registry.mark("boss:j1", JobProgressStatus.GREETED)
        tool = build_get_job_progress_tool(db)

        result = await tool.ainvoke({"job_id": "boss:j1"})

        assert result["status"] == "completed"
        assert [event["status"] for event in result["events"]] == [
            "discovered",
            "greeted",
        ]

    @pytest.mark.asyncio
    async def test_list_filters_by_status(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        with SQLiteJobRegistry(db) as registry:
            for job_id in ("boss:a", "boss:b"):
                registry.upsert_discovered(
                    job_id=job_id, source="boss", company="某公司", title="后端"
                )
            registry.mark("boss:a", JobProgressStatus.GREETED)
        tool = build_list_job_records_tool(db)

        result = await tool.ainvoke({"status": "greeted"})

        assert result["count"] == 1
        assert result["records"][0]["job_id"] == "boss:a"


@pytest.mark.asyncio
async def test_same_state_note_only_update_persists(tmp_path) -> None:
    """Regression (live false-ok 2026-08-28): correcting a wrong remark must
    work without a state change. The old same-state branch returned the
    record untouched - tools reported success, the note never changed."""
    from jobagent.journey.job_registry import JobProgressStatus, SQLiteJobRegistry

    with SQLiteJobRegistry(tmp_path / "jobs.db") as registry:
        registry.upsert_discovered(
            job_id="job-1", source="boss", company="梭翱", title="爬虫工程师"
        )
        registry.mark("job-1", JobProgressStatus.GREETED, note="2026-08-28 已回复")
        # same-state correction: only the note changes
        fixed = registry.mark(
            "job-1", JobProgressStatus.GREETED, note="更正：回复未送达，等待人工跟进"
        )
        assert fixed.note == "更正：回复未送达，等待人工跟进"
        reread = registry.get("job-1")
        assert reread is not None
        assert reread.note == "更正：回复未送达，等待人工跟进"
        assert reread.status is JobProgressStatus.GREETED
        # idempotent no-note re-mark stays a no-op
        again = registry.mark("job-1", JobProgressStatus.GREETED)
        assert again.note == "更正：回复未送达，等待人工跟进"
