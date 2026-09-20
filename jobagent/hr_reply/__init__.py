"""HR auto-reply subsystem: facts, insights, classifier, policy, pipeline.

Implements docs/boss-hr-conversation-auto-reply-design.md (2026-09-20
finalized policy): ~99% auto replies guarded by fact coverage, audit trail,
a global kill switch and an audit-only observation period.
"""

from jobagent.hr_reply.classifier import Classification, HrQuestionClassifier
from jobagent.hr_reply.facts import CandidateFactStore, CompanyInsightStore
from jobagent.hr_reply.orchestrator import HrReplyOrchestrator
from jobagent.hr_reply.policy import PolicyDecision, ReplyPolicyEngine

__all__ = [
    "CandidateFactStore",
    "CompanyInsightStore",
    "Classification",
    "HrQuestionClassifier",
    "PolicyDecision",
    "ReplyPolicyEngine",
    "HrReplyOrchestrator",
]
