"""Route a user-supplied public URL to the appropriate read-only saver."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, field_validator

from jobagent.auth.cookie_manager import CookieNotFoundError, get_cookies
from jobagent.config import Settings
from jobagent.tools.xhs_note import XhsNoteSaver, XhsNoteSaveRequest

_MAX_RESOURCE_BYTES = 20 * 1024 * 1024
_BLOCKED_PAGE_SIGNALS = (
    "安全验证",
    "访问行为异常",
    "请先登录",
    "登录后查看",
    "captcha",
    "access denied",
)


class SharedUrlSaveRequest(BaseModel):
    """One public HTTP(S) URL supplied by the user."""

    url: str = Field(min_length=1, description="Public URL to download and preserve")

    @field_validator("url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        cleaned = value.strip()
        parsed = urlsplit(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Provide an absolute public HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Credentials cannot be embedded in a shared URL")
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith(".local"):
            raise ValueError("Local network URLs cannot be downloaded")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("Local or private network URLs cannot be downloaded")
        return cleaned


class PageSaver(Protocol):
    async def save(self, url: str, *, platform: str) -> dict[str, Any]: ...


class WebPageSaver:
    """Bounded read-only saver for Boss pages and best-effort public web pages."""

    def __init__(
        self,
        settings: Settings,
        *,
        requester: Callable[..., Any] = curl_requests.get,
    ) -> None:
        self._settings = settings
        self._requester = requester
        self._root = settings.jobagent_artifact_dir.expanduser().resolve() / "shared_urls"

    async def save(self, url: str, *, platform: str) -> dict[str, Any]:
        cookies: dict[str, str] = {}
        if platform == "boss":
            try:
                stored = await get_cookies("boss", self._settings)
            except CookieNotFoundError:
                return {
                    "status": "blocked",
                    "platform": "boss",
                    "error_type": "login_required",
                    "message": "Boss 登录态不可用；请先运行 jobagent login --platform boss。",
                }
            cookies = {
                str(item["name"]): str(item["value"])
                for item in stored
                if item.get("name") and item.get("value")
            }

        current_url = url
        response: Any | None = None
        for _ in range(6):
            current_host = (urlsplit(current_url).hostname or "").lower()
            is_boss_host = current_host == "zhipin.com" or current_host.endswith(
                ".zhipin.com"
            )
            headers = {"Accept": "text/html,application/xhtml+xml,application/pdf,*/*"}
            if platform == "boss" and is_boss_host:
                headers["Referer"] = "https://www.zhipin.com/"
            try:
                current_response = await asyncio.to_thread(
                    self._requester,
                    current_url,
                    cookies=cookies if is_boss_host else {},
                    headers=headers,
                    impersonate="chrome",
                    timeout=self._settings.jobagent_request_timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except RequestException:
                return {
                    "status": "failed",
                    "platform": platform,
                    "error_type": "network_error",
                    "message": "页面读取失败；可能是网络错误或来源站点拒绝连接。",
                }
            response = current_response
            status_code = int(getattr(current_response, "status_code", 0))
            if status_code < 300 or status_code >= 400:
                break
            location = str(current_response.headers.get("location", "")).strip()
            _close_response(current_response)
            if not location:
                return {
                    "status": "failed",
                    "platform": platform,
                    "error_type": "invalid_redirect",
                    "message": "来源页面返回了缺少目标地址的重定向。",
                }
            redirected = urljoin(current_url, location)
            try:
                current_url = SharedUrlSaveRequest(url=redirected).url
            except ValueError:
                return {
                    "status": "blocked",
                    "platform": platform,
                    "error_type": "unsafe_redirect",
                    "message": "来源页面重定向到非公开或不安全地址，已停止读取。",
                }
        else:
            return {
                "status": "failed",
                "platform": platform,
                "error_type": "too_many_redirects",
                "message": "来源页面重定向次数过多，已停止读取。",
            }

        assert response is not None
        status_code = int(getattr(response, "status_code", 0))
        if status_code in {401, 403, 407, 429}:
            _close_response(response)
            return {
                "status": "blocked",
                "platform": platform,
                "error_type": "login_or_access_control",
                "http_status": status_code,
                "message": "页面需要登录、触发访问限制或暂时拒绝读取；未尝试绕过。",
            }
        if status_code < 200 or status_code >= 300:
            _close_response(response)
            return {
                "status": "failed",
                "platform": platform,
                "error_type": "http_error",
                "http_status": status_code,
                "message": f"来源页面返回 HTTP {status_code}，未保存内容。",
            }

        content = _read_bounded_content(response)
        if content is None:
            return {
                "status": "failed",
                "platform": platform,
                "error_type": "resource_too_large",
                "message": "来源内容超过第一版 20 MiB 保存上限。",
            }
        content_type = str(response.headers.get("content-type", "application/octet-stream"))
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type.startswith("text/") or media_type in {
            "application/json",
            "application/xhtml+xml",
        }:
            preview = content[:200_000].decode("utf-8", errors="ignore").lower()
            if any(signal.lower() in preview for signal in _BLOCKED_PAGE_SIGNALS):
                return {
                    "status": "blocked",
                    "platform": platform,
                    "error_type": "login_or_access_control",
                    "message": "页面显示登录或安全验证，未尝试绕过。",
                }

        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        directory = self._root / digest
        directory.mkdir(parents=True, exist_ok=True)
        filename = _resource_filename(media_type)
        content_path = directory / filename
        _write_bytes_atomic(content_path, content)
        _write_json_atomic(
            directory / "source.json",
            {
                "schema_version": "1",
                "platform": platform,
                "source_url": url,
                "content_type": media_type,
                "content_file": filename,
                "byte_count": len(content),
            },
        )
        return {
            "status": "completed",
            "platform": platform,
            "artifact_ref": f"shared_urls/{digest}",
            "content_file": filename,
            "content_type": media_type,
            "byte_count": len(content),
        }


class SharedUrlSaver:
    """Deep routing module for platform-aware and generic URL preservation."""

    def __init__(
        self,
        settings: Settings,
        *,
        xhs_saver: XhsNoteSaver | None = None,
        page_saver: PageSaver | None = None,
    ) -> None:
        self._xhs_saver = xhs_saver or XhsNoteSaver(settings)
        self._page_saver = page_saver or WebPageSaver(settings)

    async def save(self, request: SharedUrlSaveRequest) -> dict[str, Any]:
        host = (urlsplit(request.url).hostname or "").lower()
        if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            return await self._xhs_saver.save(XhsNoteSaveRequest(url=request.url))
        platform = "boss" if host == "zhipin.com" or host.endswith(".zhipin.com") else "web"
        return await self._page_saver.save(request.url, platform=platform)


def build_shared_url_save_tool(saver: SharedUrlSaver) -> BaseTool:
    """Expose one stable URL-saving interface to the conversational Agent."""

    async def save_shared_url(url: str) -> dict[str, Any]:
        """Download and preserve material from one user-supplied public URL."""

        return await saver.save(SharedUrlSaveRequest(url=url))

    return StructuredTool.from_function(
        coroutine=save_shared_url,
        name="save_shared_url",
        description=(
            "Download and preserve material from a user-supplied public URL. Xiaohongshu and "
            "Boss URLs use their platform-aware logged-in readers. Other sites are attempted "
            "read-only and report login walls or access controls without bypassing them. Call "
            "this directly when the user supplies a URL; do not ask for company or role first."
        ),
        args_schema=SharedUrlSaveRequest,
    )


def _resource_filename(media_type: str) -> str:
    if media_type in {"text/html", "application/xhtml+xml"}:
        return "page.html"
    if media_type == "text/plain":
        return "content.txt"
    if media_type == "application/json":
        return "content.json"
    if media_type == "application/pdf":
        return "document.pdf"
    if media_type.startswith("image/"):
        subtype = media_type.removeprefix("image/").replace("jpeg", "jpg")
        return f"image.{subtype}" if subtype.isalnum() else "image.bin"
    return "content.bin"


def _read_bounded_content(response: Any) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > _MAX_RESOURCE_BYTES:
                return None
            chunks.append(bytes(chunk))
        return b"".join(chunks)
    finally:
        _close_response(response)


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
