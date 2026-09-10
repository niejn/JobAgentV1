"""send_application_email attachment contract: explicit or none, never implicit."""

from pathlib import Path

import pytest

from jobagent.config import Settings
from jobagent.tools.email_apply import build_send_application_email_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("attachment_arg", ["", "none", "None"])
async def test_missing_attachment_sends_none_not_a_default_file(monkeypatch, attachment_arg):
    captured = {}
    monkeypatch.setattr(
        "jobagent.applier.email_sender.EmailSender.send",
        lambda self, **kwargs: captured.update(kwargs) or {"status": "ok"},
    )
    tool = build_send_application_email_tool(Settings(_env_file=None))

    result = await tool.ainvoke(
        {
            "to": "hr@example.com",
            "subject": "应聘测试",
            "body_html": "<p>正文</p>",
            "attachment_path": attachment_arg,
        }
    )

    assert result["status"] == "ok"
    assert captured["attachment"] is None


@pytest.mark.asyncio
async def test_explicit_attachment_path_is_passed_verbatim(monkeypatch, tmp_path: Path):
    captured = {}
    monkeypatch.setattr(
        "jobagent.applier.email_sender.EmailSender.send",
        lambda self, **kwargs: captured.update(kwargs) or {"status": "ok"},
    )
    resume = tmp_path / "tailored.pdf"
    resume.write_bytes(b"%PDF-1.7 tailored")
    tool = build_send_application_email_tool(Settings(_env_file=None))

    result = await tool.ainvoke(
        {
            "to": "hr@example.com",
            "subject": "应聘测试",
            "body_html": "<p>正文</p>",
            "attachment_path": str(resume),
        }
    )

    assert result["status"] == "ok"
    assert captured["attachment"] == resume
