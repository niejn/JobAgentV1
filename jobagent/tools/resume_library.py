"""Controlled local PDF resume library for attachment selection."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field


class RegisterResumePdfRequest(BaseModel):
    source_path: str = Field(min_length=1, description="用户本轮明确提供的已有 PDF 简历路径。")


def _describe(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"file_name": path.name, "size_bytes": path.stat().st_size, "sha256": digest}


class ResumeLibrary:
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()

    def list(self) -> dict[str, Any]:
        self._root.mkdir(parents=True, exist_ok=True)
        files = sorted(self._root.glob("*.pdf"), key=lambda item: item.name.casefold())
        return {"status": "ok", "resumes": [_describe(path) for path in files]}

    def register(self, source_path: str) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() != ".pdf":
            return {"status": "failed", "error_type": "pdf_not_found"}
        if source.stat().st_size > 25 * 1024 * 1024:
            return {"status": "failed", "error_type": "pdf_too_large"}
        if not source.read_bytes()[:5].startswith(b"%PDF-"):
            return {"status": "failed", "error_type": "invalid_pdf"}
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / source.name
        if target.exists() and _describe(target)["sha256"] == _describe(source)["sha256"]:
            return {"status": "existing", **_describe(target)}
        shutil.copy2(source, target)
        return {"status": "registered", **_describe(target)}

    def path_for(self, file_name: str) -> Path | None:
        path = (self._root / Path(file_name).name).resolve()
        return path if path.parent == self._root and path.is_file() else None


def build_resume_library_tools(library: ResumeLibrary) -> list[BaseTool]:
    async def register_resume_pdf(source_path: str) -> dict[str, Any]:
        return library.register(source_path)

    async def list_available_resume_pdfs() -> dict[str, Any]:
        return library.list()

    return [
        StructuredTool.from_function(
            coroutine=register_resume_pdf,
            name="register_resume_pdf",
            description="导入用户明确提供路径的 PDF 简历到受控简历库。",
            args_schema=RegisterResumePdfRequest,
        ),
        StructuredTool.from_function(
            coroutine=list_available_resume_pdfs,
            name="list_available_resume_pdfs",
            description="列出受控简历库中的 PDF 文件名、大小和 SHA-256。",
        ),
    ]
