"""SQLite state store for Opportunity Journeys, Task Runs and Artifacts."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid4

from jobagent.journey.identity import identity_key


def _enable_wal(connection: sqlite3.Connection) -> None:
    """Switch the connection's database to WAL, tolerating init races.

    The journal-mode change needs a brief exclusive lock and the busy
    handler does not apply to it, so several connections initializing at
    once can each see SQLITE_BUSY. Retry briefly; if another connection
    already owns the mode the pragma is a harmless no-op, and if it stays
    busy we proceed in the current mode rather than crashing the caller.
    """

    for attempt in range(5):
        try:
            row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if row is not None and str(row[0]).lower() == "wal":
                return
        except sqlite3.OperationalError:
            if attempt == 4:
                return
            time.sleep(0.05 * (attempt + 1))

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
    department: str = ""
    recruiting_cycle: str = ""
    deleted_at: str | None = None


@dataclass(frozen=True, slots=True)
class JourneyJobDescriptionVersion:
    """One distinct JD snapshot attached to an opportunity journey."""

    id: str
    journey_id: str
    content: str
    content_hash: str
    created_at: datetime


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
        # busy_timeout MUST precede the WAL switch: changing journal mode
        # needs a brief exclusive lock, and without a busy timeout a
        # concurrent connection makes it fail instantly with SQLITE_BUSY.
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        # Concurrent initializers race the WAL switch and the busy handler
        # does not apply to journal-mode changes; retry briefly and proceed
        # even if another connection already owns the mode.
        _enable_wal(self._connection)
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
        department: str = "",
        recruiting_cycle: str = "",
    ) -> OpportunityJourney:
        company = company.strip()
        role = role.strip()
        job_description = job_description.strip()
        department = department.strip()
        recruiting_cycle = recruiting_cycle.strip()
        if not company or not role:
            raise ValueError("company and role are required")
        canonical_identity = (
            identity_key(company, role, department=department, recruiting_cycle=recruiting_cycle)
            if department and recruiting_cycle else ""
        )
        existing = (
            self._connection.execute(
                "SELECT id FROM journeys WHERE identity_key = ? LIMIT 1",
                (canonical_identity,),
            ).fetchone()
            if canonical_identity else None
        )
        if existing is not None:
            journey = self.get_journey(str(existing["id"]))
            self._record_job_description_version(journey.id, job_description)
            now = _now()
            with self._connection:
                self._connection.execute(
                    "UPDATE journeys SET version = version + 1, updated_at = ? WHERE id = ?",
                    (_format_time(now), journey.id),
                )
            return self.get_journey(journey.id)

        now = _now()
        journey = OpportunityJourney(
            id=str(uuid4()),
            company=company,
            role=role,
            job_description=job_description,
            stage="targeted",
            version=1,
            created_at=now,
            updated_at=now,
        )
        with self._connection:
            self._connection.execute(
                """INSERT INTO journeys
                (id, company, role, job_description, stage, version, identity_key,
                 department, recruiting_cycle, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    journey.id,
                    journey.company,
                    journey.role,
                    journey.job_description,
                    journey.stage,
                    journey.version,
                    canonical_identity,
                    department,
                    recruiting_cycle,
                    _format_time(now),
                    _format_time(now),
                ),
            )
            self._record_job_description_version(journey.id, job_description)
        return journey

    def create_once(
        self, *, creation_key: str, company: str, role: str, job_description: str = "",
        department: str = "", recruiting_cycle: str = "",
    ) -> tuple[OpportunityJourney, bool]:
        """Atomically resolve a creation key and persist a Journey plus its JD."""
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT journey_id FROM journey_creation_keys WHERE creation_key = ?",
                (creation_key,),
            ).fetchone()
            if existing:
                return self.get_journey(str(existing["journey_id"])), False
            journey_id = str(uuid4())
            now = _format_time(_now())
            self._connection.execute(
                """INSERT INTO journeys
                (id, company, role, job_description, stage, version, identity_key,
                 department, recruiting_cycle, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'targeted', 1, '', ?, ?, ?, ?)""",
                (journey_id, company, role, job_description, department, recruiting_cycle, now, now),
            )
            self._connection.execute(
                "INSERT INTO journey_creation_keys (creation_key, journey_id) VALUES (?, ?)",
                (creation_key, journey_id),
            )
            self._record_job_description_version(journey_id, job_description)
        return self.get_journey(journey_id), True

    def get_journey(self, journey_id: str) -> OpportunityJourney:
        row = self._one("SELECT * FROM journeys WHERE id = ?", (journey_id,))
        return _journey_from_row(row)

    def list_journeys(self, *, limit: int = 100, offset: int = 0,
                      include_deleted: bool = False, company: str = "") -> tuple[OpportunityJourney, ...]:
        """Return the most recently updated opportunity journeys."""

        rows = self._connection.execute(
            """SELECT * FROM journeys WHERE (? OR deleted_at IS NULL)
            AND instr(lower(company), lower(?)) > 0
            ORDER BY updated_at DESC, id LIMIT ? OFFSET ?""",
            (include_deleted, company, max(1, min(limit, 500)), max(0, offset)),
        ).fetchall()
        return tuple(_journey_from_row(row) for row in rows)

    def manage_journey(self, journey_id: str, *, expected_version: int,
                       expected_company: str, expected_role: str,
                       action: str, reason: str, changes: dict[str, str]) -> OpportunityJourney:
        """Version-checked, audited update/soft-delete/restore; never deletes artifacts."""
        allowed = {"company", "role", "department", "recruiting_cycle", "job_description"}
        if action not in {"update", "delete", "restore"} or set(changes) - allowed:
            raise ValueError("invalid Journey operation")
        if not reason.strip():
            raise ValueError("reason is required")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            journey = self.get_journey(journey_id)
            if (journey.version != expected_version or journey.company != expected_company
                    or journey.role != expected_role):
                raise ValueError("Journey 已变化，请重新查询详情并重新审批")
            if action == "restore" and not journey.deleted_at:
                raise ValueError("Journey 未删除，无需恢复")
            if action != "restore" and journey.deleted_at:
                raise ValueError("Journey 已删除，请先恢复")
            if action in {"delete", "update"}:
                running = self._connection.execute(
                    "SELECT 1 FROM task_runs WHERE journey_id = ? AND status IN ('running','validating')",
                    (journey_id,),
                ).fetchone()
                if running:
                    raise ValueError("Journey 有运行中的任务，结束后再修改或删除")
            now = _format_time(_now())
            fields: dict[str, object] = dict(changes) if action == "update" else {
                "deleted_at": now if action == "delete" else None,
            }
            if action == "update" and not changes:
                raise ValueError("请至少提供一个修改字段")
            fields.update(version=journey.version + 1, updated_at=now)
            if action == "update":
                fields["identity_key"] = ""  # Legacy identity must not refer to old labels.
            assignments = ", ".join(f"{name} = ?" for name in fields)
            self._connection.execute(
                f"UPDATE journeys SET {assignments} WHERE id = ?", (*fields.values(), journey_id),
            )
            self._connection.execute(
                """INSERT INTO journey_change_events
                (journey_id, action, reason, before_json, after_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (journey_id, action, reason, _json({
                    "version": journey.version, "company": journey.company, "role": journey.role,
                    "department": journey.department, "recruiting_cycle": journey.recruiting_cycle,
                    "job_description": journey.job_description, "deleted_at": journey.deleted_at,
                }), _json(fields), now),
            )
            if "job_description" in changes:
                self._record_job_description_version(journey_id, changes["job_description"])
        return self.get_journey(journey_id)

    def journey_counts(self, journey_id: str) -> dict[str, int]:
        return {name: int(self._connection.execute(
            f"SELECT count(*) FROM {table} WHERE journey_id = ?", (journey_id,),
        ).fetchone()[0]) for name, table in {
            "task_count": "task_runs", "artifact_count": "artifacts",
            "jd_version_count": "journey_job_description_versions",
        }.items()}

    def journey_change_history(self, journey_id: str) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM journey_change_events WHERE journey_id = ? ORDER BY id DESC LIMIT 100",
            (journey_id,),
        ).fetchall()
        return [{**dict(row), "before": json.loads(row["before_json"]),
                 "after": json.loads(row["after_json"])} for row in rows]

    def list_tasks(self, journey_id: str, *, limit: int = 100) -> tuple[TaskRun, ...]:
        """Return task runs belonging to one journey."""

        self.get_journey(journey_id)
        rows = self._connection.execute(
            "SELECT * FROM task_runs WHERE journey_id = ? ORDER BY updated_at DESC LIMIT ?",
            (journey_id, max(1, min(limit, 500))),
        ).fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def list_artifacts(self, journey_id: str, *, limit: int = 100) -> tuple[JourneyArtifact, ...]:
        """Return artifacts belonging to one journey."""

        self.get_journey(journey_id)
        rows = self._connection.execute(
            "SELECT * FROM artifacts WHERE journey_id = ? ORDER BY created_at DESC LIMIT ?",
            (journey_id, max(1, min(limit, 500))),
        ).fetchall()
        return tuple(_artifact_from_row(row) for row in rows)

    def list_job_description_versions(
        self, journey_id: str, *, limit: int = 100
    ) -> tuple[JourneyJobDescriptionVersion, ...]:
        """Return distinct JD snapshots for one Journey, newest first."""

        self.get_journey(journey_id)
        rows = self._connection.execute(
            """SELECT * FROM journey_job_description_versions
            WHERE journey_id = ? ORDER BY created_at DESC LIMIT ?""",
            (journey_id, max(1, min(limit, 500))),
        ).fetchall()
        return tuple(_jd_version_from_row(row) for row in rows)

    def _record_job_description_version(self, journey_id: str, content: str) -> None:
        if not content:
            return
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._connection:
            self._connection.execute(
                """INSERT OR IGNORE INTO journey_job_description_versions
                (id, journey_id, content, content_hash, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (str(uuid4()), journey_id, content, content_hash, _format_time(_now())),
            )

    def start_task(
        self,
        journey_id: str,
        task_type: str,
        *,
        input_artifact_ids: tuple[str, ...] = (),
    ) -> TaskRun:
        if self.get_journey(journey_id).deleted_at:
            raise ValueError("Journey 已删除，请先恢复再启动任务")
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
        artifact_id = str(uuid4())
        # Version assignment and insert are ONE statement: the max-version
        # subquery and the insert are atomic, so concurrent writers cannot
        # both compute the same version (code review HIGH H3).
        with self._connection:
            self._connection.execute(
                """INSERT INTO artifacts
                (id, journey_id, task_run_id, artifact_type, schema_version, version,
                 status, content_ref, content_hash, provenance_refs, validation_errors, created_at)
                VALUES (?, ?, ?, ?, ?,
                        (SELECT COALESCE(MAX(version), 0) + 1 FROM artifacts
                         WHERE journey_id = ? AND artifact_type = ?),
                        ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    journey_id,
                    task_run_id,
                    artifact_type,
                    schema_version,
                    journey_id,
                    artifact_type,
                    ArtifactStatus.DRAFT.value,
                    content_ref,
                    content_hash,
                    _json(tuple(provenance_refs)),
                    _json(()),
                    _format_time(now),
                ),
            )
        return self.get_artifact(artifact_id)

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
                    identity_key TEXT NOT NULL DEFAULT '',
                    department TEXT NOT NULL DEFAULT '',
                    recruiting_cycle TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journey_creation_keys (
                    creation_key TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id)
                );
                CREATE TABLE IF NOT EXISTS journey_change_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    journey_id TEXT NOT NULL REFERENCES journeys(id),
                    action TEXT NOT NULL, reason TEXT NOT NULL,
                    before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journey_job_description_versions (
                    id TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id),
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(journey_id, content_hash)
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
            columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(journeys)")
            }
            if "identity_key" not in columns:
                self._connection.execute(
                    "ALTER TABLE journeys ADD COLUMN identity_key TEXT NOT NULL DEFAULT ''"
                )
            if "deleted_at" not in columns:
                self._connection.execute("ALTER TABLE journeys ADD COLUMN deleted_at TEXT")
            columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(journeys)")
            }
            if "department" not in columns:
                self._connection.execute(
                    "ALTER TABLE journeys ADD COLUMN department TEXT NOT NULL DEFAULT ''"
                )
            if "recruiting_cycle" not in columns:
                self._connection.execute(
                    "ALTER TABLE journeys ADD COLUMN recruiting_cycle TEXT NOT NULL DEFAULT ''"
                )
            # Existing records were created before Journey identity included
            # department and recruiting cycle; never let them participate in
            # the stricter deduplication key implicitly.
            self._connection.execute(
                "UPDATE journeys SET identity_key = '' WHERE department = '' OR recruiting_cycle = ''"
            )
            rows = self._connection.execute(
                "SELECT id, company, role, job_description FROM journeys WHERE identity_key = ''"
            ).fetchall()
            for row in rows:
                self._connection.execute(
                    """INSERT OR IGNORE INTO journey_job_description_versions
                    (id, journey_id, content, content_hash, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        str(uuid4()), row["id"], row["job_description"],
                        hashlib.sha256(str(row["job_description"]).encode("utf-8")).hexdigest(),
                        _format_time(_now()),
                    ),
                )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_journey_identity ON journeys(identity_key)"
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
        department=str(row["department"] or ""),
        recruiting_cycle=str(row["recruiting_cycle"] or ""),
        deleted_at=row["deleted_at"],
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


def _jd_version_from_row(row: sqlite3.Row) -> JourneyJobDescriptionVersion:
    return JourneyJobDescriptionVersion(
        id=row["id"],
        journey_id=row["journey_id"],
        content=row["content"],
        content_hash=row["content_hash"],
        created_at=_parse_time(row["created_at"]),
    )
