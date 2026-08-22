"""Tests for saving one user-supplied URL through chat."""

from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import ValidationError

from jobagent.agent import build_job_agent
from jobagent.config import Settings
from jobagent.interview.ocr import ImageContentExtraction
from jobagent.interview.snapshot import SnapshotMaterializer
from jobagent.scraper.xhs_backend import DownloadedXhsNote, XhsAuthenticationError, XhsFetchedNote
from jobagent.tools.shared_url import (
    SharedUrlSaver,
    SharedUrlSaveRequest,
    WebPageSaver,
    build_shared_url_save_tool,
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
