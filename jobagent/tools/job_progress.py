"""Agent Tools for the job progress registry (interview journey tracking)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.journey.job_registry import (
    JobProgressStatus,
    JobTransitionError,
    SQLiteJobRegistry,
)

_STATUS_DESCRIPTION = (
    "Job registry status (not web Journey creation): "
    "discovered(已发现未推荐), recommended(已推荐), "
    "greeted(已打招呼), hr_replied(HR已回复), no_response(HR无回应), "
    "interviewing(已安排面试), offer(已拿offer), rejected(被拒), closed(已关闭)"
)


class UpdateJobProgressInput(BaseModel):
    job_id: str = Field(min_length=1, description="Stable job ID, e.g. boss:<encryptJobId>")
    status: JobProgressStatus
    note: str = Field(
        default="",
        description="Short factual note, e.g. 'HR约了周三下午视频一面'",
    )


class ConfirmGreetingDeliveredInput(BaseModel):
    """Backfill greeted status for a delivery verified outside the greet tool."""

    job_id: str = Field(min_length=1, description="Stable job ID, e.g. boss:<encryptJobId>")
    hr_name: str = Field(
        min_length=1,
        description="HR name whose conversation history the delivery was verified against.",
    )
    sent_text: str = Field(
        default="",
        max_length=500,
        description="Snippet of the delivered greeting text used for the history match.",
    )
    company: str = Field(
        default="", description="Company name; only used to create a missing record"
    )
    title: str = Field(
        default="", description="Job title; only used to create a missing record"
    )
    url: str = Field(
        default="", description="Job URL; only used to create a missing record"
    )


class GetJobProgressInput(BaseModel):
    job_id: str = Field(min_length=1)


class ListJobRecordsInput(BaseModel):
    status: JobProgressStatus | None = Field(
        default=None,
        description="Filter by journey status; omit to list all",
    )
    company: str | None = Field(
        default=None,
        description="Substring match on company name",
    )
    limit: int = Field(default=50, ge=1, le=200)


def build_update_job_progress_tool(registry_path: Path) -> BaseTool:
    """Record one human-observable journey transition (HR replied, interview...)."""

    async def update_job_progress(
        job_id: str, status: JobProgressStatus, note: str = ""
    ) -> dict[str, Any]:
        """Record one job journey transition reported by the user or platform.

        Only call this for events that actually happened (HR replied, interview
        scheduled, rejected...); greetings are recorded automatically and must
        not be duplicated here.
        """
        try:
            with SQLiteJobRegistry(registry_path) as registry:
                record = registry.mark(job_id, status, note=note)
        except KeyError:
            return {
                "status": "not_found",
                "message": (
                    f"岗位 {job_id} 尚未在登记册中；它必须先通过岗位发现或打招呼进入登记册。"
                ),
            }
        except JobTransitionError as exc:
            return {
                "status": "invalid_transition",
                "message": str(exc),
                "hint": "先查看当前状态，再记录符合流程的下一步状态。",
            }
        return {
            "status": "completed",
            "job_id": record.job_id,
            "company": record.company,
            "title": record.title,
            "progress_status": record.status.value,
            "greeted_at": record.greeted_at.isoformat() if record.greeted_at else None,
            "note": record.note,
        }

    return StructuredTool.from_function(
        coroutine=update_job_progress,
        name="update_job_progress",
        description=(
            "Record one job's interview-journey transition (hr_replied, "
            "no_response, interviewing, offer, rejected, closed, recommended). "
            + _STATUS_DESCRIPTION
        ),
        args_schema=UpdateJobProgressInput,
    )


def build_confirm_greeting_delivered_tool(registry_path: Path) -> BaseTool:
    """Idempotently backfill greeted for deliveries verified via chat history.

    The greet tool records greeted on its own success path only. When a
    greeting was delivered but the tool reported unverified (WS ack loss) or
    the send bypassed the tool, the agent verifies delivery through the
    read-only conversation history and uses THIS tool — instead of hand-rolled
    scripts editing the registry directly.
    """

    async def confirm_greeting_delivered(
        job_id: str,
        hr_name: str,
        sent_text: str = "",
        company: str = "",
        title: str = "",
        url: str = "",
    ) -> dict[str, Any]:
        """Record a history-verified greeting delivery; safe to re-call."""

        note = f"delivery confirmed via chat history; hr={hr_name}"
        if sent_text:
            snippet = sent_text[:80].replace("\n", " ")
            note += f"; text~={snippet!r}"
        try:
            with SQLiteJobRegistry(registry_path) as registry:
                try:
                    record = registry.mark(job_id, JobProgressStatus.GREETED, note=note)
                except KeyError:
                    if not (company and title):
                        return {
                            "status": "not_found",
                            "message": (
                                f"岗位 {job_id} 不在登记册中；提供 company 和 title "
                                "可同时补建记录。"
                            ),
                        }
                    registry.upsert_discovered(
                        job_id=job_id, source="boss", company=company, title=title, url=url
                    )
                    record = registry.mark(job_id, JobProgressStatus.GREETED, note=note)
        except JobTransitionError as exc:
            return {
                "status": "invalid_transition",
                "message": str(exc),
                "hint": "该岗位状态已超过 greeted（如 hr_replied），无需补登记。",
            }
        return {
            "status": "completed",
            "job_id": record.job_id,
            "company": record.company,
            "title": record.title,
            "progress_status": record.status.value,
            "greeted_at": record.greeted_at.isoformat() if record.greeted_at else None,
            "note": record.note,
        }

    return StructuredTool.from_function(
        coroutine=confirm_greeting_delivered,
        name="confirm_greeting_delivered",
        description=(
            "Backfill greeted status for one job whose greeting was delivered but "
            "not auto-recorded (send reported unverified due to WS ack loss, or "
            "delivery was verified through read-only chat history afterwards). "
            "Verify with read_boss_conversation FIRST; pass the HR name you "
            "verified against. Idempotent — re-calling is safe. Never use this "
            "to fake a delivery you could not verify."
        ),
        args_schema=ConfirmGreetingDeliveredInput,
    )


def build_get_job_progress_tool(registry_path: Path) -> BaseTool:
    """Return one job's current status and full transition history."""

    async def get_job_progress(job_id: str) -> dict[str, Any]:
        """Look up one job's journey status and complete event history.

        Use before interview preparation to review what has happened on this
        job so far, and before status updates to check the current state.
        """
        with SQLiteJobRegistry(registry_path) as registry:
            record = registry.get(job_id)
            if record is None:
                return {
                    "status": "not_found",
                    "message": f"岗位 {job_id} 不在登记册中。",
                }
            events = registry.history(job_id)
        return {
            "status": "completed",
            "job_id": record.job_id,
            "company": record.company,
            "title": record.title,
            "location": record.location,
            "url": record.url,
            "progress_status": record.status.value,
            "first_seen_at": record.first_seen_at.isoformat(),
            "greeted_at": record.greeted_at.isoformat() if record.greeted_at else None,
            "note": record.note,
            "events": [
                {
                    "status": event.status.value,
                    "note": event.note,
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ],
        }

    return StructuredTool.from_function(
        coroutine=get_job_progress,
        name="get_job_progress",
        description=(
            "Look up one job's current journey status and its full transition "
            "history (discovered -> greeted -> hr_replied -> ...). Use it when "
            "preparing for interviews or reviewing what happened on a job."
        ),
        args_schema=GetJobProgressInput,
    )


def build_list_job_records_tool(registry_path: Path) -> BaseTool:
    """List tracked jobs by status/company for dedup checks and gap hunting."""

    async def list_job_records(
        status: JobProgressStatus | None = None,
        company: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List tracked jobs newest-activity first, optionally filtered.

        ``discovered``-status jobs were seen by discovery but never
        recommended yet - check them to avoid missing opportunities.
        """
        with SQLiteJobRegistry(registry_path) as registry:
            records = registry.list_records(
                status=status, company=company, limit=limit
            )
        return {
            "status": "completed",
            "count": len(records),
            "records": [
                {
                    "job_id": record.job_id,
                    "company": record.company,
                    "title": record.title,
                    "progress_status": record.status.value,
                    "greeted_at": (
                        record.greeted_at.isoformat() if record.greeted_at else None
                    ),
                    "last_seen_at": record.last_seen_at.isoformat(),
                    "note": record.note,
                }
                for record in records
            ],
        }

    return StructuredTool.from_function(
        coroutine=list_job_records,
        name="list_job_records",
        description=(
            "List all tracked jobs with journey status, filterable by status "
            "and company. Use it to review progress, find never-recommended "
            "(discovered) jobs, and avoid duplicate recommendations. "
            + _STATUS_DESCRIPTION
        ),
        args_schema=ListJobRecordsInput,
    )
