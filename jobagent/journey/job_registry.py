"""SQLite registry tracking every discovered job and its interview journey.

The registry is keyed by the stable platform job identity (e.g. ``boss:<encryptJobId>``)
so the same posting seen across multiple discovery runs, weeks and even slightly
different JD versions resolves to one record. It answers three questions the
JobAgent needs on every run:

1. Have we seen/greeted/interviewed at this job already? (dedup - never
   re-recommend an already-greeted job, and never greet twice)
2. Which discovered jobs were never recommended yet? (coverage - nothing slips)
3. What happened on each job's interview journey so far? (long-running status
   tracking: greeted -> hr_replied -> interview_scheduled -> offer/rejected)

Discovery and greeting tools update the registry deterministically; the Agent
only records human-observable transitions (HR replied, interview scheduled...)
through ``update_job_progress``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from jobagent.journey.store import _enable_wal


class JobProgressStatus(StrEnum):
    """Coarse-grained progress of one job's application journey."""

    DISCOVERED = "discovered"
    RECOMMENDED = "recommended"
    GREETED = "greeted"
    HR_REPLIED = "hr_replied"
    NO_RESPONSE = "no_response"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    CLOSED = "closed"


#: Allowed forward transitions. ``closed`` can be reached from any state and
#: a closed record can be reopened when reality changes (e.g. HR replies
#: months later). Invalid transitions raise so silent regressions surface.
_ALLOWED_TRANSITIONS: dict[JobProgressStatus, frozenset[JobProgressStatus]] = {
    JobProgressStatus.DISCOVERED: frozenset(
        {JobProgressStatus.RECOMMENDED, JobProgressStatus.GREETED, JobProgressStatus.CLOSED}
    ),
    JobProgressStatus.RECOMMENDED: frozenset(
        {JobProgressStatus.GREETED, JobProgressStatus.CLOSED}
    ),
    JobProgressStatus.GREETED: frozenset(
        {
            JobProgressStatus.HR_REPLIED,
            JobProgressStatus.NO_RESPONSE,
            JobProgressStatus.CLOSED,
        }
    ),
    JobProgressStatus.NO_RESPONSE: frozenset(
        {JobProgressStatus.HR_REPLIED, JobProgressStatus.CLOSED}
    ),
    JobProgressStatus.HR_REPLIED: frozenset(
        {
            JobProgressStatus.INTERVIEWING,
            JobProgressStatus.REJECTED,
            JobProgressStatus.CLOSED,
        }
    ),
    JobProgressStatus.INTERVIEWING: frozenset(
        {JobProgressStatus.OFFER, JobProgressStatus.REJECTED, JobProgressStatus.CLOSED}
    ),
    JobProgressStatus.OFFER: frozenset({JobProgressStatus.CLOSED}),
    JobProgressStatus.REJECTED: frozenset({JobProgressStatus.CLOSED}),
    JobProgressStatus.CLOSED: frozenset(
        {
            # Reopen paths for late replies and user changes of mind.
            JobProgressStatus.HR_REPLIED,
            JobProgressStatus.INTERVIEWING,
            JobProgressStatus.GREETED,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One job's current journey snapshot."""

    job_id: str
    source: str
    company: str
    title: str
    location: str
    url: str
    status: JobProgressStatus
    first_seen_at: datetime
    last_seen_at: datetime
    greeted_at: datetime | None
    note: str


@dataclass(frozen=True, slots=True)
class JobStatusEvent:
    """One recorded status transition."""

    job_id: str
    status: JobProgressStatus
    note: str
    created_at: datetime


class JobTransitionError(ValueError):
    """Raised when a status update violates the journey state machine."""


class SQLiteJobRegistry:
    """Persist job journey state behind a small synchronous interface."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        # busy_timeout MUST precede the WAL switch (see journey/store.py);
        # _enable_wal tolerates concurrent-initializer races.
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteJobRegistry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- discovery -----------------------------------------------------------

    def upsert_discovered(
        self,
        *,
        job_id: str,
        source: str,
        company: str,
        title: str,
        location: str = "",
        url: str = "",
    ) -> JobRecord | None:
        """Record a job seen by discovery; return ``None`` when already known.

        A brand-new job is inserted with status ``discovered``. A known job
        only gets ``last_seen_at`` refreshed; its journey status never
        regresses here. ``None`` for known jobs lets callers distinguish
        "new this run" from "already tracked" without a second query.
        """

        job_id = job_id.strip()
        if not job_id:
            raise ValueError("job_id is required")
        now = self._clock()
        existing = self.get(job_id)
        if existing is None:
            try:
                # The insert path is race-safe: a concurrent discovery of the
                # same new job loses the PRIMARY KEY race, falls through to
                # the update branch below instead of crashing the caller.
                with self._connection:
                    self._connection.execute(
                        """INSERT INTO job_records
                           (job_id, source, company, title, location, url, status,
                            first_seen_at, last_seen_at, greeted_at, note)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '')""",
                        (
                            job_id,
                            source.strip(),
                            company.strip(),
                            title.strip(),
                            location.strip(),
                            url.strip(),
                            JobProgressStatus.DISCOVERED.value,
                            _format_time(now),
                            _format_time(now),
                        ),
                    )
                    self._connection.execute(
                        """INSERT INTO job_status_events (job_id, status, note, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (job_id, JobProgressStatus.DISCOVERED.value, "", _format_time(now)),
                    )
                record = self.get(job_id)
                assert record is not None
                return record
            except sqlite3.IntegrityError:
                existing = self.get(job_id)
                if existing is None:
                    raise
        self._connection.execute(
            "UPDATE job_records SET last_seen_at = ?, company = ?, title = ?, "
            "location = ?, url = ? WHERE job_id = ?",
            (
                _format_time(now),
                company.strip() or existing.company,
                title.strip() or existing.title,
                location or existing.location,
                url or existing.url,
                job_id,
            ),
        )
        self._connection.commit()
        return None

    # -- journey updates -----------------------------------------------------

    def mark(
        self,
        job_id: str,
        status: JobProgressStatus,
        *,
        note: str = "",
    ) -> JobRecord:
        """Move one job to ``status``, validating the transition.

        Marking ``greeted`` stamps ``greeted_at``; re-marking the current
        status is a no-op so greeting tools stay idempotent.
        """

        record = self.get(job_id)
        if record is None:
            raise KeyError(f"job not found in registry: {job_id}")
        if record.status is status:
            return record
        allowed = _ALLOWED_TRANSITIONS.get(record.status, frozenset())
        if status not in allowed:
            raise JobTransitionError(
                f"illegal transition {record.status.value} -> {status.value} "
                f"for job {job_id}"
            )
        now = self._clock()
        greeted_at = record.greeted_at
        if status is JobProgressStatus.GREETED:
            greeted_at = now
        # Guarded update: the WHERE clause re-checks the status the
        # transition was validated against. If a concurrent mark() moved the
        # job first, rowcount is 0 and no event is appended — two concurrent
        # writers can no longer record impossible divergent histories.
        cursor = self._connection.execute(
            "UPDATE job_records SET status = ?, greeted_at = ?, note = ?, "
            "last_seen_at = ? WHERE job_id = ? AND status = ?",
            (
                status.value,
                _format_time(greeted_at) if greeted_at else None,
                note.strip() or record.note,
                _format_time(now),
                job_id,
                record.status.value,
            ),
        )
        if cursor.rowcount != 1:
            current = self.get(job_id)
            if current is None:
                raise KeyError(f"job not found in registry: {job_id}")
            raise JobTransitionError(
                f"concurrent update moved job {job_id} to "
                f"{current.status.value} before this {status.value} transition"
            )
        self._connection.execute(
            """INSERT INTO job_status_events (job_id, status, note, created_at)
               VALUES (?, ?, ?, ?)""",
            (job_id, status.value, note.strip(), _format_time(now)),
        )
        self._connection.commit()
        updated = self.get(job_id)
        assert updated is not None
        return updated

    def get(self, job_id: str) -> JobRecord | None:
        row = self._connection.execute(
            "SELECT * FROM job_records WHERE job_id = ?", (job_id.strip(),)
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def history(self, job_id: str) -> tuple[JobStatusEvent, ...]:
        rows = self._connection.execute(
            "SELECT * FROM job_status_events WHERE job_id = ? ORDER BY id",
            (job_id.strip(),),
        ).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def list_records(
        self,
        *,
        status: JobProgressStatus | None = None,
        company: str | None = None,
        limit: int = 100,
    ) -> tuple[JobRecord, ...]:
        """List records newest-activity first; ``company`` matches by substring."""

        query = "SELECT * FROM job_records"
        conditions: list[str] = []
        parameters: list[object] = []
        if status is not None:
            conditions.append("status = ?")
            parameters.append(status.value)
        if company:
            conditions.append("company LIKE ?")
            parameters.append(f"%{company.strip()}%")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY last_seen_at DESC LIMIT ?"
        parameters.append(max(1, limit))
        rows = self._connection.execute(query, parameters).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    # -- schema --------------------------------------------------------------

    def _migrate(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS job_records (
                job_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                company TEXT NOT NULL,
                title TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                greeted_at TEXT,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS job_status_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES job_records(job_id),
                status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_job_status_events_job_id
                ON job_status_events(job_id);
            CREATE INDEX IF NOT EXISTS idx_job_records_status
                ON job_records(status);
            """
        )
        self._connection.commit()


def _record_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=str(row["job_id"]),
        source=str(row["source"]),
        company=str(row["company"]),
        title=str(row["title"]),
        location=str(row["location"]),
        url=str(row["url"]),
        status=JobProgressStatus(str(row["status"])),
        first_seen_at=_parse_time(str(row["first_seen_at"])),
        last_seen_at=_parse_time(str(row["last_seen_at"])),
        greeted_at=(
            _parse_time(str(row["greeted_at"])) if row["greeted_at"] else None
        ),
        note=str(row["note"] or ""),
    )


def _event_from_row(row: sqlite3.Row) -> JobStatusEvent:
    return JobStatusEvent(
        job_id=str(row["job_id"]),
        status=JobProgressStatus(str(row["status"])),
        note=str(row["note"] or ""),
        created_at=_parse_time(str(row["created_at"])),
    )


def _format_time(value: datetime) -> str:
    return value.isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)
