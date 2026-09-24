"""Durable queue for Boss HR reply drafts and HITL decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from jobagent.journey.store import _enable_wal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boss_reply_queue (
    reply_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    friend_id INTEGER NOT NULL DEFAULT 0,
    friend_source INTEGER NOT NULL DEFAULT 0,
    encrypt_boss_id TEXT NOT NULL DEFAULT '',
    source_message_ids TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    hr_name TEXT NOT NULL,
    hr_message TEXT NOT NULL,
    draft_text TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    intent TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    fact_ids TEXT NOT NULL DEFAULT '[]',
    policy_id TEXT NOT NULL DEFAULT '',
    policy_version INTEGER NOT NULL DEFAULT 0,
    authorized_until REAL NOT NULL DEFAULT 0,
    risk_verified INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'awaiting_human',
    draft_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TEXT,
    send_started_at TEXT,
    send_owner TEXT NOT NULL DEFAULT '',
    send_lease_until REAL NOT NULL DEFAULT 0,
    send_generation INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_boss_reply_status ON boss_reply_queue(status, created_at);
CREATE TABLE IF NOT EXISTS boss_reply_policies (
    policy_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    intent TEXT NOT NULL,
    allowed_risk TEXT NOT NULL DEFAULT 'low',
    authorized_until REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS boss_reply_rate_state (
    scope TEXT PRIMARY KEY,
    last_attempt_at REAL NOT NULL
);
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
        columns = {
            row[1]
            for row in self._connection.execute("PRAGMA table_info(boss_reply_queue)")
        }
        if "source_fingerprint" not in columns:
            self._connection.execute(
                """ALTER TABLE boss_reply_queue
                ADD COLUMN source_fingerprint TEXT NOT NULL DEFAULT ''"""
            )
        if "send_started_at" not in columns:
            self._connection.execute(
                "ALTER TABLE boss_reply_queue ADD COLUMN send_started_at TEXT"
            )
        migrations = {
            "friend_id": "INTEGER NOT NULL DEFAULT 0",
            "friend_source": "INTEGER NOT NULL DEFAULT 0",
            "encrypt_boss_id": "TEXT NOT NULL DEFAULT ''",
            "policy_id": "TEXT NOT NULL DEFAULT ''",
            "policy_version": "INTEGER NOT NULL DEFAULT 0",
            "authorized_until": "REAL NOT NULL DEFAULT 0",
            "risk_verified": "INTEGER NOT NULL DEFAULT 0",
            "send_owner": "TEXT NOT NULL DEFAULT ''",
            "send_lease_until": "REAL NOT NULL DEFAULT 0",
            "send_generation": "INTEGER NOT NULL DEFAULT 0",
            "error": "TEXT",
        }
        for name, declaration in migrations.items():
            if name not in columns:
                self._connection.execute(
                    f"ALTER TABLE boss_reply_queue ADD COLUMN {name} {declaration}"
                )
        legacy_rows = self._connection.execute(
            """SELECT reply_id, conversation_id, source_message_ids, hr_message, intent
            FROM boss_reply_queue WHERE source_fingerprint='' ORDER BY created_at, reply_id"""
        ).fetchall()
        seen_legacy: set[tuple[str, str, str]] = set()
        for row in legacy_rows:
            try:
                loaded_ids = json.loads(row["source_message_ids"])
                source_ids = (
                    sorted(str(value) for value in loaded_ids)
                    if isinstance(loaded_ids, list)
                    else []
                )
            except (TypeError, ValueError):
                source_ids = []
            material = "\x1f".join(source_ids) or str(row["hr_message"] or "")
            fingerprint = hashlib.sha256(material.encode()).hexdigest()
            key = (str(row["conversation_id"]), fingerprint, str(row["intent"]))
            if key in seen_legacy:
                fingerprint = f"{fingerprint}:legacy:{row['reply_id']}"
            seen_legacy.add(key)
            self._connection.execute(
                "UPDATE boss_reply_queue SET source_fingerprint=? WHERE reply_id=?",
                (fingerprint, row["reply_id"]),
            )
        self._connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_boss_reply_source
            ON boss_reply_queue(conversation_id, source_fingerprint, intent)
            WHERE source_fingerprint <> ''"""
        )
        self._connection.commit()

    def upsert_policy(
        self,
        *,
        policy_id: str,
        version: int,
        intent: str,
        authorized_until: float,
        enabled: bool = True,
    ) -> None:
        if not policy_id or version <= 0 or authorized_until <= time.time():
            raise ValueError("policy must have an id, positive version, and future expiry")
        self._connection.execute(
            """INSERT INTO boss_reply_policies
            (policy_id, version, intent, allowed_risk, authorized_until, enabled)
            VALUES (?, ?, ?, 'low', ?, ?)
            ON CONFLICT(policy_id) DO UPDATE SET
                version=excluded.version,
                intent=excluded.intent,
                allowed_risk=excluded.allowed_risk,
                authorized_until=excluded.authorized_until,
                enabled=excluded.enabled""",
            (policy_id, version, intent, authorized_until, int(enabled)),
        )
        self._connection.commit()

    def _policy_allows(
        self,
        *,
        policy_id: str,
        policy_version: int,
        intent: str,
        risk_level: str,
        authorized_until: float,
    ) -> bool:
        row = self._connection.execute(
            """SELECT 1 FROM boss_reply_policies
            WHERE policy_id=? AND version=? AND intent=? AND allowed_risk=?
              AND enabled=1 AND authorized_until>? AND authorized_until=?""",
            (
                policy_id,
                policy_version,
                intent,
                risk_level,
                time.time(),
                authorized_until,
            ),
        ).fetchone()
        return row is not None

    def close(self) -> None:
        self._connection.close()

    def enqueue(self, **item: Any) -> str:
        reply_id = str(item.get("reply_id") or uuid.uuid4().hex)
        source_ids = sorted(str(value) for value in item.get("source_message_ids", []) if value)
        source_material = "\x1f".join(source_ids) or str(item.get("hr_message") or "")
        source_fingerprint = hashlib.sha256(source_material.encode()).hexdigest()
        conversation_id = str(item.get("conversation_id") or "")
        intent = str(item.get("intent") or "unknown")
        status = str(item.get("status") or "awaiting_human")
        policy_id = str(item.get("policy_id") or "")
        policy_version = int(item.get("policy_version") or 0)
        authorized_until = float(item.get("authorized_until") or 0)
        risk_verified = bool(item.get("risk_verified"))
        risk_level = str(item.get("risk_level") or "high")
        if status == "auto_ready" and not (
            risk_level == "low"
            and risk_verified
            and policy_id
            and policy_version > 0
            and authorized_until > time.time()
            and self._policy_allows(
                policy_id=policy_id,
                policy_version=policy_version,
                intent=intent,
                risk_level=risk_level,
                authorized_until=authorized_until,
            )
        ):
            raise ValueError("auto_ready requires a current verified low-risk policy")
        if status not in {"awaiting_human", "auto_ready"}:
            raise ValueError("invalid initial reply status")
        self._connection.execute(
            """INSERT OR IGNORE INTO boss_reply_queue
            (reply_id, conversation_id, friend_id, friend_source, encrypt_boss_id,
             source_message_ids, source_fingerprint,
             company, title, hr_name,
             hr_message, draft_text, risk_level, intent, confidence, fact_ids,
             policy_id, policy_version, authorized_until, risk_verified, status,
             error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                reply_id,
                conversation_id,
                int(item.get("friend_id") or 0),
                int(item.get("friend_source") or 0),
                str(item.get("encrypt_boss_id") or ""),
                json.dumps(source_ids),
                source_fingerprint,
                str(item.get("company") or ""),
                str(item.get("title") or ""),
                str(item.get("hr_name") or ""),
                str(item.get("hr_message") or ""),
                str(item.get("draft_text") or ""),
                risk_level,
                intent,
                float(item.get("confidence") or 0),
                json.dumps(item.get("fact_ids", [])),
                policy_id,
                policy_version,
                authorized_until,
                int(risk_verified),
                status,
                str(item.get("error") or "") or None,
            ),
        )
        self._connection.commit()
        row = self._connection.execute(
            """SELECT reply_id FROM boss_reply_queue
            WHERE conversation_id=? AND source_fingerprint=? AND intent=?""",
            (conversation_id, source_fingerprint, intent),
        ).fetchone()
        return str(row[0]) if row else reply_id

    def pending(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT * FROM boss_reply_queue
            WHERE status = 'awaiting_human'
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

    def claim_sendable(
        self,
        *,
        owner: str,
        lease_seconds: float = 60,
        global_interval_seconds: float = 5,
        conversation_interval_seconds: float = 30,
        daily_limit: int = 30,
    ) -> dict[str, Any] | None:
        if not owner:
            raise ValueError("send owner is required")
        now = time.time()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            active = self._connection.execute(
                """SELECT 1 FROM boss_reply_queue
                WHERE status='sending' AND send_lease_until>? LIMIT 1""",
                (now,),
            ).fetchone()
            if active is not None:
                self._connection.commit()
                return None
            global_rate = self._connection.execute(
                "SELECT last_attempt_at FROM boss_reply_rate_state WHERE scope='global'"
            ).fetchone()
            if global_rate and now - float(global_rate[0]) < global_interval_seconds:
                self._connection.commit()
                return None
            today_count = self._connection.execute(
                """SELECT COUNT(*) FROM boss_reply_queue
                WHERE send_started_at IS NOT NULL
                  AND date(send_started_at)=date('now','localtime')"""
            ).fetchone()[0]
            if int(today_count) >= max(1, daily_limit):
                self._connection.commit()
                return None
            rows = self._connection.execute(
                """SELECT * FROM boss_reply_queue
                WHERE status='approved' OR (
                    status='auto_ready'
                    AND risk_level='low'
                    AND risk_verified=1
                    AND EXISTS (
                        SELECT 1 FROM boss_reply_policies p
                        WHERE p.policy_id=boss_reply_queue.policy_id
                          AND p.version=boss_reply_queue.policy_version
                          AND p.intent=boss_reply_queue.intent
                          AND p.allowed_risk=boss_reply_queue.risk_level
                          AND p.enabled=1
                          AND p.authorized_until>?
                          AND p.authorized_until=boss_reply_queue.authorized_until
                    )
                )
                ORDER BY created_at LIMIT 100""",
                (now,),
            ).fetchall()
            row = None
            for candidate in rows:
                rate = self._connection.execute(
                    """SELECT last_attempt_at FROM boss_reply_rate_state
                    WHERE scope=?""",
                    (f"conversation:{candidate['conversation_id']}",),
                ).fetchone()
                if rate and now - float(rate[0]) < conversation_interval_seconds:
                    continue
                row = candidate
                break
            if row is None:
                self._connection.commit()
                return None
            generation = int(row["send_generation"] or 0) + 1
            updated = self._connection.execute(
                """UPDATE boss_reply_queue
                SET status='sending', send_started_at=CURRENT_TIMESTAMP,
                    send_owner=?, send_lease_until=?, send_generation=?
                WHERE reply_id=? AND status=?""",
                (
                    owner,
                    now + max(10, lease_seconds),
                    generation,
                    row["reply_id"],
                    row["status"],
                ),
            )
            if updated.rowcount != 1:
                self._connection.rollback()
                return None
            self._connection.executemany(
                """INSERT INTO boss_reply_rate_state(scope, last_attempt_at)
                VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE SET last_attempt_at=excluded.last_attempt_at""",
                [
                    ("global", now),
                    (f"conversation:{row['conversation_id']}", now),
                ],
            )
            self._connection.commit()
            result = dict(row)
            result["status"] = "sending"
            result["send_owner"] = owner
            result["send_generation"] = generation
            return result
        except Exception:
            self._connection.rollback()
            raise

    def finish(
        self,
        reply_id: str,
        *,
        owner: str,
        generation: int,
        status: str,
        error: str = "",
    ) -> bool:
        if status not in {"submitted", "unverified", "failed"}:
            raise ValueError("invalid terminal reply status")
        cur = self._connection.execute(
            """UPDATE boss_reply_queue
            SET status=?, sent_at=CURRENT_TIMESTAMP, error=?
            WHERE reply_id=? AND status='sending'
              AND send_owner=? AND send_generation=?""",
            (status, error, reply_id, owner, generation),
        )
        self._connection.commit()
        return cur.rowcount == 1

    def renew_send_lease(
        self,
        reply_id: str,
        *,
        owner: str,
        generation: int,
        lease_seconds: float = 60,
    ) -> bool:
        cur = self._connection.execute(
            """UPDATE boss_reply_queue SET send_lease_until=?
            WHERE reply_id=? AND status='sending'
              AND send_owner=? AND send_generation=?""",
            (time.time() + max(10, lease_seconds), reply_id, owner, generation),
        )
        self._connection.commit()
        return cur.rowcount == 1

    def recover_stale_sending(self) -> int:
        cur = self._connection.execute(
            """UPDATE boss_reply_queue
            SET status='unverified', error='worker_restarted_during_send'
            WHERE status='sending' AND send_lease_until<=?""",
            (time.time(),),
        )
        self._connection.commit()
        return cur.rowcount

    def get(self, reply_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM boss_reply_queue WHERE reply_id = ?", (reply_id,)
        ).fetchone()
        return dict(row) if row else None
