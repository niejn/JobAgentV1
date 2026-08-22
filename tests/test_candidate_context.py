"""Tests for JobAgent startup candidate context."""

from pathlib import Path

import pytest

from jobagent.profile import load_candidate_context


def test_loads_search_intent_and_background_as_distinct_models(tmp_path: Path) -> None:
    (tmp_path / "search.yaml").write_text(
        "desired_roles: [Backend Engineer]\npreferred_locations: [Beijing]\n",
        encoding="utf-8",
    )
    (tmp_path / "background.yaml").write_text(
        "name: Julien\nskills: [Python]\nyears_experience: 3\n",
        encoding="utf-8",
    )
    (tmp_path / "agent.yaml").write_text(
        "job_search_profile: search.yaml\ncandidate_background: background.yaml\n",
        encoding="utf-8",
    )

    context = load_candidate_context(tmp_path / "agent.yaml")

    assert context.search_profile.desired_roles == ["Backend Engineer"]
    assert context.background is not None
    assert context.background.skills == ["Python"]
    assert "resume_path" not in context.to_prompt_context()


def test_missing_resume_fails_before_agent_starts(tmp_path: Path) -> None:
    (tmp_path / "search.yaml").write_text(
        "desired_roles: [Backend Engineer]\n",
        encoding="utf-8",
    )
    (tmp_path / "agent.yaml").write_text(
        "job_search_profile: search.yaml\nresume: missing.pdf\n",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="resume file not found"):
        load_candidate_context(tmp_path / "agent.yaml")


def test_resume_text_is_available_to_agent_without_asking_user_to_paste_it(
    tmp_path: Path,
) -> None:
    (tmp_path / "search.yaml").write_text(
        "desired_roles: [AI Engineer]\n",
        encoding="utf-8",
    )
    (tmp_path / "resume.md").write_text(
        "# Julien\n\nBuilt a production RAG platform with FastAPI.\n",
        encoding="utf-8",
    )
    (tmp_path / "agent.yaml").write_text(
        "job_search_profile: search.yaml\nresume: resume.md\n",
        encoding="utf-8",
    )

    context = load_candidate_context(tmp_path / "agent.yaml")

    prompt_context = context.to_prompt_context()
    assert "Built a production RAG platform with FastAPI." in prompt_context
    assert str(tmp_path) not in prompt_context
