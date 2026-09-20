"""Traceable, local tailored-resume artifacts.

The tool deliberately accepts only the final draft text, not platform details.
It records the declared source facts beside an immutable Markdown/HTML/PDF set;
confirmation is a separate monotonic state transition.
"""

from __future__ import annotations

import hashlib
import html
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field


class TailoredResumeDraft(BaseModel):
    journey_id: str = Field(min_length=1)
    company: str = Field(min_length=1)
    title: str = Field(min_length=1)
    resume_markdown: str = Field(min_length=40)
    fact_source_ids: list[str] = Field(min_length=1)


class ConfirmTailoredResume(BaseModel):
    artifact_id: str = Field(min_length=1)


class TailoredResumeStore:
    def __init__(self, root: Path, *, chrome_executable: Path | None = None) -> None:
        self._root = root.expanduser().resolve()
        self._chrome = chrome_executable or Path(
            "C:/Program Files/Google/Chrome/Application/chrome.exe"
        )

    def save(self, draft: TailoredResumeDraft) -> dict[str, Any]:
        safe_journey = hashlib.sha256(draft.journey_id.encode()).hexdigest()[:16]
        directory = self._root / safe_journey / "tailored-resumes"
        directory.mkdir(parents=True, exist_ok=True)
        versions = sorted(directory.glob("resume-v*.json"))
        version = len(versions) + 1
        artifact_id = f"{safe_journey}-v{version:03d}"
        stem = directory / f"resume-v{version:03d}"
        markdown_path = stem.with_suffix(".md")
        html_path = stem.with_suffix(".html")
        pdf_path = stem.with_suffix(".pdf")
        markdown_path.write_text(draft.resume_markdown, encoding="utf-8")
        html_path.write_text(_html_document(draft), encoding="utf-8")
        render_status = self._render_pdf(html_path, pdf_path)
        record = {
            "artifact_id": artifact_id,
            "journey_id": draft.journey_id,
            "company": draft.company,
            "title": draft.title,
            "version": version,
            "status": "draft",
            "fact_source_ids": draft.fact_source_ids,
            "sha256": hashlib.sha256(draft.resume_markdown.encode("utf-8")).hexdigest(),
            "created_at": datetime.now(UTC).isoformat(),
            "markdown_path": str(markdown_path),
            "html_path": str(html_path),
            "pdf_path": str(pdf_path) if pdf_path.is_file() else None,
            "render_status": render_status,
        }
        stem.with_suffix(".json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"status": "draft", **record}

    def confirm(self, artifact_id: str) -> dict[str, Any]:
        for record_path in self._root.glob("*/tailored-resumes/resume-v*.json"):
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if record.get("artifact_id") != artifact_id:
                continue
            if record.get("status") == "confirmed":
                return {"status": "confirmed", **record}
            record["status"] = "confirmed"
            record["confirmed_at"] = datetime.now(UTC).isoformat()
            record_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return {"status": "confirmed", **record}
        return {"status": "failed", "error_type": "artifact_not_found"}

    def _render_pdf(self, source: Path, target: Path) -> str:
        if not self._chrome.is_file():
            return "pdf_renderer_unavailable"
        try:
            subprocess.run(
                [
                    str(self._chrome), "--headless=new", "--disable-gpu",
                    f"--print-to-pdf={target}", source.as_uri(),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return "pdf_render_failed"
        return "pdf_rendered" if target.is_file() else "pdf_render_failed"


def _html_document(draft: TailoredResumeDraft) -> str:
    # Markdown stays canonical; the printable view intentionally treats it as
    # pre-wrapped text so facts cannot be silently dropped by a renderer.
    return f"""<!doctype html><meta charset=\"utf-8\"><style>
    @page {{ size: A4; margin: 17mm }} body {{ font-family: 'Microsoft YaHei', sans-serif;
    color:#17243a; line-height:1.55; font-size:10.5pt }} pre {{ white-space:pre-wrap;
    font-family:inherit }} h1 {{ font-size:18pt; border-bottom:1px solid #1b365d }}
    </style><h1>定制简历 · {html.escape(draft.company)} · {html.escape(draft.title)}</h1>
    <pre>{html.escape(draft.resume_markdown)}</pre>"""


def build_tailored_resume_tools(store: TailoredResumeStore) -> list[BaseTool]:
    async def save_tailored_resume(**kwargs: Any) -> dict[str, Any]:
        return store.save(TailoredResumeDraft.model_validate(kwargs))

    async def confirm_tailored_resume(artifact_id: str) -> dict[str, Any]:
        return store.confirm(artifact_id)

    return [
        StructuredTool.from_function(
            coroutine=save_tailored_resume,
            name="save_tailored_resume",
            description=("保存可追溯的定制简历草稿；每个事实必须给出来源 ID。"),
            args_schema=TailoredResumeDraft,
        ),
        StructuredTool.from_function(
            coroutine=confirm_tailored_resume,
            name="confirm_tailored_resume",
            description="用户审阅后确认定制简历；确认后才可被任何投递渠道使用。",
            args_schema=ConfirmTailoredResume,
        ),
    ]
