"""Business Tool for persisting JD analysis artifacts."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.artifacts import (
    ApplicationState,
    FitDecision,
    LocalOpportunityArtifacts,
)


class JobAnalysisArtifactInput(BaseModel):
    company: str = Field(min_length=1)
    role: str = Field(min_length=1)
    job_description: str = Field(min_length=1)
    analysis_report: str = Field(
        min_length=1,
        description="Complete Markdown JD matching and gap analysis report",
    )
    fit: FitDecision
    application_state: ApplicationState = ApplicationState.NOT_APPLIED


class ApplicationStateUpdateInput(BaseModel):
    opportunity_id: str = Field(min_length=1)
    application_state: ApplicationState


def build_save_job_analysis_tool(store: LocalOpportunityArtifacts) -> BaseTool:
    """Expose versioned local JD/report persistence as one high-level Tool."""

    async def save_job_analysis(
        company: str,
        role: str,
        job_description: str,
        analysis_report: str,
        fit: FitDecision,
        application_state: ApplicationState = ApplicationState.NOT_APPLIED,
    ) -> dict[str, Any]:
        """Persist one completed JD analysis before presenting it to the user.

        The report must distinguish confirmed evidence, JD-based inference, and missing
        candidate facts. Never mark a job applied unless an application actually occurred.
        """

        saved = store.save_analysis(
            company=company,
            role=role,
            job_description=job_description,
            analysis_report=analysis_report,
            fit=fit,
            application_state=application_state,
        )
        return {
            "status": "completed",
            "opportunity_id": saved.opportunity_id,
            "analysis_version": saved.analysis_version,
            "fit": saved.fit.value,
            "application_state": saved.application_state.value,
            "artifact_ref": (
                f"opportunity://local/{saved.opportunity_id}/"
                f"{saved.analysis_path.name}"
            ),
        }

    return StructuredTool.from_function(
        coroutine=save_job_analysis,
        name="save_job_analysis",
        description=(
            "Save the original JD, complete Markdown matching/gap analysis, fit decision, "
            "and application state to versioned local Opportunity artifacts. Call after "
            "completing a JD analysis and before giving the final answer."
        ),
        args_schema=JobAnalysisArtifactInput,
    )


def build_update_application_state_tool(store: LocalOpportunityArtifacts) -> BaseTool:
    """Expose controlled application-status updates for analyzed opportunities."""

    async def update_job_application_state(
        opportunity_id: str,
        application_state: ApplicationState,
    ) -> dict[str, Any]:
        """Update status only after a real application workflow changes state.

        Never infer an application from user interest, fit, a drafted resume, or an intended
        action. The opportunity must already exist in local artifacts.
        """

        try:
            updated = store.update_application_state(
                opportunity_id,
                application_state,
            )
        except (KeyError, ValueError):
            return {
                "status": "failed",
                "error_type": "unknown_opportunity",
                "message": "Application state was not changed.",
            }
        return {
            "status": "completed",
            "opportunity_id": updated.opportunity_id,
            "application_state": updated.application_state.value,
        }

    return StructuredTool.from_function(
        coroutine=update_job_application_state,
        name="update_job_application_state",
        description=(
            "Update applied/interviewing/closed state for an existing analyzed Opportunity. "
            "Call only after the real application workflow confirms the state change."
        ),
        args_schema=ApplicationStateUpdateInput,
    )
