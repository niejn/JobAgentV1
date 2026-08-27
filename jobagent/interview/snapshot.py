"""Materialize immutable XHS Raw Source Snapshots and derived OCR results."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from jobagent.interview.ocr import ImageContentExtraction
from jobagent.scraper.xhs_backend import DownloadedXhsNote


class ImageExtractor(Protocol):
    def extract(self, image_path: Path) -> ImageContentExtraction: ...


@dataclass(frozen=True, slots=True)
class SourceImage:
    index: int
    path: Path
    source_url: str | None
    content_hash: str


@dataclass(frozen=True, slots=True)
class RawSourceSnapshot:
    note_id: str
    source_url: str
    title: str
    body: str
    author_id: str
    author_name: str
    tags: tuple[str, ...]
    published_at: str | None
    body_path: Path
    raw_response_path: Path
    images: tuple[SourceImage, ...]
    content_hash: str


@dataclass(frozen=True, slots=True)
class SnapshotBundle:
    snapshot: RawSourceSnapshot
    extractions: tuple[ImageContentExtraction, ...]
    manifest_path: Path


class SnapshotMaterializer:
    """Create one traceable manifest after Spider_XHS downloads a note."""

    def __init__(self, extractor: ImageExtractor) -> None:
        self._extractor = extractor

    def materialize(self, downloaded: DownloadedXhsNote) -> SnapshotBundle:
        _validate_download(downloaded)
        images: list[SourceImage] = []
        extractions: list[ImageContentExtraction] = []
        for index, image_path in enumerate(downloaded.images):
            resolved = image_path.resolve()
            content_hash = _file_hash(resolved)
            source_url = (
                downloaded.note.image_urls[index]
                if index < len(downloaded.note.image_urls)
                else None
            )
            images.append(SourceImage(index, resolved, source_url, content_hash))
            extractions.append(self._extractor.extract(resolved))

        snapshot_hash = _snapshot_hash(downloaded, images)
        snapshot = RawSourceSnapshot(
            note_id=downloaded.note.note_id,
            source_url=_canonical_url(downloaded.note.url),
            title=downloaded.note.title,
            body=downloaded.note.body,
            author_id=downloaded.note.author_id,
            author_name=downloaded.note.author_name,
            tags=downloaded.note.tags,
            published_at=downloaded.note.published_at,
            body_path=downloaded.body_path.resolve(),
            raw_response_path=downloaded.raw_response_path.resolve(),
            images=tuple(images),
            content_hash=snapshot_hash,
        )
        manifest_path = downloaded.directory.resolve() / "snapshot.json"
        _write_json_atomic(manifest_path, _manifest(snapshot, extractions))
        return SnapshotBundle(snapshot, tuple(extractions), manifest_path)


def load_snapshot_bundle(manifest_path: Path) -> SnapshotBundle:
    """Reload one immutable snapshot and its OCR provenance from its manifest."""

    resolved_manifest = manifest_path.expanduser().resolve()
    payload = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    image_rows = payload.get("images", [])
    images: list[SourceImage] = []
    extractions: list[ImageContentExtraction] = []
    for row in image_rows:
        image_path = Path(row["path"]).expanduser().resolve()
        if not image_path.is_file():
            raise FileNotFoundError(f"Cached Source Image not found: {image_path}")
        expected_image_hash = str(row["content_hash"])
        actual_image_hash = _file_hash(image_path)
        if actual_image_hash != expected_image_hash:
            raise ValueError(
                f"Cached Source Image hash mismatch: {image_path} "
                f"expected={expected_image_hash} actual={actual_image_hash}"
            )
        images.append(
            SourceImage(
                index=int(row["index"]),
                path=image_path,
                source_url=row.get("source_url"),
                content_hash=expected_image_hash,
            )
        )
        ocr = row["ocr"]
        extractions.append(
            ImageContentExtraction(
                source_image=image_path,
                source_hash=str(ocr["source_hash"]),
                text=str(ocr["text"]),
                confidence=float(ocr["confidence"]),
                engine=str(ocr["engine"]),
                engine_version=str(ocr["engine_version"]),
                language=str(ocr["language"]),
            )
        )
    body_path = Path(payload["body_path"]).expanduser().resolve()
    raw_response_path = Path(payload["raw_response_path"]).expanduser().resolve()
    if not body_path.is_file():
        raise FileNotFoundError(f"Cached XHS body file not found: {body_path}")
    if not raw_response_path.is_file():
        raise FileNotFoundError(f"Cached XHS raw response not found: {raw_response_path}")
    author = payload.get("author", {})
    expected_snapshot_hash = str(payload["content_hash"])
    actual_snapshot_hash = _snapshot_content_hash(
        note_id=str(payload["note_id"]),
        title=str(payload["title"]),
        body=str(payload["body"]),
        published_at=str(payload["published_at"]) if payload.get("published_at") else None,
        image_hashes=tuple(image.content_hash for image in images),
    )
    if actual_snapshot_hash != expected_snapshot_hash:
        raise ValueError(
            "Cached Raw Source Snapshot hash mismatch: "
            f"expected={expected_snapshot_hash} actual={actual_snapshot_hash}"
        )
    snapshot = RawSourceSnapshot(
        note_id=str(payload["note_id"]),
        source_url=str(payload["source_url"]),
        title=str(payload["title"]),
        body=str(payload["body"]),
        author_id=str(author.get("id", "")),
        author_name=str(author.get("name", "")),
        tags=tuple(str(tag) for tag in payload.get("tags", [])),
        published_at=(
            str(payload["published_at"]) if payload.get("published_at") else None
        ),
        body_path=body_path,
        raw_response_path=raw_response_path,
        images=tuple(images),
        content_hash=expected_snapshot_hash,
    )
    return SnapshotBundle(snapshot, tuple(extractions), resolved_manifest)


def _validate_download(downloaded: DownloadedXhsNote) -> None:
    required = (
        ("download directory", downloaded.directory, downloaded.directory.is_dir()),
        ("body file", downloaded.body_path, downloaded.body_path.is_file()),
        ("raw response file", downloaded.raw_response_path, downloaded.raw_response_path.is_file()),
    )
    for label, path, exists in required:
        if not exists:
            raise FileNotFoundError(f"Missing XHS {label}: {path}")
    for image in downloaded.images:
        if not image.is_file():
            raise FileNotFoundError(f"Missing XHS image file: {image}")


def _manifest(
    snapshot: RawSourceSnapshot,
    extractions: list[ImageContentExtraction],
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "note_id": snapshot.note_id,
        "source_url": snapshot.source_url,
        "title": snapshot.title,
        "body": snapshot.body,
        "author": {"id": snapshot.author_id, "name": snapshot.author_name},
        "tags": list(snapshot.tags),
        "published_at": snapshot.published_at,
        "body_path": str(snapshot.body_path),
        "raw_response_path": str(snapshot.raw_response_path),
        "content_hash": snapshot.content_hash,
        "images": [
            {
                "index": image.index,
                "path": str(image.path),
                "source_url": image.source_url,
                "content_hash": image.content_hash,
                "ocr": {
                    "text": extraction.text,
                    "confidence": extraction.confidence,
                    "engine": extraction.engine,
                    "engine_version": extraction.engine_version,
                    "language": extraction.language,
                    "source_hash": extraction.source_hash,
                },
            }
            for image, extraction in zip(snapshot.images, extractions, strict=True)
        ],
    }


def _snapshot_hash(downloaded: DownloadedXhsNote, images: list[SourceImage]) -> str:
    return _snapshot_content_hash(
        note_id=downloaded.note.note_id,
        title=downloaded.note.title,
        body=downloaded.note.body,
        published_at=downloaded.note.published_at,
        image_hashes=tuple(image.content_hash for image in images),
    )


def _snapshot_content_hash(
    *,
    note_id: str,
    title: str,
    body: str,
    published_at: str | None,
    image_hashes: tuple[str, ...],
) -> str:
    digest = hashlib.sha256()
    for value in (
        note_id,
        title,
        body,
        # The stale-note gate keys on published_at, so it must be tamper
        # evident: rewriting it in a cached manifest must break the hash.
        published_at or "",
        *image_hashes,
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
