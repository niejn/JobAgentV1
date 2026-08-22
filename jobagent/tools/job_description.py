"""Workspace-scoped text document readers and LangChain Tool adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

_ALLOWED_SUFFIXES = frozenset({".txt", ".md"})


class JobDescriptionFile(BaseModel):
    file_path: str = Field(
        min_length=1,
        description="User-named .txt or .md file inside the configured workspace",
    )


class UserDocumentFile(BaseModel):
    file_path: str = Field(
        min_length=1,
        description=(
            "User-named .txt or .md resume, JD, or candidate document inside the "
            "configured workspace"
        ),
    )


class _WorkspaceTextReader:
    """Read one explicitly named small text file without exposing filesystem access."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        max_bytes: int = 100_000,
        document_label: str = "document",
    ) -> None:
        self._root = workspace_root.expanduser().resolve()
        self._max_bytes = max_bytes
        self._document_label = document_label

    def read(self, file_path: str) -> dict[str, Any]:
        try:
            candidate = (self._root / file_path).resolve()
            if not candidate.is_relative_to(self._root):
                raise ValueError("file is outside the configured workspace")
            if candidate.suffix.lower() not in _ALLOWED_SUFFIXES:
                raise ValueError(
                    f"only .txt and .md {self._document_label} files are allowed"
                )
            if not candidate.is_file():
                raise ValueError("file does not exist")
            if candidate.stat().st_size > self._max_bytes:
                raise ValueError(f"file exceeds the {self._document_label} size limit")
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            return {
                "status": "failed",
                "error_type": "invalid_file",
                "message": str(exc),
            }
        return {
            "status": "completed",
            "source_name": candidate.name,
            "content": content,
            "characters": len(content),
        }


class JobDescriptionReader(_WorkspaceTextReader):
    """Read one explicitly named Job Description text file."""

    def __init__(self, workspace_root: Path, *, max_bytes: int = 100_000) -> None:
        super().__init__(
            workspace_root,
            max_bytes=max_bytes,
            document_label="Job Description",
        )


class UserDocumentReader(_WorkspaceTextReader):
    """Read one explicitly named user-owned resume, JD, or candidate text file."""

    def __init__(self, workspace_root: Path, *, max_bytes: int = 100_000) -> None:
        super().__init__(
            workspace_root,
            max_bytes=max_bytes,
            document_label="user document",
        )


def build_job_description_tool(reader: JobDescriptionReader) -> BaseTool:
    """Expose the restricted reader as a business Tool."""

    async def read_job_description(file_path: str) -> dict[str, Any]:
        """Read a user-named JD text file from the configured workspace.

        Only call when the user explicitly identifies a .txt or .md file. Do not guess,
        enumerate directories, or use this Tool for resumes, secrets, or unrelated files.
        """

        return reader.read(file_path)

    return StructuredTool.from_function(
        coroutine=read_job_description,
        name="read_job_description",
        description=(
            "Read one explicitly user-named .txt or .md Job Description file inside "
            "the configured JobAgent workspace. It cannot access files outside the workspace."
        ),
        args_schema=JobDescriptionFile,
    )


def build_user_document_tool(reader: UserDocumentReader) -> BaseTool:
    """Expose a restricted reader for explicitly user-selected text documents."""

    async def read_user_document(file_path: str) -> dict[str, Any]:
        """Read a user-named resume, JD, or candidate text file from the workspace.

        Call only after the user explicitly identifies the file. Never guess filenames,
        enumerate directories, or read credentials and unrelated application files.
        """

        return reader.read(file_path)

    return StructuredTool.from_function(
        coroutine=read_user_document,
        name="read_user_document",
        description=(
            "Read one explicitly user-named .txt or .md resume, JD, or candidate-owned "
            "document inside the configured JobAgent workspace. It cannot enumerate "
            "directories or access files outside the workspace."
        ),
        args_schema=UserDocumentFile,
    )
