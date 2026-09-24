"""Shared Journey queries and explicitly approved lifecycle operations."""

from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jobagent.journey.store import OpportunityJourney, SQLiteJourneyStore


class JourneyChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    company: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = Field(default=None, min_length=1, max_length=200)
    department: str | None = Field(default=None, max_length=200)
    recruiting_cycle: str | None = Field(default=None, max_length=100)
    job_description: str | None = Field(default=None, max_length=100000)

    @model_validator(mode="after")
    def nonempty_patch(self) -> "JourneyChanges":
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("Provide changed fields only; null is not a field value")
        return self


class JourneyTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    journey_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    expected_company: str = Field(min_length=1, max_length=200)
    expected_role: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


class JourneyUpdate(JourneyTarget):
    changes: JourneyChanges


def _summary(store: SQLiteJourneyStore, journey: OpportunityJourney) -> dict[str, Any]:
    return {**asdict(journey), **store.journey_counts(journey.id),
            "created_at": journey.created_at.isoformat(),
            "updated_at": journey.updated_at.isoformat()}


def list_journeys(
    path: Path, *, company: str = "", include_deleted: bool = False,
    limit: int = 50, offset: int = 0,
) -> list[dict[str, Any]]:
    with SQLiteJourneyStore(path) as store:
        return [_summary(store, journey) for journey in store.list_journeys(
            company=company, include_deleted=include_deleted, limit=limit, offset=offset)]


def get_journey(path: Path, journey_id: str) -> dict[str, Any]:
    with SQLiteJourneyStore(path) as store:
        journey = store.get_journey(journey_id)
        return {**_summary(store, journey),
                "change_history": store.journey_change_history(journey_id),
                "tasks": [asdict(item) for item in store.list_tasks(journey_id, limit=500)],
                "artifacts": [asdict(item) for item in store.list_artifacts(journey_id, limit=500)],
                "job_description_versions": [
                    asdict(item)
                    for item in store.list_job_description_versions(journey_id, limit=500)
                ]}


def change_journey(path: Path, action: str, request: JourneyTarget) -> dict[str, Any]:
    with SQLiteJourneyStore(path) as store:
        journey = store.manage_journey(
            **request.model_dump(exclude={"changes"}), action=action,
            changes=(
                request.changes.model_dump(exclude_unset=True)
                if isinstance(request, JourneyUpdate) else {}
            ),
        )
        return _summary(store, journey)
