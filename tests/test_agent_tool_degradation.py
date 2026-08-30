"""Graceful tool degradation (Hermes _discover_tools pattern, distilled).

The agent's capability set is assembled from a declarative (name -> builder)
table via _build_optional_tool:
- ImportError (missing optional dependency) -> skip that ONE tool with a
  warning; the agent still builds with its remaining capabilities.
- Any other exception -> propagate loudly (real bugs must not silently
  shrink the capability set).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import FakeListChatModel
from langchain_core.tools import BaseTool, StructuredTool

from jobagent.agent import _build_optional_tool, build_job_agent
from jobagent.config import Settings

#: Full default capability set - regression guard for the declarative table.
DEFAULT_TOOL_NAMES = {
    "discover_boss_jobs",
    "boss_greet_jobs",
    "upload_boss_resume_pdf",
    "list_boss_greetings",
    "read_boss_conversation",
    "reply_boss_greeting",
    "save_user_fact",
    "search_history",
    "update_job_progress",
    "get_job_progress",
    "list_job_records",
    "find_job_merge_candidates",
    "merge_job_identities",
    "send_application_email",
    "list_recent_emails",
    "read_email",
    "import_candidate_resume",
    "save_candidate_background",
    "save_job_search_profile",
    "save_job_analysis",
    "update_job_application_state",
    "discover_interview_evidence",
    "save_shared_url",
    "extract_shared_url",
    "browse_xhs_author_posts",
    "search_xhs_notes",
    "read_job_description",
    "read_user_document",
}


class _ToolBindableFakeModel(FakeListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
    )


# ---- _build_optional_tool unit semantics -------------------------------------


def test_builder_success_returns_tool() -> None:
    def _ok() -> str:
        """Return ok."""
        return "ok"

    tool = StructuredTool.from_function(func=_ok, name="ok_tool")
    assert _build_optional_tool("ok_tool", lambda: tool) is tool


def test_import_error_degrades_to_none_with_warning(caplog: Any) -> None:
    def broken() -> BaseTool:
        raise ImportError("No module named 'playwright'")

    with caplog.at_level(logging.WARNING, logger="jobagent.agent"):
        result = _build_optional_tool("needs_playwright", broken)

    assert result is None
    degraded = [r for r in caplog.records if r.getMessage() == "jobagent.tool_degraded"]
    assert degraded, "expected a tool_degraded warning"
    assert "needs_playwright" in str(degraded[0].__dict__["tool"])
    assert "playwright" in degraded[0].__dict__["reason"]


def test_real_bugs_propagate_loudly() -> None:
    def buggy() -> BaseTool:
        raise TypeError("builder bug must not be swallowed")

    with pytest.raises(TypeError, match="builder bug"):
        _build_optional_tool("buggy_tool", buggy)


# ---- integration through build_job_agent -------------------------------------


@pytest.mark.asyncio
async def test_default_capability_set_unchanged(tmp_path: Path) -> None:
    """All 16 default tools still register after the declarative refactor."""

    model = _ToolBindableFakeModel(responses=["好的"])
    agent = build_job_agent(_settings(tmp_path), model=model)
    try:
        names = {tool.name for tool in agent._tools}
    finally:
        await agent.close()
    assert names == DEFAULT_TOOL_NAMES


@pytest.mark.asyncio
async def test_one_degraded_tool_does_not_kill_the_rest(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """A builder whose optional deps are missing is skipped; siblings register."""

    def broken(*args: Any, **kwargs: Any) -> BaseTool:
        raise ImportError("No module named 'boss_cdp_extra'")

    monkeypatch.setattr("jobagent.agent.build_boss_greet_jobs_tool", broken)
    model = _ToolBindableFakeModel(responses=["好的"])
    with caplog.at_level(logging.WARNING, logger="jobagent.agent"):
        agent = build_job_agent(_settings(tmp_path), model=model)
    try:
        names = {tool.name for tool in agent._tools}
    finally:
        await agent.close()

    assert "boss_greet_jobs" not in names
    assert "discover_boss_jobs" in names
    assert "update_job_progress" in names
    assert len(names) == len(DEFAULT_TOOL_NAMES) - 1
    assert any(r.getMessage() == "jobagent.tool_degraded" for r in caplog.records)


@pytest.mark.asyncio
async def test_builder_bug_crashes_build_loudly(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Non-ImportError builder failures propagate - no silent capability loss."""

    def buggy(*args: Any, **kwargs: Any) -> BaseTool:
        raise ValueError("misconfigured builder")

    monkeypatch.setattr("jobagent.agent.build_boss_greet_jobs_tool", buggy)
    model = _ToolBindableFakeModel(responses=["好的"])
    with pytest.raises(ValueError, match="misconfigured builder"):
        build_job_agent(_settings(tmp_path), model=model)
