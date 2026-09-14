"""Durable queue for Boss HR reply drafts and HITL decisions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from jobagent.journey.store import _enable_wal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boss_reply_queue (
    reply_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    source_message_ids TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    hr_name TEXT NOT NULL,
    hr_message TEXT NOT NULL,
    draft_text TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    intent TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    fact_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'awaiting_human',
    draft_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TEXT,
    sent_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_boss_reply_status ON boss_reply_queue(status, created_at);
"""


class BossReplyQueue:
    def __init__(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(resolved)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def enqueue(self, **item: Any) -> str:
        reply_id = str(item.get("reply_id") or uuid.uuid4().hex)
        self._connection.execute(
            """INSERT OR IGNORE INTO boss_reply_queue
            (reply_id, conversation_id, source_message_ids, company, title, hr_name,
             hr_message, draft_text, risk_level, intent, confidence, fact_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                reply_id,
                str(item.get("conversation_id") or ""),
                json.dumps(item.get("source_message_ids", [])),
                str(item.get("company") or ""),
                str(item.get("title") or ""),
                str(item.get("hr_name") or ""),
                str(item.get("hr_message") or ""),
                str(item.get("draft_text") or ""),
                str(item.get("risk_level") or "high"),
                str(item.get("intent") or "unknown"),
                float(item.get("confidence") or 0),
                json.dumps(item.get("fact_ids", [])),
            ),
        )
        self._connection.commit()
        return reply_id

    def pending(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT * FROM boss_reply_queue
            WHERE status IN ('awaiting_human','auto_ready')
            ORDER BY created_at LIMIT ?""",
            (max(1, min(limit, 100)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def decide(
        self,
        reply_id: str,
        decision: str,
        *,
        draft_version: int,
        draft_text: str | None = None,
    ) -> bool:
        if decision not in {"approve", "skip"}:
            raise ValueError("decision must be approve or skip")
        status = "approved" if decision == "approve" else "ignored"
        fields = (
            "status = ?, approved_at = CURRENT_TIMESTAMP"
            if status == "approved"
            else "status = ?"
        )
        params: list[Any] = [status]
        if draft_text is not None:
            fields = "draft_text = ?, draft_version = draft_version + 1, " + fields
            params.insert(0, draft_text)
        params.extend([reply_id, draft_version])
        cur = self._connection.execute(
            f"""UPDATE boss_reply_queue SET {fields}
            WHERE reply_id = ? AND draft_version = ?
            AND status = 'awaiting_human'""",
            params,
        )
        self._connection.commit()
        return cur.rowcount == 1

    def get(self, reply_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM boss_reply_queue WHERE reply_id = ?", (reply_id,)
        ).fetchone()
        return dict(row) if row else None
