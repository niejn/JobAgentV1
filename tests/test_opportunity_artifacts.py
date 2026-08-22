"""Behavior tests for local Opportunity Journey analysis artifacts."""

from datetime import UTC, datetime
from pathlib import Path

from jobagent.artifacts import (
    ApplicationState,
    FitDecision,
    LocalOpportunityArtifacts,
    StatusPeriod,
)


def test_saves_immutable_jd_and_versioned_analysis_reports(tmp_path: Path) -> None:
    store = LocalOpportunityArtifacts(tmp_path)

    first = store.save_analysis(
        company="示例科技",
        role="AI Agent Engineer",
        job_description="负责 Agent、RAG 与平台工程。",
        analysis_report="# 分析\n\n匹配 Python 与 Agent。",
        fit=FitDecision.SUITABLE,
        application_state=ApplicationState.NOT_APPLIED,
    )
    second = store.save_analysis(
        company="示例科技",
        role="AI Agent Engineer",
        job_description="负责 Agent、RAG 与平台工程。",
        analysis_report="# 分析 v2\n\n补充面试准备建议。",
        fit=FitDecision.SUITABLE,
        application_state=ApplicationState.NOT_APPLIED,
    )

    assert first.opportunity_id == second.opportunity_id
    assert first.analysis_version == 1
    assert second.analysis_version == 2
    assert first.jd_path == second.jd_path
    assert first.jd_path.read_text(encoding="utf-8") == "负责 Agent、RAG 与平台工程。"
    assert second.analysis_path.name == "analysis-v002.md"
    assert "补充面试准备建议" in second.analysis_path.read_text(encoding="utf-8")
    assert second.manifest_path.is_file()


def test_status_board_filters_analysis_by_week_and_month(tmp_path: Path) -> None:
    def save_at(
        timestamp: datetime,
        *,
        company: str,
        fit: FitDecision,
        application_state: ApplicationState,
    ) -> None:
        LocalOpportunityArtifacts(tmp_path, clock=lambda: timestamp).save_analysis(
            company=company,
            role="AI Engineer",
            job_description=f"{company} JD",
            analysis_report=f"{company} analysis",
            fit=fit,
            application_state=application_state,
        )

    save_at(
        datetime(2026, 8, 20, tzinfo=UTC),
        company="本周公司",
        fit=FitDecision.SUITABLE,
        application_state=ApplicationState.APPLIED,
    )
    save_at(
        datetime(2026, 8, 2, tzinfo=UTC),
        company="本月公司",
        fit=FitDecision.UNCERTAIN,
        application_state=ApplicationState.NOT_APPLIED,
    )
    save_at(
        datetime(2026, 7, 30, tzinfo=UTC),
        company="上月公司",
        fit=FitDecision.UNSUITABLE,
        application_state=ApplicationState.CLOSED,
    )
    store = LocalOpportunityArtifacts(tmp_path)
    reference = datetime(2026, 8, 21, tzinfo=UTC)

    week = store.status_board(period=StatusPeriod.WEEK, now=reference)
    month = store.status_board(period=StatusPeriod.MONTH, now=reference)
    overall = store.status_board(period=StatusPeriod.ALL, now=reference)

    assert week.analyzed == 1
    assert week.suitable == 1
    assert week.applied == 1
    assert [item.company for item in week.items] == ["本周公司"]
    assert month.analyzed == 2
    assert month.uncertain == 1
    assert overall.analyzed == 3
    assert overall.unsuitable == 1
    assert overall.applied == 2


def test_updates_application_state_for_existing_opportunity(tmp_path: Path) -> None:
    store = LocalOpportunityArtifacts(tmp_path)
    saved = store.save_analysis(
        company="示例科技",
        role="AI Engineer",
        job_description="负责 Agent 平台。",
        analysis_report="# 分析\n\n建议投递。",
        fit=FitDecision.SUITABLE,
    )

    updated = store.update_application_state(
        saved.opportunity_id,
        ApplicationState.APPLIED,
    )

    assert updated.application_state is ApplicationState.APPLIED
    board = store.status_board()
    assert board.applied == 1
    assert board.items[0].application_state is ApplicationState.APPLIED


def test_status_period_uses_analysis_time_not_later_application_update(
    tmp_path: Path,
) -> None:
    analyzed_at = datetime(2026, 7, 30, tzinfo=UTC)
    applied_at = datetime(2026, 8, 20, tzinfo=UTC)
    initial_store = LocalOpportunityArtifacts(tmp_path, clock=lambda: analyzed_at)
    saved = initial_store.save_analysis(
        company="历史公司",
        role="AI Engineer",
        job_description="历史 JD",
        analysis_report="# 历史分析",
        fit=FitDecision.SUITABLE,
    )
    update_store = LocalOpportunityArtifacts(tmp_path, clock=lambda: applied_at)

    update_store.update_application_state(saved.opportunity_id, ApplicationState.APPLIED)

    week = update_store.status_board(
        period=StatusPeriod.WEEK,
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    overall = update_store.status_board(period=StatusPeriod.ALL)
    assert week.analyzed == 0
    assert overall.analyzed == 1
    assert overall.applied == 1
