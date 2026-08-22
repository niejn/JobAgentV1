"""SQLite state store for Opportunity Journeys, Task Runs and Artifacts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid4


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    VALIDATING = "validating"
    WAITING_INPUT = "waiting_input"
    REVISION_REQUIRED = "revision_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactStatus(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class OpportunityJourney:
    id: str
    company: str
    role: str
    job_description: str
    stage: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskRun:
    id: str
    journey_id: str
    task_type: str
    status: TaskStatus
    attempt: int
    input_artifact_ids: tuple[str, ...]
    output_artifact_ids: tuple[str, ...]
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class JourneyArtifact:
    id: str
    journey_id: str
    task_run_id: str
    artifact_type: str
    schema_version: str
    version: int
    status: ArtifactStatus
    content_ref: str
    content_hash: str
    provenance_refs: tuple[str, ...]
    validation_errors: tuple[str, ...]
    created_at: datetime


class SQLiteJourneyStore:
    """Persist workflow state behind a small synchronous interface."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteJourneyStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_journey(
        self,
        *,
        company: str,
        role: str,
        job_description: str,
    ) -> OpportunityJourney:
        now = _now()
        journey = OpportunityJourney(
            id=str(uuid4()),
            company=company.strip(),
            role=role.strip(),
            job_description=job_description.strip(),
            stage="targeted",
            version=1,
            created_at=now,
            updated_at=now,
        )
        if not journey.company or not journey.role:
            raise ValueError("company and role are required")
        with self._connection:
            self._connection.execute(
                """INSERT INTO journeys
                (id, company, role, job_description, stage, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    journey.id,
                    journey.company,
                    journey.role,
                    journey.job_description,
                    journey.stage,
                    journey.version,
                    _format_time(now),
                    _format_time(now),
                ),
            )
        return journey

    def get_journey(self, journey_id: str) -> OpportunityJourney:
        row = self._one("SELECT * FROM journeys WHERE id = ?", (journey_id,))
        return _journey_from_row(row)

    def start_task(
        self,
        journey_id: str,
        task_type: str,
        *,
        input_artifact_ids: tuple[str, ...] = (),
    ) -> TaskRun:
        self.get_journey(journey_id)
        now = _now()
        task = TaskRun(
            id=str(uuid4()),
            journey_id=journey_id,
            task_type=task_type,
            status=TaskStatus.RUNNING,
            attempt=1,
            input_artifact_ids=input_artifact_ids,
            output_artifact_ids=(),
            error=None,
            created_at=now,
            updated_at=now,
        )
        with self._connection:
            self._connection.execute(
                """INSERT INTO task_runs
                (id, journey_id, task_type, status, attempt, input_artifact_ids,
                 output_artifact_ids, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.id,
                    task.journey_id,
                    task.task_type,
                    task.status.value,
                    task.attempt,
                    _json(task.input_artifact_ids),
                    _json(task.output_artifact_ids),
                    task.error,
                    _format_time(now),
                    _format_time(now),
                ),
            )
        return task

    def get_task(self, task_id: str) -> TaskRun:
        row = self._one("SELECT * FROM task_runs WHERE id = ?", (task_id,))
        return _task_from_row(row)

    def create_artifact(
        self,
        *,
        journey_id: str,
        task_run_id: str,
        artifact_type: str,
        content_ref: str,
        content_hash: str,
        provenance_refs: list[str],
        schema_version: str = "1",
    ) -> JourneyArtifact:
        task = self.get_task(task_run_id)
        if task.journey_id != journey_id:
            raise ValueError("task and artifact must belong to the same journey")
        now = _now()
        row = self._connection.execute(
            """SELECT COALESCE(MAX(version), 0) AS current
            FROM artifacts WHERE journey_id = ? AND artifact_type = ?""",
            (journey_id, artifact_type),
        ).fetchone()
        version = int(row["current"]) + 1
        artifact = JourneyArtifact(
            id=str(uuid4()),
            journey_id=journey_id,
            task_run_id=task_run_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            version=version,
            status=ArtifactStatus.DRAFT,
            content_ref=content_ref,
            content_hash=content_hash,
            provenance_refs=tuple(provenance_refs),
            validation_errors=(),
            created_at=now,
        )
        with self._connection:
            self._connection.execute(
                """INSERT INTO artifacts
                (id, journey_id, task_run_id, artifact_type, schema_version, version,
                 status, content_ref, content_hash, provenance_refs, validation_errors, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact.id,
                    artifact.journey_id,
                    artifact.task_run_id,
                    artifact.artifact_type,
                    artifact.schema_version,
                    artifact.version,
                    artifact.status.value,
                    artifact.content_ref,
                    artifact.content_hash,
                    _json(artifact.provenance_refs),
                    _json(artifact.validation_errors),
                    _format_time(now),
                ),
            )
        return artifact

    def get_artifact(self, artifact_id: str) -> JourneyArtifact:
        row = self._one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        return _artifact_from_row(row)

    def validate_artifact(
        self,
        artifact_id: str,
        *,
        valid: bool,
        errors: tuple[str, ...] = (),
    ) -> JourneyArtifact:
        status = ArtifactStatus.ACCEPTED if valid else ArtifactStatus.REJECTED
        with self._connection:
            result = self._connection.execute(
                "UPDATE artifacts SET status = ?, validation_errors = ? WHERE id = ?",
                (status.value, _json(errors), artifact_id),
            )
        if result.rowcount != 1:
            raise KeyError(f"artifact not found: {artifact_id}")
        return self.get_artifact(artifact_id)

    def complete_task(self, task_id: str, output_artifact_ids: list[str]) -> TaskRun:
        self.get_task(task_id)
        artifacts = [self.get_artifact(item) for item in output_artifact_ids]
        if any(item.task_run_id != task_id for item in artifacts):
            raise ValueError("output artifacts must be created by the task")
        if not artifacts or any(item.status is not ArtifactStatus.ACCEPTED for item in artifacts):
            raise ValueError("task completion requires accepted artifacts")
        now = _now()
        with self._connection:
            self._connection.execute(
                """UPDATE task_runs SET status = ?, output_artifact_ids = ?, updated_at = ?
                WHERE id = ?""",
                (
                    TaskStatus.SUCCEEDED.value,
                    _json(output_artifact_ids),
                    _format_time(now),
                    task_id,
                ),
            )
        return self.get_task(task_id)

    def fail_task(self, task_id: str, error: str) -> TaskRun:
        now = _now()
        with self._connection:
            result = self._connection.execute(
                "UPDATE task_runs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.FAILED.value, error, _format_time(now), task_id),
            )
        if result.rowcount != 1:
            raise KeyError(f"task not found: {task_id}")
        return self.get_task(task_id)

    def _one(self, sql: str, parameters: tuple[object, ...]) -> sqlite3.Row:
        row = self._connection.execute(sql, parameters).fetchone()
        if row is None:
            raise KeyError(f"record not found for query: {sql}")
        return cast(sqlite3.Row, row)

    def _migrate(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS journeys (
                    id TEXT PRIMARY KEY,
                    company TEXT NOT NULL,
                    role TEXT NOT NULL,
                    job_description TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id),
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    input_artifact_ids TEXT NOT NULL,
                    output_artifact_ids TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id),
                    task_run_id TEXT NOT NULL REFERENCES task_runs(id),
                    artifact_type TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    content_ref TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    provenance_refs TEXT NOT NULL,
                    validation_errors TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_journey ON task_runs(journey_id);
                CREATE INDEX IF NOT EXISTS idx_artifact_journey ON artifacts(journey_id);
                """
            )


