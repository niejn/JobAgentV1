"""Tests for saving one user-supplied URL through chat."""

from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import ValidationError

from jobagent.agent import build_job_agent
from jobagent.config import Settings
from jobagent.interview.ocr import ImageContentExtraction
from jobagent.interview.snapshot import SnapshotMaterializer
from jobagent.scraper.xhs_backend import (
    DownloadedXhsNote,
    SpiderXhsError,
    XhsAuthenticationError,
    XhsFetchedNote,
    XhsNoteReference,
)
from jobagent.tools.shared_url import (
    SharedUrlSaver,
    SharedUrlSaveRequest,
    WebPageSaver,
    build_shared_url_save_tool,
)
from jobagent.tools.xhs_author import (
    XhsAuthorBrowseCheckpoint,
    XhsAuthorBrowseCheckpoints,
    XhsAuthorPostsBrowser,
    XhsAuthorPostsRequest,
)
from jobagent.tools.xhs_note import XhsContentReader, XhsNoteSaver, XhsNoteSaveRequest


class ToolBindableFakeListChatModel(FakeListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_default_agent_registers_shared_url_saver(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
    )

    agent = build_job_agent(settings, model=ToolBindableFakeListChatModel(responses=["ok"]))

    assert "save_shared_url" in {tool.name for tool in agent._tools}
    assert "extract_shared_url" in {tool.name for tool in agent._tools}
    assert "browse_xhs_author_posts" in {tool.name for tool in agent._tools}


class FakeXhsSaver:
    def __init__(self) -> None:
        self.url: str | None = None

    async def save(self, request: Any) -> dict[str, Any]:
        self.url = request.url
        return {"status": "completed", "platform": "xiaohongshu"}


class FakePageSaver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def save(self, url: str, *, platform: str) -> dict[str, Any]:
        self.calls.append((url, platform))
        return {"status": "completed", "platform": platform}


class FakeSharedSaver:
    def __init__(self) -> None:
        self.url: str | None = None

    async def save(self, request: SharedUrlSaveRequest) -> dict[str, Any]:
        self.url = request.url
        return {"status": "completed", "platform": "xiaohongshu"}


@pytest.mark.asyncio
async def test_shared_url_tool_requires_only_the_user_url() -> None:
    saver = FakeSharedSaver()
    tool = build_shared_url_save_tool(saver)  # type: ignore[arg-type]
    url = "https://www.xiaohongshu.com/discovery/item/example?xsec_token=share-token"

    result = await tool.ainvoke({"url": url})

    assert result["status"] == "completed"
    assert saver.url == url
    assert set(tool.args_schema.model_fields) == {"url"}


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type, **(headers or {})}

    def iter_content(self, chunk_size: int) -> list[bytes]:
        return [self.content]


class FakeExtractor:
    def extract(self, image_path):
        return ImageContentExtraction(
            source_image=image_path.resolve(),
            source_hash="sha256:fake",
            text=f"OCR {image_path.stem}",
            confidence=0.9,
            engine="fake",
            engine_version="1",
            language="chi_sim",
        )


@pytest.mark.asyncio
async def test_shared_url_saver_routes_boss_and_generic_urls(tmp_path) -> None:
    page_saver = FakePageSaver()
    saver = SharedUrlSaver(
        Settings(_env_file=None, jobagent_artifact_dir=tmp_path),
        xhs_saver=FakeXhsSaver(),  # type: ignore[arg-type]
        page_saver=page_saver,  # type: ignore[call-arg]
    )

    boss = await saver.save(
        SharedUrlSaveRequest(url="https://www.zhipin.com/job_detail/example.html")
    )
    generic = await saver.save(SharedUrlSaveRequest(url="https://example.com/guide"))

    assert boss["platform"] == "boss"
    assert generic["platform"] == "web"
    assert page_saver.calls == [
        ("https://www.zhipin.com/job_detail/example.html", "boss"),
        ("https://example.com/guide", "web"),
    ]


