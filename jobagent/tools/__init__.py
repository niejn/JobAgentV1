"""Business tools available to the conversational JobAgent."""

from jobagent.tools.boss_chat import (
    build_boss_chat_history_tool,
    build_boss_chat_list_tool,
)
from jobagent.tools.boss_chat_send import build_boss_chat_reply_tool
from jobagent.tools.boss_greet import (
    BossGreetingsManager,
    build_boss_greet_jobs_tool,
)
from jobagent.tools.boss_resume import build_boss_resume_upload_tool
from jobagent.tools.candidate_profile import (
    CandidateProfileManager,
    CandidateResumeFile,
    ConfirmedCandidateBackground,
    ConfirmedJobSearchProfile,
    build_import_candidate_resume_tool,
    build_save_candidate_background_tool,
    build_save_job_search_profile_tool,
)
from jobagent.tools.interview_evidence import (
    InterviewEvidenceDiscovery,
    InterviewEvidenceTarget,
    build_interview_evidence_tool,
)
from jobagent.tools.job_description import (
    JobDescriptionFile,
    JobDescriptionReader,
    UserDocumentFile,
    UserDocumentReader,
    build_job_description_tool,
    build_user_document_tool,
)
from jobagent.tools.job_discovery import BossJobDiscovery, build_boss_job_discovery_tool
from jobagent.tools.job_progress import (
    build_get_job_progress_tool,
    build_list_job_records_tool,
    build_update_job_progress_tool,
)
from jobagent.tools.opportunity_artifacts import (
    ApplicationStateUpdateInput,
    JobAnalysisArtifactInput,
    build_save_job_analysis_tool,
    build_update_application_state_tool,
)
from jobagent.tools.shared_url import (
    SharedUrlSaver,
    SharedUrlSaveRequest,
    build_shared_url_extract_tool,
    build_shared_url_save_tool,
)
from jobagent.tools.xhs_author import (
    XhsAuthorPostsBrowser,
    XhsAuthorPostsRequest,
    build_xhs_author_posts_tool,
)

__all__ = [
    "BossGreetingsManager",
    "build_boss_greet_jobs_tool",
    "build_boss_resume_upload_tool",
    "build_boss_chat_history_tool",
    "build_boss_chat_list_tool",
    "build_boss_chat_reply_tool",
    "CandidateProfileManager",
    "BossJobDiscovery",
    "CandidateResumeFile",
    "ConfirmedCandidateBackground",
    "ConfirmedJobSearchProfile",
    "build_import_candidate_resume_tool",
    "build_save_candidate_background_tool",
    "build_save_job_search_profile_tool",
    "InterviewEvidenceDiscovery",
    "InterviewEvidenceTarget",
    "build_interview_evidence_tool",
    "build_boss_job_discovery_tool",
    "build_get_job_progress_tool",
    "build_list_job_records_tool",
    "build_update_job_progress_tool",
    "JobDescriptionFile",
    "JobDescriptionReader",
    "build_job_description_tool",
    "UserDocumentFile",
    "UserDocumentReader",
    "build_user_document_tool",
    "JobAnalysisArtifactInput",
    "ApplicationStateUpdateInput",
    "build_save_job_analysis_tool",
    "build_update_application_state_tool",
    "SharedUrlSaveRequest",
    "SharedUrlSaver",
    "build_shared_url_save_tool",
    "build_shared_url_extract_tool",
    "XhsAuthorPostsBrowser",
    "XhsAuthorPostsRequest",
    "build_xhs_author_posts_tool",
]
