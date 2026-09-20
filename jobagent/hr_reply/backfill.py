"""Cold-start backfill: mine history first, questionnaire only for gaps.

Design decision (2026-09-20): instead of asking the user everything, the
first run scans what already exists - approved outgoing replies for A-level
facts, HR statements for B-level company insights - and only reports the
still-missing core categories as a questionnaire gap the chat tools surface
to the user.

Extraction is LLM-assisted with the same degradation contract as the
classifier: on model failure the run reports ``degraded`` and keeps the
facts already seeded. History never overwrites an existing fact.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field

from jobagent.hr_reply.facts import (
    CORE_FACT_CATEGORIES,
    CandidateFactStore,
    CompanyInsightStore,
    seed_default_facts,
)

logger = logging.getLogger(__name__)


class ExtractionSchema(BaseModel):
    """Structured extraction over one conversation's history."""

    facts: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "User-confirmed statements about the candidate. Each item: "
            "category (salary_expectation/availability/employment_status/"
            "city/outsourcing_stance), talking_points or value, evidence."
        ),
    )
    insights: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "HR statements about their company: company, insight_type "
            "(outsourcing/salary_range/other), value summary, evidence."
        ),
    )


@dataclass(slots=True)
class BackfillReport:
    """Outcome of one cold-start run, for the digest and chat tools."""

    seeded_facts: int = 0
    extracted_facts: list[str] = field(default_factory=list)
    extracted_insights: list[str] = field(default_factory=list)
    gap_categories: list[str] = field(default_factory=list)
    conversations_scanned: int = 0
    degraded: bool = False
    degraded_reason: str = ""


def run_backfill(
    state_db: Path,
    *,
    model: BaseChatModel | None = None,
    max_conversations: int = 30,
) -> BackfillReport:
    """One-shot cold-start extraction; safe to re-run (idempotent seeds)."""

    report = BackfillReport()
    connection = sqlite3.connect(state_db.expanduser().resolve())
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        conversations = _conversation_history(connection, limit=1000)[
            :max_conversations
        ]
        report.conversations_scanned = len(conversations)
        with (
            CandidateFactStore(state_db) as facts,
            CompanyInsightStore(state_db) as insights,
        ):
            report.seeded_facts = seed_default_facts(facts)
            if model is not None:
                _extract_conversations(conversations, facts, insights, report, model)
            report.gap_categories = facts.gap_categories()
    finally:
        connection.close()
    return report


def _conversation_history(
    connection: sqlite3.Connection, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Stored inbound messages grouped by conversation, newest-first."""

    rows = connection.execute(
        """SELECT conversation_id, company, hr_name, text, sent_at
        FROM boss_inbound_messages
        ORDER BY conversation_id, sent_at DESC
        LIMIT ?""",
        (limit,),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for conversation_id, company, hr_name, text, _sent_at in rows:
        entry = grouped.setdefault(
            str(conversation_id),
            {
                "conversation_id": str(conversation_id),
                "company": str(company),
                "hr_name": str(hr_name),
                "messages": [],
            },
        )
        entry["messages"].append(str(text))
    return list(grouped.values())


def _extract_conversations(
    conversations: list[dict[str, Any]],
    facts: CandidateFactStore,
    insights: CompanyInsightStore,
    report: BackfillReport,
    model: BaseChatModel,
) -> None:
    structured = model.with_structured_output(ExtractionSchema)
    for conversation in conversations:
        body = "\n".join(
            f"{conversation['hr_name']}：{text}"
            for text in conversation["messages"][:30]
        )
        prompt = (
            "从以下 Boss 直聘聊天记录提取信息。注意区分：只有【求职者发出的消息】"
            "才是求职者事实（facts）；HR 说的关于他们公司的信息是公司洞察（insights）。\n"
            f"公司：{conversation['company']}\n{body}"
        )
        try:
            raw = structured.invoke(prompt)
        except Exception as exc:  # noqa: BLE001 - degradation is the contract
            report.degraded = True
            report.degraded_reason = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("backfill extraction degraded: %s", report.degraded_reason)
            return
        if isinstance(raw, ExtractionSchema):
            payload: dict[str, Any] = raw.model_dump()
        elif isinstance(raw, dict):
            payload = raw
        else:
            continue
        _apply_facts(payload.get("facts") or [], facts, report)
        _apply_insights(payload.get("insights") or [], conversation, insights, report)


def _apply_facts(
    items: list[Any], facts: CandidateFactStore, report: BackfillReport
) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "")
        if category not in CORE_FACT_CATEGORIES:
            continue
        if facts.usable(category) is not None:
            continue  # history never overwrites an existing fact
        talking = item.get("talking_points") or item.get("value") or ""
        if not talking:
            continue
        payload: dict[str, Any] = (
            {"talking_points": [str(point) for point in talking]}
            if isinstance(talking, list)
            else {"value": str(talking)}
        )
        facts.upsert(
            category=category,
            payload=payload,
            source="approved_reply",
            source_ref=str(item.get("evidence") or "")[:200],
        )
        report.extracted_facts.append(category)


def _apply_insights(
    items: list[Any],
    conversation: dict[str, Any],
    insights: CompanyInsightStore,
    report: BackfillReport,
) -> None:
    company = str(conversation.get("company") or "").strip()
    if not company:
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        insight_type = str(item.get("insight_type") or "").strip()
        if insight_type not in {"outsourcing", "salary_range", "other"}:
            continue
        payload: dict[str, Any] = {"summary": str(item.get("value") or "")[:300]}
        if insight_type == "outsourcing":
            payload["is_outsourcing"] = True
        insights.upsert(
            company=company,
            insight_type=insight_type,
            payload=payload,
            source_conversation=str(conversation.get("conversation_id") or ""),
        )
        report.extracted_insights.append(f"{company}:{insight_type}")


def render_gap_questionnaire(gaps: list[str]) -> str:
    """The chat-facing questionnaire for still-missing core facts."""

    if not gaps:
        return "核心求职事实已齐全，无需补充。"
    labels = {
        "salary_expectation": "期望薪资（口径与底线约束）",
        "availability": "最早到岗时间",
        "employment_status": "当前在职/离职状态",
        "city": "所在城市与可接受的工作地点",
        "outsourcing_stance": "对外包/派遣岗位的立场",
    }
    lines = [f"{index}. {labels.get(gap, gap)}" for index, gap in enumerate(gaps, 1)]
    return "以下核心事实还没有记录，请逐条告诉 jobagent（成为跨会话事实）：\n" + "\n".join(lines)


def render_report(report: BackfillReport) -> str:
    return json.dumps(
        {
            "seeded_facts": report.seeded_facts,
            "extracted_facts": report.extracted_facts,
            "extracted_insights": report.extracted_insights,
            "gap_categories": report.gap_categories,
            "conversations_scanned": report.conversations_scanned,
            "degraded": report.degraded,
            "degraded_reason": report.degraded_reason,
        },
        ensure_ascii=False,
    )
