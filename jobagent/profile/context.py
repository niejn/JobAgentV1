"""Load the candidate context supplied when JobAgent starts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from jobagent.models import SalaryRange


class JobSearchProfile(BaseModel):
    """Global job-search intentions and constraints, not resume facts."""

    desired_roles: list[str] = Field(min_length=1)
    preferred_locations: list[str] = Field(default_factory=list)
    salary_expectation: SalaryRange | None = None
    remote_ok: bool = True
    prefer_online_interview: bool = Field(
        default=True,
        description=(
            "True = prefer online/video interviews; "
            "False = willing to commute for on-site interviews"
        ),
    )
    home_address: str | None = Field(
        default=None,
        description=(
            "User's home/residential address or district "
            "(e.g. '上海浦东新区张江'), used to filter nearby jobs "
            "and estimate commute"
        ),
    )
    preferred_industries: list[str] = Field(default_factory=list)
    preferred_company_sizes: list[str] = Field(default_factory=list)
    preferred_company_traits: list[str] = Field(default_factory=list)
    preferred_job_traits: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class CandidateBackground(BaseModel):
    """Confirmed experience and skills, usually derived from a resume."""

    name: str
    email: str | None = None
    years_experience: float = Field(default=0.0, ge=0.0)
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    experiences: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)


class AgentStartupConfig(BaseModel):
    """References to candidate-owned input files loaded at Agent startup."""

    job_search_profile: Path
    resume: Path | None = None
    candidate_background: Path | None = None


@dataclass(frozen=True, slots=True)
class CandidateContext:
    """Validated context available to the conversational Agent."""

    search_profile: JobSearchProfile | None
    resume_path: Path | None
    background: CandidateBackground | None
    resume_text: str | None

    def to_prompt_context(self) -> str:
        """Serialize user-supplied facts and bounded resume text, never raw paths."""

        payload = {
            "job_search_profile": (
                self.search_profile.model_dump(mode="json")
                if self.search_profile
                else None
            ),
            "candidate_background": (
                self.background.model_dump(mode="json") if self.background else None
            ),
            "resume_available": self.resume_path is not None,
            "resume_text": self.resume_text,
        }
        return json.dumps(payload, ensure_ascii=False)


def load_candidate_context(config_path: Path) -> CandidateContext:
    """Load startup references relative to the startup YAML file."""

    resolved_config = config_path.expanduser().resolve()
    startup = AgentStartupConfig.model_validate(_read_mapping(resolved_config))
    root = resolved_config.parent
    search_path = _resolve_required(root, startup.job_search_profile, "job_search_profile")
    search_profile = JobSearchProfile.model_validate(_read_mapping(search_path))
    resume_path = _resolve_optional(root, startup.resume, "resume")
    background_path = _resolve_optional(
        root,
        startup.candidate_background,
        "candidate_background",
    )
    background = (
        CandidateBackground.model_validate(_read_mapping(background_path))
        if background_path
        else None
    )
    resume_text = _read_resume_text(resume_path) if resume_path else None
    return CandidateContext(search_profile, resume_path, background, resume_text)


def _read_resume_text(path: Path, *, max_bytes: int = 100_000) -> str | None:
    """Read a configured UTF-8 text resume; binary resume parsing remains out of scope."""

    if path.suffix.lower() not in {".txt", ".md"}:
        return None
    if path.stat().st_size > max_bytes:
        raise ValueError(f"resume file exceeds {max_bytes} byte limit")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ValueError("resume text file must use UTF-8 encoding") from exc


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return payload


def _resolve_required(root: Path, path: Path, field: str) -> Path:
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{field} file not found: {resolved}")
    return resolved


def _resolve_optional(root: Path, path: Path | None, field: str) -> Path | None:
    return _resolve_required(root, path, field) if path is not None else None
