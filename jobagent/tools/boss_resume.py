"""Agent tool: upload a PDF as the Boss attachment resume (HITL-gated)."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from jobagent.config import Settings
from jobagent.crawl import CrawlGate

logger = logging.getLogger(__name__)


class BossResumeUploadRequest(BaseModel):
    """One attachment-resume upload; the HITL gate is schema-required."""

    pdf_path: str = Field(
        min_length=1,
        description="Absolute or workspace-relative path to the PDF resume file.",
    )
    user_confirmed: bool = Field(
        description=(
            "MUST be true only after the user has explicitly confirmed uploading "
            "this file to their Boss account. False or absent -> nothing is sent."
        ),
    )
    allow_delete: bool = Field(
        default=False,
        description=(
            "Allow deleting the oldest existing attachment when the account "
            "already holds the maximum of three. Only set after the user "
            "explicitly agreed to the deletion."
        ),
    )


def build_boss_resume_upload_tool(
    settings: Settings,
    *,
    crawl_gate: CrawlGate | None = None,
) -> Any:
    from langchain_core.tools import StructuredTool

    async def upload_boss_resume_pdf(
        pdf_path: str,
        user_confirmed: bool,
        allow_delete: bool = False,
    ) -> dict[str, Any]:
        """Upload one PDF as the Boss直聘 attachment resume.

        Call this only after the user has explicitly confirmed the file
        and the upload. With user_confirmed=false nothing is sent.
        """

        if not user_confirmed:
            return {
                "status": "waiting_user_confirmation",
                "message": (
                    "需要用户明确确认后才能向 Boss 上传附件简历"
                    "（文件路径、是否允许删除旧附件）。"
                ),
                "pdf_path": pdf_path,
            }
        # Upload runs in its own CDP session; never touches the pages the
        # user is interacting with (warlock blanks pages under debug attach).
        from jobagent.applier.boss_resume import BossResumeUploader

        uploader = BossResumeUploader(settings, crawl_gate=crawl_gate)
        async with uploader:
            return await uploader.upload_pdf(pdf_path, allow_delete=allow_delete)

    return StructuredTool.from_function(
        coroutine=upload_boss_resume_pdf,
        name="upload_boss_resume_pdf",
        description=(
            "Upload a PDF file as the Boss直聘 attachment resume (附件简历). "
            "Requires explicit user confirmation first; at most three "
            "attachments exist - set allow_delete only if the user agreed "
            "to delete the oldest one."
        ),
        args_schema=BossResumeUploadRequest,
    )
