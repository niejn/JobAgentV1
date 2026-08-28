"""Tests for the Boss attachment-resume upload tool and uploader guards."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobagent.applier.boss_resume import BossResumeUploader
from jobagent.config import Settings
from jobagent.tools.boss_resume import build_boss_resume_upload_tool


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, jobagent_state_db=tmp_path / "state.db")


# -- tool-level HITL gate ------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_refuses_without_user_confirmation(tmp_path: Path) -> None:
    tool = build_boss_resume_upload_tool(_settings(tmp_path))
    result = await tool.ainvoke(
        {"pdf_path": "resume.pdf", "user_confirmed": False}
    )
    assert result["status"] == "waiting_user_confirmation"
    # nothing was sent: no uploader was even constructed


@pytest.mark.asyncio
async def test_tool_passes_confirmation_and_delete_flag(tmp_path: Path) -> None:
    tool = build_boss_resume_upload_tool(_settings(tmp_path))
    calls: dict[str, Any] = {}

    class FakeUploader:
        def __init__(self, settings: Settings, *, crawl_gate: Any = None) -> None:
            calls["constructed"] = True

        async def __aenter__(self) -> FakeUploader:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def upload_pdf(self, pdf_path: str, *, allow_delete: bool = False) -> dict:
            calls["args"] = (pdf_path, allow_delete)
            return {"status": "ok"}

    with patch("jobagent.applier.boss_resume.BossResumeUploader", FakeUploader):
        result = await tool.ainvoke(
            {
                "pdf_path": "resume.pdf",
                "user_confirmed": True,
                "allow_delete": True,
            }
        )
    assert result == {"status": "ok"}
    assert calls["args"] == ("resume.pdf", True)


# -- uploader guards -------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_refuses_during_cooldown(tmp_path: Path) -> None:
    uploader = BossResumeUploader(_settings(tmp_path))
    with patch(
        "jobagent.applier.boss_resume.get_boss_cooldown",
        return_value=MagicMock(check=MagicMock(return_value=(False, 30, None))),
    ):
        result = await uploader.upload_pdf("whatever.pdf")
    assert result["status"] == "failed"
    assert result["error_type"] == "cooldown_active"


@pytest.mark.asyncio
async def test_upload_validates_pdf(tmp_path: Path) -> None:
    uploader = BossResumeUploader(_settings(tmp_path))
    txt = tmp_path / "resume.txt"
    txt.write_text("not a pdf", encoding="utf-8")
    missing = tmp_path / "ghost.pdf"

    with patch(
        "jobagent.applier.boss_resume.get_boss_cooldown",
        return_value=MagicMock(check=MagicMock(return_value=(True, 0, None))),
    ):
        assert (await uploader.upload_pdf(txt))["error_type"] == "not_a_pdf"
        assert (await uploader.upload_pdf(missing))["error_type"] == "file_not_found"


def _ok_cooldown() -> MagicMock:
    return MagicMock(check=MagicMock(return_value=(True, 0, None)))


@pytest.mark.asyncio
async def test_upload_happy_path_via_page_fetch(tmp_path: Path) -> None:
    """nav -> resume page settles -> page-scope fetch chain returns done."""
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    uploader = BossResumeUploader(_settings(tmp_path))
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/resume"
    page.goto = AsyncMock()
    page.locator = MagicMock()

    async def fake_evaluate(script: str, payload: dict | None = None):
        if payload is None:
            return True  # settle probe: document.readyState check
        assert "uploadFile.json" in script and "save.json" in script
        assert payload["name"] == "resume.pdf"
        assert payload["b64"]  # base64 payload delivered
        return {"step": "done", "resumeId": 12345, "previewUrl": "//x/b.pdf"}

    page.evaluate = fake_evaluate

    with patch(
        "jobagent.applier.boss_resume.get_boss_cooldown",
        return_value=_ok_cooldown(),
    ):
        uploader._context = MagicMock()
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=page)
        pool.release = AsyncMock()
        uploader._tab_pool = pool

        result = await uploader.upload_pdf(pdf)

    assert result["status"] == "ok"
    assert result["resume_id"] == 12345


@pytest.mark.asyncio
async def test_upload_api_rejection_is_structured(tmp_path: Path) -> None:
    """uploadFile.json code!=0 -> failed + api named + message surfaced."""
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    uploader = BossResumeUploader(_settings(tmp_path))
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/resume"
    page.goto = AsyncMock()
    page.locator = MagicMock()

    async def fake_evaluate(script: str, payload: dict | None = None):
        if payload is None:
            return True  # settle probe
        return {"step": "upload", "code": 1001, "message": "附件简历最多可上传3个"}

    page.evaluate = fake_evaluate

    with patch(
        "jobagent.applier.boss_resume.get_boss_cooldown",
        return_value=_ok_cooldown(),
    ):
        uploader._context = MagicMock()
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=page)
        pool.release = AsyncMock()
        uploader._tab_pool = pool

        result = await uploader.upload_pdf(pdf)

    assert result["status"] == "failed"
    assert result["api"] == "uploadFile.json"
    assert result["error_type"] == "attachment_limit"


@pytest.mark.asyncio
async def test_upload_page_lost_is_typed(tmp_path: Path) -> None:
    """evaluate raising (warlock navigated the page) -> page_lost, no hang."""
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    uploader = BossResumeUploader(_settings(tmp_path))
    page = MagicMock()
    page.url = "https://www.zhipin.com/web/geek/resume"
    page.goto = AsyncMock()
    page.locator = MagicMock()

    async def fake_evaluate(script: str, payload: dict | None = None):
        if payload is None:
            return True  # settle probe
        raise RuntimeError("Execution context was destroyed")

    page.evaluate = fake_evaluate

    with patch(
        "jobagent.applier.boss_resume.get_boss_cooldown",
        return_value=_ok_cooldown(),
    ):
        uploader._context = MagicMock()
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=page)
        pool.release = AsyncMock()
        uploader._tab_pool = pool

        result = await uploader.upload_pdf(pdf)

    assert result["status"] == "failed"
    assert result["error_type"] == "page_lost"


@pytest.mark.asyncio
async def test_limit_error_typed_for_retry(tmp_path: Path) -> None:
    """save.json rejecting with 最多 message -> error_type=attachment_limit."""
    from jobagent.applier.boss_resume import _looks_like_limit

    assert _looks_like_limit("附件简历最多可上传3个")
    assert _looks_like_limit("已达上限")
    assert not _looks_like_limit("文件格式不正确")
