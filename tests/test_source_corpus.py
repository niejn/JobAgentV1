"""Tests for the reusable cross-Journey Raw Source Snapshot corpus."""

from pathlib import Path

from jobagent.interview.ocr import ImageContentExtraction
from jobagent.interview.snapshot import SnapshotMaterializer
from jobagent.interview.source_corpus import SQLiteSourceCorpus
from jobagent.scraper.xhs_backend import DownloadedXhsNote, XhsFetchedNote


class FakeExtractor:
    def extract(self, image_path: Path) -> ImageContentExtraction:
        return ImageContentExtraction(
            source_image=image_path.resolve(),
            source_hash="sha256:image",
            text="Agent MCP SSE 面试题",
            confidence=0.9,
            engine="fake",
            engine_version="1",
            language="chi_sim",
        )


def _bundle(tmp_path: Path):
    directory = tmp_path / "note"
    directory.mkdir(parents=True)
    body_path = directory / "detail.txt"
    body_path.write_text("Java Agent 面试复盘", encoding="utf-8")
    image_path = directory / "image_0.jpg"
    image_path.write_bytes(b"image")
    raw_path = directory / "raw_response.json"
    raw_path.write_text('{"raw": true}', encoding="utf-8")
    note = XhsFetchedNote(
        note_id="note-rejected-for-first-jd",
        url="https://www.xiaohongshu.com/explore/note-rejected-for-first-jd?xsec=secret",
        title="Java Agent 面经",
        body="Java Agent 面试复盘",
        author_id="author-1",
        author_name="候选人A",
        image_urls=("https://image/0",),
        tags=("Agent", "面经"),
        published_at="2026-08-01 12:00:00",
        normalized={},
        raw_response={"raw": True},
    )
    downloaded = DownloadedXhsNote(
        note,
        directory,
        body_path,
        (image_path,),
        raw_path,
    )
    return SnapshotMaterializer(FakeExtractor()).materialize(downloaded)


def test_corpus_persists_and_reloads_snapshot_body_images_and_ocr(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    database = tmp_path / "state.db"
    corpus = SQLiteSourceCorpus(database)
    corpus.preserve("xhs", bundle)
    corpus.close()

    reopened = SQLiteSourceCorpus(database)
    try:
        restored = reopened.get_latest("xhs", bundle.snapshot.note_id)
    finally:
        reopened.close()

    assert restored is not None
    assert restored.snapshot.content_hash == bundle.snapshot.content_hash
    assert restored.snapshot.body == "Java Agent 面试复盘"
    assert restored.extractions[0].text == "Agent MCP SSE 面试题"
    assert restored.snapshot.images[0].path.read_bytes() == b"image"


def test_preserving_same_snapshot_is_idempotent(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    corpus = SQLiteSourceCorpus(tmp_path / "state.db")
    try:
        corpus.preserve("xhs", bundle)
        corpus.preserve("xhs", bundle)
        assert corpus.count_versions("xhs", bundle.snapshot.note_id) == 1
    finally:
        corpus.close()


def test_corpus_backfills_existing_manifests_only_once(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "legacy")
    corpus = SQLiteSourceCorpus(tmp_path / "state.db")
    try:
        assert corpus.backfill_manifests(tmp_path) == 1
        assert corpus.backfill_manifests(tmp_path) == 0
        restored = corpus.get_latest("xhs", bundle.snapshot.note_id)
        assert restored is not None
        assert restored.snapshot.content_hash == bundle.snapshot.content_hash
    finally:
        corpus.close()


def test_corpus_does_not_reuse_snapshot_with_corrupted_image(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    corpus = SQLiteSourceCorpus(tmp_path / "state.db")
    try:
        corpus.preserve("xhs", bundle)
        bundle.snapshot.images[0].path.write_bytes(b"corrupted")

        assert corpus.get_latest("xhs", bundle.snapshot.note_id) is None
    finally:
        corpus.close()
