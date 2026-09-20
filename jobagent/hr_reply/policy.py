"""Deterministic reply policy engine and conversation state.

The engine never calls the LLM and never invents text: drafts are assembled
from the user's own fact talking points through fixed templates, so the
first-person identity and the no-insight-leakage rules hold by construction.

Hard human routes (design 2026-09-20): interview time, negotiation
follow-ups, multi-intent/unknown, low confidence, fact gaps, manual-takeover
cooldown, per-conversation auto-reply limits, duplicate questions, resume
requests, offer decisions and contact/credential sharing.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobagent.hr_reply.classifier import Classification
from jobagent.hr_reply.facts import CandidateFact, CompanyInsightStore
from jobagent.journey.store import _enable_wal

CONFIDENCE_MIN = 0.7
MAX_AUTO_PER_CONVERSATION_PER_DAY = 2
DUPLICATE_WINDOW_HOURS = 24.0
MANUAL_COOLDOWN_HOURS = 4.0
POLICY_AUTHORIZED_SECONDS = 24 * 3600.0

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS hr_conversation_state (
    friend_id INTEGER PRIMARY KEY,
    auto_reply_count_24h INTEGER NOT NULL DEFAULT 0,
    window_started_at REAL NOT NULL DEFAULT 0,
    last_auto_reply_at REAL NOT NULL DEFAULT 0,
    last_auto_reply_text TEXT NOT NULL DEFAULT '',
    last_question_fingerprint TEXT NOT NULL DEFAULT '',
    last_question_at REAL NOT NULL DEFAULT 0,
    manual_cooldown_until REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Runtime switches; overrides persist in boss_reply_engine_config."""

    auto_enabled: bool = True
    audit_only: bool = True  # observation period: drafts only, no auto send
    confidence_min: float = CONFIDENCE_MIN
    max_auto_per_conversation_per_day: int = MAX_AUTO_PER_CONVERSATION_PER_DAY
    duplicate_window_hours: float = DUPLICATE_WINDOW_HOURS
    manual_cooldown_hours: float = MANUAL_COOLDOWN_HOURS

@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Engine verdict for one classified message group."""

    action: str  # 'auto' | 'human' | 'decline_human'
    draft_text: str
    risk_level: str  # 'low' | 'high'
    reasons: list[str] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)
    intent: str = "unknown"
    confidence: float = 0.0
    policy_id: str = ""
    policy_version: int = 0
    authorized_until: float = 0.0

    @property
    def would_auto_send(self) -> bool:
        return self.action == "auto"


class ConversationStateStore:
    """Per-HR auto-reply counters, cooldowns and duplicate detection."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path.expanduser().resolve())
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_STATE_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> ConversationStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _row(self, friend_id: int) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM hr_conversation_state WHERE friend_id=?", (friend_id,)
        ).fetchone()
        if row is None:
            return None
        columns = [d[0] for d in self._connection.execute(
            "SELECT * FROM hr_conversation_state WHERE friend_id=?", (friend_id,)
        ).description]
        return dict(zip(columns, row, strict=True))

    def get(self, friend_id: int) -> dict[str, Any]:
        return self._row(friend_id) or {
            "friend_id": friend_id,
            "auto_reply_count_24h": 0,
            "window_started_at": 0.0,
            "last_auto_reply_at": 0.0,
            "last_auto_reply_text": "",
            "last_question_fingerprint": "",
            "last_question_at": 0.0,
            "manual_cooldown_until": 0.0,
        }

    def note_manual_outbound(self, friend_id: int, *, cooldown_hours: float) -> None:
        """Takeover signal: the user is chatting manually - pause automation."""

        state = self.get(friend_id)
        now = time.time()
        with self._connection:
            self._connection.execute(
                """INSERT INTO hr_conversation_state
                (friend_id, auto_reply_count_24h, window_started_at, last_auto_reply_at,
                 last_auto_reply_text, last_question_fingerprint, last_question_at,
                 manual_cooldown_until, updated_at)
                VALUES (?, 0, 0, ?, '', '', ?, ?, ?)
                ON CONFLICT(friend_id) DO UPDATE SET
                    manual_cooldown_until=excluded.manual_cooldown_until,
                    updated_at=excluded.updated_at""",
                (
                    friend_id,
                    state.get("last_auto_reply_at", 0.0),
                    state.get("last_question_at", 0.0),
                    now + cooldown_hours * 3600.0,
                    now,
                ),
            )

    def note_question(self, friend_id: int, fingerprint: str) -> None:
        now = time.time()
        with self._connection:
            self._connection.execute(
                """INSERT INTO hr_conversation_state
                (friend_id, auto_reply_count_24h, window_started_at, last_auto_reply_at,
                 last_auto_reply_text, last_question_fingerprint, last_question_at,
                 manual_cooldown_until, updated_at)
                VALUES (?, 0, 0, 0, '', ?, ?, 0, ?)
                ON CONFLICT(friend_id) DO UPDATE SET
                    last_question_fingerprint=excluded.last_question_fingerprint,
                    last_question_at=excluded.last_question_at,
                    updated_at=excluded.updated_at""",
                (friend_id, fingerprint, now, now),
            )

    def note_auto_reply(self, friend_id: int, text: str) -> None:
        state = self.get(friend_id)
        now = time.time()
        window_start = float(state.get("window_started_at") or 0.0)
        if now - window_start > 24 * 3600.0:
            window_start, count = now, 0
        else:
            count = int(state.get("auto_reply_count_24h") or 0)
        with self._connection:
            self._connection.execute(
                """INSERT INTO hr_conversation_state
                (friend_id, auto_reply_count_24h, window_started_at, last_auto_reply_at,
                 last_auto_reply_text, last_question_fingerprint, last_question_at,
                 manual_cooldown_until, updated_at)
                VALUES (?, ?, ?, ?, ?, '', ?, 0, ?)
                ON CONFLICT(friend_id) DO UPDATE SET
                    auto_reply_count_24h=excluded.auto_reply_count_24h,
                    window_started_at=excluded.window_started_at,
                    last_auto_reply_at=excluded.last_auto_reply_at,
                    last_auto_reply_text=excluded.last_auto_reply_text,
                    updated_at=excluded.updated_at""",
                (friend_id, count + 1, window_start, now, text, now, now),
            )


