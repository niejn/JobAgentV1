"""Per-note failure isolation in the research loop (code review HIGH H4).

One bad note (OCR failure, hash mismatch, transient backend error) must be
skipped with feedback; it must not abort the journey and discard every
evidence item already collected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobagent.interview.ocr import ImageContentExtraction
from jobagent.interview.research import (
    InterviewResearchService,
    InterviewResearchTarget,
)
from jobagent.interview.snapshot import SnapshotMaterializer
from jobagent.interview.source_corpus import SQLiteSourceCorpus
from jobagent.journey import SQLiteJourneyStore
from jobagent.scraper.xhs_backend import (
    DownloadedXhsNote,
    XhsFetchedNote,
    XhsNoteReference,
)
from tests.test_interview_research import FakeIntelligence


class TwoNoteBackend:
    """Yields one good note, then one note whose download explodes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.served = False

    async def search_notes(
        self, query: str, *, limit: int | None = None, sort: int = 0
    ) -> list[XhsNoteReference]:
        if self.served:
            return []
        self.served = True
        return [
            XhsNoteReference("good", "good", {}),
            XhsNoteReference("broken", "broken", {}),
        ]

    async def fetch_note(self, url: str) -> XhsFetchedNote:
        return XhsFetchedNote(
            note_id=url,
            url=f"https://www.xiaohongshu.com/explore/{url}?xsec_token=secret",
            title="字节后端一面面经",
            body="一面问 Redis 和分布式锁",
            author_id=f"author-{url}",
            author_name="普通用户",
            image_urls=("https://image/0",),
            tags=("面经",),
            published_at="2026-08-01 12:00:00",
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
        note = fetched_note or await self.fetch_note(url)
        if url == "broken":
            # Simulate the failure class the guard must contain: anything
            # raised between admit and assessment for ONE note.
            raise RuntimeError("simulated download/OCR failure")
        directory = self.root / url
        directory.mkdir(parents=True, exist_ok=True)
        body = directory / "detail.txt"
        body.write_text(note.body, encoding="utf-8")
        image = directory / "image_0.jpg"
        image.write_bytes(b"image")
        raw = directory / "raw_response.json"
        raw.write_text('{"raw": true}', encoding="utf-8")
        return DownloadedXhsNote(note, directory, body, (image,), raw)


class WorkingExtractor:
    """Normal OCR outcome; the broken note never reaches it."""

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


@pytest.mark.asyncio
async def test_one_broken_note_does_not_abort_the_journey(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    intelligence = FakeIntelligence()
    store = SQLiteJourneyStore(tmp_path / "state.db")
    corpus = SQLiteSourceCorpus(tmp_path / "state.db")
    service = InterviewResearchService(
        backend=TwoNoteBackend(tmp_path / "downloads"),
        intelligence=intelligence,
        snapshot_materializer=SnapshotMaterializer(WorkingExtractor()),
        store=store,
        source_corpus=corpus,
        artifact_root=tmp_path / "artifacts",
        max_iterations=1,
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

    # The journey completes; the good note's evidence survives.
    assert outcome.status == "completed"
    assert outcome.evidence_count == 1
    assert intelligence.assessed_note_ids == ["good"]
    # The failure is contained and logged per-note, not raised through the
    # journey (pre-fix this test dies with the RuntimeError itself).
    failed_logs = [
        record for record in caplog.records if "note_failed" in record.getMessage()
    ]
    assert failed_logs, "expected interview_research.note_failed log record"
    assert store.get_task(outcome.task_run_id).status.value == "succeeded"
    store.close()
    corpus.close()
