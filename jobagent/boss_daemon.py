"""Long-lived Boss conversation monitor with durable cursor and lease state."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jobagent.config import Settings
from jobagent.journey.store import _enable_wal

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boss_monitor_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boss_monitor_leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS boss_inbound_messages (
    message_key TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    platform_message_id TEXT,
    friend_id INTEGER NOT NULL,
    hr_name TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    sent_at INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    processing_status TEXT NOT NULL DEFAULT 'unclassified',
    baseline INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_boss_inbound_processing
ON boss_inbound_messages(processing_status, baseline, first_seen_at);
"""


@dataclass(frozen=True, slots=True)
class BossInboundMessage:
    conversation_id: str
    platform_message_id: str
    friend_id: int
    hr_name: str
    company: str
    title: str
    text: str
    sent_at: int
    raw: dict[str, Any]

    @property
    def message_key(self) -> str:
        if self.platform_message_id:
            return f"boss:{self.conversation_id}:{self.platform_message_id}"
        import hashlib

        material = f"{self.conversation_id}|{self.sent_at}|{self.text}".encode()
        return "boss:fallback:" + hashlib.sha256(material).hexdigest()


class BossConversationAdapter(Protocol):
    async def poll(self) -> list[BossInboundMessage]: ...

    async def close(self) -> None: ...


class LiveBossConversationAdapter:
    """Adapter over existing Boss list/history capabilities."""

    def __init__(self, settings: Settings, *, conversation_limit: int = 100) -> None:
        self._settings = settings
        self._conversation_limit = conversation_limit

    async def poll(self) -> list[BossInboundMessage]:
        from jobagent.applier.boss_chat import list_boss_greetings_http

        listing = await list_boss_greetings_http(
            self._settings, label_id=0, limit=self._conversation_limit
        )
        if listing.get("status") != "ok":
            raise ConnectionError(
                f"Boss conversation list failed: {listing.get('error_type', 'unknown')}"
            )
        inbound: list[BossInboundMessage] = []
        for friend in listing.get("greetings", []):
            if not isinstance(friend, dict):
                continue
            hr_name = str(friend.get("name") or friend.get("bossName") or "").strip()
            friend_id = int(friend.get("friendId") or 0)
            message = friend.get("lastMessage")
            if not hr_name or not friend_id or not isinstance(message, dict):
                continue
            from_id = int(message.get("fromId") or 0)
            if from_id and from_id != friend_id:
                continue
            text = str((message.get("body") or {}).get("text") or message.get("text") or "").strip()
            if not text:
                continue
            job = friend.get("job_metadata") or {}
            conversation_id = str(friend.get("conversationId") or friend_id)
            sent_at = int(
                message.get("time")
                or message.get("createTime")
                or friend.get("updateTime")
                or 0
            )
            inbound.append(
                BossInboundMessage(
                    conversation_id=conversation_id,
                    platform_message_id=str(message.get("mid") or message.get("messageId") or ""),
                    friend_id=friend_id,
                    hr_name=hr_name,
                    company=str(job.get("company") or friend.get("brandName") or ""),
                    title=str(job.get("title") or friend.get("jobName") or ""),
                    text=text,
                    sent_at=sent_at,
                    raw=message,
                )
            )
        return inbound

    async def close(self) -> None:
        return None


class BossMonitorStore:
    def __init__(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(resolved, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def acquire_lease(self, owner: str, *, ttl_seconds: float) -> bool:
        now = time.time()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT owner, expires_at FROM boss_monitor_leases WHERE name='monitor'"
            ).fetchone()
            if row and row["owner"] != owner and float(row["expires_at"]) > now:
                self._connection.rollback()
                return False
            self._connection.execute(
                """INSERT INTO boss_monitor_leases(name, owner, expires_at)
                VALUES('monitor', ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner=excluded.owner, expires_at=excluded.expires_at""",
                (owner, now + ttl_seconds),
            )
            self._connection.commit()
            return True
        except Exception:
            self._connection.rollback()
            raise

    def release_lease(self, owner: str) -> None:
        self._connection.execute(
            "DELETE FROM boss_monitor_leases WHERE name='monitor' AND owner=?", (owner,)
        )

    def initialized(self) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM boss_monitor_state WHERE key='baseline_complete'"
        ).fetchone() is not None

    def record(self, messages: list[BossInboundMessage], *, baseline: bool) -> int:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            before = self._connection.total_changes
            self._connection.executemany(
                """INSERT OR IGNORE INTO boss_inbound_messages
                (message_key, conversation_id, platform_message_id, friend_id, hr_name,
                 company, title, text, sent_at, raw_json, baseline)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        message.message_key,
                        message.conversation_id,
                        message.platform_message_id or None,
                        message.friend_id,
                        message.hr_name,
                        message.company,
                        message.title,
                        message.text,
                        message.sent_at,
                        json.dumps(message.raw, ensure_ascii=False),
                        int(baseline),
                    )
                    for message in messages
                ],
            )
            added = self._connection.total_changes - before
            if baseline:
                self._connection.execute(
                    """INSERT OR REPLACE INTO boss_monitor_state(key, value)
                    VALUES('baseline_complete', CURRENT_TIMESTAMP)"""
                )
            self._connection.commit()
            return added
        except Exception:
            self._connection.rollback()
            raise

    def unclassified(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT * FROM boss_inbound_messages
            WHERE baseline=0 AND processing_status='unclassified'
            ORDER BY sent_at, first_seen_at LIMIT ?""",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [dict(row) for row in rows]


class BossConversationDaemon:
    def __init__(
        self,
        adapter: BossConversationAdapter,
        store: BossMonitorStore,
        *,
        poll_interval_seconds: float = 30,
        lease_ttl_seconds: float = 120,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._poll_interval = max(5.0, poll_interval_seconds)
        self._lease_ttl = max(self._poll_interval * 2, lease_ttl_seconds)
        self._owner = uuid.uuid4().hex
        self._stop = asyncio.Event()

    async def run_once(self) -> dict[str, Any]:
        if not self._store.acquire_lease(self._owner, ttl_seconds=self._lease_ttl):
            return {"status": "standby", "reason": "lease_held"}
        try:
            return await self._poll_owned()
        finally:
            self._store.release_lease(self._owner)

    async def _poll_owned(self) -> dict[str, Any]:
        baseline = not self._store.initialized()
        messages = await self._adapter.poll()
        added = self._store.record(messages, baseline=baseline)
        return {
            "status": "ok",
            "baseline": baseline,
            "fetched": len(messages),
            "new_messages": 0 if baseline else added,
        }

    async def run(self) -> None:
        failures = 0
        owns_lease = False
        try:
            while not self._stop.is_set():
                try:
                    owns_lease = self._store.acquire_lease(
                        self._owner, ttl_seconds=self._lease_ttl
                    )
                    if not owns_lease:
                        delay = self._poll_interval
                        logger.info("Boss daemon standby: another process owns the lease")
                    else:
                        await self._poll_owned()
                        # Renew after a successful pass so the lease covers the
                        # wait until the next poll as well.
                        self._store.acquire_lease(
                            self._owner, ttl_seconds=self._lease_ttl
                        )
                        delay = self._poll_interval
                    failures = 0
                except Exception:
                    failures += 1
                    delay = min(self._poll_interval * (2 ** min(failures, 4)), 300)
                    logger.exception("Boss daemon polling failed; retrying in %.1fs", delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            if owns_lease:
                self._store.release_lease(self._owner)
            await self._adapter.close()
            self._store.close()

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self.stop()
        await self._adapter.close()
        self._store.close()
