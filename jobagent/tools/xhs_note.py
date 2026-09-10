"""Save one user-supplied Xiaohongshu note behind a safe business interface."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from curl_cffi.requests.exceptions import RequestException
from pydantic import BaseModel, Field, field_validator

from jobagent.config import Settings
from jobagent.interview.ocr import TesseractOcrExtractor
from jobagent.interview.snapshot import (
    SnapshotBundle,
    SnapshotMaterializer,
    load_snapshot_bundle,
)
from jobagent.journey.recruitment_notes import CommentSnapshot
from jobagent.scraper.xhs_backend import (
    DownloadedXhsNote,
    SpiderXhsBackend,
    SpiderXhsDependencyError,
    SpiderXhsError,
    XhsAuthenticationError,
    XhsFetchedNote,
    strip_xsec_token,
)

logger = logging.getLogger(__name__)


class XhsNoteSaveRequest(BaseModel):
    """Public XHS note URL supplied by the user."""

    url: str = Field(min_length=1, description="Public Xiaohongshu note URL to save")

    @field_validator("url")
    @classmethod
    def validate_public_note_url(cls, value: str) -> str:
        cleaned = value.strip()
        parsed = urlsplit(cleaned)
        host = (parsed.hostname or "").lower()
        path_parts = tuple(part for part in parsed.path.split("/") if part)
        is_note_path = (
            len(path_parts) >= 2
            and path_parts[-2] in {"item", "explore"}
            and bool(path_parts[-1])
        )
        if (
            parsed.scheme != "https"
            or host not in {"xiaohongshu.com", "www.xiaohongshu.com"}
            or not is_note_path
        ):
            raise ValueError("Provide a public Xiaohongshu discovery/item or explore URL")
        return cleaned


def _default_materializer(settings: Settings) -> SnapshotMaterializer:
    return SnapshotMaterializer(
        TesseractOcrExtractor(
            command=settings.tesseract_cmd,
            language=settings.jobagent_ocr_language,
            page_segmentation_mode=settings.jobagent_ocr_psm,
        )
    )


class XhsContentReader:
    """Materialize or reload one XHS snapshot and expose bounded text to the Agent."""

    def __init__(
        self,
        settings: Settings,
        *,
        materializer_factory: Callable[[Settings], SnapshotMaterializer] = _default_materializer,
    ) -> None:
        self._settings = settings
        self._materializer_factory = materializer_factory

    async def materialize(self, downloaded: DownloadedXhsNote) -> dict[str, Any]:
        manifest = downloaded.directory / "snapshot.json"
        try:
            logger.info("xhs.progress [4/4] OCR 图片，共 %s 张", len(downloaded.images))
            bundle = (
                load_snapshot_bundle(manifest)
                if manifest.is_file()
                else await asyncio.to_thread(
                    self._materializer_factory(self._settings).materialize,
                    downloaded,
                )
            )
        except (FileNotFoundError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return {
                "status": "downloaded_ocr_pending",
                "message": f"帖子已保存，但本地图片 OCR 尚未完成：{type(exc).__name__}。",
                "body_text": downloaded.note.body,
                "image_ocr_text": "",
                "image_count": len(downloaded.images),
                "image_ocr_count": 0,
            }
        return _bundle_text(bundle)

    async def extract_saved(self, url: str) -> dict[str, Any]:
        downloaded = await asyncio.to_thread(self._find_downloaded, url)
        if downloaded is None:
            return {
                "status": "not_found",
                "message": "没有找到该 URL 对应的已保存小红书快照；请先调用 save_shared_url。",
            }
        return await self.materialize(downloaded)

    def _find_downloaded(self, url: str) -> DownloadedXhsNote | None:
        note_id = _note_id_from_url(url)
        root = self._settings.xhs_download_dir.expanduser().resolve()
        if not root.is_dir():
            return None
        for info_path in root.rglob("info.json"):
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            if str(info.get("note_id") or "") != note_id:
                continue
            directory = info_path.parent
            body_path = directory / "detail.txt"
            raw_path = directory / "raw_response.json"
            if not body_path.is_file() or not raw_path.is_file():
                continue
            try:
                raw_response = json.loads(raw_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            images = tuple(sorted(directory.glob("image_*"), key=lambda path: path.name))
            note = XhsFetchedNote(
                note_id=note_id,
                url=str(info.get("note_url") or url),
                title=str(info.get("title") or ""),
                body=str(info.get("desc") or ""),
                author_id=str(info.get("user_id") or ""),
                author_name=str(info.get("nickname") or ""),
                image_urls=tuple(str(item) for item in info.get("image_list", []) if item),
                tags=tuple(str(item) for item in info.get("tags", []) if item),
                published_at=str(info.get("upload_time") or "") or None,
                normalized=info,
                raw_response=raw_response,
            )
            return DownloadedXhsNote(
                note=note,
                directory=directory,
                body_path=body_path,
                images=images,
                raw_response_path=raw_path,
            )
        return None


class XhsNoteSaver:
    """Deep module that owns XHS authentication, download location, and result projection."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend_factory: Callable[[Settings], Any] = SpiderXhsBackend,
        content_reader: XhsContentReader | None = None,
    ) -> None:
        self._settings = settings
        self._backend_factory = backend_factory
        self._content_reader = content_reader or XhsContentReader(settings)

    async def save(self, request: XhsNoteSaveRequest) -> dict[str, Any]:
        try:
            async with self._backend_factory(self._settings) as backend:
                downloaded: DownloadedXhsNote = await backend.download_note(request.url)
                fetch_comments = getattr(backend, "fetch_note_comments", None)
                raw_comments = await fetch_comments(request.url) if callable(fetch_comments) else []
        except XhsAuthenticationError:
            return {
                "status": "blocked",
                "platform": "xiaohongshu",
                "error_type": "login_required",
                "message": "小红书登录态不可用；请先运行 jobagent login --platform xhs。",
            }
        except SpiderXhsDependencyError:
            return {
                "status": "failed",
                "platform": "xiaohongshu",
                "error_type": "dependency_unavailable",
                "message": "Spider_XHS 本地依赖不可用，帖子尚未保存。",
            }
        except SpiderXhsError:
            return {
                "status": "blocked",
                "platform": "xiaohongshu",
                "error_type": "source_access_control",
                "message": "小红书拒绝了本次读取或帖子不可用；未尝试绕过平台限制。",
            }
        except RequestException:
            return {
                "status": "failed",
                "platform": "xiaohongshu",
                "error_type": "network_error",
                "message": "小红书正文或媒体下载失败；可能是网络错误或来源拒绝连接。",
            }

        extracted = await self._content_reader.materialize(downloaded)
        logger.info("xhs.progress 评论读取完成，开始过滤帖主评论")
        author_id = downloaded.note.author_id
        comments = tuple(
            CommentSnapshot(
                comment_id=str(item.get("id") or ""),
                author_id=str((item.get("user_info") or {}).get("user_id") or ""),
                author_name=str((item.get("user_info") or {}).get("nickname") or ""),
                text=str(item.get("content") or ""),
                is_note_author=str((item.get("user_info") or {}).get("user_id") or "") == author_id,
            )
            for item in raw_comments
        )
        return {
            **extracted,
            "status": extracted["status"],
            "platform": "xiaohongshu",
            "note_id": downloaded.note.note_id,
            "title": downloaded.note.title,
            "body_file": downloaded.body_path.name,
            "image_files": [path.name for path in downloaded.images],
            "raw_response_file": downloaded.raw_response_path.name,
            "directory_name": downloaded.directory.name,
            "author_comments": comments,
        }

    async def extract_saved(self, request: XhsNoteSaveRequest) -> dict[str, Any]:
        """Read body and OCR from a previously saved note without downloading again."""

        return {
            **await self._content_reader.extract_saved(request.url),
            "platform": "xiaohongshu",
            "note_url": strip_xsec_token(request.url),
        }



def _note_id_from_url(url: str) -> str:
    parts = tuple(part for part in urlsplit(url).path.split("/") if part)
    return parts[-1] if parts else ""


def _bundle_text(bundle: SnapshotBundle) -> dict[str, Any]:
    body = bundle.snapshot.body
    ocr_items = tuple(item for item in bundle.extractions if item.text.strip())
    ocr_text = "\n\n".join(
        f"图片 {index + 1}：\n{item.text}" for index, item in enumerate(ocr_items)
    )
    combined = f"正文：\n{body}\n\n图片 OCR：\n{ocr_text}".strip()
    return {
        "status": "completed",
        "note_id": bundle.snapshot.note_id,
        "source_url": strip_xsec_token(bundle.snapshot.source_url),
        "title": bundle.snapshot.title,
        "body_text": body,
        "image_ocr_text": ocr_text,
        "extracted_text": combined[:50_000],
        "image_count": len(bundle.snapshot.images),
        "image_ocr_count": len(ocr_items),
        "manifest_file": bundle.manifest_path.name,
    }
