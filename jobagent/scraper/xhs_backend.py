"""In-process adapter for the Spider_XHS library.

This module deliberately stays thin: Spider_XHS owns authentication, request
signing, HTTP sessions, search, detail normalization, retries, and media
downloads. JobAgent only provides an async boundary and maps results into stable
objects used by the Opportunity Journey workflow.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self, cast
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from jobagent.auth.cookie_manager import get_cookies
from jobagent.config import Settings
from jobagent.rate_limit import (
    AsyncRateLimiter,
    BlockingRateLimiter,
    PyrateAsyncRateLimiter,
    PyrateBlockingRateLimiter,
)

logger = logging.getLogger(__name__)

_XHS_BASE = "https://www.xiaohongshu.com"
_REQUIRED_COOKIE_NAMES = frozenset({"a1", "web_session"})
_IMAGE_INDEX_RE = re.compile(r"image_(\d+)", re.IGNORECASE)


def strip_xsec_token(url: str) -> str:
    """Remove the ephemeral xsec_token query param for model-facing output.

    architecture.md: ephemeral source tokens are never returned in Tool
    results. Internal fetch paths keep the full URL; only projections to
    the agent/user go through this.
    """

    parts = urlsplit(url)
    if "xsec_token" not in (parts.query or ""):
        return url
    kept = "&".join(
        piece
        for piece in parts.query.split("&")
        if piece and not piece.startswith("xsec_token=")
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, kept, parts.fragment))


class SpiderXhsError(RuntimeError):
    """Base error raised by the Spider_XHS adapter."""


class SpiderXhsDependencyError(SpiderXhsError):
    """Raised when Spider_XHS or one of its dependencies cannot be imported."""


class XhsAuthenticationError(SpiderXhsError):
    """Raised when no complete authenticated XHS cookie is available."""


class XhsBackend(Protocol):
    """Stable interface consumed by XHS source workflows."""

    async def start(self) -> None:
        """Initialize the backend."""

    async def close(self) -> None:
        """Close the backend."""

    async def search_notes(
        self,
        query: str,
        *,
        limit: int | None = None,
        sort: int = 0,
    ) -> list[XhsNoteReference]:
        """Search XHS image notes."""

    async def list_user_notes(
        self,
        user_id: str,
        *,
        limit: int | None = None,
    ) -> list[XhsNoteReference]:
        """List notes published by one XHS user."""

    async def fetch_note(self, url: str) -> XhsFetchedNote:
        """Fetch one note without downloading media."""

    async def download_note(
        self,
        url: str,
        *,
        output_dir: Path | None = None,
        fetched_note: XhsFetchedNote | None = None,
    ) -> DownloadedXhsNote:
        """Fetch and download one note."""

    async def fetch_note_comments(self, url: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class SpiderXhsBindings:
    """Imported Spider_XHS entry points, injectable for unit tests."""

    auth_class: type[Any]
    api_class: type[Any]
    handle_note_info: Callable[[dict[str, Any]], dict[str, Any]]
    download_note: Callable[[dict[str, Any], str, str], str]
    download_media: Callable[[str, str, str, str], None]


class RateLimitedHttpClient:
    """Transparent proxy that limits every real Spider_XHS HTTP request."""

    def __init__(self, client: Any, limiter: BlockingRateLimiter) -> None:
        self._client = client
        self._limiter = limiter

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self._limiter.acquire()
        return self._client.request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        self._limiter.acquire()
        return self._client.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        self._limiter.acquire()
        return self._client.post(url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        self._limiter.acquire()
        return self._client.put(url, **kwargs)

    def close(self) -> None:
        self._client.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


@dataclass(frozen=True, slots=True)
class XhsNoteReference:
    """A note returned by keyword search, including its required xsec token."""

    note_id: str
    url: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class XhsFetchedNote:
    """Normalized note detail plus the untouched platform response."""

    note_id: str
    url: str
    title: str
    body: str
    author_id: str
    author_name: str
    image_urls: tuple[str, ...]
    tags: tuple[str, ...]
    published_at: str | None
    normalized: dict[str, Any]
    raw_response: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DownloadedXhsNote:
    """A fetched note and the files produced by Spider_XHS."""

    note: XhsFetchedNote
    directory: Path
    body_path: Path
    images: tuple[Path, ...]
    raw_response_path: Path


def cookies_to_header(cookies: list[dict[str, Any]]) -> str:
    """Convert Playwright-format cookies to a Cookie request header."""

    pairs = []
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if name and value:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _cookie_names(cookie_header: str) -> set[str]:
    names: set[str] = set()
    for part in cookie_header.split(";"):
        name, separator, _ = part.strip().partition("=")
        if separator and name:
            names.add(name)
    return names


def load_spider_xhs_bindings(spider_path: Path) -> SpiderXhsBindings:
    """Import the supported Spider_XHS library entry points."""

    resolved = spider_path.expanduser().resolve()
    if not resolved.is_dir():
        raise SpiderXhsDependencyError(f"Spider_XHS path does not exist: {resolved}")

    path_text = str(resolved)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

    try:
        api_module = importlib.import_module("apis.xhs_pc_apis")
        auth_module = importlib.import_module("xhs_utils.xhs_pc")
        data_module = importlib.import_module("xhs_utils.data_util")
    except ImportError as exc:
        raise SpiderXhsDependencyError(
            f"Unable to import Spider_XHS from {resolved}: {exc}"
        ) from exc

    return SpiderXhsBindings(
        auth_class=api_attr(auth_module, "XHSPcAuth"),
        api_class=api_attr(api_module, "XHS_Apis"),
        handle_note_info=api_attr(data_module, "handle_note_info"),
        download_note=api_attr(data_module, "download_note"),
        download_media=api_attr(data_module, "download_media"),
    )


def api_attr(module: Any, name: str) -> Any:
    """Return one required Spider_XHS attribute with a useful error."""

    try:
        return getattr(module, name)
    except AttributeError as exc:
        raise SpiderXhsDependencyError(
            f"Spider_XHS module {module.__name__!r} has no {name!r}"
        ) from exc


class SpiderXhsBackend:
    """Use Spider_XHS directly inside the JobAgent process."""

    def __init__(
        self,
        settings: Settings,
        *,
        bindings: SpiderXhsBindings | None = None,
        api_limiter: BlockingRateLimiter | None = None,
        media_limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._bindings = bindings
        self._api: Any | None = None
        self._closing = False
        self._inflight: set[asyncio.Task[Any]] = set()
        self._request_lock = asyncio.Lock()
        self._api_limiter = api_limiter or PyrateBlockingRateLimiter(
            requests=settings.xhs_api_rate_requests,
            period_seconds=settings.xhs_api_rate_period_seconds,
            name="xhs-api",
        )
        self._media_limiter = media_limiter or PyrateAsyncRateLimiter(
            requests=settings.xhs_media_rate_requests,
            period_seconds=settings.xhs_media_rate_period_seconds,
            name="xhs-media",
        )

    @property
    def started(self) -> bool:
        """Whether authentication and Spider_XHS bootstrap completed."""

        return self._api is not None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Load Spider_XHS, authenticate, and bootstrap its PC API client."""

        if self.started:
            return
        self._closing = False

        bindings = self._bindings or load_spider_xhs_bindings(
            Path(self._settings.spider_xhs_path)
        )
        cookie_header = await self._load_cookie_header()
        missing = _REQUIRED_COOKIE_NAMES - _cookie_names(cookie_header)
        if missing:
            names = ", ".join(sorted(missing))
            raise XhsAuthenticationError(
                f"XHS cookie header is missing required fields: {names}. "
                "Provide XHS_COOKIE_HEADER or run `jobagent login --platform xhs`."
            )

        self._bindings = bindings
        await asyncio.to_thread(self._api_limiter.acquire)
        api = await asyncio.to_thread(
            self._bootstrap_sync,
            bindings,
            cookie_header,
        )
        api.http = RateLimitedHttpClient(api.http, self._api_limiter)
        self._api = api

    async def close(self) -> None:
        """Close Spider_XHS's reusable curl_cffi session."""

        self._closing = True
        async with self._request_lock:
            pending = tuple(self._inflight)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            api = self._api
            self._api = None
        http_client = getattr(api, "http", None) if api is not None else None
        close = getattr(http_client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)

    async def _run_sync(self, function: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking Spider_XHS call and let shutdown await its thread."""

        if self._closing:
            raise SpiderXhsError("SpiderXhsBackend is closing")
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._inflight.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(self._forget_inflight)
            raise
        finally:
            if task.done():
                self._inflight.discard(task)

    def _forget_inflight(self, task: asyncio.Task[Any]) -> None:
        self._inflight.discard(task)

    async def search_notes(
        self,
        query: str,
        *,
        limit: int | None = None,
        sort: int = 0,
    ) -> list[XhsNoteReference]:
        """Search image notes through Spider_XHS and retain raw result items."""

        self._ensure_started()
        requested = limit or self._settings.xhs_referral_max_posts
        async with self._request_lock:
            return cast(
                "list[XhsNoteReference]",
                await self._run_sync(self._search_notes_sync, query, requested, sort),
            )

    async def list_user_notes(
        self,
        user_id: str,
        *,
        limit: int | None = None,
    ) -> list[XhsNoteReference]:
        """List a user's notes through Spider_XHS's paginated user API."""

        self._ensure_started()
        requested = limit or self._settings.xhs_referral_max_posts
        async with self._request_lock:
            return cast(
                "list[XhsNoteReference]",
                await self._run_sync(
                    self._list_user_notes_sync,
                    user_id.strip(),
                    requested,
                ),
            )

    async def fetch_note(self, url: str) -> XhsFetchedNote:
        """Fetch and normalize one note using Spider_XHS functions."""

        self._ensure_started()
        async with self._request_lock:
            return cast(
                "XhsFetchedNote", await self._run_sync(self._fetch_note_sync, url)
            )

    async def download_note(
        self,
        url: str,
        *,
        output_dir: Path | None = None,
        fetched_note: XhsFetchedNote | None = None,
    ) -> DownloadedXhsNote:
        """Fetch a note and let Spider_XHS save its body and all images."""

        self._ensure_started()
        logger.info("xhs.progress [1/4] 获取帖子详情")
        note = fetched_note or await self.fetch_note(url)
        destination = (output_dir or Path(self._settings.xhs_download_dir)).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        async with self._request_lock:
            logger.info("xhs.progress [2/4] 保存帖子元数据")
            directory = await self._run_sync(
                self._download_note_metadata_sync,
                note,
                destination,
            )
        for index, image_url in enumerate(note.image_urls):
            logger.info("xhs.progress [3/4] 下载图片 %s/%s", index + 1, len(note.image_urls))
            await self._media_limiter.acquire()
            async with self._request_lock:
                await self._run_sync(
                    self._download_image_sync,
                    directory,
                    index,
                    image_url,
                )
        return self._build_downloaded_note(note, directory)

    async def fetch_note_comments(self, url: str) -> list[dict[str, Any]]:
        """Read Spider_XHS's first comment page; never unboundedly crawl comments."""

        self._ensure_started()
        async with self._request_lock:
            result = await self._run_sync(self._fetch_note_comments_sync, url)
            return cast(list[dict[str, Any]], result)

    def _fetch_note_comments_sync(self, url: str) -> list[dict[str, Any]]:
        api = cast(Any, self._api)
        parsed = urlsplit(url)
        note_id = parsed.path.rstrip("/").split("/")[-1]
        token = (parse_qs(parsed.query).get("xsec_token") or [""])[0]
        success, message, response = api.get_note_out_comment(note_id, "", token)
        if not success:
            raise SpiderXhsError(f"Spider_XHS comment fetch failed: {message}")
        data = response.get("data", {}) if isinstance(response, dict) else {}
        comments = data.get("comments", []) if isinstance(data, dict) else []
        return list(comments) if isinstance(comments, list) else []

    async def _load_cookie_header(self) -> str:
        configured = self._settings.xhs_cookie_header
        if configured:
            return configured.strip()

        cookies = await get_cookies("xhs", self._settings)
        return cookies_to_header(cookies)

    @staticmethod
    def _bootstrap_sync(bindings: SpiderXhsBindings, cookie_header: str) -> Any:
        auth = bindings.auth_class.from_cookie(cookie_header)
        return bindings.api_class(auth).bootstrap()

    def _search_notes_sync(
        self,
        query: str,
        limit: int,
        sort: int,
    ) -> list[XhsNoteReference]:
        api = cast(Any, self._api)
        success, message, items = api.search_some_note(
            query=query,
            require_num=limit,
            sort_type_choice=sort,
            note_type=2,
        )
        if not success:
            raise SpiderXhsError(f"Spider_XHS search failed: {message}")

        references: list[XhsNoteReference] = []
        for raw_item in items or []:
            if raw_item.get("model_type") != "note":
                continue
            note_id = str(raw_item.get("id") or "").strip()
            if not note_id:
                continue
            params = urlencode(
                {
                    "xsec_token": raw_item.get("xsec_token") or "",
                    "xsec_source": "pc_search",
                }
            )
            references.append(
                XhsNoteReference(
                    note_id=note_id,
                    url=f"{_XHS_BASE}/explore/{note_id}?{params}",
                    raw=dict(raw_item),
                )
            )
        return references

    def _list_user_notes_sync(
        self,
        user_id: str,
        limit: int,
    ) -> list[XhsNoteReference]:
        if not user_id:
            raise ValueError("XHS user_id cannot be empty")

        api = cast(Any, self._api)
        references: list[XhsNoteReference] = []
        cursor = ""
        while len(references) < limit:
            success, message, response = api.get_user_note_info(
                user_id,
                cursor,
                "",
                "pc_user",
            )
            if not success:
                raise SpiderXhsError(f"Spider_XHS user note listing failed: {message}")
            try:
                data = response["data"]
                items = data.get("notes", [])
            except (KeyError, TypeError) as exc:
                raise SpiderXhsError(
                    "Spider_XHS user note response has no note list"
                ) from exc

            for raw_item in items:
                note_id = str(raw_item.get("note_id") or raw_item.get("id") or "").strip()
                if not note_id:
                    continue
                params = urlencode(
                    {
                        "xsec_token": raw_item.get("xsec_token") or "",
                        "xsec_source": "pc_user",
                    }
                )
                references.append(
                    XhsNoteReference(
                        note_id=note_id,
                        url=f"{_XHS_BASE}/explore/{note_id}?{params}",
                        raw=dict(raw_item),
                    )
                )
                if len(references) >= limit:
                    break

            next_cursor = str(data.get("cursor") or "")
            if not data.get("has_more") or not items or next_cursor == cursor:
                break
            cursor = next_cursor

        return references

    def _fetch_note_sync(self, url: str) -> XhsFetchedNote:
        bindings = cast(SpiderXhsBindings, self._bindings)
        api = cast(Any, self._api)
        success, message, raw_response = api.get_note_info(url)
        if not success:
            raise SpiderXhsError(f"Spider_XHS note fetch failed: {message}")
        if not isinstance(raw_response, dict):
            raise SpiderXhsError("Spider_XHS note response is not a JSON object")

        try:
            item = dict(raw_response["data"]["items"][0])
        except (KeyError, IndexError, TypeError) as exc:
            raise SpiderXhsError("Spider_XHS note response contains no detail item") from exc

        item["url"] = url
        normalized = bindings.handle_note_info(item)
        return self._to_fetched_note(url, normalized, raw_response)

    def _download_note_metadata_sync(
        self,
        note: XhsFetchedNote,
        destination: Path,
    ) -> Path:
        bindings = cast(SpiderXhsBindings, self._bindings)
        saved = bindings.download_note(note.normalized, str(destination), "metadata")
        return Path(saved).resolve()

    def _download_image_sync(self, directory: Path, index: int, url: str) -> None:
        bindings = cast(SpiderXhsBindings, self._bindings)
        bindings.download_media(str(directory), f"image_{index}", url, "image")

    def _build_downloaded_note(
        self,
        note: XhsFetchedNote,
        directory: Path,
    ) -> DownloadedXhsNote:
        raw_path = directory / "raw_response.json"
        self._write_json_atomic(raw_path, note.raw_response)

        body_path = directory / "detail.txt"
        images = tuple(sorted(directory.glob("image_*"), key=self._image_sort_key))
        return DownloadedXhsNote(
            note=note,
            directory=directory,
            body_path=body_path,
            images=images,
            raw_response_path=raw_path,
        )

    @staticmethod
    def _to_fetched_note(
        url: str,
        normalized: Mapping[str, Any],
        raw_response: dict[str, Any],
    ) -> XhsFetchedNote:
        images = tuple(str(value) for value in normalized.get("image_list", []) if value)
        tags = tuple(str(value) for value in normalized.get("tags", []) if value)
        return XhsFetchedNote(
            note_id=str(normalized.get("note_id") or ""),
            url=url,
            title=str(normalized.get("title") or ""),
            body=str(normalized.get("desc") or ""),
            author_id=str(normalized.get("user_id") or ""),
            author_name=str(normalized.get("nickname") or ""),
            image_urls=images,
            tags=tags,
            published_at=(
                str(normalized["upload_time"]) if normalized.get("upload_time") else None
            ),
            normalized=dict(normalized),
            raw_response=raw_response,
        )

    @staticmethod
    def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _image_sort_key(path: Path) -> tuple[int, str]:
        match = _IMAGE_INDEX_RE.search(path.stem)
        return (int(match.group(1)) if match else sys.maxsize, path.name)

    def _ensure_started(self) -> None:
        if not self.started:
            raise SpiderXhsError(
                "SpiderXhsBackend is not started; call start() or use `async with`."
            )
