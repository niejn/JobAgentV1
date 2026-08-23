"""Bounded adaptive research workflow for one Job/JD."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from jobagent.interview.ocr import ImageContentExtraction
from jobagent.interview.snapshot import RawSourceSnapshot, SnapshotMaterializer
from jobagent.interview.source_corpus import SQLiteSourceCorpus
from jobagent.journey import SQLiteJourneyStore
from jobagent.observability import log_decision
from jobagent.scraper.xhs_backend import DownloadedXhsNote, XhsFetchedNote, XhsNoteReference
from jobagent.scraper.xhs_discovery import is_recent, seller_risk

_SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
logger = logging.getLogger(__name__)


class InterviewResearchTarget(BaseModel):
    company: str = Field(min_length=1)
    role: str = Field(min_length=1)
    job_description: str = Field(min_length=1)
    city: str | None = None


class InterviewSearchQuery(BaseModel):
    kind: str
    text: str = Field(min_length=1)


class InterviewSearchPlan(BaseModel):
    iteration: int = Field(ge=1)
    queries: tuple[InterviewSearchQuery, ...] = Field(min_length=1, max_length=6)


class EvidenceAssessment(BaseModel):
    note_id: str
    grade: Literal["A", "B", "REJECTED"]
    decision: Literal["accept", "reject"]
    reasons: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    source_answers: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.decision == "accept" and self.grade in {"A", "B"}


class PreparedQuestion(BaseModel):
    """One question with explicit answer provenance and no positional ambiguity."""

    question: str = Field(min_length=1)
    source_answer: str | None = None
    model_answer: str | None = None
    source_note_ids: tuple[str, ...] = ()


class PreparationPackDraft(BaseModel):
    jd_focus: tuple[str, ...] = ()
    candidate_gaps: tuple[str, ...] = ()
    questions: tuple[PreparedQuestion, ...] = ()


class EvidenceCoverage(BaseModel):
    """Deterministic stop-gate summary, independent of the LLM's opinion."""

    accepted_a: int = Field(ge=0)
    accepted_b: int = Field(ge=0)
    covered_topics: tuple[str, ...] = ()
    minimum_evidence: int = Field(ge=1)
    required_topics: int = Field(ge=1)
    sufficient: bool


class ResearchBackend(Protocol):
    async def search_notes(
        self, query: str, *, limit: int | None = None, sort: int = 0
    ) -> list[XhsNoteReference]: ...

    async def fetch_note(self, url: str) -> XhsFetchedNote: ...

    async def download_note(
        self,
        url: str,
        *,
        output_dir: Path | None = None,
        fetched_note: XhsFetchedNote | None = None,
    ) -> DownloadedXhsNote: ...


class ResearchIntelligence(Protocol):
    async def plan(
        self,
        target: InterviewResearchTarget,
        feedback: tuple[str, ...],
        iteration: int,
    ) -> InterviewSearchPlan: ...

    async def assess(
        self,
        target: InterviewResearchTarget,
        snapshot_text: str,
        note_id: str,
    ) -> EvidenceAssessment: ...

    async def prepare(
        self,
        target: InterviewResearchTarget,
        evidence: tuple[EvidenceAssessment, ...],
        candidate_context: str | None,
    ) -> PreparationPackDraft: ...


@dataclass(frozen=True, slots=True)
class InterviewResearchOutcome:
    status: str
    journey_id: str
    task_run_id: str
    stop_reason: str
    executed_queries: tuple[str, ...]
    evidence_count: int
    evidence_artifact_ids: tuple[str, ...]
    source_cache_hits: int
    coverage: EvidenceCoverage
    markdown_path: Path
    json_path: Path


