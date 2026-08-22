"""Global candidate profile module and its LangChain Tool adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.profile import (
    CandidateBackground,
    JobSearchProfile,
    SQLiteCandidateProfileStore,
)
from jobagent.tools.job_description import UserDocumentReader


class CandidateResumeFile(BaseModel):
    file_path: str = Field(
        min_length=1,
        description="User-named UTF-8 .txt or .md resume inside the configured workspace",
    )


class ConfirmedCandidateBackground(BaseModel):
    resume_version_id: str = Field(min_length=1)
    background: CandidateBackground
    user_confirmed: bool = Field(
        description="True only after the user explicitly confirms the extracted facts"
    )


class ConfirmedJobSearchProfile(BaseModel):
    profile: JobSearchProfile
    user_confirmed: bool = Field(
        description="True only when the user explicitly supplied or confirmed the intentions"
    )


class CandidateProfileManager:
    """Import candidate-owned documents without exposing storage or filesystem details."""

    def __init__(self, *, workspace_root: Path, database: Path) -> None:
        self._reader = UserDocumentReader(workspace_root)
        self._database = database

    def import_resume(self, file_path: str) -> dict[str, Any]:
        """Read and persist one explicitly selected resume version."""

        document = self._reader.read(file_path)
        if document["status"] != "completed":
            return document
        with SQLiteCandidateProfileStore(self._database) as store:
            resume = store.import_resume(
                source_name=str(document["source_name"]),
                content=str(document["content"]),
            )
        return {
            "status": "completed",
            "source_name": resume.source_name,
            "resume_version_id": resume.id,
            "resume_version": resume.version,
            "content": resume.content,
            "characters": len(resume.content),
            "next_action": (
                "Extract Candidate Background facts, present them to the user, and only "
                "persist them after explicit confirmation."
            ),
        }

    def save_confirmed_background(
        self,
        *,
        resume_version_id: str,
        background: CandidateBackground,
        user_confirmed: bool,
    ) -> dict[str, Any]:
        """Persist structured facts only after explicit user confirmation."""

        if not user_confirmed:
            return {
                "status": "waiting_user_confirmation",
                "message": "Candidate Background was not saved without explicit confirmation.",
            }
        try:
            with SQLiteCandidateProfileStore(self._database) as store:
                saved = store.save_confirmed_background(
                    resume_version_id=resume_version_id,
                    background=background,
                )
        except KeyError:
            return {
                "status": "failed",
                "error_type": "invalid_resume_version",
                "message": "The referenced resume version does not exist.",
            }
        return {
            "status": "completed",
            "background_version_id": saved.id,
            "background_version": saved.version,
            "resume_version_id": saved.resume_version_id,
        }

    def save_job_search_profile(
        self,
        *,
        profile: JobSearchProfile,
        user_confirmed: bool,
    ) -> dict[str, Any]:
        """Persist explicitly supplied or confirmed global job-search intentions."""

        if not user_confirmed:
            return {
                "status": "waiting_user_confirmation",
                "message": "Job Search Profile was not saved without user confirmation.",
            }
        with SQLiteCandidateProfileStore(self._database) as store:
            saved = store.save_job_search_profile(profile)
        return {
            "status": "completed",
            "search_profile_version_id": saved.id,
            "search_profile_version": saved.version,
        }


def build_import_candidate_resume_tool(manager: CandidateProfileManager) -> BaseTool:
    """Expose safe resume import as one high-level Agent Tool."""

    async def import_candidate_resume(file_path: str) -> dict[str, Any]:
        """Import a user-selected text resume into the global candidate profile.

        Call only when the user explicitly names the resume file. After import, extract
        facts from the returned content, show them to the user, and request confirmation.
        """

        return manager.import_resume(file_path)

    return StructuredTool.from_function(
        coroutine=import_candidate_resume,
        name="import_candidate_resume",
        description=(
            "Read an explicitly user-named UTF-8 .txt or .md resume from the configured "
            "workspace, deduplicate it by content, and persist it as the global base-resume "
            "version."
        ),
        args_schema=CandidateResumeFile,
    )


def build_save_candidate_background_tool(manager: CandidateProfileManager) -> BaseTool:
    """Expose confirmed Candidate Background persistence to the Agent."""

    async def save_candidate_background(
        resume_version_id: str,
        background: CandidateBackground,
        user_confirmed: bool,
    ) -> dict[str, Any]:
        """Save resume-derived candidate facts after explicit user confirmation.

        Never call in the same turn that facts are first extracted or presented. Call only
        after a later user message explicitly confirms the proposed Candidate Background.
        """

        return manager.save_confirmed_background(
            resume_version_id=resume_version_id,
            background=background,
            user_confirmed=user_confirmed,
        )

    return StructuredTool.from_function(
        coroutine=save_candidate_background,
        name="save_candidate_background",
        description=(
            "Persist a resume-derived Candidate Background in SQLite only after the user "
            "explicitly confirms the extracted facts."
        ),
        args_schema=ConfirmedCandidateBackground,
    )


def build_save_job_search_profile_tool(manager: CandidateProfileManager) -> BaseTool:
    """Expose versioned Job Search Profile persistence to the Agent."""

    async def save_job_search_profile(
        profile: JobSearchProfile,
        user_confirmed: bool,
    ) -> dict[str, Any]:
        """Save user-supplied roles, locations, salary, company/job traits, and constraints.

        Call when the user explicitly states these intentions or confirms a summary. Never
        infer preferences from a resume or from one target JD.
        """

        return manager.save_job_search_profile(
            profile=profile,
            user_confirmed=user_confirmed,
        )

    return StructuredTool.from_function(
        coroutine=save_job_search_profile,
        name="save_job_search_profile",
        description=(
            "Version the user's explicitly supplied global desired roles, locations, salary "
            "expectations, company/job traits, industries, and constraints in SQLite."
        ),
        args_schema=ConfirmedJobSearchProfile,
    )
