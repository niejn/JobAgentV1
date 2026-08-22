"""Save one user-supplied Xiaohongshu note behind a safe business interface."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from curl_cffi.requests.exceptions import RequestException
from pydantic import BaseModel, Field, field_validator

from jobagent.config import Settings
from jobagent.scraper.xhs_backend import (
    DownloadedXhsNote,
    SpiderXhsBackend,
    SpiderXhsDependencyError,
    SpiderXhsError,
    XhsAuthenticationError,
)


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


class XhsNoteSaver:
    """Deep module that owns XHS authentication, download location, and result projection."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend_factory: Callable[[Settings], Any] = SpiderXhsBackend,
    ) -> None:
        self._settings = settings
        self._backend_factory = backend_factory

    async def save(self, request: XhsNoteSaveRequest) -> dict[str, Any]:
        try:
            async with self._backend_factory(self._settings) as backend:
                downloaded: DownloadedXhsNote = await backend.download_note(request.url)
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

        return {
            "status": "completed",
            "platform": "xiaohongshu",
            "note_id": downloaded.note.note_id,
            "title": downloaded.note.title,
            "body_file": downloaded.body_path.name,
            "image_files": [path.name for path in downloaded.images],
            "raw_response_file": downloaded.raw_response_path.name,
            "directory_name": downloaded.directory.name,
        }