class InterviewResearchService:
    """Own adaptive search, snapshots, validation, stopping and pack generation."""

    def __init__(
        self,
        *,
        backend: ResearchBackend,
        intelligence: ResearchIntelligence,
        snapshot_materializer: SnapshotMaterializer,
        store: SQLiteJourneyStore,
        source_corpus: SQLiteSourceCorpus,
        artifact_root: Path,
        max_iterations: int = 3,
        queries_per_iteration: int = 4,
        results_per_query: int = 8,
        minimum_evidence: int = 3,
        required_topics: int = 3,
        stale_days: int = 90,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend
        self._intelligence = intelligence
        self._snapshots = snapshot_materializer
        self._store = store
        self._source_corpus = source_corpus
        self._artifact_root = artifact_root.resolve()
        self._max_iterations = max_iterations
        self._queries_per_iteration = queries_per_iteration
        self._results_per_query = results_per_query
        self._minimum_evidence = minimum_evidence
        self._required_topics = required_topics
        self._stale_days = stale_days
        self._now = now or (lambda: datetime.now(_SHANGHAI))

    async def run(
        self,
        target: InterviewResearchTarget,
        *,
        candidate_context: str | None = None,
    ) -> InterviewResearchOutcome:
        journey = self._store.create_journey(
            company=target.company,
            role=target.role,
            job_description=target.job_description,
        )
        task = self._store.start_task(journey.id, "interview_research")
        output_ids: list[str] = []
        evidence_artifact_ids: list[str] = []
        source_cache_hits = 0
        evidence: list[EvidenceAssessment] = []
        feedback: list[str] = []
        executed_queries: list[str] = []
        seen_notes: set[str] = set()
        stop_reason = "max_iterations"
        coverage = self._coverage(evidence)
        journey_root = self._artifact_root / journey.id
        journey_root.mkdir(parents=True, exist_ok=True)

        try:
            for iteration in range(1, self._max_iterations + 1):
                logger.info("interview_research.plan", extra={"iteration": iteration})
                plan = await self._intelligence.plan(target, tuple(feedback), iteration)
                new_evidence = 0
                for query in plan.queries[: self._queries_per_iteration]:
                    logger.info(
                        "interview_research.search",
                        extra={"iteration": iteration, "query": query.text},
                    )
                    executed_queries.append(query.text)
                    references = await self._backend.search_notes(
                        query.text,
                        limit=self._results_per_query,
                        sort=1,
                    )
                    for reference in references:
                        if reference.note_id in seen_notes:
                            continue
                        seen_notes.add(reference.note_id)
                        current = self._now()
                        if current.tzinfo is None:
                            current = current.replace(tzinfo=_SHANGHAI)
                        cutoff = current.astimezone(_SHANGHAI) - timedelta(
                            days=self._stale_days
                        )
                        bundle = self._source_corpus.get_latest("xhs", reference.note_id)
                        note = _snapshot_as_fetched_note(bundle.snapshot) if bundle else None
                        if note is None:
                            note = await self._backend.fetch_note(reference.url)
                        if not is_recent(note.published_at, cutoff):
                            log_decision(
                                logger,
                                "interview_research.candidate_gate",
                                basis={
                                    "iteration": iteration,
                                    "note_id": reference.note_id,
                                    "is_recent": False,
                                    "seller_risk_score": 0,
                                },
                                outcome="reject_stale",
                            )
                            feedback.append("帖子过旧或发布时间无效")
                            continue
                        risk_flags, risk_score = seller_risk(note)
                        if risk_score >= 2:
                            log_decision(
                                logger,
                                "interview_research.candidate_gate",
                                basis={
                                    "iteration": iteration,
                                    "note_id": reference.note_id,
                                    "is_recent": True,
                                    "seller_risk_score": risk_score,
                                    "risk_flags": risk_flags,
                                },
                                outcome="reject_seller_risk",
                            )
                            feedback.append(f"卖资料风险：{','.join(risk_flags)}")
                            continue
                        log_decision(
                            logger,
                            "interview_research.candidate_gate",
                            basis={
                                "iteration": iteration,
                                "note_id": reference.note_id,
                                "is_recent": True,
                                "seller_risk_score": risk_score,
                            },
                            outcome="admit_to_snapshot",
                        )
                        if bundle is None:
                            downloaded = await self._backend.download_note(
                                reference.url,
                                output_dir=journey_root / "sources",
                                fetched_note=note,
                            )
                            bundle = self._snapshots.materialize(downloaded)
                            self._source_corpus.preserve("xhs", bundle)
                        else:
                            source_cache_hits += 1
                            logger.info(
                                "interview_research.source_cache_hit",
                                extra={"note_id": bundle.snapshot.note_id},
                            )
                        logger.info(
                            "interview_research.snapshot",
                            extra={"note_id": bundle.snapshot.note_id},
                        )
                        snapshot_artifact = self._store.create_artifact(
                            journey_id=journey.id,
                            task_run_id=task.id,
                            artifact_type="raw_source_snapshot",
                            content_ref=str(bundle.manifest_path),
                            content_hash=bundle.snapshot.content_hash,
                            provenance_refs=[f"xhs:{bundle.snapshot.note_id}"],
                        )
                        self._store.validate_artifact(snapshot_artifact.id, valid=True)
                        output_ids.append(snapshot_artifact.id)

                        snapshot_text = _snapshot_text(bundle.snapshot.body, bundle.extractions)
                        assessment = await self._intelligence.assess(
                            target,
                            snapshot_text,
                            bundle.snapshot.note_id,
                        )
                        assessment_path = journey_root / "assessments" / f"{note.note_id}.json"
                        _write_json_atomic(assessment_path, assessment.model_dump(mode="json"))
                        assessment_artifact = self._store.create_artifact(
                            journey_id=journey.id,
                            task_run_id=task.id,
                            artifact_type="post_relevance_assessment",
                            content_ref=str(assessment_path),
                            content_hash=_file_hash(assessment_path),
                            provenance_refs=[snapshot_artifact.id],
                        )
                        self._store.validate_artifact(assessment_artifact.id, valid=True)
                        output_ids.append(assessment_artifact.id)
                        if assessment.accepted:
                            evidence.append(assessment)
                            evidence_artifact_ids.append(assessment_artifact.id)
                            new_evidence += 1
                            logger.info(
                                "interview_research.accepted",
                                extra={
                                    "note_id": assessment.note_id,
                                    "grade": assessment.grade,
                                },
                            )
                        else:
                            feedback.extend(assessment.reasons)

                coverage = self._coverage(evidence)
                if coverage.sufficient:
                    stop_reason = "coverage_satisfied"
                    log_decision(
                        logger,
                        "interview_research.stop_gate",
                        basis={
                            "iteration": iteration,
                            "new_evidence": new_evidence,
                            "accepted_a": coverage.accepted_a,
                            "accepted_b": coverage.accepted_b,
                            "covered_topics": coverage.covered_topics,
                            "minimum_evidence": self._minimum_evidence,
                            "required_topics": self._required_topics,
                        },
                        outcome=stop_reason,
                    )
                    break
                if new_evidence == 0 and iteration == self._max_iterations:
                    stop_reason = "no_marginal_gain"
                    log_decision(
                        logger,
                        "interview_research.stop_gate",
                        basis={
                            "iteration": iteration,
                            "new_evidence": new_evidence,
                            "accepted_a": coverage.accepted_a,
                            "accepted_b": coverage.accepted_b,
                            "covered_topics": coverage.covered_topics,
                            "max_iterations": self._max_iterations,
                        },
                        outcome=stop_reason,
                    )

            draft = await self._intelligence.prepare(
                target,
                tuple(evidence),
                candidate_context,
            )
            logger.info("interview_research.prepare_complete")
            coverage_path = journey_root / "evidence-coverage.json"
            _write_json_atomic(coverage_path, coverage.model_dump(mode="json"))
            coverage_artifact = self._store.create_artifact(
                journey_id=journey.id,
                task_run_id=task.id,
                artifact_type="evidence_coverage",
                content_ref=str(coverage_path),
                content_hash=_file_hash(coverage_path),
                provenance_refs=evidence_artifact_ids,
            )
            self._store.validate_artifact(coverage_artifact.id, valid=True)
            output_ids.append(coverage_artifact.id)
            markdown_path, json_path = _write_preparation_pack(
                journey_root,
                target,
                tuple(evidence),
                draft,
                stop_reason,
                tuple(executed_queries),
                coverage,
            )
            for artifact_type, path in (
                ("interview_preparation_markdown", markdown_path),
                ("interview_preparation_json", json_path),
            ):
                artifact = self._store.create_artifact(
                    journey_id=journey.id,
                    task_run_id=task.id,
                    artifact_type=artifact_type,
                    content_ref=str(path),
                    content_hash=_file_hash(path),
                    provenance_refs=evidence_artifact_ids,
                )
                self._store.validate_artifact(artifact.id, valid=True)
                output_ids.append(artifact.id)
            self._store.complete_task(task.id, output_ids)
            return InterviewResearchOutcome(
                status="completed",
                journey_id=journey.id,
                task_run_id=task.id,
                stop_reason=stop_reason,
                executed_queries=tuple(executed_queries),
                evidence_count=len(evidence),
                evidence_artifact_ids=tuple(evidence_artifact_ids),
                source_cache_hits=source_cache_hits,
                coverage=coverage,
                markdown_path=markdown_path,
                json_path=json_path,
            )
        except asyncio.CancelledError:
            self._store.fail_task(task.id, "CancelledError: research cancelled or timed out")
            raise
        except Exception as exc:
            self._store.fail_task(task.id, f"{type(exc).__name__}: {exc}")
            raise

    def _coverage(self, evidence: list[EvidenceAssessment]) -> EvidenceCoverage:
        topics = tuple(
            dict.fromkeys(topic for item in evidence for topic in item.topics if topic.strip())
        )
        accepted_a = sum(item.grade == "A" for item in evidence)
        accepted_b = sum(item.grade == "B" for item in evidence)
        return EvidenceCoverage(
            accepted_a=accepted_a,
            accepted_b=accepted_b,
            covered_topics=topics,
            minimum_evidence=self._minimum_evidence,
            required_topics=self._required_topics,
            sufficient=(
                accepted_a + accepted_b >= self._minimum_evidence
                and len(topics) >= self._required_topics
            ),
        )


def _snapshot_text(body: str, extractions: tuple[ImageContentExtraction, ...]) -> str:
    ocr_text = "\n".join(item.text for item in extractions)
    return f"正文：\n{body}\n\n图片识别：\n{ocr_text}"[:50_000]


def _snapshot_as_fetched_note(snapshot: RawSourceSnapshot) -> XhsFetchedNote:
    return XhsFetchedNote(
        note_id=snapshot.note_id,
        url=snapshot.source_url,
        title=snapshot.title,
        body=snapshot.body,
        author_id=snapshot.author_id,
        author_name=snapshot.author_name,
        image_urls=tuple(image.source_url or "" for image in snapshot.images),
        tags=snapshot.tags,
        published_at=snapshot.published_at,
        normalized={},
        raw_response={},
    )


def _write_preparation_pack(
    root: Path,
    target: InterviewResearchTarget,
    evidence: tuple[EvidenceAssessment, ...],
    draft: PreparationPackDraft,
    stop_reason: str,
    queries: tuple[str, ...],
    coverage: EvidenceCoverage,
) -> tuple[Path, Path]:
    payload = {
        "schema_version": "1",
        "target": target.model_dump(mode="json"),
        "stop_reason": stop_reason,
        "executed_queries": list(queries),
        "coverage": coverage.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "preparation": draft.model_dump(mode="json"),
    }
    json_path = root / "interview-preparation.json"
    _write_json_atomic(json_path, payload)
    markdown_path = root / "interview-preparation.md"
    lines = [
        f"# {target.company} · {target.role} 面试准备包",
        "",
        f"停止原因：`{stop_reason}`",
        f"已准入证据：{len(evidence)} 条",
        "",
        "## JD 重点",
        *(f"- {item}" for item in draft.jd_focus),
        "",
        "## 候选人准备缺口",
        *(f"- {item}" for item in draft.candidate_gaps),
        "",
        "## 面试题与答案",
    ]
    for index, item in enumerate(draft.questions):
        lines.extend((f"### {index + 1}. {item.question}", ""))
        if item.source_note_ids:
            lines.extend((f"来源帖子：{', '.join(item.source_note_ids)}", ""))
        if item.source_answer:
            lines.extend((f"原帖答案：{item.source_answer}", ""))
        else:
            lines.extend(("原帖答案：未提供", ""))
        if item.model_answer:
            lines.extend((f"模型补充答案：{item.model_answer}", ""))
    _write_text_atomic(markdown_path, "\n".join(lines))
    return markdown_path, json_path


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _text_hash(text: str) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _file_hash(path: Path) -> str:
    return _text_hash(path.read_text(encoding="utf-8"))
