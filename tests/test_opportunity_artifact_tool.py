"""Behavior tests for the Opportunity analysis artifact Tool."""

from pathlib import Path

import pytest

from jobagent.artifacts import FitDecision, LocalOpportunityArtifacts
from jobagent.tools.opportunity_artifacts import (
    build_save_job_analysis_tool,
    build_update_application_state_tool,
)


@pytest.mark.asyncio
async def test_agent_tool_saves_jd_and_markdown_analysis_to_local_artifacts(
    tmp_path: Path,
) -> None:
    tool = build_save_job_analysis_tool(LocalOpportunityArtifacts(tmp_path))

    result = await tool.ainvoke(
        {
            "company": "示例科技",
            "role": "AI Agent Engineer",
            "job_description": "负责 Agent 与 RAG 平台。",
            "analysis_report": "# 岗位分析\n\n候选人匹配 Python。",
            "fit": "suitable",
            "application_state": "not_applied",
        }
    )

    assert result["status"] == "completed"
    assert result["analysis_version"] == 1
    assert result["artifact_ref"].startswith("opportunity://local/")
    assert str(tmp_path) not in str(result)
    board = LocalOpportunityArtifacts(tmp_path).status_board()
    assert board.analyzed == 1
    assert board.items[0].company == "示例科技"


@pytest.mark.asyncio
async def test_agent_tool_updates_application_state_only_for_saved_opportunity(
    tmp_path: Path,
) -> None:
    store = LocalOpportunityArtifacts(tmp_path)
    saved = store.save_analysis(
        company="示例科技",
        role="AI Engineer",
        job_description="负责 Agent 平台。",
        analysis_report="# 分析\n\n建议投递。",
        fit=FitDecision.SUITABLE,
    )
    tool = build_update_application_state_tool(store)

    result = await tool.ainvoke(
        {"opportunity_id": saved.opportunity_id, "application_state": "applied"}
    )

    assert result == {
        "status": "completed",
        "opportunity_id": saved.opportunity_id,
        "application_state": "applied",
    }
    assert store.status_board().applied == 1
