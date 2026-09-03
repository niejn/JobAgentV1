"""Durable registry for Boss HR conversations and contact attempts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jobagent.journey.store import _enable_wal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boss_conversations (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    friend_id INTEGER NOT NULL,
    friend_source INTEGER NOT NULL,
    encrypt_boss_id TEXT NOT NULL,
    friend_name TEXT NOT NULL,
    company TEXT NOT NULL,
    job_title TEXT NOT NULL,
    status TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(job_id, friend_id)
);
CREATE TABLE IF NOT EXISTS boss_contact_attempts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    conversation_id TEXT,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    requested_text_hash TEXT,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS boss_contact_audit (
    id INTEGER PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS boss_job_transport (
    job_id TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class BossContactAttempt:
    id: str
    job_id: str
    action: str
    idempotency_key: str
    status: str
    result: dict[str, Any]


class BossContactRegistry:
    """Small persistence seam for idempotent Boss contact workflows."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path.expanduser().resolve())
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> BossContactRegistry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def begin_attempt(
        self, *, job_id: str, action: str, requested_text: str | None
    ) -> BossContactAttempt:
        key = self.idempotency_key(job_id=job_id, action=action, text=requested_text)
        now = int(time.time() * 1000)
        attempt_id = str(uuid.uuid4())
        text_hash = hashlib.sha256(requested_text.encode()).hexdigest() if requested_text else None
        self._connection.execute(
            """
            INSERT OR IGNORE INTO boss_contact_attempts
                (id, job_id, action, idempotency_key, requested_text_hash,
                 status, result_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'started', '{}', ?, ?)
            """,
            (attempt_id, job_id, action, key, text_hash, now, now),
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT id, job_id, action, idempotency_key, status, result_json "
            "FROM boss_contact_attempts WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("contact attempt was not readable")
        return self._attempt(row)

    def save_conversation(self, *, job_id: str, target: dict[str, Any]) -> str:
        now = int(time.time() * 1000)
        conversation_id = f"boss:{target['friend_id']}"
        self._connection.execute(
            """
            INSERT INTO boss_conversations
                (id, job_id, friend_id, friend_source, encrypt_boss_id,
                 friend_name, company, job_title, status, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(job_id, friend_id) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                status='active',
                encrypt_boss_id=excluded.encrypt_boss_id
            """,
            (
                conversation_id,
                job_id,
                int(target["friend_id"]),
                int(target.get("friend_source") or 0),
                str(target["encrypt_boss_id"]),
                str(target.get("name") or ""),
                str(target.get("company") or ""),
                str(target.get("job_title") or ""),
                now,
                now,
            ),
        )
        self._connection.commit()
        return conversation_id

    def save_job_transport(self, *, job_id: str, metadata: dict[str, Any]) -> None:
        """Persist only internal fields needed to contact a discovered job."""

        allowed = {
            key: metadata[key]
            for key in (
                "security_id",
                "encrypt_boss_id",
                "boss_name",
                "boss_title",
                "lid",
                "job_source",
            )
            if metadata.get(key) not in (None, "")
        }
        if not allowed:
            return
        self._connection.execute(
            """
            INSERT INTO boss_job_transport(job_id, metadata_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (job_id, json.dumps(allowed, ensure_ascii=False), int(time.time() * 1000)),
        )
        self._connection.commit()

    def get_job_transport(self, job_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT metadata_json FROM boss_job_transport WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return {}
        payload = json.loads(str(row[0]))
        return cast(dict[str, Any], payload) if isinstance(payload, dict) else {}

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        result: dict[str, Any],
        conversation_id: str | None = None,
    ) -> None:
        now = int(time.time() * 1000)
        self._connection.execute(
            """
            UPDATE boss_contact_attempts
            SET status=?, result_json=?, conversation_id=COALESCE(?, conversation_id), updated_at=?
            WHERE id=?
            """,
            (status, json.dumps(result, ensure_ascii=False), conversation_id, now, attempt_id),
        )
        self._connection.execute(
            """
            INSERT INTO boss_contact_audit
                (attempt_id, event_type, details_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (attempt_id, status, json.dumps(result, ensure_ascii=False), now),
        )
        self._connection.commit()

    @staticmethod
    def idempotency_key(*, job_id: str, action: str, text: str | None) -> str:
        digest = hashlib.sha256((text or "").encode()).hexdigest()[:16]
        return f"{job_id}:{action}:{digest}"

    @staticmethod
    def _attempt(row: tuple[Any, ...]) -> BossContactAttempt:
        return BossContactAttempt(
            id=str(row[0]),
            job_id=str(row[1]),
            action=str(row[2]),
            idempotency_key=str(row[3]),
            status=str(row[4]),
            result=json.loads(str(row[5])),
        )
