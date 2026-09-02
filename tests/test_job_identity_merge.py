"""Tests for L3 identity merge tools (F1-R4 JI-3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.journey.job_registry import JobProgressStatus, SQLiteJobRegistry
from jobagent.tools.job_identity_merge import (
    build_find_merge_candidates_tool,
    build_merge_job_identities_tool,
)


def _setup(db: Path) -> None:
    with SQLiteJobRegistry(db) as registry:
        # Same company, two identities: one carries the agent direction,
        # the other only says backend - L2 refuses, L3 may merge.
        registry.upsert_discovered(
            job_id="boss:a1", source="boss", company="小而美科技",
            title="AI Agent 工程师",
        )
        registry.mark("boss:a1", JobProgressStatus.GREETED)
        registry.upsert_discovered(
            job_id="email:xyz", source="xhs_email", company="小而美科技",
            title="后端工程师（Agent 方向）",
        )


@pytest.mark.asyncio
async def test_find_candidates_same_company(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    _setup(db)
    tool = build_find_merge_candidates_tool(db)

    result = await tool.ainvoke({"company": "小而美科技有限公司", "title": "后端工程师"})

    assert result["status"] == "ok"
    assert result["count"] >= 1
    keys = {c["identity_key"] for c in result["candidates"]}
    assert any("agent" in key for key in keys)


@pytest.mark.asyncio
async def test_registry_merge_refuses_without_confirmation(tmp_path: Path) -> None:
    """Library guard: SQLiteJobRegistry.merge_identities still refuses
    without user_confirmed - the HITL middleware approves the tool call,
    then the tool passes user_confirmed=True on its behalf."""
    db = tmp_path / "registry.db"
    _setup(db)
    with SQLiteJobRegistry(db) as registry:
        result = registry.merge_identities(
            "idn:小而美|后端工程师|agent",
            "idn:小而美|aiagent工程师|agent",
            rationale="同岗位：同一招聘帖的两个平台来源",
        )

    assert result["status"] == "waiting_user_confirmation"
    assert "hint" in result


@pytest.mark.asyncio
async def test_merge_tool_runs_without_confirmation_parameter(tmp_path: Path) -> None:
    """The tool schema has no user_confirmed flag; middleware approval is
    the gate and the tool self-attests user_confirmed=True to the registry."""
    db = tmp_path / "registry.db"
    _setup(db)
    tool = build_merge_job_identities_tool(db)

    result = await tool.ainvoke({
        "source_key": "idn:小而美|后端工程师|agent",
        "target_key": "idn:小而美|aiagent工程师|agent",
        "rationale": "同岗位：同一招聘帖的两个平台来源",
    })

    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_merge_confirmed_inherits_deeper_status(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    _setup(db)
    tool = build_merge_job_identities_tool(db)

    result = await tool.ainvoke({
        "source_key": "idn:小而美|后端工程师|agent",
        "target_key": "idn:小而美|aiagent工程师|agent",
        "rationale": "同岗位",
    })

    assert result["status"] == "ok"
    assert result["merged_status"] == "greeted"  # deeper side wins

    with SQLiteJobRegistry(db) as registry:
        both = registry.list_records()
        assert {r.status for r in both} == {JobProgressStatus.GREETED}
        # The source identity row is gone; one identity remains.
        identities = registry._connection.execute(
            "SELECT identity_key FROM job_identity"
        ).fetchall()
        assert len(identities) == 1
