"""Behavior tests for Candidate Profile business Tools."""

from pathlib import Path

import pytest

from jobagent.profile import SQLiteCandidateProfileStore
from jobagent.tools.candidate_profile import (
    CandidateProfileManager,
    build_import_candidate_resume_tool,
    build_save_candidate_background_tool,
    build_save_job_search_profile_tool,
)


@pytest.mark.asyncio
async def test_agent_tool_imports_user_named_resume_into_global_profile_store(
    tmp_path: Path,
) -> None:
    (tmp_path / "resume-v5-四大项目版.md").write_text(
        "# Julien\n\nBuilt a production Agent platform.",
        encoding="utf-8",
    )
    database = tmp_path / "jobagent.db"
    tool = build_import_candidate_resume_tool(
        CandidateProfileManager(workspace_root=tmp_path, database=database)
    )

    result = await tool.ainvoke({"file_path": "resume-v5-四大项目版.md"})

    assert result["status"] == "completed"
    assert result["resume_version"] == 1
    assert "production Agent platform" in result["content"]
    assert str(tmp_path) not in str(result)
    with SQLiteCandidateProfileStore(database) as store:
        restored = store.load_context()
    assert restored is not None
    assert restored.resume_text == result["content"]


@pytest.mark.asyncio
async def test_agent_tool_persists_background_only_with_user_confirmation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobagent.db"
    manager = CandidateProfileManager(workspace_root=tmp_path, database=database)
    with SQLiteCandidateProfileStore(database) as store:
        resume = store.import_resume(source_name="resume.md", content="# Julien\nAI Agent")
    tool = build_save_candidate_background_tool(manager)

    result = await tool.ainvoke(
        {
            "resume_version_id": resume.id,
            "background": {
                "name": "Julien",
                "years_experience": 5,
                "skills": ["Python", "LangGraph"],
            },
            "user_confirmed": True,
        }
    )

    assert result["status"] == "completed"
    assert result["background_version"] == 1
    with SQLiteCandidateProfileStore(database) as store:
        restored = store.load_context()
    assert restored is not None
    assert restored.background is not None
    assert restored.background.skills == ["Python", "LangGraph"]


@pytest.mark.asyncio
async def test_agent_tool_saves_explicit_job_search_intentions_without_yaml(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobagent.db"
    manager = CandidateProfileManager(workspace_root=tmp_path, database=database)
    tool = build_save_job_search_profile_tool(manager)

    result = await tool.ainvoke(
        {
            "profile": {
                "desired_roles": ["AI Agent Engineer"],
                "preferred_locations": ["Shanghai"],
                "constraints": ["No 996"],
            },
            "user_confirmed": True,
        }
    )

    assert result["status"] == "completed"
    assert result["search_profile_version"] == 1
    with SQLiteCandidateProfileStore(database) as store:
        restored = store.load_context()
    assert restored is not None
    assert restored.search_profile is not None
    assert restored.search_profile.desired_roles == ["AI Agent Engineer"]
