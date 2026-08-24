"""Read-only Chrome CDP fallback for Xiaohongshu note and author reads.

Spider_XHS remains the primary transport for search, detail fetches, author
listing, media downloads, and rate limiting. This module only provides a
bounded, read-only fallback that reuses an already-running, already-logged-in
Chrome session exposed through the DevTools protocol, and only when the
primary backend fails with a token/access-control/browser-context error.

Safety boundaries:
- never launches a browser, opens a login flow, or reads/prints cookies;
- connects to a loopback DevTools endpoint only, on demand;
- closes only adapter-owned pages and disconnects only its own connection,
  never the user's browser, contexts, or unrelated pages;
- attempts the fallback at most once per operation and never retries
  arbitrary failures, rotates proxies, solves CAPTCHAs, or evades
  fingerprinting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlencode, urlsplit

from jobagent.config import Settings
from jobagent.observability import log_decision
from jobagent.scraper.xhs_backend import (
    DownloadedXhsNote,
    SpiderXhsBackend,
    SpiderXhsError,
    XhsAuthenticationError,
    XhsFetchedNote,
    XhsNoteReference,
)

logger = logging.getLogger(__name__)

_XHS_BASE = "https://www.xiaohongshu.com"
_XHS_NOTE_HOSTS = frozenset({"xiaohongshu.com", "www.xiaohongshu.com"})

# Bounded page-level signals; no reliance on a single fragile selector.
_LOGIN_SELECTORS = (".login-container", ".login-modal", ".login-mask")
_RISK_URL_SIGNALS = ("captcha", "verify")

# Primary-failure messages that classify as token/access-control/context
# errors and therefore justify one CDP fallback attempt.
_ACCESS_CONTROL_SIGNALS = (
    "token",
    "xsec",
    "登录",
    "过期",
    "失效",
    "无权",
    "权限",
    "访问受限",
    "unauthorized",
    "forbidden",
    "authentication",
    "login",
    "401",
    "403",
    "461",
)

_NOTE_STATE_READY = (
    "() => { const s = window.__INITIAL_STATE__; "
    "return !!s && !!s.note && s.note.noteDetailMap "
    "&& Object.keys(s.note.noteDetailMap).length > 0; }"
)
_USER_STATE_READY = (
    "() => { const s = window.__INITIAL_STATE__; "
    "return !!s && !!s.user && Array.isArray(s.user.notes); }"
)


class XhsCdpError(SpiderXhsError):
    """Base error for the read-only CDP fallback."""


class XhsCdpUnavailableError(XhsCdpError):
    """The CDP fallback cannot be used (disabled, unreachable, or no XHS page)."""


class XhsCdpBlockedError(XhsCdpError):
    """The browser session shows login, risk-control, or private content."""


def is_access_control_failure(error: BaseException) -> bool:
    """Classify one primary-backend failure as a token/access-control error."""

    if isinstance(error, XhsAuthenticationError):
        return True
    if not isinstance(error, SpiderXhsError):
        return False
    message = str(error).lower()
    return any(signal in message for signal in _ACCESS_CONTROL_SIGNALS)


class CdpSession(Protocol):
    """Adapter-owned connection to an already-running Chrome."""

    async def new_xhs_page(self) -> Any: ...

    async def disconnect(self) -> None: ...


class PlaywrightCdpSession:
    """Wrap one CDP connection and the context holding an XHS page.

    ``disconnect`` only tears down this adapter's connection; the user's
    Chrome process, its contexts, and its pre-existing pages keep running.
    """

    def __init__(self, browser: Any, context: Any, driver: Any) -> None:
        self._browser = browser
        self._context = context
        self._driver = driver

    async def new_xhs_page(self) -> Any:
        """Open a new page inside the already-logged-in XHS context."""

        return await self._context.new_page()

    async def disconnect(self) -> None:
        """Disconnect the CDP connection without closing the user's Chrome."""

        try:
            # For a connect_over_cdp() browser this only disconnects; it does
            # not terminate the externally-owned Chrome process.
            await self._browser.close()
        finally:
            if self._driver is not None:
                await self._driver.stop()


