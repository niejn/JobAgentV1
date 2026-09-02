"""Agent tool: upload a PDF as the Boss attachment resume (middleware-approved)."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from jobagent.config import Settings
from jobagent.crawl import CrawlGate

logger = logging.getLogger(__name__)


class BossResumeUploadRequest(BaseModel):
    """One attachment-resume upload; approved via the HITL middleware."""

    pdf_path: str = Field(
        min_length=1,
        description="Absolute or workspace-relative path to the PDF resume file.",
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
        allow_delete: bool = False,
    ) -> dict[str, Any]:
        """Upload one PDF as the Boss直聘 attachment resume.

        The HITL middleware pauses for explicit user approval before this
        body runs; no parameter gate is needed here.
        """
        # Upload runs in its own CDP session; never touches the pages the
        # user is interacting with (warlock blanks pages under debug attach).
        from jobagent.applier.boss_circuit import BossCircuit, boss_circuit_path

        circuit = BossCircuit(boss_circuit_path(settings.jobagent_state_db))
        if (refusal := circuit.check()) is not None:
            return refusal
        from jobagent.applier.boss_resume import BossResumeUploader

        async with BossResumeUploader(settings) as uploader:
            result = await uploader.upload_pdf(pdf_path, allow_delete=allow_delete)
        circuit.record(result)
        return result

    return StructuredTool.from_function(
        coroutine=upload_boss_resume_pdf,
        name="upload_boss_resume_pdf",
        description=(
            "Upload a PDF file as the Boss直聘 attachment resume (附件简历). "
            "Execution pauses for explicit user approval; at most three "
            "attachments exist - set allow_delete only if the user agreed "
            "to delete the oldest one."
        ),
        args_schema=BossResumeUploadRequest,
    )
