"""Shared Boss reply management seam for CLI and messaging adapters."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobagent.boss_reply_queue import BossReplyQueue
from jobagent.journey.store import _enable_wal

_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS management_outbox (
    event_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at TEXT,
    error TEXT,
    UNIQUE(channel, event_type, aggregate_id)
);
CREATE INDEX IF NOT EXISTS idx_management_outbox_pending
ON management_outbox(channel, status, available_at, created_at);
"""


@dataclass(frozen=True, slots=True)
class ManagementNotification:
    event_id: str
    text: str


class BossReplyApplicationService:
    """Own reply decisions and notification outbox semantics."""

    def __init__(self, state_db: Path) -> None:
        self._state_db = state_db.expanduser().resolve()
        self._state_db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._state_db)
        try:
            _enable_wal(connection)
            connection.executescript(_OUTBOX_SCHEMA)
            connection.commit()
        finally:
            connection.close()

    def create_draft(self, **item: Any) -> str:
        queue = BossReplyQueue(self._state_db)
        try:
            reply_id = queue.enqueue(**item)
            row = queue.get(reply_id)
        finally:
            queue.close()
        if row and row["status"] == "awaiting_human":
            self._enqueue_notification(reply_id, row)
        return reply_id

    def list_pending(self, limit: int = 20) -> list[dict[str, Any]]:
        queue = BossReplyQueue(self._state_db)
        try:
            return queue.pending(limit)
        finally:
            queue.close()

    def list_inbox(self, limit: int = 10) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT message_key, conversation_id, company, title, hr_name,
                          text, sent_at, processing_status
                FROM boss_inbound_messages
                WHERE baseline=0 AND processing_status='unclassified'
                ORDER BY sent_at, first_seen_at LIMIT ?""",
                (max(1, min(limit, 50)),),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            connection.close()

    def publish_inbound(self, messages: list[object]) -> None:
        connection = self._connect()
        try:
            for message in messages:
                message_key = str(getattr(message, "message_key", ""))
                if not message_key:
                    continue
                text = (
                    "Boss 收到一条新的 HR 消息\n"
                    f"{getattr(message, 'company', '')} / "
                    f"{getattr(message, 'title', '')} / "
                    f"HR {getattr(message, 'hr_name', '')}\n"
                    f"HR：{getattr(message, 'text', '')}\n"
                    "发送 /boss inbox 查看未处理消息。"
                )
                connection.execute(
                    """INSERT OR IGNORE INTO management_outbox
                    (event_id, channel, event_type, aggregate_id, payload_json, available_at)
                    VALUES (?, 'wechat', 'boss_inbound_detected', ?, ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        message_key,
                        json.dumps({"text": text}, ensure_ascii=False),
                        time.time(),
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def resolve(self, reply_ref: str) -> dict[str, Any] | None:
        queue = BossReplyQueue(self._state_db)
        try:
            exact = queue.get(reply_ref)
            if exact is not None:
                return exact
            matches = [
                row for row in queue.pending(100) if str(row["reply_id"]).startswith(reply_ref)
            ]
            return matches[0] if len(matches) == 1 else None
        finally:
            queue.close()

    def decide(
        self,
        reply_ref: str,
        *,
        decision: str,
        draft_version: int,
        draft_text: str | None = None,
    ) -> dict[str, Any]:
        row = self.resolve(reply_ref)
        if row is None:
            return {"status": "not_found_or_ambiguous"}
        queue = BossReplyQueue(self._state_db)
        try:
            changed = queue.decide(
                str(row["reply_id"]),
                decision,
                draft_version=draft_version,
                draft_text=draft_text,
            )
            current = queue.get(str(row["reply_id"]))
        finally:
            queue.close()
        return {
            "status": "updated" if changed else "version_or_state_conflict",
            "reply": current,
        }

    def sync_pending_notifications(self) -> int:
        added = 0
        for row in self.list_pending(100):
            added += int(self._enqueue_notification(str(row["reply_id"]), row))
        return added

    def pending_notifications(
        self, channel: str, limit: int = 5
    ) -> list[ManagementNotification]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT event_id, payload_json FROM management_outbox
                WHERE channel=? AND status='pending' AND available_at<=?
                ORDER BY created_at LIMIT ?""",
                (channel, time.time(), max(1, min(limit, 20))),
            ).fetchall()
        finally:
            connection.close()
        notifications: list[ManagementNotification] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            notifications.append(
                ManagementNotification(event_id=row["event_id"], text=str(payload["text"]))
            )
        return notifications

    def mark_notification(
        self, event_id: str, *, delivered: bool, error: str = ""
    ) -> None:
        connection = self._connect()
        try:
            if delivered:
                connection.execute(
                    """UPDATE management_outbox
                    SET status='delivered', delivered_at=CURRENT_TIMESTAMP, error=''
                    WHERE event_id=? AND status='pending'""",
                    (event_id,),
                )
            else:
                connection.execute(
                    """UPDATE management_outbox
                    SET attempts=attempts+1, error=?, available_at=?
                    WHERE event_id=? AND status='pending'""",
                    (error[:200], time.time() + 30, event_id),
                )
            connection.commit()
        finally:
            connection.close()

    def _enqueue_notification(self, reply_id: str, row: dict[str, Any]) -> bool:
        text = (
            "Boss 有一条高风险回复待确认\n"
            f"{row['company']} / {row['title']} / HR {row['hr_name']}\n"
            f"HR：{row['hr_message']}\n"
            f"编号：{reply_id[:12]} V{row['draft_version']}\n"
            "回复 /boss next 查看，或使用 jobagent boss reply。"
        )
        connection = self._connect()
        try:
            before = connection.total_changes
            connection.execute(
                """INSERT OR IGNORE INTO management_outbox
                (event_id, channel, event_type, aggregate_id, payload_json, available_at)
                VALUES (?, 'wechat', 'boss_reply_pending', ?, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    reply_id,
                    json.dumps({"text": text}, ensure_ascii=False),
                    time.time(),
                ),
            )
            connection.commit()
            return connection.total_changes > before
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._state_db)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(connection)
        return connection
