"""Behavior tests for the global SQLite candidate profile module."""

from pathlib import Path

from jobagent.models import SalaryRange
from jobagent.profile import (
    CandidateBackground,
    CandidateContext,
    JobSearchProfile,
    SQLiteCandidateContextProvider,
    SQLiteCandidateProfileStore,
)


def test_resume_import_is_versioned_deduplicated_and_restored(tmp_path: Path) -> None:
    database = tmp_path / "jobagent.db"
    with SQLiteCandidateProfileStore(database) as store:
        first = store.import_resume(
            source_name="resume.md",
            content="# Julien\nBuilt a production RAG platform.",
        )
        duplicate = store.import_resume(
            source_name="renamed-resume.md",
            content="# Julien\nBuilt a production RAG platform.",
        )
        second = store.import_resume(
            source_name="resume-v2.md",
            content="# Julien\nBuilt a production Agent and RAG platform.",
        )

    assert first.version == 1
    assert duplicate.id == first.id
    assert second.version == 2

    with SQLiteCandidateProfileStore(database) as reopened:
        context = reopened.load_context()

    assert context is not None
    assert context.resume_path is None
    assert "production Agent and RAG platform" in (context.resume_text or "")
    assert context.search_profile is None
    assert context.background is None


def test_confirmed_candidate_background_is_restored_across_sessions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobagent.db"
    background = CandidateBackground(
        name="Julien",
        years_experience=5,
        summary="AI Agent engineer",
        skills=["Python", "LangGraph"],
        projects=[{"name": "JobAgent", "result": "Built adaptive research loop"}],
    )
    with SQLiteCandidateProfileStore(database) as store:
        resume = store.import_resume(source_name="resume.md", content="# Julien\nAI Agent")
        saved = store.save_confirmed_background(
            resume_version_id=resume.id,
            background=background,
        )

    assert saved.version == 1
    assert saved.background == background

    with SQLiteCandidateProfileStore(database) as reopened:
        context = reopened.load_context()

    assert context is not None
    assert context.background == background


def test_job_search_profile_is_versioned_and_restored_without_yaml(tmp_path: Path) -> None:
    database = tmp_path / "jobagent.db"
    profile = JobSearchProfile(
        desired_roles=["AI Agent Engineer"],
        preferred_locations=["Shanghai"],
        salary_expectation=SalaryRange(
            min_annual=300_000,
            max_annual=420_000,
            currency="CNY",
        ),
        preferred_company_sizes=["growth", "large"],
        preferred_company_traits=["foreign company", "engineering culture"],
        preferred_job_traits=["AI Agent platform", "individual contributor"],
        constraints=["No 996"],
    )
    with SQLiteCandidateProfileStore(database) as store:
        saved = store.save_job_search_profile(profile)

    assert saved.version == 1
    with SQLiteCandidateProfileStore(database) as reopened:
        context = reopened.load_context()

    assert context is not None
    assert context.search_profile == profile
    assert context.resume_text is None


def test_yaml_candidate_context_can_be_imported_once_then_restored_from_sqlite(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobagent.db"
    imported = CandidateContext(
        search_profile=JobSearchProfile(desired_roles=["Backend Engineer"]),
        resume_path=tmp_path / "resume.md",
        background=None,
        resume_text="# Julien\nBackend and Agent engineering",
    )
    with SQLiteCandidateProfileStore(database) as store:
        store.import_context(imported)

    with SQLiteCandidateProfileStore(database) as reopened:
        restored = reopened.load_context()

    assert restored is not None
    assert restored.search_profile == imported.search_profile
    assert restored.resume_text == imported.resume_text


def test_confirmed_yaml_background_can_be_imported_without_resume(tmp_path: Path) -> None:
    database = tmp_path / "jobagent.db"
    background = CandidateBackground(name="Julien", skills=["Python"])
    imported = CandidateContext(
        search_profile=JobSearchProfile(desired_roles=["Backend Engineer"]),
        resume_path=None,
        background=background,
        resume_text=None,
    )
    with SQLiteCandidateProfileStore(database) as store:
        store.import_context(imported)
        restored = store.load_context()

    assert restored is not None
    assert restored.background == background


def test_sqlite_context_provider_observes_profile_updates_without_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobagent.db"
    provider = SQLiteCandidateContextProvider(database)

    assert provider.load() is None
    with SQLiteCandidateProfileStore(database) as store:
        store.import_resume(source_name="resume.md", content="# Julien\nAI Agent")

    refreshed = provider.load()
    assert refreshed is not None
    assert refreshed.resume_text == "# Julien\nAI Agent"
