"""Tests for the in-process Spider_XHS backend adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobagent.config import Settings
from jobagent.scraper.xhs_backend import (
    SpiderXhsBackend,
    SpiderXhsBindings,
    XhsAuthenticationError,
    load_spider_xhs_bindings,
)


class FakeAuth:
    cookie: str = ""

    @classmethod
    def from_cookie(cls, cookie: str) -> FakeAuth:
        cls.cookie = cookie
        return cls()


class FakeApi:
    bootstrapped = False

    def __init__(self, auth: FakeAuth) -> None:
        self.auth = auth
        self.http = FakeHttpClient()
        self.detail_calls = 0

    def bootstrap(self) -> FakeApi:
        self.bootstrapped = True
        return self

    def search_some_note(
        self,
        query: str,
        require_num: int,
        sort_type_choice: int = 0,
        note_type: int = 0,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        assert query == "字节 后端 面经"
        assert require_num == 2
        assert sort_type_choice == 0
        assert note_type == 2
        self.http.get("https://example.com/search?page=1")
        self.http.get("https://example.com/search?page=2")
        return (
            True,
            "success",
            [
                {
                    "model_type": "note",
                    "id": "note-1",
                    "xsec_token": "token=1",
                    "note_card": {"display_title": "一面复盘"},
                },
                {"model_type": "hot_query", "id": "ignored"},
            ],
        )

    def get_note_info(self, url: str) -> tuple[bool, str, dict[str, Any]]:
        self.detail_calls += 1
        self.http.post("https://example.com/note-detail")
        return (
            True,
            "success",
            {
                "success": True,
                "data": {
                    "items": [
                        {
                            "id": "note-1",
                            "note_card": {
                                "type": "normal",
                                "title": "字节后端一面",
                                "desc": "面试正文",
                                "user": {
                                    "user_id": "user-1",
                                    "nickname": "测试作者",
                                    "avatar": "ignored",
                                },
                                "interact_info": {
                                    "liked_count": "12",
                                    "collected_count": "3",
                                    "comment_count": "4",
                                    "share_count": "1",
                                },
                                "image_list": [],
                                "tag_list": [{"name": "面经"}],
                                "time": 1_700_000_000_000,
                            },
                        }
                    ]
                },
            },
        )

    def get_user_note_info(
        self,
        user_id: str,
        cursor: str,
        xsec_token: str = "",
        xsec_source: str = "",
    ) -> tuple[bool, str, dict[str, Any]]:
        assert user_id == "user-1"
        assert cursor == ""
        assert xsec_token == ""
        assert xsec_source == "pc_user"
        return (
            True,
            "success",
            {
                "data": {
                    "notes": [
                        {"note_id": "note-2", "xsec_token": "user-token"},
                        {"note_id": "note-3", "xsec_token": "user-token-2"},
                    ],
                    "cursor": "next",
                    "has_more": False,
                }
            },
        )


class FakeHttpClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def get(self, url: str, **kwargs: Any) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> None:
        return None

    def put(self, url: str, **kwargs: Any) -> None:
        return None


def fake_handle_note_info(item: dict[str, Any]) -> dict[str, Any]:
    card = item["note_card"]
    return {
        "note_id": item["id"],
        "note_url": item["url"],
        "note_type": "图集",
        "user_id": card["user"]["user_id"],
        "nickname": card["user"]["nickname"],
        "title": card["title"],
        "desc": card["desc"],
        "image_list": ["http://example.com/0.webp", "https://example.com/1.webp"],
        "tags": ["面经"],
        "upload_time": "2025-01-02 03:04:05",
        "liked_count": "12",
        "comment_count": "4",
        "ip_location": "北京",
    }


def fake_download_note(note: dict[str, Any], path: str, save_choice: str) -> str:
    assert save_choice == "metadata"
    note_dir = Path(path) / note["note_id"]
    note_dir.mkdir(parents=True)
    (note_dir / "detail.txt").write_text(note["desc"], encoding="utf-8")
    (note_dir / "info.json").write_text(json.dumps(note), encoding="utf-8")
    return str(note_dir)


MEDIA_DOWNLOAD_CALLS: list[dict[str, str]] = []


def fake_download_media(path: str, name: str, url: str, media_type: str) -> None:
    assert media_type == "image"
    MEDIA_DOWNLOAD_CALLS.append(
        {"path": path, "name": name, "url": url, "media_type": media_type}
    )
    (Path(path) / f"{name}.jpg").write_bytes(name.encode())


class RecordingBlockingRateLimiter:
    def __init__(self, label: str, events: list[str]) -> None:
        self._label = label
        self._events = events

    def acquire(self) -> None:
        self._events.append(self._label)


class RecordingAsyncRateLimiter:
    def __init__(self, label: str, events: list[str]) -> None:
        self._label = label
        self._events = events

    async def acquire(self) -> None:
        self._events.append(self._label)


@pytest.fixture()
def bindings() -> SpiderXhsBindings:
    return SpiderXhsBindings(
        auth_class=FakeAuth,
        api_class=FakeApi,
        handle_note_info=fake_handle_note_info,
        download_note=fake_download_note,
        download_media=fake_download_media,
    )


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        xhs_cookie_header="a1=a-value; web_session=session-value",
        spider_xhs_path=tmp_path,
        xhs_download_dir=tmp_path / "downloads",
        xhs_api_rate_requests=1_000,
        xhs_api_rate_period_seconds=1,
        xhs_media_rate_requests=1_000,
        xhs_media_rate_period_seconds=1,
    )


@pytest.mark.asyncio
async def test_start_bootstraps_spider_with_full_cookie(
    settings: Settings,
    bindings: SpiderXhsBindings,
) -> None:
    backend = SpiderXhsBackend(settings, bindings=bindings)

    await backend.start()

    assert FakeAuth.cookie == "a1=a-value; web_session=session-value"
    assert backend.started is True


@pytest.mark.asyncio
async def test_close_reuses_spider_http_client_close(
    settings: Settings,
    bindings: SpiderXhsBindings,
) -> None:
    backend = SpiderXhsBackend(settings, bindings=bindings)
    await backend.start()
    http_client = backend._api.http

    await backend.close()

    assert http_client.closed is True
    assert backend.started is False


@pytest.mark.asyncio
async def test_start_rejects_cookie_without_required_fields(
    tmp_path: Path,
    bindings: SpiderXhsBindings,
) -> None:
    settings = Settings(
        xhs_cookie_header="web_session=session-only",
        spider_xhs_path=tmp_path,
    )
    backend = SpiderXhsBackend(settings, bindings=bindings)

    with pytest.raises(XhsAuthenticationError, match="a1"):
        await backend.start()


@pytest.mark.asyncio
async def test_search_notes_reuses_spider_search(
    settings: Settings,
    bindings: SpiderXhsBindings,
) -> None:
    backend = SpiderXhsBackend(settings, bindings=bindings)
    await backend.start()

    notes = await backend.search_notes("字节 后端 面经", limit=2)

    assert len(notes) == 1
    assert notes[0].note_id == "note-1"
    assert "xsec_token=token%3D1" in notes[0].url
    assert notes[0].raw["note_card"]["display_title"] == "一面复盘"


@pytest.mark.asyncio
async def test_list_user_notes_reuses_spider_user_api(
    settings: Settings,
    bindings: SpiderXhsBindings,
) -> None:
    backend = SpiderXhsBackend(settings, bindings=bindings)
    await backend.start()

    notes = await backend.list_user_notes("user-1", limit=1)

    assert [note.note_id for note in notes] == ["note-2"]
    assert "xsec_token=user-token" in notes[0].url
    assert "xsec_source=pc_user" in notes[0].url


@pytest.mark.asyncio
async def test_download_note_reuses_spider_normalizer_and_downloader(
    settings: Settings,
    bindings: SpiderXhsBindings,
) -> None:
    MEDIA_DOWNLOAD_CALLS.clear()
    backend = SpiderXhsBackend(settings, bindings=bindings)
    await backend.start()

    result = await backend.download_note(
        "https://www.xiaohongshu.com/explore/note-1?xsec_token=token"
    )

    assert result.note.note_id == "note-1"
    assert result.note.body == "面试正文"
    assert result.note.author_id == "user-1"
    assert result.directory.name == "note-1"
    assert [path.name for path in result.images] == ["image_0.jpg", "image_1.jpg"]
    assert result.body_path.read_text(encoding="utf-8") == "面试正文"
    assert json.loads(result.raw_response_path.read_text(encoding="utf-8"))["success"] is True
    # Thin adapter contract: Spider_XHS owns URL handling, so the backend
    # forwards image URLs untouched instead of rewriting their scheme.
    assert [call["url"] for call in MEDIA_DOWNLOAD_CALLS] == [
        "http://example.com/0.webp",
        "https://example.com/1.webp",
    ]
    assert all(call["media_type"] == "image" for call in MEDIA_DOWNLOAD_CALLS)


@pytest.mark.asyncio
async def test_download_note_can_reuse_already_fetched_detail(
    settings: Settings,
    bindings: SpiderXhsBindings,
) -> None:
    backend = SpiderXhsBackend(settings, bindings=bindings)
    await backend.start()
    url = "https://www.xiaohongshu.com/explore/note-1?xsec_token=token"

    fetched = await backend.fetch_note(url)
    result = await backend.download_note(url, fetched_note=fetched)

    assert result.note is fetched
    assert backend._api.detail_calls == 1


@pytest.mark.asyncio
async def test_backend_rate_limits_each_api_call_and_each_image_download(
    settings: Settings,
    bindings: SpiderXhsBindings,
) -> None:
    events: list[str] = []
    backend = SpiderXhsBackend(
        settings,
        bindings=bindings,
        api_limiter=RecordingBlockingRateLimiter("api", events),
        media_limiter=RecordingAsyncRateLimiter("media", events),
    )
    await backend.start()

    await backend.search_notes("字节 后端 面经", limit=2)
    await backend.download_note(
        "https://www.xiaohongshu.com/explore/note-1?xsec_token=token"
    )

    assert events == ["api", "api", "api", "api", "media", "media"]


def test_real_spider_library_bindings_can_be_loaded() -> None:
    spider_path = Path(r"D:\mashibing\xiaohongshu_crawler\Spider_XHS")
    if not spider_path.exists():
        pytest.skip("Local Spider_XHS checkout is not available")

    real_bindings = load_spider_xhs_bindings(spider_path)

    assert real_bindings.auth_class.__name__ == "XHSPcAuth"
    assert real_bindings.api_class.__name__ == "XHS_Apis"
    assert real_bindings.handle_note_info.__name__ == "handle_note_info"
    assert real_bindings.download_note.__name__ == "download_note"
    assert real_bindings.download_media.__name__ == "download_media"
