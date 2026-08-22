"""Interview-evidence discovery module and its LangChain Tool adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.config import Settings
from jobagent.interview.intelligence import LLMInterviewIntelligence
from jobagent.interview.ocr import TesseractOcrExtractor
from jobagent.interview.research import InterviewResearchService, InterviewResearchTarget
from jobagent.interview.snapshot import SnapshotMaterializer
from jobagent.interview.source_corpus import SQLiteSourceCorpus
from jobagent.journey import SQLiteJourneyStore
from jobagent.models.llm_client import build_chat_client
from jobagent.profile import SQLiteCandidateContextProvider
from jobagent.profile.context import CandidateContext
from jobagent.scraper.xhs_backend import SpiderXhsBackend


class InterviewEvidenceTarget(BaseModel):
    """Business input understood by the Agent; collection controls stay internal."""

    company: str = Field(min_length=1, description="Target company or product brand")
    role: str = Field(min_length=1, description="Target job title or role family")
    city: str | None = Field(default=None, description="Job city when known")
    job_description: str | None = Field(
        default=None,
        description="The target job description when available",
    )


class InterviewEvidenceDiscovery:
    """Deep module hiding XHS search, filtering, persistence, and source tokens."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend_factory: Callable[[Settings], Any] = SpiderXhsBackend,
        candidate_context: CandidateContext | None = None,
    ) -> None:
        self._settings = settings
        self._backend_factory = backend_factory
        self._candidate_context = candidate_context
        self._candidate_context_provider = SQLiteCandidateContextProvider(
            settings.jobagent_state_db
        )

    async def discover(self, target: InterviewEvidenceTarget) -> dict[str, Any]:
        """Return safe evidence metadata; raw bodies and images remain on disk."""

        if self._settings.jobagent_ocr_engine.strip().lower() != "tesseract":
            raise ValueError("First release supports JOBAGENT_OCR_ENGINE=tesseract only")
        resources = ExitStack()
        try:
            store = resources.enter_context(
                SQLiteJourneyStore(self._settings.jobagent_state_db)
            )
            source_corpus = resources.enter_context(
                SQLiteSourceCorpus(self._settings.jobagent_state_db)
            )
            source_corpus.backfill_manifests(self._settings.jobagent_artifact_dir)
            async with self._backend_factory(self._settings) as backend:
                service = InterviewResearchService(
                    backend=backend,
                    intelligence=LLMInterviewIntelligence(build_chat_client(self._settings)),
                    snapshot_materializer=SnapshotMaterializer(
                        TesseractOcrExtractor(
                            command=self._settings.tesseract_cmd,
                            language=self._settings.jobagent_ocr_language,
                            page_segmentation_mode=self._settings.jobagent_ocr_psm,
                        )
                    ),
                    store=store,
                    source_corpus=source_corpus,
                    artifact_root=self._settings.jobagent_artifact_dir,
                    max_iterations=self._settings.jobagent_research_max_iterations,
                    queries_per_iteration=(
                        self._settings.jobagent_research_queries_per_iteration
                    ),
                    results_per_query=self._settings.jobagent_research_results_per_query,
                    minimum_evidence=self._settings.jobagent_research_minimum_evidence,
                    required_topics=self._settings.jobagent_research_required_topics,
                    stale_days=self._settings.xhs_referral_stale_days,
                )
                try:
                    latest_candidate_context = (
                        self._candidate_context_provider.load()
                        or self._candidate_context
                    )
                    async with asyncio.timeout(self._settings.jobagent_research_timeout):
                        outcome = await service.run(
                            InterviewResearchTarget(
                                company=target.company,
                                role=target.role,
                                city=target.city,
                                job_description=(
                                    target.job_description
                                    or f"{target.company} {target.role}"
                                ),
                            ),
                            candidate_context=(
                                latest_candidate_context.to_prompt_context()
                                if latest_candidate_context
                                else None
                            ),
                        )
                except TimeoutError as exc:
                    raise TimeoutError(
                        "Interview research exceeded "
                        f"{self._settings.jobagent_research_timeout}s total timeout"
                    ) from exc
        finally:
            resources.close()

        return {
            "status": outcome.status,
            "strategy": "adaptive-research-loop",
            "target": target.model_dump(exclude_none=True),
            "journey_id": outcome.journey_id,
            "task_run_id": outcome.task_run_id,
            "stop_reason": outcome.stop_reason,
            "executed_queries": list(outcome.executed_queries),
            "evidence_count": outcome.evidence_count,
            "coverage": outcome.coverage.model_dump(mode="json"),
            "evidence_artifact_ids": list(outcome.evidence_artifact_ids),
            "source_cache_hits": outcome.source_cache_hits,
            "preparation_pack_ready": True,
        }


def build_interview_evidence_tool(discovery: InterviewEvidenceDiscovery) -> BaseTool:
    """Expose the business module as a LangChain Tool without crawler controls."""

    async def discover_interview_evidence(
        company: str,
        role: str,
        city: str | None = None,
        job_description: str | None = None,
    ) -> dict[str, Any]:
        """Find recent interview experience for one concrete company and job.

        Call this only after company and role are known. If either is missing,
        ask the user before calling. Provide the JD when the user has one.
        """

        try:
            return await discovery.discover(
                InterviewEvidenceTarget(
                    company=company,
                    role=role,
                    city=city,
                    job_description=job_description,
                )
            )
        except TimeoutError:
            return {
                "status": "failed",
                "error_type": "timeout",
                "message": (
                    "面经研究达到本轮时间预算，已安全停止；"
                    "可以缩小岗位范围后继续。"
                ),
                "retryable": True,
            }

    return StructuredTool.from_function(
        coroutine=discover_interview_evidence,
        name="discover_interview_evidence",
        description=(
            "Find and preserve recent Xiaohongshu interview evidence for a concrete "
            "company and role. Ask the user for missing company or role first."
        ),
        args_schema=InterviewEvidenceTarget,
    )
