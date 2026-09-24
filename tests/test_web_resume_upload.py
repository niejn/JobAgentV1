"""Tests for the web resume upload endpoint's size and format guardrails."""

from pathlib import Path

from fastapi.testclient import TestClient

from jobagent import web


def _with_temp_resume_dir(monkeypatch, tmp_path: Path) -> Path:
    directory = tmp_path / "resumes"
    directory.mkdir(parents=True)
    monkeypatch.setattr(web, "_safe_skill_dir", lambda _base: directory)
    return directory


def test_upload_rejects_oversized_file_and_cleans_partial(tmp_path, monkeypatch):
    directory = _with_temp_resume_dir(monkeypatch, tmp_path)
    payload = b"x" * (web.RESUME_MAX_BYTES + 1)
    with TestClient(web.app) as client:
        result = client.post(
            "/api/resumes/upload",
            files={"file": ("big.pdf", payload, "application/pdf")},
        )

    assert result.status_code == 413
    assert list(directory.iterdir()) == []


def test_upload_accepts_small_pdf(tmp_path, monkeypatch):
    directory = _with_temp_resume_dir(monkeypatch, tmp_path)
    with TestClient(web.app) as client:
        result = client.post(
            "/api/resumes/upload",
            files={"file": ("me.pdf", b"%PDF-1.4 tiny", "application/pdf")},
        )

    assert result.status_code == 201
    assert result.json()["saved_as"] == "me.pdf"
    assert (directory / "me.pdf").read_bytes() == b"%PDF-1.4 tiny"


def test_upload_rejects_unknown_suffix(tmp_path, monkeypatch):
    _with_temp_resume_dir(monkeypatch, tmp_path)
    with TestClient(web.app) as client:
        result = client.post(
            "/api/resumes/upload",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        )

    assert result.status_code == 422
