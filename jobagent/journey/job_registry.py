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
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from jobagent.journey.identity import derive_business_line
from jobagent.journey.identity import identity_key as derive_identity_key
from jobagent.journey.store import _enable_wal


class JobProgressStatus(StrEnum):
    """Coarse-grained progress of one job's application journey."""

    DISCOVERED = "discovered"
    RECOMMENDED = "recommended"
    GREETED = "greeted"
    APPLIED = "applied"
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
        {
            JobProgressStatus.RECOMMENDED,
            JobProgressStatus.GREETED,
            JobProgressStatus.APPLIED,
            JobProgressStatus.CLOSED,
        }
    ),
    JobProgressStatus.RECOMMENDED: frozenset(
        {JobProgressStatus.GREETED, JobProgressStatus.APPLIED, JobProgressStatus.CLOSED}
    ),
    JobProgressStatus.GREETED: frozenset(
        {
            JobProgressStatus.HR_REPLIED,
            JobProgressStatus.NO_RESPONSE,
            JobProgressStatus.CLOSED,
        }
    ),
    JobProgressStatus.APPLIED: frozenset(
        {
            JobProgressStatus.HR_REPLIED,
            JobProgressStatus.NO_RESPONSE,
            JobProgressStatus.INTERVIEWING,
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
            JobProgressStatus.APPLIED,
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
            # F1-R4 L2: before inserting, resolve the deterministic identity.
            # An existing identity for the same normalized company+title+line
            # means this is a new posting of a job we already track (Boss
            # re-post with a fresh encryptJobId, or the same job surfaced on
            # another platform). The posting inherits the identity's current
            # status so cross-platform journeys never fork.
            key = derive_identity_key(company, title)
            identity_row = self._connection.execute(
                "SELECT status FROM job_identity WHERE identity_key = ?", (key,)
            ).fetchone()
            initial_status = (
                JobProgressStatus(str(identity_row["status"]))
                if identity_row is not None
                else JobProgressStatus.DISCOVERED
            )
            try:
                # The insert path is race-safe: a concurrent discovery of the
                # same new job loses the PRIMARY KEY race, falls through to
                # the update branch below instead of crashing the caller.
                with self._connection:
                    self._connection.execute(
                        """INSERT INTO job_records
                           (job_id, source, company, title, location, url, status,
                            first_seen_at, last_seen_at, greeted_at, note,
                            identity_key)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?)""",
                        (
                            job_id,
                            source.strip(),
                            company.strip(),
                            title.strip(),
                            location.strip(),
                            url.strip(),
                            initial_status.value,
                            _format_time(now),
                            _format_time(now),
                            key,
                        ),
                    )
                    self._connection.execute(
                        """INSERT INTO job_status_events (job_id, status, note, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (job_id, initial_status.value, "", _format_time(now)),
                    )
                    if identity_row is None:
                        self._connection.execute(
                            """INSERT INTO job_identity
                               (identity_key, company, title, business_line,
                                status, first_seen_at, last_seen_at, greeted_at, note)
                               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '')""",
                            (
                                key,
                                company.strip(),
                                title.strip(),
                                derive_business_line(title),
                                initial_status.value,
                                _format_time(now),
                                _format_time(now),
                            ),
                        )
                    else:
                        self._connection.execute(
                            "UPDATE job_identity SET last_seen_at = ? WHERE identity_key = ?",
                            (_format_time(now), key),
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

        Marking ``greeted`` stamps ``greeted_at``. Re-marking the current
        status stays idempotent for the status itself, but a supplied
        ``note`` is still persisted - note-only corrections (e.g. fixing a
        wrong remark) are a legitimate same-state update. The old no-op
        branch silently dropped the note and returned success, which
        produced a live false-ok (2026-08-28: wrong remark could not be
        corrected through the agent).
        """

        job_id = job_id.strip()
        record = self.get(job_id)
        if record is None:
            raise KeyError(f"job not found in registry: {job_id}")
        if record.status is status:
            if not note or note == record.note:
                return record
            now = self._clock()
            self._connection.execute(
                "UPDATE job_records SET note = ?, last_seen_at = ? WHERE job_id = ?",
                (note, now.isoformat(), job_id),
            )
            self._connection.commit()
            return replace(record, note=note, last_seen_at=now)
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
        # F1-R4: the journey lives on the identity; every posting under it
        # sees the same status so cross-platform records never fork.
        key_row = self._connection.execute(
            "SELECT identity_key FROM job_records WHERE job_id = ?", (job_id,)
        ).fetchone()
        if key_row is not None and str(key_row["identity_key"] or ""):
            key = str(key_row["identity_key"])
            self._connection.execute(
                "UPDATE job_identity SET status = ?, greeted_at = COALESCE(?, greeted_at), "
                "note = ?, last_seen_at = ? WHERE identity_key = ?",
                (
                    status.value,
                    _format_time(greeted_at) if greeted_at else None,
                    note.strip() or "",
                    _format_time(now),
                    key,
                ),
            )
            self._connection.execute(
                "UPDATE job_records SET status = ? WHERE identity_key = ? AND job_id != ?",
                (status.value, key, job_id),
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

    # -- cross-platform identity (F1-R4 L3) -----------------------------------

    def find_merge_candidates(
        self,
        company: str,
        title: str,
        *,
        limit: int = 10,
    ) -> tuple[dict[str, object], ...]:
        """Fuzzy candidates for L3 review: same canonical company, other keys.

        Returns identities whose company token matches the query's but whose
        identity_key differs (direction qualifier missing on one side, etc.).
        These are *suggestions only* — merging goes through
        ``merge_identities`` with explicit user confirmation.
        """

        from jobagent.journey.identity import normalize_company

        token = normalize_company(company)
        rows = self._connection.execute(
            "SELECT identity_key, company, title, status, business_line "
            "FROM job_identity WHERE identity_key LIKE ? "
            "ORDER BY last_seen_at DESC LIMIT ?",
            (f"idn:{token}|%", max(1, limit)),
        ).fetchall()
        target = derive_identity_key(company, title)
        return tuple(
            {
                "identity_key": str(row["identity_key"]),
                "company": str(row["company"]),
                "title": str(row["title"]),
                "business_line": str(row["business_line"]),
                "status": str(row["status"]),
            }
            for row in rows
            if str(row["identity_key"]) != target
        )

    def merge_identities(
        self,
        source_key: str,
        target_key: str,
        *,
        user_confirmed: bool = False,
        rationale: str = "",
    ) -> dict[str, object]:
        """Merge ``source_key`` into ``target_key`` (HITL: L3, never auto).

        The target identity keeps the deeper journey status of the two;
        every posting under source is re-hung on target; status events stay
        attached to their postings so ``history`` remains intact. The source
        identity row is deleted. Without ``user_confirmed`` nothing changes
        and the payload is returned for review.
        """

        source_key = source_key.strip()
        target_key = target_key.strip()
        if source_key == target_key:
            return {"status": "error", "message": "source and target are identical"}
        source = self._connection.execute(
            "SELECT * FROM job_identity WHERE identity_key = ?", (source_key,)
        ).fetchone()
        target = self._connection.execute(
            "SELECT * FROM job_identity WHERE identity_key = ?", (target_key,)
        ).fetchone()
        if source is None or target is None:
            return {"status": "error", "message": "unknown identity key(s)"}

        source_status = JobProgressStatus(str(source["status"]))
        target_status = JobProgressStatus(str(target["status"]))

        def _depth(status: JobProgressStatus) -> int:
            order = [
                JobProgressStatus.DISCOVERED,
                JobProgressStatus.RECOMMENDED,
                JobProgressStatus.GREETED,
                JobProgressStatus.APPLIED,
                JobProgressStatus.NO_RESPONSE,
                JobProgressStatus.HR_REPLIED,
                JobProgressStatus.INTERVIEWING,
                JobProgressStatus.OFFER,
            ]
            return order.index(status) if status in order else -1

        winner = (
            target_status if _depth(target_status) >= _depth(source_status) else source_status
        )

        if not user_confirmed:
            return {
                "status": "waiting_user_confirmation",
                "source": dict(source),
                "target": dict(target),
                "merged_status": winner.value,
                "rationale": rationale,
                "hint": "向用户展示两侧岗位与合并依据，确认后带 user_confirmed=true 重试。",
            }

        now = self._clock()
        with self._connection:
            self._connection.execute(
                "UPDATE job_records SET identity_key = ? WHERE identity_key = ?",
                (target_key, source_key),
            )
            self._connection.execute(
                "UPDATE job_records SET status = ? WHERE identity_key = ?",
                (winner.value, target_key),
            )
            self._connection.execute(
                "UPDATE job_identity SET status = ?, last_seen_at = ?, "
                "note = ? WHERE identity_key = ?",
                (winner.value, _format_time(now), rationale.strip(), target_key),
            )
            self._connection.execute(
                "DELETE FROM job_identity WHERE identity_key = ?", (source_key,)
            )
        return {
            "status": "ok",
            "identity_key": target_key,
            "merged_status": winner.value,
            "rationale": rationale,
        }

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
            CREATE TABLE IF NOT EXISTS job_identity (
                identity_key TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                title TEXT NOT NULL,
                business_line TEXT NOT NULL DEFAULT 'general',
                status TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                greeted_at TEXT,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_job_identity_status
                ON job_identity(status);
            """
        )
        # F1-R4 layering: postings gain an identity_key column. Legacy rows
        # are backfilled by deriving the key from their own company/title,
        # then the deepest existing status per identity wins the identity row.
        # The check-then-ALTER must serialize against concurrent initializers
        # (fresh database + several processes opening the registry at once):
        # BEGIN IMMEDIATE makes the loser wait, then re-read the migrated
        # schema instead of issuing a duplicate ALTER.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(job_records)"
                ).fetchall()
            }
            if "identity_key" not in columns:
                self._connection.execute(
                    "ALTER TABLE job_records ADD COLUMN identity_key TEXT NOT NULL DEFAULT ''"
                )
                for row in self._connection.execute(
                    "SELECT job_id, company, title, status, first_seen_at, "
                    "last_seen_at, greeted_at FROM job_records"
                ).fetchall():
                    key = derive_identity_key(str(row["company"]), str(row["title"]))
                    self._connection.execute(
                        "UPDATE job_records SET identity_key = ? WHERE job_id = ?",
                        (key, row["job_id"]),
                    )
                    self._connection.execute(
                        """INSERT INTO job_identity
                           (identity_key, company, title, status, first_seen_at,
                            last_seen_at, greeted_at, note)
                           VALUES (?, ?, ?, ?, ?, ?, ?, '')
                           ON CONFLICT(identity_key) DO UPDATE SET
                             status = excluded.status,
                             last_seen_at = excluded.last_seen_at""",
                        (
                            key,
                            str(row["company"]),
                            str(row["title"]),
                            str(row["status"]),
                            str(row["first_seen_at"]),
                            str(row["last_seen_at"]),
                            row["greeted_at"],
                        ),
                    )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise


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
