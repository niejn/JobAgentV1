"""Offline tests for the XHS Chrome CDP fallback backend.

No network, no Chrome, no real XHS requests: the CDP transport is replaced
with recording fakes. These tests pin the composition contract (primary
first, one classified fallback attempt), permission boundaries (never close
externally-owned browsers), failure classification, settings validation, and
the xhs.backend_route / xhs.cdp.connection / xhs.cdp.fallback decisions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jobagent.config import Settings
from jobagent.observability import begin_trace, current_trace_id, reset_trace
from jobagent.scraper.xhs_backend import (
    DownloadedXhsNote,
    SpiderXhsError,
    XhsAuthenticationError,
    XhsFetchedNote,
    XhsNoteReference,
)
from jobagent.scraper.xhs_cdp import (
    FallbackXhsBackend,
    PlaywrightCdpSession,
    XhsCdpBackend,
    XhsCdpBlockedError,
    XhsCdpUnavailableError,
    build_xhs_backend,
    is_access_control_failure,
)

CDP_LOGGER = "jobagent.scraper.xhs_cdp"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "xhs_cdp_enabled": True,
        "debug_chrome_cdp_endpoint": "http://127.0.0.1:9222",
        "xhs_cdp_timeout_seconds": 5,
    }
    values.update(overrides)
    return Settings(**values)


def _note_state() -> dict[str, Any]:
    return {
        "note": {
            "currentNoteId": "note-1",
            "noteDetailMap": {
                "note-1": {
                    "note": {
                        "noteId": "note-1",
                        "type": "normal",
                        "title": "字节后端一面",
                        "desc": "面试正文",
                        "user": {"userId": "user-1", "nickname": "测试作者"},
                        "interactInfo": {
                            "likedCount": "12",
                            "collectedCount": "3",
                            "commentCount": "4",
                            "shareCount": "1",
                        },
                        "imageList": [
                            {"infoList": [{"url": "http://img.example/0.webp"}]},
                            {"urlDefault": "https://img.example/1.webp"},
                        ],
                        "tagList": [{"name": "面经"}],
                        "time": 1_700_000_000_000,
                        "ipLocation": "上海",
                    }
                }
            },
        }
    }


def _user_state(token_suffix: str = "") -> dict[str, Any]:
    return {
        "user": {
            "notes": [
                {
                    "noteId": f"note-a{token_suffix}",
                    "xsecToken": f"SECRET_TOKEN_A{token_suffix}",
                    "displayTitle": "笔记A",
                    "type": "normal",
                },
                {
                    "noteId": "note-b",
                    "xsecToken": "SECRET_TOKEN_B",
                    "displayTitle": "笔记B",
                    "type": "video",
                },
                {
                    "noteId": "note-c",
                    "xsecToken": "SECRET_TOKEN_C",
                    "displayTitle": "笔记C",
                    "type": "normal",
                },
            ]
        }
    }


class FakeElement:
    def __init__(self, visible: bool = True) -> None:
        self._visible = visible

    async def is_visible(self) -> bool:
        return self._visible


class FakeCdpPage:
    def __init__(
        self,
        state: dict[str, Any] | None,
        *,
        url: str = "https://www.xiaohongshu.com/explore/note-1",
        login_element: FakeElement | None = None,
    ) -> None:
        self.url = url
        self._state = state
        self._login_element = login_element
        self.closed = False
        self.visited: list[str] = []

    async def goto(self, url: str, *, wait_until: str = "load", timeout: int = 0) -> None:
        self.visited.append(url)
        self.url = url

    async def wait_for_function(self, expression: str, *, timeout: int = 0) -> None:
        if self._state is None:
            raise TimeoutError("state never became ready")

    async def evaluate(self, expression: str) -> Any:
        if "__INITIAL_STATE__" in expression:
            return self._state
        return None

    async def query_selector(self, selector: str) -> FakeElement | None:
        return self._login_element

    async def close(self) -> None:
        self.closed = True


class FakeCdpSession:
    """Records adapter behavior; the external browser stays untouched."""

    def __init__(self, page: FakeCdpPage) -> None:
        self._page = page
        self.pages_created = 0
        self.disconnected = False
        self.external_browser_closed = False
        self.external_context_closed = False

    async def new_xhs_page(self) -> FakeCdpPage:
        self.pages_created += 1
        return self._page

    async def disconnect(self) -> None:
        self.disconnected = True

    def close_external_browser(self) -> None:
        """Only a buggy adapter would ever call this."""

        self.external_browser_closed = True


class FakePrimaryBackend:
    def __init__(
        self,
        *,
        list_error: Exception | None = None,
        fetch_error: Exception | None = None,
        search_error: Exception | None = None,
    ) -> None:
        self._list_error = list_error
        self._fetch_error = fetch_error
        self._search_error = search_error
        self.list_calls = 0
        self.fetch_calls = 0
        self.download_calls = 0
        self.downloaded_note_arg: XhsFetchedNote | None = None
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def search_notes(
        self, query: str, *, limit: int | None = None, sort: int = 0
    ) -> list[XhsNoteReference]:
        if self._search_error is not None:
            raise self._search_error
        return []

    async def list_user_notes(
        self, user_id: str, *, limit: int | None = None
    ) -> list[XhsNoteReference]:
        self.list_calls += 1
        if self._list_error is not None:
            raise self._list_error
        return []

    async def fetch_note(self, url: str) -> XhsFetchedNote:
        self.fetch_calls += 1
        if self._fetch_error is not None:
            raise self._fetch_error
        return SpiderXhsBackend_stub_note(url)

    async def download_note(
        self,
        url: str,
        *,
        output_dir: Path | None = None,
        fetched_note: XhsFetchedNote | None = None,
    ) -> DownloadedXhsNote:
        self.download_calls += 1
        self.downloaded_note_arg = fetched_note
        note = fetched_note or SpiderXhsBackend_stub_note(url)
        return DownloadedXhsNote(
            note=note,
            directory=Path("data/xhs/fake"),
            body_path=Path("data/xhs/fake/detail.txt"),
            images=(),
            raw_response_path=Path("data/xhs/fake/raw_response.json"),
        )


def SpiderXhsBackend_stub_note(url: str) -> XhsFetchedNote:  # noqa: N802
    return XhsFetchedNote(
        note_id="note-1",
        url=url,
        title="主通道标题",
        body="主通道正文",
        author_id="user-1",
        author_name="主通道作者",
        image_urls=("https://img.example/0.webp",),
        tags=("面经",),
        published_at="2025-01-02 03:04:05",
        normalized={},
        raw_response={},
    )


def _decisions(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [
        (record.decision_name, record.outcome)
        for record in caplog.records
        if getattr(record, "decision_name", None)
    ]


def _cdp_backend(
    settings: Settings,
    page: FakeCdpPage,
    *,
    connect_error: Exception | None = None,
) -> tuple[XhsCdpBackend, FakeCdpSession]:
    session = FakeCdpSession(page)

    async def fake_connect(endpoint: str, timeout_seconds: int) -> FakeCdpSession:
        if connect_error is not None:
            raise connect_error
        return session

    return XhsCdpBackend(settings, connect=fake_connect), session


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (XhsAuthenticationError("missing a1"), True),
        (SpiderXhsError("Spider_XHS note fetch failed: 登录已过期"), True),
        (SpiderXhsError("invalid xsec token"), True),
        (SpiderXhsError("Spider_XHS note fetch failed: 461"), True),
        (SpiderXhsError("Spider_XHS note fetch failed: connection reset"), False),
        (RuntimeError("token expired"), False),
        (TimeoutError("login page timeout"), False),
    ],
)
def test_is_access_control_failure(error: Exception, expected: bool) -> None:
    assert is_access_control_failure(error) is expected


# ---------------------------------------------------------------------------
# Composition: primary first, one classified fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_success_never_invokes_cdp(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=CDP_LOGGER)
    settings = _settings()
    page = FakeCdpPage(_note_state())
    cdp, session = _cdp_backend(settings, page)
    primary = FakePrimaryBackend()
    backend = FallbackXhsBackend(primary, cdp)

    note = await backend.fetch_note("https://www.xiaohongshu.com/explore/note-1")

    assert note.title == "主通道标题"
    assert primary.fetch_calls == 1
    assert session.pages_created == 0
    assert ("xhs.backend_route", "primary") in _decisions(caplog)
    await backend.close()


@pytest.mark.asyncio
async def test_classified_failure_invokes_cdp_once_and_returns_note(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=CDP_LOGGER)
    settings = _settings()
    page = FakeCdpPage(_note_state())
    cdp, session = _cdp_backend(settings, page)
    primary = FakePrimaryBackend(fetch_error=XhsAuthenticationError("cookie expired"))
    backend = FallbackXhsBackend(primary, cdp)
    trace_token = begin_trace()
    trace_id = current_trace_id()
    try:
        note = await backend.fetch_note(
            "https://www.xiaohongshu.com/explore/note-1?xsec_token=abc&xsec_source=pc_search"
        )
    finally:
        reset_trace(trace_token)

    assert note.note_id == "note-1"
    assert note.title == "字节后端一面"
    assert note.body == "面试正文"
    assert note.author_id == "user-1"
    assert note.image_urls == ("http://img.example/0.webp", "https://img.example/1.webp")
    assert note.tags == ("面经",)
    assert note.raw_response["source"] == "chrome_cdp_page_state"
    assert primary.fetch_calls == 1
    assert session.pages_created == 1
    assert page.closed is True
    decisions = _decisions(caplog)
    assert ("xhs.cdp.connection", "connected") in decisions
    assert ("xhs.cdp.fallback", "attempted") in decisions
    assert ("xhs.cdp.fallback", "succeeded") in decisions
    assert ("xhs.backend_route", "cdp_fallback") in decisions
    for record in caplog.records:
        if getattr(record, "decision_name", None):
            assert record.trace_id == trace_id
    # The same session is reused for a second fallback read.
    await backend.fetch_note("https://www.xiaohongshu.com/explore/note-1")
    assert session.pages_created == 2
    await backend.close()


@pytest.mark.asyncio
async def test_non_classified_failure_does_not_invoke_cdp(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=CDP_LOGGER)
    settings = _settings()
    page = FakeCdpPage(_note_state())
    cdp, session = _cdp_backend(settings, page)
    primary = FakePrimaryBackend(
        fetch_error=SpiderXhsError("Spider_XHS note fetch failed: connection reset")
    )
    backend = FallbackXhsBackend(primary, cdp)

    with pytest.raises(SpiderXhsError):
        await backend.fetch_note("https://www.xiaohongshu.com/explore/note-1")

    assert session.pages_created == 0
    assert ("xhs.cdp.fallback", "attempted") not in _decisions(caplog)
    await backend.close()


@pytest.mark.asyncio
async def test_search_never_falls_back() -> None:
    settings = _settings()
    page = FakeCdpPage(_note_state())
    cdp, session = _cdp_backend(settings, page)
    primary = FakePrimaryBackend(search_error=XhsAuthenticationError("login expired"))
    backend = FallbackXhsBackend(primary, cdp)

    with pytest.raises(XhsAuthenticationError):
        await backend.search_notes("字节 面经")

    assert session.pages_created == 0
    await backend.close()


@pytest.mark.asyncio
async def test_download_note_fetches_via_fallback_then_primary_downloader() -> None:
    settings = _settings()
    page = FakeCdpPage(_note_state())
    cdp, _session = _cdp_backend(settings, page)
    primary = FakePrimaryBackend(fetch_error=XhsAuthenticationError("login expired"))
    backend = FallbackXhsBackend(primary, cdp)

    downloaded = await backend.download_note(
        "https://www.xiaohongshu.com/explore/note-1"
    )

    assert primary.download_calls == 1
    assert primary.downloaded_note_arg is not None
    assert primary.downloaded_note_arg.title == "字节后端一面"
    assert downloaded.note.title == "字节后端一面"
    await backend.close()


@pytest.mark.asyncio
async def test_download_note_with_prefetched_note_skips_fetch() -> None:
    settings = _settings()
    page = FakeCdpPage(_note_state())
    cdp, session = _cdp_backend(settings, page)
    primary = FakePrimaryBackend()
    backend = FallbackXhsBackend(primary, cdp)
    prefetched = SpiderXhsBackend_stub_note("https://www.xiaohongshu.com/explore/note-1")

    downloaded = await backend.download_note(
        "https://www.xiaohongshu.com/explore/note-1", fetched_note=prefetched
    )

    assert primary.fetch_calls == 0
    assert primary.download_calls == 1
    assert downloaded.note is prefetched
    assert session.pages_created == 0
    await backend.close()


# ---------------------------------------------------------------------------
# Structured blocked/unavailable semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_disabled_skips_fallback_and_preserves_blocked_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=CDP_LOGGER)
    settings = _settings(xhs_cdp_enabled=False)
    assert settings.xhs_cdp_enabled is False
    page = FakeCdpPage(_note_state())
    cdp, session = _cdp_backend(settings, page)
    primary = FakePrimaryBackend(fetch_error=XhsAuthenticationError("cookie expired"))
    backend = FallbackXhsBackend(primary, cdp)

    with pytest.raises(XhsAuthenticationError):
        await backend.fetch_note("https://www.xiaohongshu.com/explore/note-1")

    assert session.pages_created == 0
    decisions = _decisions(caplog)
    assert ("xhs.cdp.fallback", "skipped") in decisions
    assert ("xhs.cdp.fallback", "attempted") not in decisions
    await backend.close()


@pytest.mark.asyncio
async def test_cdp_unreachable_returns_primary_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=CDP_LOGGER)
    settings = _settings()
    page = FakeCdpPage(_note_state())
    cdp, session = _cdp_backend(
        settings, page, connect_error=ConnectionRefusedError("cdp refused")
    )
    primary = FakePrimaryBackend(fetch_error=XhsAuthenticationError("cookie expired"))
    backend = FallbackXhsBackend(primary, cdp)

    with pytest.raises(XhsAuthenticationError):
        await backend.fetch_note("https://www.xiaohongshu.com/explore/note-1")

    decisions = _decisions(caplog)
    assert ("xhs.cdp.connection", "unavailable") in decisions
    assert ("xhs.cdp.fallback", "attempted") in decisions
    assert ("xhs.cdp.fallback", "failed") in decisions
    assert ("xhs.cdp.fallback", "succeeded") not in decisions
    assert session.pages_created == 0
    await backend.close()


@pytest.mark.asyncio
async def test_cdp_login_page_returns_primary_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=CDP_LOGGER)
    settings = _settings()
    page = FakeCdpPage(_note_state(), login_element=FakeElement(visible=True))
    cdp, _session = _cdp_backend(settings, page)
    primary = FakePrimaryBackend(fetch_error=XhsAuthenticationError("cookie expired"))
    backend = FallbackXhsBackend(primary, cdp)

    with pytest.raises(XhsAuthenticationError):
        await backend.fetch_note("https://www.xiaohongshu.com/explore/note-1")

    decisions = _decisions(caplog)
    assert ("xhs.cdp.fallback", "failed") in decisions
    assert ("xhs.cdp.fallback", "succeeded") not in decisions
    await backend.close()


@pytest.mark.asyncio
async def test_cdp_backend_direct_read_raises_blocked_on_login_page() -> None:
    settings = _settings()
    page = FakeCdpPage(_note_state(), login_element=FakeElement(visible=True))
    cdp, _session = _cdp_backend(settings, page)

    with pytest.raises(XhsCdpBlockedError):
        await cdp.fetch_note("https://www.xiaohongshu.com/explore/note-1")
    await cdp.close()


@pytest.mark.asyncio
async def test_cdp_backend_blocked_when_state_never_loads() -> None:
    settings = _settings()
    page = FakeCdpPage(None)
    cdp, _session = _cdp_backend(settings, page)

    with pytest.raises(XhsCdpBlockedError):
        await cdp.fetch_note("https://www.xiaohongshu.com/explore/note-1")
    await cdp.close()


@pytest.mark.asyncio
async def test_cdp_backend_unsupported_operations() -> None:
    settings = _settings()
    page = FakeCdpPage(_note_state())
    cdp, _session = _cdp_backend(settings, page)

    from jobagent.scraper.xhs_cdp import XhsCdpError

    with pytest.raises(XhsCdpError):
        await cdp.search_notes("query")
    with pytest.raises(XhsCdpError):
        await cdp.download_note("https://www.xiaohongshu.com/explore/note-1")
    await cdp.close()


# ---------------------------------------------------------------------------
# Permission boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_never_closes_external_browser_or_leaks_pages() -> None:
    settings = _settings()
    page = FakeCdpPage(_note_state())
    cdp, session = _cdp_backend(settings, page)

    await cdp.fetch_note("https://www.xiaohongshu.com/explore/note-1")
    await cdp.close()

    assert page.closed is True
    assert session.disconnected is True
    assert session.external_browser_closed is False
    assert session.external_context_closed is False
    # A second close is a safe no-op.
    await cdp.close()


@pytest.mark.asyncio
async def test_playwright_session_disconnect_only_disconnects() -> None:
    class FakeBrowser:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class FakeDriver:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    browser = FakeBrowser()
    driver = FakeDriver()
    session = PlaywrightCdpSession(browser, context=object(), driver=driver)

    await session.disconnect()

    assert browser.closed is True
    assert driver.stopped is True


def test_find_xhs_context_requires_xiaohongshu_page() -> None:
    class FakePage:
        def __init__(self, url: str) -> None:
            self.url = url

    class FakeContext:
        def __init__(self, pages: list[FakePage]) -> None:
            self.pages = pages

    class FakeBrowser:
        def __init__(self, contexts: list[FakeContext]) -> None:
            self.contexts = contexts

    from jobagent.scraper.xhs_cdp import _find_xhs_context

    xhs_context = FakeContext([FakePage("https://www.example.com/")])
    browser = FakeBrowser(
        [
            FakeContext([FakePage("https://www.example.com/")]),
            xhs_context,
        ]
    )
    xhs_context.pages.append(FakePage("https://www.xiaohongshu.com/explore/abc"))
    assert _find_xhs_context(browser) is xhs_context

    assert _find_xhs_context(FakeBrowser([FakeContext([])])) is None


# ---------------------------------------------------------------------------
# Author fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_author_fallback_returns_bounded_references_without_secrets_in_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=CDP_LOGGER)
    settings = _settings()
    page = FakeCdpPage(
        _user_state(),
        url="https://www.xiaohongshu.com/user/profile/user-1",
    )
    cdp, session = _cdp_backend(settings, page)
    primary = FakePrimaryBackend(
        list_error=SpiderXhsError("Spider_XHS user note listing failed: 登录已过期")
    )
    backend = FallbackXhsBackend(primary, cdp)

    references = await backend.list_user_notes("user-1", limit=2)

    assert [item.note_id for item in references] == ["note-a", "note-b"]
    assert all(
        item.url.startswith("https://www.xiaohongshu.com/explore/note-")
        for item in references
    )
    assert references[0].url.endswith("xsec_source=pc_user")
    assert references[0].raw == {
        "note_id": "note-a",
        "display_title": "笔记A",
        "type": "normal",
    }
    assert primary.list_calls == 1
    assert session.pages_created == 1
    assert ("xhs.backend_route", "cdp_fallback") in _decisions(caplog)
    # No token value ever reaches the logs.
    for output in caplog.text.splitlines():
        assert "SECRET_TOKEN" not in output
    await backend.close()


# ---------------------------------------------------------------------------
# Settings validation and factory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:9222",
        "http://localhost:9222",
        "http://[::1]:9222",
        "http://127.0.0.1:9222/devtools/browser-id",
    ],
)
def test_settings_accept_loopback_cdp_endpoints(endpoint: str) -> None:
    assert _settings(debug_chrome_cdp_endpoint=endpoint).debug_chrome_cdp_endpoint == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://192.168.1.5:9222",
        "http://example.com:9222",
        "ws://127.0.0.1:9222",
        "not-a-url",
        "",
    ],
)
def test_settings_reject_non_loopback_or_invalid_cdp_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        _settings(debug_chrome_cdp_endpoint=endpoint)



def test_settings_accept_new_and_legacy_cdp_endpoint_env_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XHS_CDP_ENDPOINT", raising=False)
    monkeypatch.delenv("DEBUG_CHROME_CDP_ENDPOINT", raising=False)
    fresh = Settings(_env_file=None)

    monkeypatch.setenv("DEBUG_CHROME_CDP_ENDPOINT", "http://127.0.0.1:9223")
    assert Settings(_env_file=None).debug_chrome_cdp_endpoint == "http://127.0.0.1:9223"

    monkeypatch.delenv("DEBUG_CHROME_CDP_ENDPOINT")
    monkeypatch.setenv("XHS_CDP_ENDPOINT", "http://localhost:9224")
    with pytest.warns(DeprecationWarning, match="DEBUG_CHROME_CDP_ENDPOINT"):
        legacy = Settings(_env_file=None)
    assert legacy.debug_chrome_cdp_endpoint == "http://localhost:9224"

    assert fresh.debug_chrome_cdp_endpoint == "http://127.0.0.1:9222"

@pytest.mark.asyncio
async def test_build_xhs_backend_disabled_returns_plain_spider_backend() -> None:
    settings = _settings(xhs_cdp_enabled=False)
    from jobagent.scraper.xhs_backend import SpiderXhsBackend

    backend = build_xhs_backend(settings)
    assert isinstance(backend, SpiderXhsBackend)


@pytest.mark.asyncio
async def test_build_xhs_backend_enabled_composes_fallback() -> None:
    backend = build_xhs_backend(_settings())
    assert isinstance(backend, FallbackXhsBackend)
    assert isinstance(backend._fallback, XhsCdpBackend)


# ---------------------------------------------------------------------------
# Real connector contract (offline: no Chrome is started)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_cdp_backend_fails_fast_without_connecting() -> None:
    # The real connector defers the playwright import and only ever runs when
    # a fallback is invoked; a disabled backend must not connect at all.
    settings = _settings(xhs_cdp_enabled=False)

    async def unexpected_connect(endpoint: str, timeout_seconds: int) -> FakeCdpSession:
        raise AssertionError("connect must not be called when disabled")

    cdp = XhsCdpBackend(settings, connect=unexpected_connect)
    assert cdp.enabled is False
    with pytest.raises(XhsCdpUnavailableError):
        await cdp.fetch_note("https://www.xiaohongshu.com/explore/note-1")
    await cdp.close()
