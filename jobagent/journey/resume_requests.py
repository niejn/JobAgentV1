"""Durable, idempotent queue for HR resume-request cards."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobagent.journey.store import _enable_wal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resume_requests (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    source_mid INTEGER NOT NULL,
    friend_name TEXT NOT NULL,
    company TEXT NOT NULL,
    job_title TEXT NOT NULL,
    card_payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    selected_resume_id TEXT,
    received_at INTEGER NOT NULL,
    expires_at INTEGER,
    UNIQUE(conversation_id, source_mid)
);
CREATE INDEX IF NOT EXISTS idx_resume_requests_status
    ON resume_requests(status, received_at);
"""

_STATUSES = {
    "received",
    "waiting_approval",
    "approved",
    "accept_action_sent",
    "resume_delivery_pending",
    "confirmed",
    "rejected",
    "expired",
    "unverified",
    "failed",
}
_TRANSITIONS = {
    "received": {"waiting_approval", "approved", "rejected", "expired", "failed"},
    "waiting_approval": {"approved", "rejected", "expired"},
    "approved": {"accept_action_sent", "failed"},
    "accept_action_sent": {"resume_delivery_pending", "confirmed", "unverified", "failed"},
    "resume_delivery_pending": {"confirmed", "unverified", "failed"},
    "confirmed": set(),
    "rejected": set(),
    "expired": set(),
    "unverified": set(),
    "failed": set(),
}


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    id: str
    conversation_id: str
    source_mid: int
    friend_name: str
    company: str
    job_title: str
    card_payload: dict[str, Any]
    status: str
    selected_resume_id: str | None
    received_at: int
    expires_at: int | None


class ResumeRequestQueue:
    """Small public interface hiding SQLite persistence and state rules."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path.expanduser().resolve())
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> ResumeRequestQueue:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def receive(
        self,
        *,
        conversation_id: str,
        source_mid: int,
        friend_name: str,
        company: str,
        job_title: str,
        card_payload: dict[str, Any],
        expires_at: int | None = None,
    ) -> ResumeRequest:
        """Insert a card once, returning the existing row on duplicate delivery."""

        request_id = str(uuid.uuid4())
        now = int(time.time() * 1000)
        self._connection.execute(
            """
            INSERT OR IGNORE INTO resume_requests
                (id, conversation_id, source_mid, friend_name, company, job_title,
                 card_payload_json, status, received_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?, ?)
            """,
            (
                request_id,
                conversation_id,
                int(source_mid),
                friend_name,
                company,
                job_title,
                json.dumps(card_payload, ensure_ascii=False, sort_keys=True),
                now,
                expires_at,
            ),
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT * FROM resume_requests WHERE conversation_id = ? AND source_mid = ?",
            (conversation_id, int(source_mid)),
        ).fetchone()
        if row is None:
            raise RuntimeError("resume request insert was not readable")
        return self._from_row(row)

    def get(self, request_id: str) -> ResumeRequest | None:
        row = self._connection.execute(
            "SELECT * FROM resume_requests WHERE id = ?", (request_id,)
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_pending(self) -> list[ResumeRequest]:
        rows = self._connection.execute(
            """
            SELECT * FROM resume_requests
            WHERE status IN ('received', 'waiting_approval', 'approved',
                             'accept_action_sent', 'resume_delivery_pending')
            ORDER BY received_at ASC
            """
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def transition(
        self, request_id: str, new_status: str, *, selected_resume_id: str | None = None
    ) -> ResumeRequest:
        if new_status not in _STATUSES:
            raise ValueError(f"unknown resume request status: {new_status}")
        current = self.get(request_id)
        if current is None:
            raise KeyError(request_id)
        if new_status not in _TRANSITIONS[current.status]:
            raise ValueError(
                f"invalid resume request transition: {current.status} -> {new_status}"
            )
        self._connection.execute(
            """
            UPDATE resume_requests
            SET status = ?, selected_resume_id = COALESCE(?, selected_resume_id)
            WHERE id = ?
            """,
            (new_status, selected_resume_id, request_id),
        )
        self._connection.commit()
        updated = self.get(request_id)
        if updated is None:
            raise RuntimeError("resume request update was not readable")
        return updated

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ResumeRequest:
        return ResumeRequest(
            id=str(row[0]),
            conversation_id=str(row[1]),
            source_mid=int(row[2]),
            friend_name=str(row[3]),
            company=str(row[4]),
            job_title=str(row[5]),
            card_payload=json.loads(str(row[6])),
            status=str(row[7]),
            selected_resume_id=str(row[8]) if row[8] is not None else None,
            received_at=int(row[9]),
            expires_at=int(row[10]) if row[10] is not None else None,
        )
