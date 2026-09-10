"""Shared application service for explicitly approved Journey creation."""

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jobagent.journey.store import OpportunityJourney, SQLiteJourneyStore


class CreateJourneyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    job_description: str = Field(default="", max_length=100000)
    source_job_id: str = Field(default="", max_length=300)
    department: str = Field(default="", max_length=200)
    recruiting_cycle: str = Field(default="", max_length=100)

    @field_validator("company", "role", "source_job_id", "department", "recruiting_cycle", "job_description")
    @classmethod
    def strip_text(cls, value: str, info) -> str:
        value = value.strip()
        if info.field_name in {"company", "role"} and not value:
            raise ValueError("company and role must not be blank")
        return value


def create_journey(path: Path, request: CreateJourneyRequest) -> tuple[OpportunityJourney, bool]:
    """Called only after UI confirmation or the CLI's framework HITL gate.

    No network side effects and no fabricated JD/stage. Source IDs are authoritative;
    without an ID only the exact company/role/department/cycle tuple is deduplicated.
    """
    key = "source:" + request.source_job_id if request.source_job_id else "fields:" + hashlib.sha256(
        json.dumps([request.company.casefold(), request.role.casefold(),
                    request.department.casefold(), request.recruiting_cycle.casefold()],
                   ensure_ascii=False).encode()
    ).hexdigest()
    with SQLiteJourneyStore(path) as store:
        return store.create_once(creation_key=key, **request.model_dump(exclude={"source_job_id"}))


def find_journey_by_source_id(path: Path, source_job_id: str) -> OpportunityJourney | None:
    if not source_job_id.strip():
        return None
    with SQLiteJourneyStore(path) as store:
        return store.get_by_creation_key("source:" + source_job_id.strip())
