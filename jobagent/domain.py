"""Core domain models used across scraping, matching, and applying."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, model_validator


class JobSource(StrEnum):
    """Supported job sources."""

    BOSS = "boss"
    LINKEDIN = "linkedin"
    LAGOU = "lagou"


class ApplicationStatus(StrEnum):
    """Application lifecycle states."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    FAILED = "failed"
    CAPTCHA_BLOCKED = "captcha_blocked"


class AuthorAuthenticity(StrEnum):
    """Authenticity of a referral post author (XHS anti-scam, see docs 4.5.2)."""

    EMPLOYEE = "employee"        # real employee of the company
    RECRUITER = "recruiter"      # official recruiter / HR
    BUSINESS = "business"        # referral-business / agency account
    UNKNOWN = "unknown"          # not enough signals


class ReferralStatus(StrEnum):
    """Lifecycle states of a referral request."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    SENT = "sent"
    SKIPPED = "skipped"
    REPLIED = "replied"


class SalaryRange(BaseModel):
    """Represents a salary range with currency."""

    min_annual: int | None = Field(default=None, ge=0)
    max_annual: int | None = Field(default=None, ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_range(self) -> SalaryRange:
        """Ensure max salary is not lower than min salary."""

        if self.min_annual is not None and self.max_annual is not None:
            if self.max_annual < self.min_annual:
                raise ValueError("max_annual must be greater than or equal to min_annual")
        return self


class Job(BaseModel):
    """A normalized job listing from any source platform."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    source: JobSource
    title: str
    company: str
    location: str
    url: HttpUrl
    description: str
    salary: SalaryRange | None = None
    tags: list[str] = Field(default_factory=list)
    posted_at: datetime | None = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class Profile(BaseModel):
    """Candidate profile loaded from local config files."""

    name: str
    email: str | None = None
    years_experience: float = Field(default=0.0, ge=0.0)
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    desired_roles: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    salary_expectation: SalaryRange | None = None
    remote_ok: bool = True
    links: dict[str, HttpUrl] = Field(default_factory=dict)


class Match(BaseModel):
    """Match result between a profile and a job listing."""

    job_id: str
    score: float = Field(ge=0.0, le=1.0)
    reasoning: list[str] = Field(default_factory=list)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    evaluation_failed: bool = Field(
        default=False,
        description=(
            "True when the evaluation itself errored (network, malformed "
            "model output). score=0.0 here means 'unknown', not 'no match'."
        ),
    )
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))



class Application(BaseModel):
    """Tracks application submission and follow-up details."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    job_id: str
    source: JobSource
    status: ApplicationStatus = ApplicationStatus.DRAFT
    message: str = ""
    applied_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    extra: dict[str, Any] = Field(default_factory=dict)
    # Common extra keys:
    #   "reason": str — failure/skip reason (e.g. "captcha", "already_applied",
    #                    "daily_limit", "button_not_found", "inactive_hr")
    #   "greeting_sent": str — the actual greeting message sent
    #   "hr_name": str — recruiter name if available
    #   "response_time": float — seconds taken for the apply action


# ---------------------------------------------------------------------------
# Referral (Xiaohongshu) models
# ---------------------------------------------------------------------------


class ReferralPost(BaseModel):
    """A Xiaohongshu note that may contain a referral offer."""

    id: str  # XHS note id (deterministic, used for dedup)
    url: str = ""  # full URL incl. xsec_token (may be too long for HttpUrl)
    author_name: str = ""
    author_id: str = ""  # XHS user id, used to locate the author profile
    title: str = ""
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    likes: int = 0
    comments_count: int = 0
    published_at: datetime | None = None
    ip_location: str | None = None  # e.g. "北京" (signal S6)
    top_comments: list[str] = Field(default_factory=list)
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuthorProfile(BaseModel):
    """Public info about a referral post author (signal source for 4.5.2)."""

    author_id: str
    nickname: str = ""
    description: str = ""  # bio text (marketing-word detection, signal S3)
    ip_location: str | None = None
    recent_note_titles: list[str] = Field(default_factory=list)  # signals S1/S2
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReferralOffer(BaseModel):
    """Structured referral offer extracted from a ReferralPost by the LLM."""

    post_id: str
    company: str  # normalized company name
    company_raw: str = ""  # company mention as written in the post
    departments: list[str] = Field(default_factory=list)
    role_types: list[str] = Field(default_factory=list)  # 社招/实习/校招
    is_active: bool = True
    posted_at: datetime | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    scam_risk: float = Field(default=0.0, ge=0.0, le=1.0)  # post-level, 4.5.1
    authenticity: AuthorAuthenticity = AuthorAuthenticity.UNKNOWN  # 4.5.2
    authenticity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    red_flags: list[str] = Field(default_factory=list)  # evidence for CP-1 review
    companies_promoted: list[str] = Field(default_factory=list)  # signal S1
    apply_link: str | None = None
    stale_days: int = 0
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReferralRequest(BaseModel):
    """A drafted outreach to a referral author (comment hook or DM)."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    post_id: str
    offer_id: str = ""  # ReferralOffer.post_id (same as post_id, kept for clarity)
    company: str = ""
    job_id: str | None = None  # cross-referenced Job.id (may be None)
    match_score: float | None = Field(default=None, ge=0.0, le=1.0)
    message_draft: str = ""
    status: ReferralStatus = ReferralStatus.DRAFT
    contact_mode: Literal["comment", "dm_review", "auto_dm"] = "comment"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    extra: dict[str, Any] = Field(default_factory=dict)
