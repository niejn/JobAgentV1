"""End-to-end behavior tests for the bounded interview research loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobagent.interview.ocr import ImageContentExtraction
from jobagent.interview.research import (
    EvidenceAssessment,
    InterviewResearchService,
    InterviewResearchTarget,
    InterviewSearchPlan,
    InterviewSearchQuery,
    PreparationPackDraft,
    PreparedQuestion,
)
from jobagent.interview.snapshot import SnapshotMaterializer
from jobagent.interview.source_corpus import SQLiteSourceCorpus
from jobagent.journey import SQLiteJourneyStore
from jobagent.scraper.xhs_backend import DownloadedXhsNote, XhsFetchedNote, XhsNoteReference


class FakeBackend:
    def __init__(self, root: Path, *, stale_first: bool = False) -> None:
        self.root = root
        self.stale_first = stale_first
        self.queries: list[str] = []
        self.downloaded: list[str] = []

    async def search_notes(
        self, query: str, *, limit: int | None = None, sort: int = 0
    ) -> list[XhsNoteReference]:
        self.queries.append(query)
        note_id = "stale" if self.stale_first and query == "首轮" else (
            "low" if query == "首轮" else "good"
        )
        return [XhsNoteReference(note_id, note_id, {})]

    async def fetch_note(self, url: str) -> XhsFetchedNote:
        title = "招聘讨论" if url in {"low", "stale"} else "字节后端一面面经"
        body = "招聘信息" if url in {"low", "stale"} else "一面问 Redis 和分布式锁"
        return XhsFetchedNote(
            note_id=url,
            url=f"https://www.xiaohongshu.com/explore/{url}?xsec_token=secret",
            title=title,
            body=body,
            author_id=f"author-{url}",
            author_name="普通用户",
            image_urls=("https://image/0",),
            tags=("面经",),
            published_at=(
                "2025-01-01 12:00:00" if url == "stale" else "2026-08-01 12:00:00"
            ),
            normalized={},
            raw_response={"id": url},
        )

    async def download_note(
        self,
        url: str,
        *,
        output_dir: Path | None = None,
        fetched_note: XhsFetchedNote | None = None,
    ) -> DownloadedXhsNote:
        self.downloaded.append(url)
        note = fetched_note or await self.fetch_note(url)
        directory = self.root / url
        directory.mkdir(parents=True, exist_ok=True)
        body = directory / "detail.txt"
        body.write_text(note.body, encoding="utf-8")
        image = directory / "image_0.jpg"
        image.write_bytes(b"image")
        raw = directory / "raw_response.json"
        raw.write_text('{"raw": true}', encoding="utf-8")
        return DownloadedXhsNote(note, directory, body, (image,), raw)


class FakeExtractor:
    def extract(self, image_path: Path) -> ImageContentExtraction:
        return ImageContentExtraction(
            image_path.resolve(),
            "sha256:image",
            "一面 Redis 分布式锁",
            0.9,
            "fake",
            "1",
            "chi_sim",
        )


class FakeIntelligence:
    def __init__(self) -> None:
        self.feedback_seen: list[tuple[str, ...]] = []
        self.assessed_note_ids: list[str] = []

    async def plan(
        self, target: InterviewResearchTarget, feedback: tuple[str, ...], iteration: int
    ) -> InterviewSearchPlan:
        self.feedback_seen.append(feedback)
        query = "首轮" if iteration == 1 else "改进查询"
        return InterviewSearchPlan(
            iteration=iteration,
            queries=(InterviewSearchQuery(kind="test", text=query),),
        )

    async def assess(
        self, target: InterviewResearchTarget, snapshot_text: str, note_id: str
    ) -> EvidenceAssessment:
        self.assessed_note_ids.append(note_id)
        if note_id == "low":
            return EvidenceAssessment(
                note_id=note_id,
                grade="REJECTED",
                decision="reject",
                reasons=("没有真实面试过程",),
            )
        return EvidenceAssessment(
            note_id=note_id,
            grade="A",
            decision="accept",
            reasons=("同公司同岗位且包含真实问题",),
            topics=("Redis", "分布式"),
            questions=("Redis 分布式锁如何实现？",),
        )

    async def prepare(
        self,
        target: InterviewResearchTarget,
        evidence: tuple[EvidenceAssessment, ...],
        candidate_context: str | None,
    ) -> PreparationPackDraft:
        return PreparationPackDraft(
            jd_focus=("分布式系统",),
            candidate_gaps=("Redis 深度尚未获得证据",),
            questions=(
                PreparedQuestion(
                    question="Redis 分布式锁如何实现？",
                    model_answer="使用唯一 token 和原子释放脚本。",
                    source_note_ids=("good",),
                ),
            ),
        )


@pytest.mark.asyncio
async def test_research_replans_after_rejection_and_generates_pack(tmp_path: Path) -> None:
    intelligence = FakeIntelligence()
    store = SQLiteJourneyStore(tmp_path / "state.db")
    corpus = SQLiteSourceCorpus(tmp_path / "state.db")
    service = InterviewResearchService(
        backend=FakeBackend(tmp_path / "downloads"),
        intelligence=intelligence,
        snapshot_materializer=SnapshotMaterializer(FakeExtractor()),
        store=store,
        source_corpus=corpus,
        artifact_root=tmp_path / "artifacts",
        max_iterations=3,
        minimum_evidence=1,
        required_topics=1,
        stale_days=90,
        now=lambda: datetime(2026, 8, 21, tzinfo=timezone(timedelta(hours=8))),
    )

    outcome = await service.run(
        InterviewResearchTarget(
            company="字节跳动",
            role="后端开发",
            city="北京",
            job_description="负责 Redis 与分布式系统",
        )
    )

    assert outcome.status == "completed"
    assert outcome.stop_reason == "coverage_satisfied"
    assert outcome.executed_queries == ("首轮", "改进查询")
    assert outcome.evidence_count == 1
    assert outcome.coverage.sufficient is True
    assert outcome.coverage.accepted_a == 1
    assert outcome.coverage.covered_topics == ("Redis", "分布式")
    assert intelligence.feedback_seen[1] == ("没有真实面试过程",)
    assert outcome.markdown_path.is_file()
    assert outcome.json_path.is_file()
    assert "Redis 分布式锁" in outcome.markdown_path.read_text(encoding="utf-8")
    assert store.get_task(outcome.task_run_id).status.value == "succeeded"
    store.close()
    corpus.close()


@pytest.mark.asyncio
async def test_research_rejects_stale_note_before_download(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path / "downloads", stale_first=True)
    intelligence = FakeIntelligence()
    store = SQLiteJourneyStore(tmp_path / "state.db")
    corpus = SQLiteSourceCorpus(tmp_path / "state.db")
    service = InterviewResearchService(
        backend=backend,
        intelligence=intelligence,
        snapshot_materializer=SnapshotMaterializer(FakeExtractor()),
        store=store,
        source_corpus=corpus,
        artifact_root=tmp_path / "artifacts",
        max_iterations=2,
        minimum_evidence=1,
        required_topics=1,
        stale_days=90,
        now=lambda: datetime(2026, 8, 21, tzinfo=timezone(timedelta(hours=8))),
    )

    outcome = await service.run(
        InterviewResearchTarget(
            company="字节跳动",
            role="后端开发",
            city="北京",
            job_description="负责 Redis 与分布式系统",
        )
    )

    assert "stale" not in backend.downloaded
    assert backend.downloaded == ["good"]
    assert "帖子过旧或发布时间无效" in intelligence.feedback_seen[1]
    assert outcome.coverage.sufficient is True
    store.close()
    corpus.close()


class CacheHitBackend(FakeBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fetch_calls = 0

    async def fetch_note(self, url: str) -> XhsFetchedNote:
        self.fetch_calls += 1
        raise AssertionError("cached source should avoid detail refetch")

    async def download_note(
        self,
        url: str,
        *,
        output_dir: Path | None = None,
        fetched_note: XhsFetchedNote | None = None,
    ) -> DownloadedXhsNote:
        raise AssertionError("cached source should avoid image redownload and OCR")


@pytest.mark.asyncio
async def test_rejected_snapshot_is_reassessed_for_a_later_jd_without_redownload(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    store = SQLiteJourneyStore(database)
    corpus = SQLiteSourceCorpus(database)
    first_intelligence = FakeIntelligence()
    first_service = InterviewResearchService(
        backend=FakeBackend(tmp_path / "first-downloads"),
        intelligence=first_intelligence,
        snapshot_materializer=SnapshotMaterializer(FakeExtractor()),
        store=store,
        source_corpus=corpus,
        artifact_root=tmp_path / "artifacts",
        max_iterations=1,
        minimum_evidence=2,
        required_topics=2,
        stale_days=90,
        now=lambda: datetime(2026, 8, 21, tzinfo=timezone(timedelta(hours=8))),
    )
    await first_service.run(
        InterviewResearchTarget(
            company="第一家公司",
            role="视觉算法",
            job_description="VLA 多模态",
        )
    )
    assert first_intelligence.assessed_note_ids == ["low"]

    backend = CacheHitBackend(tmp_path / "second-downloads")
    second_intelligence = FakeIntelligence()
    second_service = InterviewResearchService(
        backend=backend,
        intelligence=second_intelligence,
        snapshot_materializer=SnapshotMaterializer(FakeExtractor()),
        store=store,
        source_corpus=corpus,
        artifact_root=tmp_path / "artifacts",
        max_iterations=1,
        minimum_evidence=2,
        required_topics=2,
        stale_days=90,
        now=lambda: datetime(2026, 8, 21, tzinfo=timezone(timedelta(hours=8))),
    )
    second_outcome = await second_service.run(
        InterviewResearchTarget(
            company="第二家公司",
            role="Java Agent 工程师",
            job_description="Java Agent MCP SSE",
        )
    )

    assert backend.fetch_calls == 0
    assert second_intelligence.assessed_note_ids == ["low"]
    assert second_outcome.source_cache_hits == 1
    assert corpus.count_versions("xhs", "low") == 1
    store.close()
    corpus.close()