def _find_xhs_context(browser: Any) -> Any | None:
    """Return the first context that already holds a xiaohongshu.com page."""

    for context in browser.contexts:
        for page in context.pages:
            host = (urlsplit(getattr(page, "url", "") or "").hostname or "").lower()
            if host in _XHS_NOTE_HOSTS or host.endswith(".xiaohongshu.com"):
                return context
    return None


async def connect_over_cdp(endpoint: str, timeout_seconds: int) -> PlaywrightCdpSession:
    """Connect to an already-running Chrome through its DevTools endpoint."""

    from playwright.async_api import async_playwright

    driver = await async_playwright().start()
    try:
        browser = await driver.chromium.connect_over_cdp(
            endpoint,
            timeout=timeout_seconds * 1000,
        )
    except Exception:
        await driver.stop()
        raise
    context = _find_xhs_context(browser)
    if context is None:
        try:
            await browser.close()
        finally:
            await driver.stop()
        raise XhsCdpUnavailableError(
            "Chrome CDP endpoint has no open xiaohongshu.com page/context"
        )
    return PlaywrightCdpSession(browser, context, driver)


class XhsCdpBackend:
    """Narrowly scoped, read-only XHS reads through a logged-in Chrome session.

    Only ``fetch_note`` and ``list_user_notes`` are implemented; keyword
    search and media downloads stay on the primary Spider_XHS backend.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        connect: Callable[[str, int], Awaitable[CdpSession]] | None = None,
        decision_logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._connect = connect or connect_over_cdp
        self._logger = decision_logger or logger
        self._session: CdpSession | None = None
        self._session_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """Whether the CDP fallback is switched on in settings."""

        return bool(self._settings.xhs_cdp_enabled)

    async def start(self) -> None:
        """Connections are lazy: only a fallback invocation connects."""

    async def close(self) -> None:
        """Disconnect only the adapter-owned CDP connection."""

        session = self._session
        self._session = None
        if session is not None:
            await session.disconnect()

    async def search_notes(
        self,
        query: str,
        *,
        limit: int | None = None,
        sort: int = 0,
    ) -> list[XhsNoteReference]:
        raise XhsCdpError("CDP fallback does not support keyword search")

    async def download_note(
        self,
        url: str,
        *,
        output_dir: Path | None = None,
        fetched_note: XhsFetchedNote | None = None,
    ) -> DownloadedXhsNote:
        raise XhsCdpError("CDP fallback does not download media; Spider_XHS owns downloads")

    async def fetch_note(self, url: str) -> XhsFetchedNote:
        """Read one note detail through the logged-in browser session."""

        note_id = _note_id_from_url(url)
        state = await self._load_page_state(url, _NOTE_STATE_READY)
        normalized = _normalize_note_detail(state, note_id)
        raw_response = {
            "note_id": normalized["note_id"],
            "title": normalized["title"],
            "liked_count": normalized["liked_count"],
            "collected_count": normalized["collected_count"],
            "comment_count": normalized["comment_count"],
            "share_count": normalized["share_count"],
            "image_count": len(normalized["image_list"]),
            "tag_list": list(normalized["tags"]),
            "source": "chrome_cdp_page_state",
        }
        return SpiderXhsBackend._to_fetched_note(url, normalized, raw_response)

    async def list_user_notes(
        self,
        user_id: str,
        *,
        limit: int | None = None,
    ) -> list[XhsNoteReference]:
        """Read a bounded list of public note references from a profile page."""

        if not user_id:
            raise ValueError("XHS user_id cannot be empty")
        bounded = limit or self._settings.xhs_referral_max_posts
        state = await self._load_page_state(
            f"{_XHS_BASE}/user/profile/{user_id}",
            _USER_STATE_READY,
        )
        return _note_references_from_user_state(state, bounded)

    async def _ensure_session(self) -> CdpSession:
        if self._session is not None:
            return self._session
        async with self._session_lock:
            if self._session is not None:
                return self._session
            basis = {"endpoint": self._settings.xhs_cdp_endpoint}
            try:
                session = await self._connect(
                    self._settings.xhs_cdp_endpoint,
                    self._settings.xhs_cdp_timeout_seconds,
                )
            except Exception as exc:
                log_decision(
                    self._logger,
                    "xhs.cdp.connection",
                    basis={**basis, "error_type": type(exc).__name__},
                    outcome="unavailable",
                )
                if isinstance(exc, XhsCdpError):
                    raise
                raise XhsCdpUnavailableError(
                    "Chrome CDP endpoint is unreachable"
                ) from exc
            log_decision(self._logger, "xhs.cdp.connection", basis=basis, outcome="connected")
            self._session = session
            return session

    async def _load_page_state(self, url: str, ready_expression: str) -> dict[str, Any]:
        if not self.enabled:
            raise XhsCdpUnavailableError("XHS CDP fallback is disabled")
        session = await self._ensure_session()
        timeout_ms = self._settings.xhs_cdp_timeout_seconds * 1000
        page = await session.new_xhs_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_function(ready_expression, timeout=timeout_ms)
            except Exception as exc:
                raise XhsCdpBlockedError(
                    "page did not expose its data in the browser session"
                ) from exc
            await self._ensure_publicly_readable(page)
            state = await page.evaluate("() => window.__INITIAL_STATE__ || null")
            if not isinstance(state, dict):
                raise XhsCdpBlockedError("page state is unavailable in the browser session")
            return state
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _ensure_publicly_readable(self, page: Any) -> None:
        """Reject login, risk-control, and private-content pages explicitly."""

        current_url = str(getattr(page, "url", "") or "").lower()
        if any(signal in current_url for signal in _RISK_URL_SIGNALS):
            raise XhsCdpBlockedError("browser session hit a risk-control page")
        for selector in _LOGIN_SELECTORS:
            element = await page.query_selector(selector)
            if element is not None and await element.is_visible():
                raise XhsCdpBlockedError("browser session requires login")


class FallbackXhsBackend:
    """Primary Spider_XHS transport with a bounded one-shot CDP fallback.

    The primary backend always runs first. The CDP fallback is attempted at
    most once per operation and only for classified token/access-control
    failures; every other exception propagates unchanged.
    """

    def __init__(
        self,
        primary: Any,
        fallback: XhsCdpBackend,
        *,
        decision_logger: logging.Logger | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._logger = decision_logger or logger

    async def start(self) -> None:
        """Start the primary backend; CDP connects lazily on fallback."""

        await self._primary.start()

    async def close(self) -> None:
        """Close the primary backend and disconnect the adapter-owned CDP link."""

        try:
            await self._primary.close()
        finally:
            await self._fallback.close()

    async def __aenter__(self) -> FallbackXhsBackend:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def search_notes(
        self,
        query: str,
        *,
        limit: int | None = None,
        sort: int = 0,
    ) -> list[XhsNoteReference]:
        """Keyword search stays on the primary backend; no CDP fallback."""

        return cast(
            "list[XhsNoteReference]",
            await self._run(
                "search_notes",
                lambda: self._primary.search_notes(query, limit=limit, sort=sort),
            ),
        )

    async def list_user_notes(
        self,
        user_id: str,
        *,
        limit: int | None = None,
    ) -> list[XhsNoteReference]:
        return cast(
            "list[XhsNoteReference]",
            await self._run(
                "list_user_notes",
                lambda: self._primary.list_user_notes(user_id, limit=limit),
                lambda: self._fallback.list_user_notes(user_id, limit=limit),
            ),
        )

    async def fetch_note(self, url: str) -> XhsFetchedNote:
        return cast(
            "XhsFetchedNote",
            await self._run(
                "fetch_note",
                lambda: self._primary.fetch_note(url),
                lambda: self._fallback.fetch_note(url),
            ),
        )

    async def download_note(
        self,
        url: str,
        *,
        output_dir: Path | None = None,
        fetched_note: XhsFetchedNote | None = None,
    ) -> DownloadedXhsNote:
        """Fetch (with fallback) through the primary downloader and OCR path."""

        note = fetched_note or await self.fetch_note(url)
        return cast(
            "DownloadedXhsNote",
            await self._primary.download_note(
                url, output_dir=output_dir, fetched_note=note
            ),
        )

    async def _run(
        self,
        operation: str,
        primary_call: Callable[[], Awaitable[Any]],
        fallback_call: Callable[[], Awaitable[Any]] | None = None,
    ) -> Any:
        try:
            result = await primary_call()
        except Exception as exc:
            if fallback_call is None or not is_access_control_failure(exc):
                raise
            return await self._fallback_once(operation, exc, fallback_call)
        log_decision(
            self._logger,
            "xhs.backend_route",
            basis={"operation": operation},
            outcome="primary",
        )
        return result

    async def _fallback_once(
        self,
        operation: str,
        primary_error: Exception,
        fallback_call: Callable[[], Awaitable[Any]],
    ) -> Any:
        error_type = type(primary_error).__name__
        if not getattr(self._fallback, "enabled", True):
            log_decision(
                self._logger,
                "xhs.cdp.fallback",
                basis={"operation": operation, "error_type": error_type, "reason": "disabled"},
                outcome="skipped",
            )
            raise primary_error
        log_decision(
            self._logger,
            "xhs.cdp.fallback",
            basis={"operation": operation, "error_type": error_type},
            outcome="attempted",
        )
        try:
            result = await fallback_call()
        except Exception as fallback_error:
            log_decision(
                self._logger,
                "xhs.cdp.fallback",
                basis={
                    "operation": operation,
                    "error_type": type(fallback_error).__name__,
                },
                outcome="failed",
            )
            # A failed fallback must not become a false success: surface the
            # original primary error so existing blocked/unavailable mapping
            # keeps working.
            raise primary_error from fallback_error
        log_decision(
            self._logger,
            "xhs.cdp.fallback",
            basis={"operation": operation, "error_type": error_type},
            outcome="succeeded",
        )
        log_decision(
            self._logger,
            "xhs.backend_route",
            basis={"operation": operation},
            outcome="cdp_fallback",
        )
        return result


def build_xhs_backend(settings: Settings) -> Any:
    """Compose the Spider_XHS backend with the optional CDP fallback.

    With ``XHS_CDP_ENABLED=false`` (the default) this returns the plain
    SpiderXhsBackend so existing behavior is untouched.
    """

    primary = SpiderXhsBackend(settings)
    if not settings.xhs_cdp_enabled:
        return primary
    return FallbackXhsBackend(primary, XhsCdpBackend(settings))


def _note_id_from_url(url: str) -> str:
    parts = tuple(part for part in urlsplit(url).path.split("/") if part)
    return parts[-1] if parts else ""


def _format_timestamp(milliseconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(milliseconds / 1000))


def _image_url(image: Any) -> str | None:
    if not isinstance(image, dict):
        return None
    info_list = image.get("infoList") or image.get("info_list") or []
    if isinstance(info_list, list):
        for info in reversed(info_list):
            if isinstance(info, dict) and info.get("url"):
                return str(info["url"])
    fallback = image.get("urlDefault") or image.get("url_default") or image.get("url")
    return str(fallback) if fallback else None


def _normalize_note_detail(state: dict[str, Any], note_id: str) -> dict[str, Any]:
    """Normalize one note's browser page state into the Spider_XHS shape."""

    note_section = state.get("note") or {}
    note_map = note_section.get("noteDetailMap") or {}
    if not isinstance(note_map, dict):
        raise XhsCdpBlockedError("note detail is not visible in the browser session")
    detail = note_map.get(note_id)
    if not isinstance(detail, dict):
        detail = note_map.get(note_section.get("currentNoteId") or "")
    if not isinstance(detail, dict) and len(note_map) == 1:
        detail = next(iter(note_map.values()))
    if not isinstance(detail, dict):
        raise XhsCdpBlockedError("note detail is not visible in the browser session")
    inner_note = detail.get("note")
    note: dict[str, Any] = inner_note if isinstance(inner_note, dict) else detail

    user = note.get("user") or {}
    interact = note.get("interactInfo") or note.get("interact_info") or {}
    images = [url for url in (_image_url(item) for item in note.get("imageList") or []) if url]
    tags = [
        str(tag.get("name"))
        for tag in note.get("tagList") or []
        if isinstance(tag, dict) and tag.get("name")
    ]
    is_video = str(note.get("type") or "").lower() == "video"
    video_addr = None
    if is_video:
        video = note.get("video") or {}
        streams = ((video.get("media") or {}).get("stream") or {}).get("h264") or []
        if isinstance(streams, list) and streams:
            stream = streams[0] if isinstance(streams[0], dict) else {}
            video_addr = (
                stream.get("masterUrl")
                or stream.get("master_url")
                or stream.get("url")
            )
        if not video_addr:
            consumer = video.get("consumer") or {}
            origin_key = consumer.get("originVideoKey") or consumer.get("origin_video_key")
            if origin_key:
                video_addr = f"https://sns-video-bd.xhscdn.com/{origin_key}"
    raw_time = note.get("time")
    upload_time = (
        _format_timestamp(float(raw_time))
        if isinstance(raw_time, (int, float)) and raw_time > 0
        else None
    )
    user_id = str(user.get("userId") or user.get("user_id") or "")
    return {
        "note_id": str(note.get("noteId") or note.get("note_id") or note_id),
        "note_url": "",
        "note_type": "视频" if is_video else "图集",
        "user_id": user_id,
        "home_url": f"{_XHS_BASE}/user/profile/{user_id}" if user_id else "",
        "nickname": str(user.get("nickname") or ""),
        "avatar": str(user.get("avatar") or ""),
        "title": str(note.get("title") or "") or "无标题",
        "desc": str(note.get("desc") or ""),
        "liked_count": str(interact.get("likedCount") or interact.get("liked_count") or ""),
        "collected_count": str(
            interact.get("collectedCount") or interact.get("collected_count") or ""
        ),
        "comment_count": str(
            interact.get("commentCount") or interact.get("comment_count") or ""
        ),
        "share_count": str(interact.get("shareCount") or interact.get("share_count") or ""),
        "video_cover": images[0] if is_video and images else None,
        "video_addr": video_addr,
        "image_list": images,
        "tags": tags,
        "upload_time": upload_time or "",
        "ip_location": str(note.get("ipLocation") or note.get("ip_location") or "未知"),
    }


def _note_references_from_user_state(
    state: dict[str, Any],
    limit: int,
) -> list[XhsNoteReference]:
    """Build bounded note references from a profile page's browser state."""

    user_section = state.get("user") or {}
    notes = user_section.get("notes") or []
    if not isinstance(notes, list):
        raise XhsCdpBlockedError("author note list is not visible in the browser session")
    references: list[XhsNoteReference] = []
    for item in notes:
        if not isinstance(item, dict):
            continue
        note_id = str(item.get("noteId") or item.get("note_id") or "").strip()
        if not note_id:
            continue
        # The xsec token stays in memory only; decision logs redact it.
        token = str(item.get("xsecToken") or item.get("xsec_token") or "")
        params = urlencode({"xsec_token": token, "xsec_source": "pc_user"})
        references.append(
            XhsNoteReference(
                note_id=note_id,
                url=f"{_XHS_BASE}/explore/{note_id}?{params}",
                raw={
                    "note_id": note_id,
                    "display_title": str(
                        item.get("displayTitle") or item.get("display_title") or ""
                    ),
                    "type": str(item.get("type") or ""),
                },
            )
        )
        if len(references) >= limit:
            break
    return references
