"""Agent tools for sending a selected Boss resume after an HR reply."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.applier.boss_resume_delivery import BossResumeDelivery


class BossResumeDeliveryPrepareRequest(BaseModel):
    """One stable Boss conversation selected from ``list_boss_greetings``."""

    conversation_id: str = Field(
        min_length=1,
        description="Boss friendId from the selected chat conversation; never use HR name alone.",
    )


class BossResumeDeliverySendRequest(BaseModel):
    """The user-selected option from a fresh resume-delivery preparation."""

    delivery_id: str = Field(min_length=1, description="Short-lived delivery ID from preparation.")
    resume_option_id: str = Field(min_length=1, description="Selected resume option ID.")
    resume_file_name: str = Field(
        min_length=1,
        description="Exact selected Boss resume filename; displayed in the approval prompt.",
    )
    hr_name: str = Field(min_length=1, description="HR name shown in the approval prompt.")
    company: str = Field(min_length=1, description="Company shown in the approval prompt.")
    job_title: str = Field(min_length=1, description="Job title shown in the approval prompt.")


def build_prepare_boss_resume_after_hr_reply_tool(
    manager: BossResumeDelivery,
) -> BaseTool:
    """List current sendable Boss resumes without sending one."""

    async def prepare_boss_resume_after_hr_reply(conversation_id: str) -> dict[str, Any]:
        """Check one HR reply and list the resumes the user can send."""

        return await manager.prepare(conversation_id)

    return StructuredTool.from_function(
        coroutine=prepare_boss_resume_after_hr_reply,
        name="prepare_boss_resume_after_hr_reply",
        description=(
            "Read-only preflight for proactively sending a Boss resume after an HR reply. "
            "Use a selected stable conversation_id from list_boss_greetings, not an HR name. "
            "Returns HR/job context and sendable resume filenames. It never sends a resume."
        ),
        args_schema=BossResumeDeliveryPrepareRequest,
    )


def build_send_boss_resume_after_hr_reply_tool(manager: BossResumeDelivery) -> BaseTool:
    """Build the HITL-gated external resume delivery tool."""

    async def send_boss_resume_after_hr_reply(
        delivery_id: str,
        resume_option_id: str,
        resume_file_name: str,
        hr_name: str,
        company: str,
        job_title: str,
    ) -> dict[str, Any]:
        """Send the exact user-selected Boss resume to the prepared HR conversation."""

        return await manager.send(
            delivery_id=delivery_id,
            resume_option_id=resume_option_id,
            resume_file_name=resume_file_name,
            hr_name=hr_name,
            company=company,
            job_title=job_title,
        )

    return StructuredTool.from_function(
        coroutine=send_boss_resume_after_hr_reply,
        name="send_boss_resume_after_hr_reply",
        description=(
            "Send one specifically selected Boss online/attachment resume after an HR replied. "
            "Call prepare_boss_resume_after_hr_reply first, show the exact resume filename to "
            "the user, then call this tool. Execution pauses for explicit human approval."
        ),
        args_schema=BossResumeDeliverySendRequest,
    )
