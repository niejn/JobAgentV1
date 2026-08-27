"""Stop-loss + fallback rotation in the research loop (review M5)."""

from __future__ import annotations

from jobagent.interview.intelligence import _fallback_plan
from jobagent.interview.research import InterviewResearchTarget


def test_fallback_plan_rotates_queries_per_iteration() -> None:
    target = InterviewResearchTarget(
        company="字节跳动", role="后端开发", city="北京", job_description="jd"
    )
    p1 = _fallback_plan(target, 1)
    p2 = _fallback_plan(target, 2)
    p4 = _fallback_plan(target, 4)  # wraps to group 1

    t1 = {q.text for q in p1.queries}
    t2 = {q.text for q in p2.queries}
    assert t1 and t2 and not (t1 & t2), "consecutive fallback rounds must differ"
    assert {q.text for q in p4.queries} == t1

    # All texts still anchor to the target company/role.
    assert all("字节跳动 后端开发" in q.text for q in p1.queries)


def test_two_dry_iterations_trigger_no_marginal_gain_stop() -> None:
    """Backend that never accepts anything must stop after the 2nd dry round.

    A pathological loop (e.g. repeating fallback queries against XHS dedup)
    previously burned every max_iterations round with zero gain.
    """
    import asyncio
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from jobagent.interview.ocr import ImageContentExtraction  # noqa: F401
    from jobagent.interview.research import (
        EvidenceAssessment,
        InterviewResearchService,
        PreparationPackDraft,
    )
    from jobagent.interview.snapshot import SnapshotMaterializer
    from jobagent.interview.source_corpus import SQLiteSourceCorpus
    from jobagent.journey import SQLiteJourneyStore
    from jobagent.scraper.xhs_backend import XhsNoteReference
    from tests.test_interview_research import FakeBackend
    from tests.test_research_note_failure_isolation import WorkingExtractor

    class AlwaysRejectIntelligence:
        def __init__(self) -> None:
            self.plan_calls = 0

        async def plan(self, target, feedback, iteration):
            self.plan_calls += 1
            from jobagent.interview.research import InterviewSearchPlan, InterviewSearchQuery

            return InterviewSearchPlan(
                iteration=iteration,
                queries=(InterviewSearchQuery(kind="test", text=f"q{iteration}"),),
            )

        async def assess(self, target, snapshot_text, note_id):
            return EvidenceAssessment(
                note_id=note_id,
                grade="REJECTED",
                decision="reject",
                reasons=("无关内容",),
            )

        async def prepare(self, target, evidence, candidate_context):
            return PreparationPackDraft(
                jd_focus=(), candidate_gaps=(), questions=()
            )

    class ManyNotesBackend(FakeBackend):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self._n = 0

        async def search_notes(self, query, *, limit=None, sort=0):
            self._n += 1
            return [XhsNoteReference(f"note{self._n}", f"note{self._n}", {})]

    async def run() -> tuple[str, int]:
        tmp = Path(".ua/tmp_m5")
        tmp.mkdir(parents=True, exist_ok=True)
        intel = AlwaysRejectIntelligence()
        store = SQLiteJourneyStore(tmp / "state.db")
        corpus = SQLiteSourceCorpus(tmp / "state.db")
        service = InterviewResearchService(
            backend=ManyNotesBackend(tmp / "dl"),
            intelligence=intel,
            snapshot_materializer=SnapshotMaterializer(WorkingExtractor()),
            store=store,
            source_corpus=corpus,
            artifact_root=tmp / "art",
            max_iterations=10,
            minimum_evidence=5,
            required_topics=5,
            stale_days=90,
            now=lambda: datetime(2026, 8, 21, tzinfo=timezone(timedelta(hours=8))),
        )
        outcome = await service.run(
            InterviewResearchTarget(
                company="字节跳动", role="后端", city="北京", job_description="jd"
            )
        )
        store.close()
        corpus.close()
        return outcome.stop_reason, intel.plan_calls

    stop_reason, plan_calls = asyncio.run(run())
    assert stop_reason == "no_marginal_gain"
    assert plan_calls == 2, f"expected stop after 2 dry rounds, ran {plan_calls}"
