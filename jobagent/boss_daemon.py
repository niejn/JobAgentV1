"""Long-lived Boss conversation monitor with durable cursor and lease state."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from jobagent.config import Settings
from jobagent.journey.resume_requests import RESUME_CARD_TYPE
from jobagent.journey.store import _enable_wal

if TYPE_CHECKING:
    from jobagent.journey.resume_requests import ResumeRequestQueue

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boss_monitor_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boss_monitor_cursors (
    conversation_id TEXT PRIMARY KEY,
    head_key TEXT NOT NULL,
    sent_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS boss_monitor_leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS boss_inbound_messages (
    message_key TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    platform_message_id TEXT,
    friend_id INTEGER NOT NULL,
    friend_source INTEGER NOT NULL DEFAULT 0,
    encrypt_boss_id TEXT NOT NULL DEFAULT '',
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
    friend_source: int
    encrypt_boss_id: str
    hr_name: str
    company: str
    title: str
    text: str
    sent_at: int
    raw: dict[str, Any]
    # True → pre-monitor cold-start material, not a live HR reply. Per-message
    # because a non-baseline poll can still meet first-seen conversations
    # whose whole history predates the monitor.
    baseline: bool = False

    @property
    def message_key(self) -> str:
        if self.platform_message_id:
            return f"boss:{self.conversation_id}:{self.platform_message_id}"
        import hashlib

        material = f"{self.conversation_id}|{self.sent_at}|{self.text}".encode()
        return "boss:fallback:" + hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class BossConversationCursor:
    head_key: str
    sent_at: int


@dataclass(frozen=True, slots=True)
class BossPollBatch:
    messages: list[BossInboundMessage]
    cursors: dict[str, BossConversationCursor]


class BossConversationAdapter(Protocol):
    async def poll(
        self,
        cursors: dict[str, BossConversationCursor],
        *,
        baseline: bool,
    ) -> BossPollBatch: ...

    async def close(self) -> None: ...

class BossInboundEventSink(Protocol):
    def publish_inbound(self, messages: list[object]) -> None: ...




class BossOutboundEventSink(Protocol):
    def note_outbound(self, friend_id: int, text: str, sent_at: int) -> None: ...


class LiveBossConversationAdapter:
    """Adapter over existing Boss list/history capabilities."""
    def __init__(
        self,
        settings: Settings,
        *,
        conversation_limit: int = 100,
        history_pull_cap: int = 8,
        outbound_sink: BossOutboundEventSink | None = None,
    ) -> None:
        self._settings = settings
        self._conversation_limit = conversation_limit
        # Per-poll bound on history reads so a cold-start sweep over ~100
        # conversations spreads across polls instead of bursting Boss; 8 is
        # the budget the scan tool (scan_boss_hr_replies) has proven safe
        # against the chat page's warlock rate limiting.
        self._history_pull_cap = history_pull_cap
        self._outbound_sink = outbound_sink

    async def poll(
        self,
        cursors: dict[str, BossConversationCursor],
        *,
        baseline: bool,
    ) -> BossPollBatch:
        from jobagent.applier.boss_chat import BossChatReader, list_boss_greetings_http

        listing = await list_boss_greetings_http(
            self._settings, label_id=0, limit=self._conversation_limit
        )
        if listing.get("status") != "ok":
            raise ConnectionError(
                f"Boss conversation list failed: {listing.get('error_type', 'unknown')}"
            )
        inbound: list[BossInboundMessage] = []
        cursor_updates: dict[str, BossConversationCursor] = {}
        changed: list[tuple[dict[str, Any], str, int, str, int, str]] = []
        for friend in listing.get("greetings", []):
            if not isinstance(friend, dict):
                continue
            hr_name = str(friend.get("name") or friend.get("bossName") or "").strip()
            try:
                friend_id = int(friend.get("friendId") or 0)
            except (TypeError, ValueError):
                continue
            try:
                friend_source = int(friend.get("friendSource") or 0)
            except (TypeError, ValueError):
                friend_source = 0
            encrypt_boss_id = str(
                friend.get("encryptBossId") or friend.get("encryptFriendId") or ""
            )
            message = friend.get("lastMessage")
            if not hr_name or not friend_id:
                continue
            conversation_id = str(friend.get("conversationId") or friend_id)
            raw_job = friend.get("job_metadata")
            job = raw_job if isinstance(raw_job, dict) else {}
            try:
                update_time = int(friend.get("updateTime") or 0)
            except (TypeError, ValueError):
                update_time = 0
            if isinstance(message, dict):
                # CDP transport: the listing carries the last message, so the
                # per-message id is the head signal.
                try:
                    from_id = int(message.get("fromId") or 0)
                except (TypeError, ValueError):
                    from_id = 0
                direction = str(message.get("direction") or "").lower()
                body = message.get("body")
                body_text = body.get("text") if isinstance(body, dict) else ""
                text = str(body_text or message.get("text") or "").strip()
                try:
                    sent_at = int(
                        message.get("time")
                        or message.get("createTime")
                        or update_time
                        or 0
                    )
                except (TypeError, ValueError):
                    continue
                head_key = _platform_message_key(conversation_id, message, sent_at, text)
            else:
                # HTTP transport (geekFilterByLabel): the listing carries no
                # per-message payload, so the conversation-level updateTime is
                # the only change signal; the history diff resolves details.
                message = None
                sent_at = update_time
                if not sent_at:
                    continue
                head_key = f"boss:{conversation_id}:t{sent_at}"
            next_cursor = BossConversationCursor(head_key=head_key, sent_at=sent_at)
            previous = cursors.get(conversation_id)
            if previous is not None and previous.head_key == head_key:
                continue
            if baseline:
                cursor_updates[conversation_id] = next_cursor
                if (
                    message is not None
                    and text
                    and (direction == "boss" or from_id == friend_id)
                ):
                    inbound.append(
                        _inbound_from_raw(
                            message,
                            conversation_id=conversation_id,
                            friend_id=friend_id,
                            friend_source=friend_source,
                            encrypt_boss_id=encrypt_boss_id,
                            hr_name=hr_name,
                            company=str(job.get("company") or friend.get("brandName") or ""),
                            title=str(job.get("title") or friend.get("jobName") or ""),
                        )
                    )
                continue
            changed.append((friend, conversation_id, friend_id, hr_name, sent_at, head_key))

        if changed:
            # Newest conversations first; the cap leaves the rest of a
            # cold-start sweep to the next polls (cursor stays unset).
            changed.sort(key=lambda item: item[4], reverse=True)
            async with BossChatReader(self._settings) as reader:
                for index, (friend, conversation_id, friend_id, hr_name, sent_at, head_key) in (
                    enumerate(changed[: self._history_pull_cap])
                ):
                    if index:
                        # Same settle interval the scan tool uses between reads.
                        await asyncio.sleep(1.5)
                    history = await reader.read_conversation(
                        hr_name=hr_name, friend_id=friend_id, page=1
                    )
                    if history.get("status") != "ok":
                        logger.warning(
                            "Boss daemon history skipped for %s: %s",
                            hr_name,
                            history.get("error_type", "unknown"),
                        )
                        if history.get("error_type") == "api_rejected":
                            # The chat page's per-load automated-fetch budget is
                            # spent; every remaining read this poll would be
                            # rejected. Leave their cursors unset — the next
                            # poll opens a fresh reader (fresh page, fresh
                            # budget) and the sweep resumes there.
                            logger.info(
                                "Boss daemon history sweep paused; resuming next poll"
                            )
                            break
                        continue
                    previous_cursor = cursors.get(conversation_id)
                    first_seen = previous_cursor is None
                    previous_at = (
                        previous_cursor.sent_at if previous_cursor is not None else 0
                    )
                    previous_head = (
                        previous_cursor.head_key if previous_cursor is not None else ""
                    )
                    raw_job = history.get("job_metadata") or friend.get("job_metadata")
                    job = raw_job if isinstance(raw_job, dict) else {}
                    for raw_message in history.get("messages", []):
                        if not isinstance(raw_message, dict):
                            continue
                        try:
                            normalized = _inbound_from_raw(
                                raw_message,
                                conversation_id=conversation_id,
                                friend_id=friend_id,
                                friend_source=int(friend.get("friendSource") or 0),
                                encrypt_boss_id=str(
                                    friend.get("encryptBossId")
                                    or friend.get("encryptFriendId")
                                    or ""
                                ),
                                hr_name=hr_name,
                                company=str(
                                    job.get("company") or friend.get("brandName") or ""
                                ),
                                title=str(
                                    job.get("title") or friend.get("jobName") or ""
                                ),
                            )
                        except ValueError:
                            # Outbound (geek-sent) message — manual takeover
                            # detection (design P0-1): if it doesn't match a
                            # system-sent draft, the user is chatting manually.
                            if self._outbound_sink is not None:
                                _body = raw_message.get("body")
                                _text = str(
                                    (_body.get("text") if isinstance(_body, dict) else "")
                                    or raw_message.get("text")
                                    or ""
                                ).strip()
                                _time = int(
                                    raw_message.get("time")
                                    or raw_message.get("createTime")
                                    or 0
                                )
                                if _text:
                                    self._outbound_sink.note_outbound(
                                        friend_id, _text, _time
                                    )
                            continue
                        # The equal-time/different-key arm catches a head
                        # message replaced at the same timestamp — but only
                        # for message-id cursors. Against a t{updateTime}
                        # cursor, a message at previous_at IS the old head.
                        same_time_is_new = previous_head != (
                            f"boss:{conversation_id}:t{previous_at}"
                        )
                        if (
                            normalized.sent_at > previous_at
                            or (
                                normalized.sent_at == previous_at
                                and same_time_is_new
                                and normalized.message_key != previous_head
                            )
                        ):
                            if first_seen:
                                # Whole history predates the monitor → cold-start
                                # material, not a live reply (no notify, no inbox).
                                normalized = replace(normalized, baseline=True)
                            inbound.append(normalized)
                    cursor_updates[conversation_id] = BossConversationCursor(
                        head_key=head_key, sent_at=sent_at
                    )
        return BossPollBatch(messages=inbound, cursors=cursor_updates)

    async def close(self) -> None:
        return None


def _platform_message_key(
    conversation_id: str, message: dict[str, Any], sent_at: int, text: str
) -> str:
    message_id = str(message.get("mid") or message.get("messageId") or "")
    if message_id:
        return f"boss:{conversation_id}:{message_id}"
    import hashlib

    material = f"{conversation_id}|{sent_at}|{text}|{message.get('fromId', '')}".encode()
    return "boss:fallback:" + hashlib.sha256(material).hexdigest()


def _inbound_from_raw(
    message: dict[str, Any],
    *,
    conversation_id: str,
    friend_id: int,
    friend_source: int,
    encrypt_boss_id: str,
    hr_name: str,
    company: str,
    title: str,
) -> BossInboundMessage:
    try:
        from_id = int(message.get("fromId") or 0)
    except (TypeError, ValueError):
        from_id = 0
    direction = str(message.get("direction") or "").lower()
    if direction != "boss" and from_id != friend_id:
        raise ValueError("message direction is not confirmed as HR inbound")
    body = message.get("body")
    body_text = body.get("text") if isinstance(body, dict) else ""
    text = str(body_text or message.get("text") or "").strip()
    if not text:
        raise ValueError("inbound message has no text")
    try:
        sent_at = int(message.get("time") or message.get("createTime") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("inbound message has invalid timestamp") from exc
    return BossInboundMessage(
        conversation_id=conversation_id,
        platform_message_id=str(message.get("mid") or message.get("messageId") or ""),
        friend_id=friend_id,
        friend_source=friend_source,
        encrypt_boss_id=encrypt_boss_id,
        hr_name=hr_name,
        company=company,
        title=title,
        text=text,
        sent_at=sent_at,
        raw=message,
    )


class BossMonitorStore:
    def __init__(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(resolved, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_SCHEMA)
        columns = {
            row[1]
            for row in self._connection.execute("PRAGMA table_info(boss_monitor_leases)")
        }
        if "generation" not in columns:
            self._connection.execute(
                "ALTER TABLE boss_monitor_leases ADD COLUMN generation INTEGER NOT NULL DEFAULT 1"
            )
        message_columns = {
            row[1]
            for row in self._connection.execute("PRAGMA table_info(boss_inbound_messages)")
        }
        if "friend_source" not in message_columns:
            self._connection.execute(
                """ALTER TABLE boss_inbound_messages
                ADD COLUMN friend_source INTEGER NOT NULL DEFAULT 0"""
            )
        if "encrypt_boss_id" not in message_columns:
            self._connection.execute(
                """ALTER TABLE boss_inbound_messages
                ADD COLUMN encrypt_boss_id TEXT NOT NULL DEFAULT ''"""
            )

    def close(self) -> None:
        self._connection.close()

    def acquire_lease(self, owner: str, *, ttl_seconds: float) -> int | None:
        now = time.time()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                """SELECT owner, expires_at, generation
                FROM boss_monitor_leases WHERE name='monitor'"""
            ).fetchone()
            if row and row["owner"] != owner and float(row["expires_at"]) > now:
                self._connection.rollback()
                return None
            generation = (
                int(row["generation"])
                if row and row["owner"] == owner
                else int(row["generation"] if row else 0) + 1
            )
            self._connection.execute(
                """INSERT INTO boss_monitor_leases(name, owner, expires_at, generation)
                VALUES('monitor', ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner=excluded.owner, expires_at=excluded.expires_at,
                    generation=excluded.generation""",
                (owner, now + ttl_seconds, generation),
            )
            self._connection.commit()
            return generation
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

    def cursors(self) -> dict[str, BossConversationCursor]:
        rows = self._connection.execute(
            "SELECT conversation_id, head_key, sent_at FROM boss_monitor_cursors"
        ).fetchall()
        return {
            str(row["conversation_id"]): BossConversationCursor(
                head_key=str(row["head_key"]), sent_at=int(row["sent_at"])
            )
            for row in rows
        }

    def record(
        self,
        messages: list[BossInboundMessage],
        cursors: dict[str, BossConversationCursor],
        *,
        baseline: bool,
        owner: str,
        generation: int,
    ) -> list[BossInboundMessage]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            lease = self._connection.execute(
                """SELECT owner, generation, expires_at FROM boss_monitor_leases
                WHERE name='monitor'"""
            ).fetchone()
            if (
                lease is None
                or lease["owner"] != owner
                or int(lease["generation"]) != generation
                or float(lease["expires_at"]) <= time.time()
            ):
                self._connection.rollback()
                raise RuntimeError("Boss monitor lease lost before commit")
            inserted: list[BossInboundMessage] = []
            for message in messages:
                cursor = self._connection.execute(
                    """INSERT OR IGNORE INTO boss_inbound_messages
                (message_key, conversation_id, platform_message_id, friend_id,
                 friend_source, encrypt_boss_id, hr_name, company, title, text,
                 sent_at, raw_json, baseline)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        message.message_key,
                        message.conversation_id,
                        message.platform_message_id or None,
                        message.friend_id,
                        message.friend_source,
                        message.encrypt_boss_id,
                        message.hr_name,
                        message.company,
                        message.title,
                        message.text,
                        message.sent_at,
                        json.dumps(message.raw, ensure_ascii=False),
                        int(baseline or message.baseline),
                    ),
                )
                if cursor.rowcount == 1:
                    inserted.append(message)
            self._connection.executemany(
                """INSERT INTO boss_monitor_cursors(conversation_id, head_key, sent_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    head_key=excluded.head_key, sent_at=excluded.sent_at""",
                [
                    (conversation_id, cursor.head_key, cursor.sent_at)
                    for conversation_id, cursor in cursors.items()
                ],
            )
            if baseline:
                self._connection.execute(
                    """INSERT OR REPLACE INTO boss_monitor_state(key, value)
                    VALUES('baseline_complete', CURRENT_TIMESTAMP)"""
                )
            self._connection.commit()
            return inserted
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
        event_sink: BossInboundEventSink | None = None,
        outbound_sink: BossOutboundEventSink | None = None,
        poll_interval: float = 30.0,
        resume_queue: ResumeRequestQueue | None = None,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._event_sink = event_sink
        self._outbound_sink = outbound_sink
        self._resume_queue = resume_queue
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._owner = f"daemon-{uuid.uuid4().hex[:8]}"
        self._lease_ttl = max(60.0, poll_interval * 3)
    async def run_once(self) -> dict[str, Any]:
        generation = self._store.acquire_lease(self._owner, ttl_seconds=self._lease_ttl)
        if generation is None:
            return {"status": "standby", "reason": "lease_held"}
        try:
            return await self._poll_owned(generation)
        finally:
            self._store.release_lease(self._owner)

    async def _poll_owned(self, generation: int) -> dict[str, Any]:
        baseline = not self._store.initialized()
        batch = await self._adapter.poll(self._store.cursors(), baseline=baseline)
        inserted = self._store.record(
            batch.messages,
            cursors=batch.cursors,
            baseline=baseline,
            owner=self._owner,
            generation=generation,
        )
        resume_received = self._receive_resume_requests(inserted)
        live_inserted = [m for m in inserted if not m.baseline]
        if not baseline and live_inserted and self._event_sink is not None:
            self._event_sink.publish_inbound(list(live_inserted))
        return {
            "status": "ok",
            "baseline": baseline,
            "fetched": len(batch.messages),
            "new_messages": 0 if baseline else len(live_inserted),
            "resume_requests": resume_received,
        }

    def _receive_resume_requests(self, inserted: list[BossInboundMessage]) -> int:
        """Feed HR resume-request cards into the durable queue (idempotent).

        The card text label comes from the reader (boss_chat); this feed is
        the polling-path twin of the realtime WS listener, landing in the
        same resume_requests table keyed by mid. Baseline (cold-start) cards
        are included: a card is actionable regardless of when it was sent.

        Contract (review 2026-09-23): the queue dedupes on
        UNIQUE(conversation_id, source_mid), so cross-path idempotency
        (WS listener + this poll delivering the same card) holds only if
        both paths pass the SAME conversation_id this daemon uses -
        str(friend.conversationId or friendId). WS wiring must honor this.
        Premise: the geekFilterByLabel listing carries no conversationId
        (field matrix in this module's reader), so the formula degrades to
        str(friendId) - the only key a WS packet (uids + mid only) can ever
        match. If the listing ever starts returning conversationId !=
        friendId, re-anchor BOTH paths on str(friendId).

        Returns how many cards were received this poll.
        """

        if self._resume_queue is None:
            return 0
        received = 0
        for message in inserted:
            raw = message.raw if isinstance(message.raw, dict) else {}
            try:
                is_card = int(raw.get("type")) == RESUME_CARD_TYPE
            except (TypeError, ValueError):
                is_card = False
            if not is_card:
                continue
            try:
                source_mid = int(message.platform_message_id)
            except (TypeError, ValueError):
                logger.warning(
                    "resume card without numeric mid skipped: %s", message.message_key
                )
                continue
            self._resume_queue.receive(
                conversation_id=message.conversation_id,
                source_mid=source_mid,
                friend_name=message.hr_name,
                company=message.company,
                job_title=message.title,
                card_payload=raw,
            )
            received += 1
        return received

    async def run(self) -> None:
        failures = 0
        owns_lease = False
        try:
            while not self._stop.is_set():
                try:
                    generation = self._store.acquire_lease(
                        self._owner, ttl_seconds=self._lease_ttl
                    )
                    owns_lease = generation is not None
                    if generation is None:
                        delay = self._poll_interval
                        logger.info("Boss daemon standby: another process owns the lease")
                    else:
                        await self._poll_owned(generation)
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
            if self._resume_queue is not None:
                self._resume_queue.close()

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self.stop()
        await self._adapter.close()
        self._store.close()
        if self._resume_queue is not None:
            self._resume_queue.close()
