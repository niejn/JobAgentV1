"""Candidate facts and company insights behind HR auto-replies.

Source grading (design 2026-09-20):
- A: user-authored/approved statements about themselves  -> CandidateFact
- B: HR statements about their company/jobs              -> CompanyInsight
     (context only - never quoted in any reply text)
- C: inferring the candidate's stance from HR statements -> prohibited,
     enforced by the policy engine requiring user-sourced facts.

Facts are versioned: a later upsert supersedes the previous row for its
category and keeps the history; conflicting extractions mark the category
``conflict_pending`` and pause auto-replies for it until the user resolves.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobagent.journey.store import _enable_wal

#: Categories the auto-reply policy requires before answering HR questions.
CORE_FACT_CATEGORIES = (
    "salary_expectation",
    "availability",
    "employment_status",
    "city",
    "outsourcing_stance",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_facts (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    constraints_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    valid_until REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_facts_lookup
ON candidate_facts(category, status, version);
CREATE TABLE IF NOT EXISTS company_insights (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    insight_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_conversation TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(company, insight_type)
);
"""


@dataclass(frozen=True, slots=True)
class CandidateFact:
    """One versioned user-confirmed fact."""

    id: str
    category: str
    payload: dict[str, Any]
    constraints: list[str]
    source: str
    source_ref: str
    version: int
    status: str
    valid_until: float | None

    @property
    def expired(self) -> bool:
        return self.valid_until is not None and self.valid_until < time.time()


class CandidateFactStore:
    """Versioned facts about the candidate, one active row per category."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path.expanduser().resolve())
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> CandidateFactStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def active(self, category: str) -> CandidateFact | None:
        row = self._connection.execute(
            """SELECT id, category, payload_json, constraints_json, source,
                      source_ref, version, status, valid_until
            FROM candidate_facts
            WHERE category=? AND status IN ('active','conflict_pending')
            ORDER BY version DESC LIMIT 1""",
            (category,),
        ).fetchone()
        if row is None:
            return None
        return CandidateFact(
            id=str(row[0]),
            category=str(row[1]),
            payload=json.loads(row[2]),
            constraints=json.loads(row[3]),
            source=str(row[4]),
            source_ref=str(row[5]),
            version=int(row[6]),
            status=str(row[7]),
            valid_until=float(row[8]) if row[8] is not None else None,
        )

    def usable(self, category: str) -> CandidateFact | None:
        """The fact auto-replies may cite: active, not expired, no conflict."""

        fact = self.active(category)
        if fact is None or fact.status != "active" or fact.expired:
            return None
        return fact

    def upsert(
        self,
        *,
        category: str,
        payload: dict[str, Any],
        source: str,
        source_ref: str = "",
        constraints: list[str] | None = None,
        valid_until: float | None = None,
    ) -> CandidateFact:
        """Record a new fact version; supersedes the previous active row.

        ``source`` is one of ``user_statement`` / ``approved_reply`` /
        ``questionnaire`` / ``seed`` - only user-sourced origins may seed a
        category the first time (grading rule A).
        """

        if source not in {"user_statement", "approved_reply", "questionnaire", "seed"}:
            raise ValueError(f"invalid fact source: {source}")
        now = time.time()
        current = self.active(category)
        version = (current.version + 1) if current is not None else 1
        fact_id = f"fact-{uuid.uuid4().hex[:12]}"
        with self._connection:
            if current is not None:
                self._connection.execute(
                    "UPDATE candidate_facts SET status='superseded' WHERE id=?",
                    (current.id,),
                )
            self._connection.execute(
                """INSERT INTO candidate_facts
                (id, category, payload_json, constraints_json, source, source_ref,
                 version, status, valid_until, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (
                    fact_id,
                    category,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(constraints or [], ensure_ascii=False),
                    source,
                    source_ref,
                    version,
                    valid_until,
                    now,
                    now,
                ),
            )
        fact = self.active(category)
        assert fact is not None
        return fact

    def mark_conflict(self, category: str, *, note: str) -> None:
        """Pause auto-replies for a category until the user resolves it."""

        fact = self.active(category)
        if fact is None:
            return
        payload = {**fact.payload, "conflict_note": note[:200]}
        with self._connection:
            self._connection.execute(
                """UPDATE candidate_facts
                SET status='conflict_pending', payload_json=?, updated_at=?
                WHERE id=?""",
                (
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                    fact.id,
                ),
            )

    def snapshot(self) -> dict[str, CandidateFact]:
        """All usable facts keyed by category - the classifier's fact base."""

        return {
            category: fact
            for category in CORE_FACT_CATEGORIES
            if (fact := self.usable(category)) is not None
        }

    def gap_categories(self) -> list[str]:
        """Core categories without a usable fact - the questionnaire gap."""

        return [c for c in CORE_FACT_CATEGORIES if self.usable(c) is None]


class CompanyInsightStore:
    """Company-level world knowledge (grading rule B).

    Insights drive internal decisions only - company marked outsourcing plus
    a user stance refusing outsourcing stops initiating contact - and are
    never quoted in reply text (enforced by policy templates).
    """

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path.expanduser().resolve())
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> CompanyInsightStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upsert(
        self,
        *,
        company: str,
        insight_type: str,
        payload: dict[str, Any],
        source_conversation: str = "",
    ) -> None:
        with self._connection:
            self._connection.execute(
                """INSERT INTO company_insights
                (id, company, insight_type, payload_json, source_conversation, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(company, insight_type) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    source_conversation=excluded.source_conversation,
                    created_at=excluded.created_at""",
                (
                    f"insight-{uuid.uuid4().hex[:12]}",
                    company.strip(),
                    insight_type,
                    json.dumps(payload, ensure_ascii=False),
                    source_conversation,
                    time.time(),
                ),
            )

    def get(self, company: str, insight_type: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json FROM company_insights WHERE company=? AND insight_type=?",
            (company.strip(), insight_type),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None

    def is_outsourcing(self, company: str) -> bool:
        insight = self.get(company, "outsourcing")
        return bool(insight and insight.get("is_outsourcing") is True)


def seed_default_facts(store: CandidateFactStore) -> int:
    """Seed the user's 2026-09-20 salary talking points if absent.

    Returns the number of facts seeded (0 when the category already has one).
    """

    if store.usable("salary_expectation") is not None:
        return 0
    store.upsert(
        category="salary_expectation",
        payload={
            "talking_points": [
                "更看重团队氛围和所做工作的价值",
                "薪资在 3 万多",
                "岗位比较匹配的话薪资可以折中，不会成为 blocking 的问题",
            ]
        },
        source="user_statement",
        source_ref="2026-09-20 用户原话",
        constraints=["不暴露底线数字", "不报区间下限/上限", "不主动展开谈判"],
    )
    return 1
