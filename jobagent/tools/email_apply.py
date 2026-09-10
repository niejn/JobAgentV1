"""Business Tool for sending application emails (XHS referral channel).

Approval lives in the agent's HumanInTheLoopMiddleware: the tool call is
physically paused before this body runs; only an approved resume reaches it.
On a successful send the job is registered under its cross-platform identity
(F1-R4) and marked ``applied``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, field_validator

from jobagent.config import Settings

logger = logging.getLogger(__name__)


class SendApplicationEmailRequest(BaseModel):
    """One application email plus registry linkage."""

    to: str = Field(min_length=3, description="HR 邮箱地址")
    subject: str = Field(min_length=1, max_length=200)
    body_html: str = Field(min_length=1, description="HTML 邮件正文")
    body_text: str = Field(default="", description="纯文本正文（缺省自动从 HTML 降级）")
    attachment_path: str = Field(
        default="",
        description="简历 PDF 路径；留空或传 none 字符串则不附简历（不存在隐式默认简历）",
    )
    company: str = Field(default="", description="公司名（用于登记册入册）")
    title: str = Field(default="", description="岗位名（用于登记册入册）")
    source_url: str = Field(default="", description="来源帖/岗位链接")
    record_id: str = Field(
        default="",
        description="登记册记录 ID；空则按 company+title 生成（xhs/email 前缀）",
    )

    @field_validator("to")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        cleaned = value.strip()
        if "@" not in cleaned or "." not in cleaned.split("@")[-1]:
            raise ValueError(f"invalid recipient email: {value}")
        return cleaned


def _default_record_id(request: SendApplicationEmailRequest) -> str:
    if request.record_id.strip():
        return request.record_id.strip()
    from jobagent.journey.identity import normalize_company

    token = normalize_company(request.company) or "unknown"
    domain = request.to.split("@")[1].split(".")[0]
    tail = request.title[:20].strip() if request.title else ""
    return f"email:{domain}:{token}:{tail}" if tail else f"email:{domain}:{token}"


def build_send_application_email_tool(settings: Settings) -> BaseTool:
    """Send one application email with a hard HITL gate."""

    from jobagent.applier.email_sender import EmailConfigError, EmailSender

    sender = EmailSender(settings)
    state_db = settings.jobagent_state_db.expanduser().resolve()

    async def _run(
        to: str,
        subject: str,
        body_html: str,
        body_text: str = "",
        attachment_path: str = "",
        company: str = "",
        title: str = "",
        source_url: str = "",
        record_id: str = "",
    ) -> dict[str, Any]:
        request = SendApplicationEmailRequest(
            to=to,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            attachment_path=attachment_path,
            company=company,
            title=title,
            source_url=source_url,
            record_id=record_id,
        )
        # Approval moved to HumanInTheLoopMiddleware: this tool body runs
        # only after the user approved the interrupt (or in direct tests).

        attachment: Path | None = None
        if request.attachment_path.strip().lower() not in {"", "none"}:
            attachment = Path(request.attachment_path.strip())

        def _send() -> dict[str, Any]:
            return sender.send(
                to=request.to,
                subject=request.subject,
                body_html=request.body_html,
                body_text=request.body_text,
                attachment=attachment,
            )

        try:
            result = await asyncio.to_thread(_send)
        except EmailConfigError as exc:
            return {"status": "failed", "error_type": "config_missing", "message": str(exc)}

        if result.get("status") != "ok":
            return result

        # Register the job in the cross-platform journey registry (F1-R4).
        registry_note = f"邮件投递 {request.to}"
        job_id = _default_record_id(request)
        if request.company.strip() and request.title.strip():
            try:
                from jobagent.journey.job_registry import (
                    JobProgressStatus,
                    SQLiteJobRegistry,
                )

                def _register() -> str | None:
                    with SQLiteJobRegistry(state_db) as registry:
                        registry.upsert_discovered(
                            job_id=job_id,
                            source="email",
                            company=request.company,
                            title=request.title,
                            url=request.source_url,
                        )
                        record = registry.get(job_id)
                        if record is not None and record.status in {
                            JobProgressStatus.DISCOVERED,
                            JobProgressStatus.RECOMMENDED,
                        }:
                            registry.mark(job_id, JobProgressStatus.APPLIED, note=registry_note)
                        else:
                            # Already greeted/applied on another platform:
                            # the identity carries it; refresh the note only.
                            registry.mark(
                                job_id,
                                record.status if record else JobProgressStatus.APPLIED,
                                note=registry_note,
                            )
                        final = registry.get(job_id)
                        return final.status.value if final else None

                applied_status = await asyncio.to_thread(_register)
                result["registry"] = {
                    "job_id": job_id,
                    "status": applied_status,
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("registry update after email send failed", exc_info=True)
                result["registry"] = {"job_id": job_id, "error": str(exc)}
        else:
            result["registry"] = {"job_id": job_id, "skipped": "missing company/title"}

        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name="send_application_email",
        description=(
            "发送求职投递邮件（HTML 正文 + 简历 PDF 附件）到 HR 邮箱。执行前会暂停等待人工批准。"
            "发送成功后自动在岗位登记册入册并标记 applied（跨平台身份归并）。"
            "HITL：必须先向用户展示完整邮件（收件人/主题/正文/附件）并确认，"
            ""
        ),
        args_schema=SendApplicationEmailRequest,
    )
