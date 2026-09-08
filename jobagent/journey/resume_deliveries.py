"""Durable receipts for platform resume deliveries."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from jobagent.journey.store import _enable_wal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resume_deliveries (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    source_mid TEXT NOT NULL,
    resume_file_name TEXT NOT NULL,
    hr_name TEXT NOT NULL,
    company TEXT NOT NULL,
    job_title TEXT NOT NULL,
    status TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(conversation_id, source_mid, resume_file_name)
);
"""


class ResumeDeliveryRegistry:
    """Small idempotency and audit seam for successful resume deliveries."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path.expanduser().resolve())
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> ResumeDeliveryRegistry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def already_confirmed(
        self, *, conversation_id: str, source_mid: str, resume_file_name: str
    ) -> bool:
        row = self._connection.execute(
            """SELECT 1 FROM resume_deliveries
               WHERE conversation_id = ? AND source_mid = ? AND resume_file_name = ?
                 AND status = 'confirmed'""",
            (conversation_id, source_mid, resume_file_name),
        ).fetchone()
        return row is not None

    def record(
        self,
        *,
        conversation_id: str,
        source_mid: str,
        resume_file_name: str,
        hr_name: str,
        company: str,
        job_title: str,
        status: str,
        receipt: dict[str, Any],
    ) -> None:
        self._connection.execute(
            """INSERT INTO resume_deliveries
               (conversation_id, source_mid, resume_file_name, hr_name, company,
                job_title, status, receipt_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(conversation_id, source_mid, resume_file_name)
               DO UPDATE SET status = excluded.status, receipt_json = excluded.receipt_json,
                             created_at = excluded.created_at""",
            (
                conversation_id,
                source_mid,
                resume_file_name,
                hr_name,
                company,
                job_title,
                status,
                json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                int(time.time() * 1000),
            ),
        )
        self._connection.commit()
