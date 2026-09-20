"""Pipeline from unclassified Boss inbound messages to the reply queue.

Runs after the Boss daemon: pulls ``boss_inbound_messages`` rows still
marked ``unclassified`` (baseline rows are cold-start material, not live),
merges consecutive HR messages per conversation, classifies, applies the
deterministic policy and enqueues drafts (auto_ready only when a verified
policy authorizes it; audit-only observation routes everything to
awaiting_human with a would-auto marker).

Send time window (design P1-5): groups arriving outside 09:00-21:00 local
are left unclassified and picked up by the next in-window run, so nothing
auto-sends at night and no draft is lost.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jobagent.boss_reply_queue import BossReplyQueue
from jobagent.config import Settings
from jobagent.hr_reply.classifier import HrQuestionClassifier
from jobagent.hr_reply.facts import CandidateFactStore, CompanyInsightStore
from jobagent.hr_reply.policy import (
    ConversationStateStore,
    EngineConfig,
    PolicyDecision,
    ReplyPolicyEngine,
    engine_config_from_overrides,
    question_fingerprint,
)
from jobagent.models.llm_client import build_agent_model

logger = logging.getLogger(__name__)

MERGE_WINDOW_SECONDS = 5 * 60
SEND_WINDOW_DEFAULT = (9, 21)  # local hour, inclusive start / exclusive end

_ENGINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS boss_reply_engine_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

PROCESSING_STATUSES = ("unclassified", "drafted", "skipped_manual", "declined_company")


@dataclass(slots=True)
class PipelineStats:
    """One orchestrator tick, for logging and the daily digest."""

    processed: int = 0
    auto_ready: int = 0
    awaiting_human: int = 0
    deferred_window: int = 0
    skipped_manual: int = 0
    degraded: int = 0
    reasons: list[str] = field(default_factory=list)


def in_send_window(now: float, window: tuple[int, int] = SEND_WINDOW_DEFAULT) -> bool:
    hour = datetime.fromtimestamp(now).hour
    return window[0] <= hour < window[1]


class HrReplyOrchestrator:
    """Consume unclassified inbound HR messages into the reply queue."""

    def __init__(
        self,
        settings: Settings,
        *,
        classifier: HrQuestionClassifier | None = None,
    ) -> None:
        self._settings = settings
        self._state_db = settings.jobagent_state_db.expanduser().resolve()
        self._classifier = classifier
        self._model_started = False

    # -- lazy singletons (cheap to construct, reused across ticks) ----------

    def _get_classifier(self) -> HrQuestionClassifier:
        if self._classifier is None:
            if not self._model_started:
                model = build_agent_model(self._settings)
                self._classifier = HrQuestionClassifier(model)
                self._model_started = True
            else:
                raise RuntimeError("classifier model already failed to start")
        assert self._classifier is not None
        return self._classifier

    def _load_config(self, connection: sqlite3.Connection) -> EngineConfig:
        base = EngineConfig(
            auto_enabled=self._settings.boss_auto_reply_enabled,
            audit_only=self._settings.boss_auto_reply_audit_only,
        )
        rows = connection.execute(
            "SELECT key, value FROM boss_reply_engine_config"
        ).fetchall()
        overrides = {str(key): str(value) for key, value in rows}
        return engine_config_from_overrides(base, overrides)

    # -- main tick ------------------------------------------------------------

    async def run_once(self) -> PipelineStats:
        stats = PipelineStats()
        connection = sqlite3.connect(self._state_db)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.executescript(_ENGINE_SCHEMA)
        now = time.time()
        try:
            if not in_send_window(now):
                stats.deferred_window = self._count_unclassified(connection)
                return stats
            config = self._load_config(connection)
            groups = self._fetch_groups(connection)
            if not groups:
                return stats
            classifier = self._get_classifier()
            # Idempotent: ensures the seeded salary talking points exist
            # even before any backfill ran.
            from jobagent.hr_reply.facts import seed_default_facts

            facts_store = CandidateFactStore(self._state_db)
            seed_default_facts(facts_store)
            insights = CompanyInsightStore(self._state_db)
            state_store = ConversationStateStore(self._state_db)
            queue = BossReplyQueue(self._state_db)
            try:
                engine = ReplyPolicyEngine(config=config, insights=insights)
                for group in groups:
                    stats = self._process_group(
                        group=group,
                        classifier=classifier,
                        engine=engine,
                        facts_store=facts_store,
                        state_store=state_store,
                        queue=queue,
                        connection=connection,
                        stats=stats,
                        now=now,
                    )
            finally:
                queue.close()
                state_store.close()
                insights.close()
                facts_store.close()
            connection.commit()
        finally:
            connection.close()
        return stats

    # -- grouping -------------------------------------------------------------

    def _count_unclassified(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            """SELECT COUNT(*) FROM boss_inbound_messages
            WHERE processing_status='unclassified' AND baseline=0"""
        ).fetchone()
        return int(row[0]) if row else 0

    def _fetch_groups(
        self, connection: sqlite3.Connection
    ) -> list[dict[str, Any]]:
        """Merge consecutive unclassified HR messages per conversation."""

        rows = connection.execute(
            """SELECT message_key, conversation_id, friend_id, friend_source,
                      encrypt_boss_id, company, title, hr_name, text, sent_at
            FROM boss_inbound_messages
            WHERE processing_status='unclassified' AND baseline=0
            ORDER BY conversation_id, sent_at"""
        ).fetchall()
        columns = [
            "message_key", "conversation_id", "friend_id", "friend_source",
            "encrypt_boss_id", "company", "title", "hr_name", "text", "sent_at",
        ]
        groups: list[dict[str, Any]] = []
        for row in rows:
            record = dict(zip(columns, row, strict=True))
            last = groups[-1] if groups else None
            if (
                last is not None
                and last["conversation_id"] == record["conversation_id"]
                and record["sent_at"] - last["sent_at_end"] <= MERGE_WINDOW_SECONDS
            ):
                last["messages"].append(record)
                last["sent_at_end"] = record["sent_at"]
            else:
                groups.append(
                    {
                        "conversation_id": record["conversation_id"],
                        "friend_id": int(record["friend_id"]),
                        "friend_source": int(record["friend_source"]),
                        "encrypt_boss_id": str(record["encrypt_boss_id"]),
                        "company": str(record["company"]),
                        "title": str(record["title"]),
                        "hr_name": str(record["hr_name"]),
                        "messages": [record],
                        "sent_at_start": record["sent_at"],
                        "sent_at_end": record["sent_at"],
                    }
                )
        return groups

    # -- one group ------------------------------------------------------------

    def _process_group(
        self,
        *,
        group: dict[str, Any],
        classifier: HrQuestionClassifier,
        engine: ReplyPolicyEngine,
        facts_store: CandidateFactStore,
        state_store: ConversationStateStore,
        queue: BossReplyQueue,
        connection: sqlite3.Connection,
        stats: PipelineStats,
        now: float,
    ) -> PipelineStats:
        keys = [message["message_key"] for message in group["messages"]]
        texts = [str(message["text"]) for message in group["messages"]]
        fingerprint = question_fingerprint(texts)
        state = state_store.get(group["friend_id"])

        if float(state.get("manual_cooldown_until") or 0) > now:
            self._mark(connection, keys, "skipped_manual")
            stats.skipped_manual += len(keys)
            stats.reasons.append("manual_takeover_cooldown")
            return stats

        classification = classifier.classify(
            hr_messages=texts,
            conversation_context=f"{group['company']} / {group['title']}",
        )
        if classification.degraded:
            stats.degraded += 1
        decision = engine.decide(
            classification=classification,
            facts=facts_store.snapshot(),
            conversation_state=state,
            company=group["company"],
            fingerprint=fingerprint,
            now=now,
        )
        stats.reasons.extend(decision.reasons)

        status = "awaiting_human"
        if decision.action == "auto":
            queue.upsert_policy(
                policy_id=decision.policy_id,
                version=decision.policy_version,
                intent=decision.intent,
                authorized_until=decision.authorized_until,
            )
            status = "auto_ready"
            stats.auto_ready += 1
            state_store.note_auto_reply(group["friend_id"], decision.draft_text)
        elif decision.action == "decline_human":
            status = "awaiting_human"
            stats.awaiting_human += 1
        else:
            stats.awaiting_human += 1
        state_store.note_question(group["friend_id"], fingerprint)

        queue.enqueue(
            conversation_id=group["conversation_id"],
            friend_id=group["friend_id"],
            friend_source=group["friend_source"],
            encrypt_boss_id=group["encrypt_boss_id"],
            source_message_ids=keys,
            company=group["company"],
            title=group["title"],
            hr_name=group["hr_name"],
            hr_message="\n".join(texts),
            draft_text=decision.draft_text,
            risk_level=decision.risk_level,
            intent=decision.intent,
            confidence=decision.confidence,
            fact_ids=decision.fact_ids,
            policy_id=decision.policy_id,
            policy_version=decision.policy_version,
            authorized_until=decision.authorized_until,
            risk_verified=1 if decision.action == "auto" else 0,
            status=status,
        )
        self._mark(connection, keys, "drafted")
        stats.processed += len(keys)
        logger.info(
            "hr_reply pipeline: %s %s intent=%s action=%s reasons=%s",
            group["company"],
            group["hr_name"],
            decision.intent,
            decision.action,
            ",".join(decision.reasons) or "-",
        )
        return stats

    @staticmethod
    def _mark(
        connection: sqlite3.Connection, keys: list[str], status: str
    ) -> None:
        connection.executemany(
            "UPDATE boss_inbound_messages SET processing_status=? WHERE message_key=?",
            [(status, key) for key in keys],
        )

    # -- runtime switches (kill switch / audit) --------------------------------

    @staticmethod
    def set_engine_override(state_db: Path, key: str, value: str) -> None:
        connection = sqlite3.connect(state_db.expanduser().resolve())
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.executescript(_ENGINE_SCHEMA)
            with connection:
                connection.execute(
                    """INSERT INTO boss_reply_engine_config (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                        updated_at=excluded.updated_at""",
                    (key, value, time.time()),
                )
        finally:
            connection.close()

    @staticmethod
    def get_engine_overrides(state_db: Path) -> dict[str, str]:
        connection = sqlite3.connect(state_db.expanduser().resolve())
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.executescript(_ENGINE_SCHEMA)
            rows = connection.execute(
                "SELECT key, value FROM boss_reply_engine_config"
            ).fetchall()
            return {str(key): str(value) for key, value in rows}
        finally:
            connection.close()


def would_auto_send_flag(decision: PolicyDecision) -> str:
    """Marker appended to audit-only drafts so reviewers see what would fire."""

    if decision.action == "auto":
        return "[would_auto_send]"
    return ""