@pytest.mark.asyncio
async def test_shared_url_saver_forwards_complete_xhs_share_url_unchanged(tmp_path) -> None:
    xhs_saver = FakeXhsSaver()
    saver = SharedUrlSaver(
        Settings(_env_file=None, jobagent_artifact_dir=tmp_path),
        xhs_saver=xhs_saver,  # type: ignore[arg-type]
        page_saver=FakePageSaver(),
    )
    url = (
        "https://www.xiaohongshu.com/discovery/item/6a87a6c7000000001f01ee90"
        "?xsec_source=app_share&xsec_token=temporary-share-token"
    )

    result = await saver.save(SharedUrlSaveRequest(url=url))

    assert result["status"] == "completed"
    assert result["platform"] == "xiaohongshu"
    assert xhs_saver.url == url


@pytest.mark.asyncio
async def test_generic_page_saver_preserves_content_and_manifest(tmp_path) -> None:
    captured: dict[str, Any] = {}

    def request(url: str, **kwargs: Any) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(b"<html><title>Guide</title><p>Useful material</p></html>")

    saver = WebPageSaver(
        Settings(_env_file=None, jobagent_artifact_dir=tmp_path),
        requester=request,
    )

    result = await saver.save("https://example.com/guide", platform="web")

    assert result["status"] == "completed"
    assert result["content_file"] == "page.html"
    artifact_dir = tmp_path / result["artifact_ref"]
    assert (artifact_dir / "page.html").read_bytes().startswith(b"<html>")
    assert (artifact_dir / "source.json").is_file()
    assert captured["cookies"] == {}
    assert captured["impersonate"] == "chrome"


