"""Prepare, but never send, XHS application email drafts."""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol, cast

from jobagent.applier.email_sender import EmailConfigError, EmailSender
from jobagent.config import Settings
from jobagent.journey.recruitment_notes import RecruitmentNoteRegistry
from jobagent.tools.resume_library import ResumeLibrary

_DRAFT_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS xhs_email_drafts (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
)
_DELIVERY_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS xhs_email_deliveries "
    "(draft_id TEXT PRIMARY KEY, status TEXT NOT NULL, receipt TEXT NOT NULL)"
)


class EmailTransport(Protocol):
    def send(self, **kwargs: Any) -> dict[str, Any]: ...


def contact_evidence_line(draft: dict[str, Any]) -> str:
    evidence = draft.get("contact_evidence")
    if not isinstance(evidence, dict):
        return "邮箱出处：未知（旧版草稿缺少出处记录）"
    return f"邮箱出处：{evidence.get('source', '')}（置信 {evidence.get('confidence', '')}）"


class XhsEmailDraftService:
    def __init__(
        self,
        database: Path,
        resumes: ResumeLibrary,
        settings: Settings,
        sender: EmailTransport | None = None,
    ) -> None:
        self._database, self._resumes = database, resumes
        self._sender = sender or cast(EmailTransport, EmailSender(settings))

    def prepare(
        self,
        note_id: str,
        resume_file_name: str,
        sender_name: str,
        subject: str | None = None,
        body_text: str | None = None,
    ) -> dict[str, Any]:
        """Persist a reviewable draft; user-confirmed subject/body are kept verbatim."""
        with RecruitmentNoteRegistry(self._database) as notes:
            try:
                note = notes.get(note_id)
            except KeyError:
                return {"status": "failed", "error_type": "note_not_saved"}
            try:
                company, position = notes.selected_position(note_id)
            except KeyError:
                return {"status": "failed", "error_type": "position_not_selected"}
        contact = next((item for item in note.contacts if item.confidence == "verified"), None)
        if contact is None:
            return {"status": "blocked", "error_type": "contact_missing"}
        resume = next(
            (
                item
                for item in self._resumes.list()["resumes"]
                if item["file_name"] == resume_file_name
            ),
            None,
        )
        if resume is None:
            return {"status": "failed", "error_type": "resume_not_found"}
        confirmed_subject = subject or ""
        confirmed_body = body_text or ""
        draft = {
            "status": "drafted",
            "note_id": note_id,
            "source_url": note.source_url,
            "company": company,
            "role": position.title,
            "to": contact.email,
            "contact_evidence": {"source": contact.source, "confidence": contact.confidence},
            "subject": confirmed_subject or f"应聘 {position.title}｜{sender_name}",
            "body_text": (
                confirmed_body
                or (
                    f"您好，\n\n我想应聘贵司 {position.title} 岗位，"
                    f"附件为我的简历，期待交流。\n\n{sender_name}"
                )
            ),
            "content_origin": (
                "user_confirmed" if (confirmed_subject or confirmed_body) else "template"
            ),
            "resume": resume,
        }
        draft_id = f"{note_id}:{resume['sha256'][:16]}:{contact.email}"
        draft["draft_id"] = draft_id
        with sqlite3.connect(self._database) as connection:
            connection.execute(_DRAFT_SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO xhs_email_drafts VALUES (?, ?)",
                (draft_id, json.dumps(draft, ensure_ascii=False)),
            )
        return draft

    def get(self, draft_id: str) -> dict[str, Any]:
        with sqlite3.connect(self._database) as connection:
            row = connection.execute(
                "SELECT payload FROM xhs_email_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        if row is None:
            raise KeyError(draft_id)
        payload = json.loads(row[0])
        return cast(dict[str, Any], payload)

    def approval_preview(self, draft_id: str) -> str:
        draft = self.get(draft_id)
        resume = cast(dict[str, Any], draft.get("resume") or {})
        attachment = (
            f"{resume.get('file_name', '')} | {resume.get('size_bytes', '')} bytes | "
            f"SHA-256: {resume.get('sha256', '')}"
        )
        return "\n".join(
            (
                "发送 XHS 求职邮件（完整审阅）：",
                f"来源帖子：{draft.get('note_id', '')}",
                f"帖子链接：{draft.get('source_url', '')}",
                f"公司 / 岗位：{draft.get('company', '')} / {draft.get('role', '')}",
                contact_evidence_line(draft),
                f"收件人：{draft.get('to', '')}",
                f"主题：{draft.get('subject', '')}",
                "正文：",
                str(draft.get("body_text", "")),
                "附件：",
                attachment,
            )
        )

    async def send(self, draft_id: str) -> dict[str, Any]:
        draft = self.get(draft_id)
        with sqlite3.connect(self._database) as connection:
            connection.execute(_DELIVERY_SCHEMA)
            prior = connection.execute(
                "SELECT status, receipt FROM xhs_email_deliveries WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            if prior is not None and prior[0] == "submitted":
                return {
                    "status": "blocked",
                    "error_type": "already_submitted",
                    "receipt": json.loads(prior[1]),
                }
        resume = self._resumes.path_for(str(draft["resume"]["file_name"]))
        if resume is None:
            return {"status": "failed", "error_type": "resume_not_found"}
        try:
            body_html = f"<p>{draft['body_text'].replace(chr(10), '<br>')}</p>"
            result = await asyncio.to_thread(
                self._sender.send,
                to=draft["to"],
                subject=draft["subject"],
                body_html=body_html,
                body_text=draft["body_text"],
                attachment=resume,
            )
        except EmailConfigError as exc:
            result = {"status": "failed", "error_type": "config_missing", "message": str(exc)}
        status = (
            "submitted"
            if result.get("status") == "ok"
            else "unverified"
            if result.get("status") == "unverified"
            else "failed"
        )
        with sqlite3.connect(self._database) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO xhs_email_deliveries VALUES (?, ?, ?)",
                (draft_id, status, json.dumps(result, ensure_ascii=False)),
            )
        if status == "submitted":
            from jobagent.journey.job_registry import JobProgressStatus, SQLiteJobRegistry

            job_id = f"xhs:{draft['note_id']}"
            with SQLiteJobRegistry(self._database) as registry:
                registry.upsert_discovered(
                    job_id=job_id,
                    source="xhs",
                    company=str(draft["company"]),
                    title=str(draft["role"]),
                )
                record = registry.get(job_id)
                if record is not None and record.status in {
                    JobProgressStatus.DISCOVERED,
                    JobProgressStatus.RECOMMENDED,
                }:
                    registry.mark(
                        job_id,
                        JobProgressStatus.APPLIED,
                        note=f"XHS 邮件投递 {draft['to']}",
                    )
            from jobagent.journey.store import SQLiteJourneyStore
            with SQLiteJourneyStore(self._database) as journeys:
                journeys.mark_applied_by_creation_key(
                    f"source:xhs:{draft['note_id']}",
                    reason=f"XHS 邮件已提交至 {draft['to']}",
                )
        return {"status": status, "draft_id": draft_id, "receipt": result}
