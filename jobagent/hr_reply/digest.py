"""Daily digest: HR reply activity, fact changes and job status moves.

Rendered as markdown under the git-ignored reports directory, pushed as a
short summary through the WeChat management outbox, and queryable from
chat via the digest tool.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class DigestData:
    """One day of Boss HR-reply activity."""

    day: str
    new_inbound: int = 0
    auto_ready: int = 0
    awaiting_human: int = 0
    sent_today: int = 0
    unverified_today: int = 0
    open_awaiting: int = 0
    fact_changes_today: int = 0
    insight_changes_today: int = 0
    degraded_events: int = 0
    top_companies: list[str] = field(default_factory=list)


def build_digest(state_db: Path, *, day: date | None = None) -> DigestData:
    day = day or date.today()
    day_str = day.isoformat()
    connection = sqlite3.connect(state_db.expanduser().resolve())
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        digest = DigestData(day=day_str)
        def one(query: str, *params: Any) -> int:
            try:
                row = connection.execute(query, params).fetchone()
            except sqlite3.OperationalError:
                return 0  # boss tables not created yet on a fresh database
            return int(row[0]) if row and row[0] is not None else 0
        digest.new_inbound = one(
            """SELECT COUNT(*) FROM boss_inbound_messages
            WHERE date(first_seen_at)=date(?)""",  # first_seen_at is TEXT datetime
            day_str,
        ) or one(
            """SELECT COUNT(*) FROM boss_inbound_messages
            WHERE date(first_seen_at, 'localtime')=date(?)""",
            day_str,
        )
        digest.auto_ready = one(
            """SELECT COUNT(*) FROM boss_reply_queue
            WHERE status IN ('auto_ready','approved','sending','sent')
              AND intent!='unknown' AND risk_verified=1
              AND date(created_at)=date(?)""",
            day_str,
        )
        digest.awaiting_human = one(
            """SELECT COUNT(*) FROM boss_reply_queue
            WHERE status='awaiting_human' AND date(created_at)=date(?)""",
            day_str,
        )
        digest.sent_today = one(
            """SELECT COUNT(*) FROM boss_reply_queue
            WHERE sent_at IS NOT NULL AND date(sent_at)=date(?)""",
            day_str,
        )
        digest.unverified_today = one(
            """SELECT COUNT(*) FROM boss_reply_queue
            WHERE error LIKE '%unverified%' AND date(created_at)=date(?)""",
            day_str,
        )
        digest.open_awaiting = one(
            "SELECT COUNT(*) FROM boss_reply_queue WHERE status='awaiting_human'"
        )
        digest.fact_changes_today = one(
            """SELECT COUNT(*) FROM candidate_facts
            WHERE date(updated_at,'unixepoch','localtime')=date(?)""",
            day_str,
        )
        digest.insight_changes_today = one(
            """SELECT COUNT(*) FROM company_insights
            WHERE date(created_at,'unixepoch','localtime')=date(?)""",
            day_str,
        )
        try:
            degraded = connection.execute(
                "SELECT value FROM boss_reply_engine_config WHERE key=?",
                (f"degraded_events:{day_str}",),
            ).fetchone()
            digest.degraded_events = int(degraded[0]) if degraded else 0
        except sqlite3.OperationalError:
            digest.degraded_events = 0
        try:
            companies = connection.execute(
                """SELECT company, COUNT(*) AS hits FROM boss_reply_queue
                WHERE date(created_at)=date(?) GROUP BY company
                ORDER BY hits DESC LIMIT 5""",
                (day_str,),
            ).fetchall()
        except sqlite3.OperationalError:
            companies = []
        digest.top_companies = [f"{str(row[0])}({row[1]})" for row in companies]
        return digest
    finally:
        connection.close()


def render_markdown(digest: DigestData) -> str:
    lines = [
        f"# Boss HR 沟通日报 · {digest.day}",
        "",
        f"- 新增 HR 消息：{digest.new_inbound}",
        f"- 生成草稿：自动 {digest.auto_ready} 条 / 待人工 {digest.awaiting_human} 条",
        f"- 今日已发送：{digest.sent_today}（回执未确认 {digest.unverified_today}）",
        f"- 当前待人工处理：{digest.open_awaiting}",
        f"- 事实变更：{digest.fact_changes_today}；公司洞察变更：{digest.insight_changes_today}",
        f"- LLM 降级事件：{digest.degraded_events}",
    ]
    if digest.top_companies:
        lines.append(f"- 活跃公司：{', '.join(digest.top_companies)}")
    return "\n".join(lines) + "\n"


def render_wechat_summary(digest: DigestData) -> str:
    return (
        f"[{digest.day}] Boss 日报：新消息 {digest.new_inbound}，"
        f"草稿 自动{digest.auto_ready}/待人工{digest.awaiting_human}，"
        f"已发送 {digest.sent_today}，待处理队列 {digest.open_awaiting}。"
        "发送 /boss list 查看待回复。"
    )


def write_report(digest: DigestData, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"boss-digest-{digest.day}.md"
    path.write_text(render_markdown(digest), encoding="utf-8")
    return path


def push_wechat_summary(state_db: Path, digest: DigestData) -> None:
    """Queue the short summary on the WeChat management outbox."""

    connection = sqlite3.connect(state_db.expanduser().resolve())
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        with connection:
            connection.execute(
                """INSERT INTO management_outbox
                (event_id, channel, event_type, aggregate_id, payload_json, available_at)
                VALUES (?, 'wechat', 'daily_digest', ?, ?, ?)""",
                (
                    f"digest-{digest.day}-{int(time.time())}",
                    digest.day,
                    json.dumps(
                        {"text": render_wechat_summary(digest)}, ensure_ascii=False
                    ),
                    time.time(),
                ),
            )
    finally:
        connection.close()


def digest_summary_for_chat(state_db: Path, *, day: date | None = None) -> str:
    digest = build_digest(state_db, day=day)
    stamp = datetime.now().strftime("%m-%d %H:%M")
    return f"{render_wechat_summary(digest)}（{stamp} 生成）"
