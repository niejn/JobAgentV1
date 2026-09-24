"""In-chat management tools for the Boss HR reply pipeline.

Shares ``BossReplyApplicationService`` with the WeChat ``/boss`` commands -
one service layer, two channels (design 2026-09-20). Also exposes the fact
base, the auto-reply kill switch / audit toggle, the cold-start backfill
and the daily digest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.boss_reply_service import BossReplyApplicationService
from jobagent.hr_reply.backfill import (
    render_gap_questionnaire,
    render_report,
    run_backfill,
)
from jobagent.hr_reply.digest import digest_summary_for_chat
from jobagent.hr_reply.facts import CORE_FACT_CATEGORIES, CandidateFactStore
from jobagent.hr_reply.orchestrator import HrReplyOrchestrator


class BossReplyViewRequest(BaseModel):
    which: Literal["inbox", "pending"] = Field(
        default="pending",
        description="inbox=未处理 HR 新消息；pending=待人工确认的回复草稿。",
    )
    limit: int = Field(default=10, ge=1, le=20)


class BossReplyDecideRequest(BaseModel):
    action: Literal["approve", "edit", "skip"]
    reply_ref: str = Field(
        min_length=4,
        description="reply_id 前缀（list 结果里的 编号 列，唯一前缀即可）。",
    )
    draft_version: int = Field(ge=1, description="草稿版本（list 结果里的 V 列）。")
    draft_text: str = Field(
        default="",
        description="edit 时的新正文；approve/skip 留空。",
    )


class BossFactSetRequest(BaseModel):
    category: str = Field(description=f"One of: {', '.join(CORE_FACT_CATEGORIES)}")
    value: str = Field(
        min_length=1,
        max_length=600,
        description="事实内容（用户原话），会成为该类别最新版本。",
    )


class BossAutoConfigRequest(BaseModel):
    key: Literal["auto_enabled", "audit_only"]
    value: Literal["on", "off"]


def build_boss_management_tools(state_db: Path) -> list[BaseTool]:
    """Build the in-chat Boss queue / facts / config / digest tools."""

    service = BossReplyApplicationService(state_db)

    async def boss_reply_view(which: str, limit: int) -> dict[str, Any]:
        if which == "inbox":
            items = service.list_inbox(limit)
            result: dict[str, Any] = {
                "status": "ok",
                "which": "inbox",
                "count": len(items),
                "items": items,
            }
            if not service.monitor_status()["initialized"]:
                result["monitor"] = "not_initialized"
                result["hint"] = (
                    "Boss 监控守护进程尚未完成基线扫描，inbox 只反映本地已同步的数据"
                    "（很可能不是 Boss 上的真实情况）。请先运行 "
                    "`jobagent boss daemon --once` 或 `jobagent watch` 启动监控。"
                )
            return result
        items = service.list_pending(limit)
        rendered = [
            {
                "reply_id": str(row["reply_id"])[:12],
                "draft_version": row.get("draft_version"),
                "company": row.get("company"),
                "hr_name": row.get("hr_name"),
                "intent": row.get("intent"),
                "hr_message": row.get("hr_message"),
                "draft_text": row.get("draft_text"),
                "risk_level": row.get("risk_level"),
            }
            for row in items
        ]
        return {"status": "ok", "which": "pending", "count": len(rendered), "items": rendered}

    async def boss_reply_decide(
        action: str, reply_ref: str, draft_version: int, draft_text: str
    ) -> dict[str, Any]:
        decision = "approve" if action in {"approve", "edit"} else "skip"
        return service.decide(
            reply_ref,
            decision=decision,
            draft_version=draft_version,
            draft_text=draft_text or None,
        )

    async def boss_facts_view() -> dict[str, Any]:
        with CandidateFactStore(state_db) as store:
            facts = {}
            for category in CORE_FACT_CATEGORIES:
                fact = store.usable(category)
                if fact is not None:
                    facts[category] = {
                        "payload": fact.payload,
                        "constraints": fact.constraints,
                        "source": fact.source,
                        "version": fact.version,
                    }
            gaps = store.gap_categories()
        return {"status": "ok", "facts": facts, "gaps": gaps}

    async def boss_fact_set(category: str, value: str) -> dict[str, Any]:
        if category not in CORE_FACT_CATEGORIES:
            return {
                "status": "failed",
                "error_type": "invalid_category",
                "valid": list(CORE_FACT_CATEGORIES),
            }
        with CandidateFactStore(state_db) as store:
            fact = store.upsert(
                category=category,
                payload={"value": value},
                source="user_statement",
                source_ref="chat 工具录入",
            )
        return {"status": "ok", "category": category, "version": fact.version}

    async def boss_auto_config(key: str, value: str) -> dict[str, Any]:
        HrReplyOrchestrator.set_engine_override(
            state_db, key, "1" if value == "on" else "0"
        )
        overrides = HrReplyOrchestrator.get_engine_overrides(state_db)
        return {"status": "ok", "overrides": overrides}

    async def boss_daily_digest() -> dict[str, Any]:
        return {"status": "ok", "summary": digest_summary_for_chat(state_db)}

    async def boss_backfill_cold_start() -> dict[str, Any]:
        import asyncio

        from jobagent.config import get_settings
        from jobagent.models.llm_client import build_agent_model

        report = await asyncio.to_thread(
            run_backfill, state_db, model=build_agent_model(get_settings())
        )
        return {
            "status": "ok",
            "report": render_report(report),
            "questionnaire": render_gap_questionnaire(report.gap_categories),
        }

    return [
        StructuredTool.from_function(
            coroutine=boss_reply_view,
            name="boss_reply_view",
            description=(
                "查看 Boss HR 回复队列：which=inbox 看未处理的新 HR 消息，"
                "which=pending 看待人工确认的草稿（含 reply_id 前缀、版本 V、"
                "HR 原文与草稿）。与微信 /boss inbox、/boss list 同一数据。"
            ),
            args_schema=BossReplyViewRequest,
        ),
        StructuredTool.from_function(
            coroutine=boss_reply_decide,
            name="boss_reply_decide",
            description=(
                "处理一条待确认草稿：approve 批准发送 / edit 修改后发送 / skip 忽略。"
                "必须携带 list 里的 reply_id 前缀和草稿版本。执行前会暂停等待人工批准。"
            ),
            args_schema=BossReplyDecideRequest,
        ),
        StructuredTool.from_function(
            coroutine=boss_facts_view,
            name="boss_facts_view",
            description=(
                "查看跨会话候选人事实库（薪资口径/到岗/在职/城市/外包立场）"
                "及仍缺失的核心类别（gaps）。"
            ),
        ),
        StructuredTool.from_function(
            coroutine=boss_fact_set,
            name="boss_fact_set",
            description=(
                "录入或更新一条跨会话事实（用户新原话覆盖旧版本）。"
                "回答 gaps 里的缺失项时使用；只记录用户亲口确认的内容。"
            ),
            args_schema=BossFactSetRequest,
        ),
        StructuredTool.from_function(
            coroutine=boss_auto_config,
            name="boss_auto_config",
            description=(
                "自动回复运行开关：key=auto_enabled 总闸（off=全部落人工），"
                "key=audit_only 观察期（on=只生成草稿不自动发送）。"
            ),
            args_schema=BossAutoConfigRequest,
        ),
        StructuredTool.from_function(
            coroutine=boss_daily_digest,
            name="boss_daily_digest",
            description="生成今日 Boss HR 沟通日报摘要（新消息/草稿/发送/队列）。",
        ),
        StructuredTool.from_function(
            coroutine=boss_backfill_cold_start,
            name="boss_backfill_cold_start",
            description=(
                "冷启动回填：先从历史 HR 对话提取事实与公司洞察，"
                "再报告仍缺失的核心事实（问卷只问缺口）。可重复执行。"
            ),
        ),
    ]
