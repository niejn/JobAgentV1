"""Durable XHS drafts, exclusive SMTP submission, and recoverable state projection."""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Protocol, cast

from jobagent.applier.email_sender import EmailConfigError, EmailSender
from jobagent.applier.email_sent_verifier import EmailSentFolderVerifier
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
logger = logging.getLogger(__name__)


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
        verifier: EmailSentFolderVerifier | None = None,
    ) -> None:
        self._database, self._resumes = database, resumes
        self._sender = sender or cast(EmailTransport, EmailSender(settings))
        self._verifier = verifier or EmailSentFolderVerifier.from_smtp_settings(settings)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Close connections deterministically; serialize additive legacy migrations."""
        self._database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_DRAFT_SCHEMA)
                connection.execute(_DELIVERY_SCHEMA)
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(xhs_email_deliveries)")
                }
                for column, default in (("message_id", ""), ("sync_status", "pending")):
                    if column not in columns:
                        connection.execute(
                            f"ALTER TABLE xhs_email_deliveries ADD COLUMN {column} "
                            f"TEXT NOT NULL DEFAULT '{default}'"
                        )
            with connection:
                yield connection
        finally:
            connection.close()

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
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO xhs_email_drafts VALUES (?, ?)",
                (draft_id, json.dumps(draft, ensure_ascii=False)),
            )
        return draft

    def get(self, draft_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM xhs_email_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        if row is None:
            raise KeyError(draft_id)
        payload = json.loads(row[0])
        return cast(dict[str, Any], payload)

    def approval_preview(self, draft_id: str) -> str:
        try:
            draft = self.get(draft_id)
        except KeyError:
            return "draft_not_found：邮件草稿不存在，请重新准备草稿。"
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
        try:
            draft = self.get(draft_id)
        except KeyError:
            return {"status": "failed", "error_type": "draft_not_found"}
        prior = self._claim(draft_id)
        if prior is not None:
            if prior["status"] == "submitted":
                sync_status = await asyncio.to_thread(self._sync_submitted, draft_id)
                return {
                    "status": "blocked",
                    "error_type": "already_submitted",
                    "receipt": json.loads(prior["receipt"]),
                    "sync_status": sync_status,
                }
            return {
                "status": prior["status"],
                "draft_id": draft_id,
                "error_type": "delivery_in_progress"
                if prior["status"] == "sending"
                else "delivery_unverified",
                "receipt": json.loads(prior["receipt"]),
            }
        # The committed claim owns this Message-ID before any SMTP operation.
        with self._connect() as connection:
            row = connection.execute(
                "SELECT message_id FROM xhs_email_deliveries WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            message_id = str(row[0])
        resume = self._resumes.path_for(str(draft["resume"]["file_name"]))
        if resume is None:
            result = {"status": "failed", "error_type": "resume_not_found"}
            return self._finish(draft_id, message_id, result)
        try:
            body_html = f"<p>{draft['body_text'].replace(chr(10), '<br>')}</p>"
            result = await asyncio.to_thread(
                self._sender.send,
                to=draft["to"],
                subject=draft["subject"],
                body_html=body_html,
                body_text=draft["body_text"],
                attachment=resume,
                message_id=message_id,
            )
        except EmailConfigError as exc:
            result = {"status": "failed", "error_type": "config_missing", "message": str(exc)}
        except Exception as exc:
            # An unexpected transport error cannot prove SMTP did not accept DATA.
            result = {"status": "unverified", "error_type": type(exc).__name__}
        response = self._finish(draft_id, message_id, result)
        if response["status"] == "submitted":
            response["sync_status"] = await asyncio.to_thread(self._sync_submitted, draft_id)
        return response

    def _claim(self, draft_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM xhs_email_deliveries WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if prior is not None and prior["status"] != "failed":
                return cast(sqlite3.Row, prior)
            message_id = make_msgid()
            connection.execute(
                "INSERT OR REPLACE INTO xhs_email_deliveries "
                "(draft_id, status, receipt, message_id, sync_status) VALUES (?, ?, ?, ?, ?)",
                (
                    draft_id,
                    "sending",
                    json.dumps({"message_id": message_id}),
                    message_id,
                    "pending",
                ),
            )
        return None

    def _finish(self, draft_id: str, message_id: str, result: dict[str, Any]) -> dict[str, Any]:
        result = {**result, "message_id": message_id}
        status = (
            "submitted"
            if result.get("status") == "ok"
            else "failed"
            if result.get("status") == "failed"
            else "unverified"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # A concurrent recovery may already have positive Sent evidence.
            connection.execute(
                "UPDATE xhs_email_deliveries SET status = ?, receipt = ? "
                "WHERE draft_id = ? AND message_id = ? AND status != 'submitted'",
                (status, json.dumps(result, ensure_ascii=False), draft_id, message_id),
            )
            row = connection.execute(
                "SELECT status, receipt FROM xhs_email_deliveries WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return {
            "status": row["status"],
            "draft_id": draft_id,
            "receipt": json.loads(row["receipt"]),
        }

    def recover(self) -> list[dict[str, Any]]:
        """Startup reconciliation; never calls SMTP, including on missing Sent evidence."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM xhs_email_deliveries WHERE status IN ('sending', 'unverified') "
                "OR (status = 'submitted' AND sync_status != 'done')"
            ).fetchall()
        results = []
        for row in rows:
            try:
                if row["status"] in {"sending", "unverified"}:
                    receipt = json.loads(row["receipt"])
                    message_id = row["message_id"] or receipt.get("message_id", "")
                    verification = self._verifier.find(message_id)
                    receipt.update(message_id=message_id, verification=verification.reason)
                    with self._connect() as connection:
                        # Compare-and-swap prevents a stale lookup overwriting SMTP success.
                        connection.execute(
                            "UPDATE xhs_email_deliveries SET status = ?, receipt = ?, "
                            "message_id = ? WHERE draft_id = ? AND status = ? AND receipt = ?",
                            (
                                verification.status,
                                json.dumps(receipt, ensure_ascii=False),
                                message_id,
                                row["draft_id"],
                                row["status"],
                                row["receipt"],
                            ),
                        )
                sync_status = self._sync_submitted(row["draft_id"])
                results.append({"draft_id": row["draft_id"], "sync_status": sync_status})
            except Exception:
                logger.exception("XHS email recovery failed for %s", row["draft_id"])
        return results

    def _sync_submitted(self, draft_id: str) -> str:
        """Replay idempotent projections; SMTP receipt remains durable on any failure."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, sync_status FROM xhs_email_deliveries WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None or row["status"] != "submitted":
            return "pending"
        if row["sync_status"] == "done":
            return "done"
        try:
            draft = self.get(draft_id)
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
            with self._connect() as connection:
                connection.execute(
                    "UPDATE xhs_email_deliveries SET sync_status = 'done' "
                    "WHERE draft_id = ? AND status = 'submitted'",
                    (draft_id,),
                )
            return "done"
        except Exception:
            logger.exception("XHS email state sync pending for %s", draft_id)
            return "pending"