@pytest.mark.asyncio
async def test_page_saver_validates_each_redirect_before_following(tmp_path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(url: str, **kwargs: Any) -> FakeResponse:
        calls.append((url, kwargs))
        if len(calls) == 1:
            return FakeResponse(
                b"",
                status_code=302,
                headers={"location": "https://www.example.com/final"},
            )
        return FakeResponse(b"<html><p>final material</p></html>")

    saver = WebPageSaver(
        Settings(_env_file=None, jobagent_artifact_dir=tmp_path),
        requester=request,
    )

    result = await saver.save("https://example.com/start", platform="web")

    assert result["status"] == "completed"
    assert [url for url, _ in calls] == [
        "https://example.com/start",
        "https://www.example.com/final",
    ]
    assert all(kwargs["allow_redirects"] is False for _, kwargs in calls)


@pytest.mark.asyncio
async def test_page_saver_blocks_redirect_to_private_network(tmp_path) -> None:
    calls: list[str] = []

    def request(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse(
            b"",
            status_code=302,
            headers={"location": "http://127.0.0.1/admin"},
        )

    saver = WebPageSaver(
        Settings(_env_file=None, jobagent_artifact_dir=tmp_path),
        requester=request,
    )

    result = await saver.save("https://example.com/start", platform="web")

    assert result["status"] == "blocked"
    assert result["error_type"] == "unsafe_redirect"
    assert calls == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_boss_page_saver_reuses_saved_login_cookies(
    tmp_path,
    monkeypatch,
) -> None:
    async def fake_get_cookies(platform: str, settings: object) -> list[dict[str, str]]:
        assert platform == "boss"
        return [{"name": "wt2", "value": "saved-session"}]

    captured: dict[str, Any] = {}

    def request(url: str, **kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(b"<html><p>AI Agent engineer job</p></html>")

    monkeypatch.setattr("jobagent.tools.shared_url.get_cookies", fake_get_cookies)
    saver = WebPageSaver(
        Settings(_env_file=None, jobagent_artifact_dir=tmp_path),
        requester=request,
    )

    result = await saver.save(
        "https://www.zhipin.com/job_detail/example.html",
        platform="boss",
    )

    assert result["status"] == "completed"
    assert result["platform"] == "boss"
    assert captured["cookies"] == {"wt2": "saved-session"}
    assert captured["headers"]["Referer"] == "https://www.zhipin.com/"


@pytest.mark.asyncio
async def test_boss_cookie_is_not_forwarded_to_external_redirect(
    tmp_path,
    monkeypatch,
) -> None:
    async def fake_get_cookies(platform: str, settings: object) -> list[dict[str, str]]:
        return [{"name": "wt2", "value": "saved-session"}]

    calls: list[dict[str, Any]] = []

    def request(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        if len(calls) == 1:
            return FakeResponse(
                b"",
                status_code=302,
                headers={"location": "https://example.com/public"},
            )
        return FakeResponse(b"<html><p>public page</p></html>")

    monkeypatch.setattr("jobagent.tools.shared_url.get_cookies", fake_get_cookies)
    saver = WebPageSaver(
        Settings(_env_file=None, jobagent_artifact_dir=tmp_path),
        requester=request,
    )

    result = await saver.save(
        "https://www.zhipin.com/job_detail/example.html",
        platform="boss",
    )

    assert result["status"] == "completed"
    assert calls[0]["cookies"] == {"wt2": "saved-session"}
    assert calls[1]["cookies"] == {}
    assert "Referer" not in calls[1]["headers"]


@pytest.mark.asyncio
async def test_page_saver_reports_access_control_without_writing(tmp_path) -> None:
    saver = WebPageSaver(
        Settings(_env_file=None, jobagent_artifact_dir=tmp_path),
        requester=lambda *args, **kwargs: FakeResponse(b"Forbidden", status_code=403),
    )

    result = await saver.save("https://example.com/private", platform="web")

    assert result["status"] == "blocked"
    assert result["error_type"] == "login_or_access_control"
    assert not (tmp_path / "shared_urls").exists()


@pytest.mark.asyncio
async def test_page_saver_stops_when_resource_exceeds_size_limit(tmp_path) -> None:
    response = FakeResponse(b"")

    def oversized_chunks(chunk_size: int):
        yield b"a" * (11 * 1024 * 1024)
        yield b"b" * (11 * 1024 * 1024)
        raise AssertionError("reader must stop once the limit is exceeded")

    response.iter_content = oversized_chunks  # type: ignore[method-assign]
    saver = WebPageSaver(
        Settings(_env_file=None, jobagent_artifact_dir=tmp_path),
        requester=lambda *args, **kwargs: response,
    )

    result = await saver.save("https://example.com/large", platform="web")

    assert result["status"] == "failed"
    assert result["error_type"] == "resource_too_large"
    assert not (tmp_path / "shared_urls").exists()


@pytest.mark.parametrize(
    "url",
    (
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1/private",
        "http://192.168.1.10/private",
        "https://user:password@example.com/private",
    ),
)
def test_shared_url_request_rejects_non_public_targets(url: str) -> None:
    with pytest.raises(ValidationError):
        SharedUrlSaveRequest(url=url)


@pytest.mark.asyncio
async def test_xhs_saver_reports_missing_login_without_downloading(tmp_path) -> None:
    class AuthenticationBlockedBackend:
        async def __aenter__(self) -> object:
            raise XhsAuthenticationError("missing cookie")

        async def __aexit__(self, *args: object) -> None:
            return None

    saver = XhsNoteSaver(
        Settings(_env_file=None, xhs_download_dir=tmp_path),
        backend_factory=lambda settings: AuthenticationBlockedBackend(),
    )

    result = await saver.save(
        XhsNoteSaveRequest(
            url="https://www.xiaohongshu.com/discovery/item/example"
        )
    )

    assert result["status"] == "blocked"
    assert result["error_type"] == "login_required"


@pytest.mark.asyncio
async def test_xhs_content_reader_materializes_and_reloads_body_and_image_ocr(tmp_path) -> None:
    directory = tmp_path / "xhs" / "saved-note"
    directory.mkdir(parents=True)
    body_path = directory / "detail.txt"
    body_path.write_text("正文：LangGraph 生产经验", encoding="utf-8")
    image_path = directory / "image_0.jpg"
    image_path.write_bytes(b"fake-image")
    raw_path = directory / "raw_response.json"
    raw_path.write_text("{}", encoding="utf-8")
    (directory / "info.json").write_text(
        '{"note_id":"note-ocr-1","title":"LangGraph 经验","desc":"正文：LangGraph 生产经验",'
        '"image_list":["https://img.example/0"]}',
        encoding="utf-8",
    )
    note = XhsFetchedNote(
        note_id="note-ocr-1",
        url="https://www.xiaohongshu.com/discovery/item/note-ocr-1",
        title="LangGraph 经验",
        body="正文：LangGraph 生产经验",
        author_id="author",
        author_name="作者",
        image_urls=("https://img.example/0",),
        tags=("LangGraph",),
        published_at=None,
        normalized={},
        raw_response={},
    )
    downloaded = DownloadedXhsNote(note, directory, body_path, (image_path,), raw_path)
    reader = XhsContentReader(
        Settings(_env_file=None, xhs_download_dir=tmp_path / "xhs"),
        materializer_factory=lambda settings: SnapshotMaterializer(FakeExtractor()),
    )

    first = await reader.materialize(downloaded)
    second = await reader.extract_saved(note.url)

    assert first["status"] == "completed"
    assert first["body_text"] == "正文：LangGraph 生产经验"
    assert "OCR image_0" in first["image_ocr_text"]
    assert second["extracted_text"] == first["extracted_text"]


@pytest.mark.asyncio
async def test_xhs_author_browser_lists_and_filters_public_posts(tmp_path) -> None:
    note = XhsFetchedNote(
        note_id="author-note-1",
        url="https://www.xiaohongshu.com/explore/author-note-1",
        title="LangGraph 学习笔记",
        body="StateGraph 的生产实践",
        author_id="5c0645200050148e8",
        author_name="作者",
        image_urls=(),
        tags=("LangGraph",),
        published_at="2026-08-20 12:00:00",
        normalized={},
        raw_response={},
    )

    class FakeBackend:
        """模拟真实 SpiderXhsBackend 的生命周期：__aexit__ 后即不可用。

        这是回归护栏：曾有 bug 把 fetch_note 循环写在 async with 块外，
        fake 的空操作 __aexit__ 掩盖了它（真实 backend close 后抛
        "not started"，fake 却继续可用）。现在 fake 与真实语义对齐，
        生命周期错位会直接测试红。
        """

        def __init__(self) -> None:
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True
            return None

        def _ensure_open(self) -> None:
            if self.closed:
                raise SpiderXhsError(
                    "SpiderXhsBackend is not started; call start() or use `async with`."
                )

        async def list_user_notes(self, user_id: str, *, limit: int | None = None):
            self._ensure_open()
            assert user_id == "5c0645200050148e"
            return [XhsNoteReference(note.note_id, note.url, {})]

        async def fetch_note(self, url: str):
            self._ensure_open()
            return note

    browser = XhsAuthorPostsBrowser(
        Settings(_env_file=None),
        backend_factory=lambda settings: FakeBackend(),
    )

    result = await browser.browse(
        XhsAuthorPostsRequest(
            profile_url="https://www.xiaohongshu.com/user/profile/5c0645200050148e",
            keyword="LangGraph",
        )
    )

    assert result["status"] == "completed"
    assert result["author_id"] == "5c0645200050148e"
    assert result["total_found"] == 1
    assert result["fetched"] == 1
    assert result["failed"] == 0
    assert result["count"] == 1
    assert result["posts"][0]["title"] == "LangGraph 学习笔记"


@pytest.mark.asyncio
async def test_xhs_author_browser_tolerates_partial_failures() -> None:
    from jobagent.scraper.xhs_backend import XhsFetchedNote, XhsNoteReference

    note = XhsFetchedNote(
        note_id="note-1",
        url="https://www.xiaohongshu.com/explore/note-1",
        title="LangGraph 学习笔记",
        body="StateGraph 的生产实践",
        author_id="5c0645200050148e8",
        author_name="作者",
        image_urls=(),
        tags=("LangGraph",),
        published_at="2026-08-20 12:00:00",
        normalized={},
        raw_response={},
    )

    class FakeBackendPartial:
        def __init__(self) -> None:
            self.fetch_calls = 0
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True
            return None

        def _ensure_open(self) -> None:
            if self.closed:
                raise SpiderXhsError(
                    "SpiderXhsBackend is not started; call start() or use `async with`."
                )

        async def list_user_notes(self, user_id: str, *, limit: int | None = None):
            self._ensure_open()
            return [
                XhsNoteReference("note-ok", "https://xhs/note-ok", {}),
                XhsNoteReference("note-bad", "https://xhs/note-bad", {}),
                XhsNoteReference("note-ok-2", "https://xhs/note-ok-2", {}),
            ]

        async def fetch_note(self, url: str):
            self._ensure_open()
            if "note-bad" in url:
                raise SpiderXhsError("笔记详情受限：token 过期")
            return note

    browser = XhsAuthorPostsBrowser(
        Settings(_env_file=None),
        backend_factory=lambda settings: FakeBackendPartial(),
    )

    result = await browser.browse(
        XhsAuthorPostsRequest(
            profile_url="https://www.xiaohongshu.com/user/profile/test-user",
            limit=10,
        )
    )

    assert result["status"] == "partial"
    assert result["total_found"] == 3
    assert result["fetched"] == 2
    assert result["failed"] == 1
    assert result["count"] == 2
    assert len(result["failed_details"]) == 1
    assert result["failed_details"][0]["note_id"] == "note-bad"
    assert "token 过期" in result["failed_details"][0]["message"]


# ---- fetch_all 模式：全量抓取 + checkpoint 断点恢复 ---------------------------


def _fetch_all_note(note_id: str) -> XhsFetchedNote:
    return XhsFetchedNote(
        note_id=note_id,
        url=f"https://www.xiaohongshu.com/explore/{note_id}",
        title=f"帖子 {note_id}",
        body="Agent 面试经验正文",
        author_id="author-1",
        author_name="作者",
        image_urls=(),
        tags=("面试",),
        published_at="2026-08-20 12:00:00",
        normalized={},
        raw_response={},
    )


class FakeFetchAllBackend:
    """list_user_notes 忽略 limit 返回全部；fetch_note 按失败名单拒绝。

    模拟真实工厂语义：每次调用产出一个新实例（共享 refs/fail_ids/调用
    记录），单例复用会让 closed 状态泄漏到下一次 browse。
    """

    def __init__(self, refs: list[XhsNoteReference], fail_ids: set[str]) -> None:
        self._refs = refs
        self._fail_ids = fail_ids
        self.closed = False
        self.list_calls: list[int | None] = []

    async def __aenter__(self) -> "FakeFetchAllBackend":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    def _ensure_open(self) -> None:
        if self.closed:
            raise SpiderXhsError(
                "SpiderXhsBackend is not started; call start() or use `async with`."
            )

    async def list_user_notes(self, user_id: str, *, limit: int | None = None):
        self._ensure_open()
        self.list_calls.append(limit)
        return list(self._refs)

    async def fetch_note(self, url: str) -> XhsFetchedNote:
        self._ensure_open()
        note_id = url.rsplit("/", 1)[-1].split("?", 1)[0]
        if note_id in self._fail_ids:
            raise SpiderXhsError(f"笔记详情受限：{note_id}")
        return _fetch_all_note(note_id)


def _fetch_all_refs(count: int) -> list[XhsNoteReference]:
    return [
        XhsNoteReference(
            note_id=f"note-{i}",
            url=f"https://www.xiaohongshu.com/explore/note-{i}?xsec_token=tok{i}",
            raw={},
        )
        for i in range(count)
    ]


def _fetch_all_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        jobagent_state_db=tmp_path / "state.db",
    )


def _fresh_backend_factory(
    refs: list[XhsNoteReference], fail_ids: set[str]
) -> tuple[Callable[[Settings], FakeFetchAllBackend], list[int | None]]:
    """每次调用产出新实例（真实工厂语义），共享 list_calls 供断言。"""

    calls: list[int | None] = []

    def factory(settings: Settings) -> FakeFetchAllBackend:
        backend = FakeFetchAllBackend(refs, fail_ids)
        backend.list_calls = calls
        return backend

    return factory, calls


@pytest.mark.asyncio
async def test_fetch_all_fresh_start_returns_first_batch_and_persists_checkpoint(
    tmp_path,
) -> None:
    refs = _fetch_all_refs(5)
    factory, list_calls = _fresh_backend_factory(refs, set())
    browser = XhsAuthorPostsBrowser(
        _fetch_all_settings(tmp_path),
        backend_factory=factory,
    )

    result = await browser.browse(
        XhsAuthorPostsRequest(
            profile_url="https://www.xiaohongshu.com/user/profile/author-1",
            fetch_all=True,
            batch_limit=2,
        )
    )

    assert result["status"] == "resumable"
    assert result["mode"] == "fetch_all"
    assert result["resumed"] is False
    assert result["total_found"] == 5
    assert result["fetched_this_batch"] == 2
    assert result["remaining"] == 3
    assert [p["note_id"] for p in result["posts"]] == ["note-0", "note-1"]
    assert result["next_action"].startswith("More batches remain")

    # 列表一次到底（防御性 cap 传入），断点已持久化剩余 3 篇
    assert list_calls == [10_000]
    with XhsAuthorBrowseCheckpoints(tmp_path / "state.db") as store:
        checkpoint = store.load("author-1")
    assert checkpoint is not None
    assert checkpoint.total_found == 5
    assert checkpoint.completed_count == 2
    assert [r.note_id for r in checkpoint.remaining_refs] == [
        "note-2",
        "note-3",
        "note-4",
    ]


@pytest.mark.asyncio
async def test_fetch_all_resume_continues_from_checkpoint_and_cleans_up(
    tmp_path,
) -> None:
    # 预置断点：已抓 2 篇，剩余 note-2/3/4
    with XhsAuthorBrowseCheckpoints(tmp_path / "state.db") as store:
        store.save(
            XhsAuthorBrowseCheckpoint(
                author_id="author-1",
                keyword="",
                total_found=5,
                completed_count=2,
                failed_count=0,
                remaining_refs=tuple(_fetch_all_refs(5)[2:]),
                updated_at="2026-08-26T10:00:00+08:00",
            )
        )
    factory, list_calls = _fresh_backend_factory(_fetch_all_refs(5), set())
    browser = XhsAuthorPostsBrowser(
        _fetch_all_settings(tmp_path),
        backend_factory=factory,
    )

    result = await browser.browse(
        XhsAuthorPostsRequest(
            profile_url="https://www.xiaohongshu.com/user/profile/author-1",
            fetch_all=True,
            resume=True,
        )
    )

    # batch_limit 默认 50 > 剩余 3：一次收尾
    assert result["status"] == "completed"
    assert result["resumed"] is True
    assert result["completed_total"] == 5
    assert result["remaining"] == 0
    assert [p["note_id"] for p in result["posts"]] == ["note-2", "note-3", "note-4"]
    # 恢复路径不再调列表接口（断点里已有全部引用）
    assert list_calls == []
    # 完成后断点删除
    with XhsAuthorBrowseCheckpoints(tmp_path / "state.db") as store:
        assert store.load("author-1") is None


@pytest.mark.asyncio
async def test_fetch_all_resume_without_checkpoint_starts_fresh(tmp_path) -> None:
    factory, list_calls = _fresh_backend_factory(_fetch_all_refs(2), set())
    browser = XhsAuthorPostsBrowser(
        _fetch_all_settings(tmp_path),
        backend_factory=factory,
    )

    result = await browser.browse(
        XhsAuthorPostsRequest(
            profile_url="https://www.xiaohongshu.com/user/profile/author-1",
            fetch_all=True,
            resume=True,
        )
    )

    assert result["resumed"] is False
    assert result["status"] == "completed"
    assert list_calls == [10_000]


@pytest.mark.asyncio
async def test_fetch_all_counts_failures_across_batches(tmp_path) -> None:
    factory, _calls = _fresh_backend_factory(
        _fetch_all_refs(4), {"note-1", "note-3"}
    )
    browser = XhsAuthorPostsBrowser(
        _fetch_all_settings(tmp_path),
        backend_factory=factory,
    )
    url = "https://www.xiaohongshu.com/user/profile/author-1"

    first = await browser.browse(
        XhsAuthorPostsRequest(profile_url=url, fetch_all=True, batch_limit=2)
    )
    assert first["status"] == "resumable"
    assert first["fetched_this_batch"] == 1
    assert first["failed_this_batch"] == 1
    assert first["failed_total"] == 1

    second = await browser.browse(
        XhsAuthorPostsRequest(profile_url=url, fetch_all=True, resume=True, batch_limit=2)
    )
    # 全部批次结束：有永久失败 → partial，失败累计跨批
    assert second["status"] == "partial"
    assert second["failed_total"] == 2
    assert second["completed_total"] == 2
    assert second["remaining"] == 0
    with XhsAuthorBrowseCheckpoints(tmp_path / "state.db") as store:
        assert store.load("author-1") is None


@pytest.mark.asyncio
async def test_fetch_all_keyword_filters_posts_but_fetches_everything(tmp_path) -> None:
    factory, _calls = _fresh_backend_factory(_fetch_all_refs(2), set())
    browser = XhsAuthorPostsBrowser(
        _fetch_all_settings(tmp_path),
        backend_factory=factory,
    )

    result = await browser.browse(
        XhsAuthorPostsRequest(
            profile_url="https://www.xiaohongshu.com/user/profile/author-1",
            keyword="不存在的关键词",
            fetch_all=True,
        )
    )

    # 全部详情已抓（completed_total=2），keyword 只过滤展示
    assert result["completed_total"] == 2
    assert result["count"] == 0
    assert result["posts"] == []
    assert result["status"] == "completed"


def test_resume_requires_fetch_all_rejected_by_validation() -> None:
    import pytest as _pytest

    with _pytest.raises(ValueError, match="resume requires fetch_all"):
        XhsAuthorPostsRequest(
            profile_url="https://www.xiaohongshu.com/user/profile/author-1",
            resume=True,
        )


@pytest.mark.asyncio
async def test_bounded_browse_writes_no_checkpoint(tmp_path) -> None:
    factory, _calls = _fresh_backend_factory(_fetch_all_refs(1), set())
    browser = XhsAuthorPostsBrowser(
        _fetch_all_settings(tmp_path),
        backend_factory=factory,
    )

    result = await browser.browse(
        XhsAuthorPostsRequest(
            profile_url="https://www.xiaohongshu.com/user/profile/author-1",
            limit=1,
        )
    )

    assert result["status"] == "completed"
    assert "mode" not in result
    with XhsAuthorBrowseCheckpoints(tmp_path / "state.db") as store:
        assert store.load("author-1") is None
