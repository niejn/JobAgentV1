"""Behavior tests for immutable XHS Raw Source Snapshots."""

from pathlib import Path

import pytest

from jobagent.interview.ocr import ImageContentExtraction
from jobagent.interview.snapshot import SnapshotMaterializer
from jobagent.scraper.xhs_backend import DownloadedXhsNote, XhsFetchedNote


class FakeExtractor:
    def extract(self, image_path: Path) -> ImageContentExtraction:
        return ImageContentExtraction(
            source_image=image_path.resolve(),
            source_hash=f"sha256:{image_path.stem}",
            text=f"OCR {image_path.stem}",
            confidence=0.9,
            engine="fake-ocr",
            engine_version="1",
            language="chi_sim",
        )


def test_materializer_preserves_body_images_ocr_and_provenance(tmp_path: Path) -> None:
    directory = tmp_path / "note"
    directory.mkdir()
    body = directory / "detail.txt"
    body.write_text("一面问了 Redis", encoding="utf-8")
    image_0 = directory / "image_0.jpg"
    image_1 = directory / "image_1.jpg"
    image_0.write_bytes(b"zero")
    image_1.write_bytes(b"one")
    raw = directory / "raw_response.json"
    raw.write_text('{"data": "original"}', encoding="utf-8")
    note = XhsFetchedNote(
        note_id="note-1",
        url="https://www.xiaohongshu.com/explore/note-1?xsec_token=secret",
        title="字节后端面经",
        body="一面问了 Redis",
        author_id="author-1",
        author_name="用户A",
        image_urls=("https://image/0", "https://image/1"),
        tags=("面经",),
        published_at="2026-08-01 12:00:00",
        normalized={},
        raw_response={"data": "original"},
    )
    downloaded = DownloadedXhsNote(note, directory, body, (image_0, image_1), raw)

    bundle = SnapshotMaterializer(FakeExtractor()).materialize(downloaded)

    assert bundle.manifest_path.is_file()
    assert bundle.snapshot.body == "一面问了 Redis"
    assert bundle.snapshot.source_url == "https://www.xiaohongshu.com/explore/note-1"
    assert [item.index for item in bundle.snapshot.images] == [0, 1]
    assert [item.text for item in bundle.extractions] == ["OCR image_0", "OCR image_1"]
    assert bundle.snapshot.content_hash.startswith("sha256:")


def test_materializer_rejects_incomplete_download(tmp_path: Path) -> None:
    directory = tmp_path / "note"
    directory.mkdir()
    missing_body = directory / "detail.txt"
    raw = directory / "raw_response.json"
    raw.write_text("{}", encoding="utf-8")
    note = XhsFetchedNote(
        note_id="note-1",
        url="https://www.xiaohongshu.com/explore/note-1",
        title="面经",
        body="正文",
        author_id="author-1",
        author_name="用户A",
        image_urls=(),
        tags=("面经",),
        published_at="2026-08-01 12:00:00",
        normalized={},
        raw_response={},
    )

    with pytest.raises(FileNotFoundError, match="body file"):
        SnapshotMaterializer(FakeExtractor()).materialize(
            DownloadedXhsNote(note, directory, missing_body, (), raw)
        )