def question_fingerprint(hr_messages: list[str]) -> str:
    joined = "|".join(message.strip() for message in hr_messages)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _talking_points(fact: CandidateFact) -> list[str]:
    points = fact.payload.get("talking_points")
    if isinstance(points, list) and points:
        return [str(point) for point in points]
    value = fact.payload.get("value")
    return [str(value)] if value else []


def _join_draft(sentences: list[str]) -> str:
    cleaned = [sentence.strip().rstrip("。；;") for sentence in sentences if sentence.strip()]
    return "。".join(cleaned) + "。"


def assemble_draft(intent: str, facts: dict[str, CandidateFact]) -> str:
    """Deterministic first-person draft from the user's own facts only."""

    if intent == "salary_expectation" and "salary_expectation" in facts:
        return _join_draft(_talking_points(facts["salary_expectation"]))
    if intent == "availability" and "availability" in facts:
        return _join_draft(_talking_points(facts["availability"]))
    if intent == "employment_status" and "employment_status" in facts:
        return _join_draft(_talking_points(facts["employment_status"]))
    if intent == "city" and "city" in facts:
        return _join_draft(_talking_points(facts["city"]))
    if intent == "interview_mode":
        # No fact requirements and no location leak (review finding 19):
        # only the willingness to talk online, same as basic_interest.
        return _join_draft(["可以先线上沟通，方便的话我们先电话或视频聊"])
    if intent == "outsourcing":
        stance = facts.get("outsourcing_stance")
        if stance is not None and str(stance.payload.get("stance") or "") == "refuse":
            return _join_draft(
                [
                    "想先确认一下岗位的用工主体",
                    "这个岗位是贵司自有编制，还是外包/派遣用工？劳动合同签署主体和汇报关系分别是什么？",
                ]
            )
    if intent == "basic_interest":
        return _join_draft(["对这个岗位很感兴趣，想进一步了解团队和业务方向"])
    return ""


