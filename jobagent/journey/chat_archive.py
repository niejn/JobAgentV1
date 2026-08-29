"""Boss chat message archive: append-only rows, idempotent on msg_id.

Design (settled 2026-08-29, row-per-message over blob):
- Conversation flow is append-mostly; msg_id (Boss's server-assigned
  mid) is the natural idempotency key - INSERT OR IGNORE dedupes
  repeated history pulls for free.
- Row-per-message keeps SQL analytics available (reply rate, response
  latency, last-speaker per friend) that a per-friend JSON blob could
  only do with read-modify-write races (the M8 lesson).
- Write points: read_boss_conversation results (history_pull) and
  successful sends (ws_sent / ui_sent). raw_json preserves full
  message bodies for future features (resume-request cards).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from jobagent.journey.store import _enable_wal

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boss_chat_messages (
    id          INTEGER PRIMARY KEY,
    msg_id      INTEGER UNIQUE,
    friend_id   INTEGER NOT NULL,
    friend_name TEXT,
    direction   TEXT NOT NULL,
    msg_type    INTEGER,
    text        TEXT,
    raw_json    TEXT,
    sent_at     INTEGER,
    fetched_at  INTEGER NOT NULL,
    source      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_friend
    ON boss_chat_messages(friend_id, sent_at);
"""


class BossChatArchive:
    """Persist Boss conversation messages, idempotently."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path)
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> BossChatArchive:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- write ------------------------------------------------------------------

    def upsert_history(
        self, *, friend_id: int, friend_name: str, messages: list[dict[str, Any]]
    ) -> int:
        """Idempotently archive messages pulled via historyMsg.

        Returns how many NEW rows landed (0 = everything already known).
        """

        now_ms = int(time.time() * 1000)
        rows = []
        for m in messages:
            mid = m.get("mid") or None
            if mid in (0, None, ""):
                continue  # system/empty rows without ids cannot dedupe
            rows.append(
                (
                    int(mid),
                    int(friend_id),
                    friend_name,
                    str(m.get("direction") or ("geek" if m.get("isSelf") else "boss")),
                    int(m.get("type") or 0),
                    str(m.get("text") or ""),
                    json.dumps(m, ensure_ascii=False),
                    int(m.get("time") or 0),
                    now_ms,
                    "history_pull",
                )
            )
        if not rows:
            return 0
        before = self._connection.total_changes
        self._connection.executemany(
            """
            INSERT OR IGNORE INTO boss_chat_messages
                (msg_id, friend_id, friend_name, direction, msg_type,
                 text, raw_json, sent_at, fetched_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self._connection.commit()
        added = self._connection.total_changes - before
        if added:
            logger.info(
                "Chat archive: +%d new / %d pulled for %s", added, len(rows), friend_name
            )
        return added

    def append_sent(
        self, *, friend_id: int, friend_name: str, text: str, source: str
    ) -> None:
        """Archive one of OUR outgoing messages (no server mid yet)."""

        self._connection.execute(
            """
            INSERT INTO boss_chat_messages
                (msg_id, friend_id, friend_name, direction, msg_type,
                 text, raw_json, sent_at, fetched_at, source)
            VALUES (NULL, ?, ?, 'geek', 1, ?, NULL, ?, ?, ?)
            """,
            (
                int(friend_id),
                friend_name,
                text,
                int(time.time() * 1000),
                int(time.time() * 1000),
                source,
            ),
        )
        self._connection.commit()

    # -- read (analytics later; minimal now) --------------------------------------

    def latest_per_friend(self) -> list[dict[str, Any]]:
        """One row per friend: who spoke last and when (follow-up signal)."""

        cursor = self._connection.execute(
            """
            SELECT friend_id, friend_name, direction, text, sent_at
            FROM boss_chat_messages m
            WHERE id = (
                SELECT id FROM boss_chat_messages m2
                WHERE m2.friend_id = m.friend_id
                ORDER BY sent_at DESC, id DESC LIMIT 1
            )
            ORDER BY sent_at DESC
            """
        )
        return [
            {
                "friend_id": r[0],
                "friend_name": r[1],
                "last_direction": r[2],
                "last_text": (r[3] or "")[:60],
                "last_sent_at": r[4],
            }
            for r in cursor.fetchall()
        ]
