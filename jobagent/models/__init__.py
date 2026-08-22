"""LLM abstraction layer + re-exported domain models for backward compat."""

# --- LLM layer -----------------------------------------------------------
# --- Domain models (moved to jobagent.domain, re-exported here) -----------
from jobagent.domain import (
    Application,
    ApplicationStatus,
    AuthorAuthenticity,
    AuthorProfile,
    Job,
    JobSource,
    Match,
    Profile,
    ReferralOffer,
    ReferralPost,
    ReferralRequest,
    ReferralStatus,
    SalaryRange,
)
from jobagent.models.claude_api import ClaudeClient
from jobagent.models.streaming import (
    StreamContext,
    StreamOptions,
    UnifiedStreamer,
    is_oauth_token,
)

__all__ = [
    # LLM
    "ClaudeClient",
    "StreamContext",
    "StreamOptions",
    "UnifiedStreamer",
    "is_oauth_token",
    # Domain
    "Application",
    "ApplicationStatus",
    "AuthorAuthenticity",
    "AuthorProfile",
    "Job",
    "JobSource",
    "Match",
    "Profile",
    "ReferralOffer",
    "ReferralPost",
    "ReferralRequest",
    "ReferralStatus",
    "SalaryRange",
]