def _now() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    return value.isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _tuple_json(value: str) -> tuple[str, ...]:
    return tuple(json.loads(value))


def _journey_from_row(row: sqlite3.Row) -> OpportunityJourney:
    return OpportunityJourney(
        id=row["id"],
        company=row["company"],
        role=row["role"],
        job_description=row["job_description"],
        stage=row["stage"],
        version=row["version"],
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _task_from_row(row: sqlite3.Row) -> TaskRun:
    return TaskRun(
        id=row["id"],
        journey_id=row["journey_id"],
        task_type=row["task_type"],
        status=TaskStatus(row["status"]),
        attempt=row["attempt"],
        input_artifact_ids=_tuple_json(row["input_artifact_ids"]),
        output_artifact_ids=_tuple_json(row["output_artifact_ids"]),
        error=row["error"],
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _artifact_from_row(row: sqlite3.Row) -> JourneyArtifact:
    return JourneyArtifact(
        id=row["id"],
        journey_id=row["journey_id"],
        task_run_id=row["task_run_id"],
        artifact_type=row["artifact_type"],
        schema_version=row["schema_version"],
        version=row["version"],
        status=ArtifactStatus(row["status"]),
        content_ref=row["content_ref"],
        content_hash=row["content_hash"],
        provenance_refs=_tuple_json(row["provenance_refs"]),
        validation_errors=_tuple_json(row["validation_errors"]),
        created_at=_parse_time(row["created_at"]),
    )