class ReplyPolicyEngine:
    """Route one classification to auto / human with full audit reasons."""

    def __init__(
        self,
        *,
        config: EngineConfig,
        insights: CompanyInsightStore | None = None,
    ) -> None:
        self._config = config
        self._insights = insights

    @property
    def config(self) -> EngineConfig:
        return self._config

    def decide(
        self,
        *,
        classification: Classification,
        facts: dict[str, CandidateFact],
        conversation_state: dict[str, Any],
        company: str,
        fingerprint: str,
        now: float | None = None,
    ) -> PolicyDecision:
        now = now if now is not None else time.time()
        reasons: list[str] = []
        intent = classification.intent

        def human(draft: str = "", extra_reasons: list[str] | None = None) -> PolicyDecision:
            merged = [*reasons, *(extra_reasons or [])]
            return PolicyDecision(
                action="human",
                draft_text=draft,
                risk_level="high" if merged else "low",
                reasons=merged,
                intent=intent,
                confidence=classification.confidence,
            )

        # --- hard human gates (ordered by design priority) -------------------
        if classification.degraded:
            reasons.append(f"degraded:{classification.degraded_reason}")
            return human()
        if classification.requires_human:
            reasons.append(f"intent_human:{intent}")
            if classification.negotiation_followup:
                reasons.append("negotiation_followup")
            return human()
        if classification.confidence < self._config.confidence_min:
            reasons.append(
                f"low_confidence:{classification.confidence:.2f}"
                f"<{self._config.confidence_min:.2f}"
            )
            return human()

        # --- fact coverage ---------------------------------------------------
        missing = [
            category
            for category in classification.required_facts
            if category not in facts
        ]
        if missing:
            reasons.append(f"fact_missing:{','.join(missing)}")
            return human()
        fact_ids = [facts[category].id for category in classification.required_facts]

        # --- company-level action rule (insight, never quoted) ----------------
        if (
            self._insights is not None
            and self._insights.is_outsourcing(company)
            and "outsourcing_stance" in facts
            and str(facts["outsourcing_stance"].payload.get("stance") or "") == "refuse"
        ):
            reasons.append("company_outsourcing_refused")
            return PolicyDecision(
                action="decline_human",
                draft_text=_join_draft(
                    ["感谢您的关注", "这个岗位和我的预期不太匹配，就不占用您时间了，祝招聘顺利"]
                ),
                risk_level="low",
                reasons=reasons,
                fact_ids=[facts["outsourcing_stance"].id],
                intent=intent,
                confidence=classification.confidence,
            )

        # --- conversation state gates -----------------------------------------
        if float(conversation_state.get("manual_cooldown_until") or 0) > now:
            reasons.append("manual_takeover_cooldown")
            return human()
        # Effective auto count: the 24h window rolls at read time, so a
        # conversation that hit yesterday's cap is not capped forever
        # (review finding 5).
        window_start = float(conversation_state.get("window_started_at") or 0)
        effective_count = int(conversation_state.get("auto_reply_count_24h") or 0)
        if now - window_start > 24 * 3600.0:
            effective_count = 0
        if effective_count >= self._config.max_auto_per_conversation_per_day:
            reasons.append("auto_reply_limit_reached")
            return human()
        if (
            str(conversation_state.get("last_question_fingerprint") or "") == fingerprint
            and now - float(conversation_state.get("last_question_at") or 0)
            < self._config.duplicate_window_hours * 3600.0
        ):
            reasons.append("duplicate_question_within_window")
            return human()

        # --- audit-only observation period ------------------------------------
        if self._config.audit_only or not self._config.auto_enabled:
            reasons.append("audit_only_observation" if self._config.audit_only else "auto_disabled")
            return PolicyDecision(
                action="human",
                draft_text=assemble_draft(intent, facts),
                risk_level="low",
                reasons=reasons,
                fact_ids=fact_ids,
                intent=intent,
                confidence=classification.confidence,
            )

        draft = assemble_draft(intent, facts)
        if not draft:
            reasons.append("no_template_for_intent")
            return human()
        return PolicyDecision(
            action="auto",
            draft_text=draft,
            risk_level="low",
            reasons=["fact_covered", "policy_authorized"],
            fact_ids=fact_ids,
            intent=intent,
            confidence=classification.confidence,
            policy_id=f"auto-{intent}",
            policy_version=1,
            authorized_until=now + POLICY_AUTHORIZED_SECONDS,
        )


def engine_config_from_overrides(
    base: EngineConfig, overrides: dict[str, str]
) -> EngineConfig:
    """Apply persisted key/value overrides onto the base config."""

    config = base
    if "auto_enabled" in overrides:
        config = _replace(config, auto_enabled=overrides["auto_enabled"] == "1")
    if "audit_only" in overrides:
        config = _replace(config, audit_only=overrides["audit_only"] == "1")
    return config


def _replace(config: EngineConfig, **changes: Any) -> EngineConfig:
    from dataclasses import replace

    return replace(config, **changes)


