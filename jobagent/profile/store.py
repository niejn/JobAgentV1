"""SQLite persistence for the single user's candidate profile and resume versions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from jobagent.profile.context import CandidateBackground, CandidateContext, JobSearchProfile


@dataclass(frozen=True, slots=True)
class ResumeVersion:
    """One immutable, content-addressed base resume version."""

    id: str
    version: int
    source_name: str
    content_hash: str
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CandidateBackgroundVersion:
    """One immutable user-confirmed Candidate Background version."""

    id: str
    version: int
    resume_version_id: str | None
    background: CandidateBackground
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class JobSearchProfileVersion:
    """One immutable global job-search intention version."""

    id: str
    version: int
    profile: JobSearchProfile
    content_hash: str
    created_at: datetime


class SQLiteCandidateProfileStore:
    """Persist and restore the global candidate state behind a small interface."""

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

    def __enter__(self) -> SQLiteCandidateProfileStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def import_resume(self, *, source_name: str, content: str) -> ResumeVersion:
        """Import a UTF-8 text resume, reusing an existing identical version."""

        normalized_name = Path(source_name).name.strip()
        if not normalized_name:
            raise ValueError("resume source_name is required")
        if not content.strip():
            raise ValueError("resume content is required")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = self._connection.execute(
            "SELECT * FROM candidate_resume_versions WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if existing is not None:
            return _resume_from_row(cast(sqlite3.Row, existing))

        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS current FROM candidate_resume_versions"
        ).fetchone()
        version = int(row["current"]) + 1
        created_at = datetime.now(UTC)
        record = ResumeVersion(
            id=str(uuid4()),
            version=version,
            source_name=normalized_name,
            content_hash=content_hash,
            content=content,
            created_at=created_at,
        )
        with self._connection:
            self._connection.execute(
                """INSERT INTO candidate_resume_versions
                (id, version, source_name, content_hash, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.version,
                    record.source_name,
                    record.content_hash,
                    record.content,
                    record.created_at.isoformat(),
                ),
            )
        return record

    def save_confirmed_background(
        self,
        *,
        resume_version_id: str | None,
        background: CandidateBackground,
    ) -> CandidateBackgroundVersion:
        """Persist one user-confirmed background linked to its source resume."""

        if resume_version_id is not None:
            resume = self._connection.execute(
                "SELECT id FROM candidate_resume_versions WHERE id = ?",
                (resume_version_id,),
            ).fetchone()
            if resume is None:
                raise KeyError(f"resume version not found: {resume_version_id}")
        background_json = json.dumps(
            background.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
        existing = self._connection.execute(
            """SELECT * FROM candidate_background_versions
            WHERE resume_version_id IS ? AND background_json = ?""",
            (resume_version_id, background_json),
        ).fetchone()
        if existing is not None:
            return _background_from_row(cast(sqlite3.Row, existing))
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS current FROM candidate_background_versions"
        ).fetchone()
        version = int(row["current"]) + 1
        confirmed_at = datetime.now(UTC)
        record = CandidateBackgroundVersion(
            id=str(uuid4()),
            version=version,
            resume_version_id=resume_version_id,
            background=background,
            confirmed_at=confirmed_at,
        )
        with self._connection:
            self._connection.execute(
                """INSERT INTO candidate_background_versions
                (id, version, resume_version_id, background_json, confirmed_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.version,
                    record.resume_version_id,
                    background_json,
                    record.confirmed_at.isoformat(),
                ),
            )
        return record

    def import_context(self, context: CandidateContext) -> CandidateContext:
        """Import a legacy YAML-loaded context into the SQLite source of truth."""

        if context.search_profile is not None:
            self.save_job_search_profile(context.search_profile)
        resume: ResumeVersion | None = None
        if context.resume_text:
            source_name = (
                context.resume_path.name
                if context.resume_path is not None
                else "imported-resume.md"
            )
            resume = self.import_resume(source_name=source_name, content=context.resume_text)
        if context.background is not None:
            self.save_confirmed_background(
                resume_version_id=resume.id if resume else None,
                background=context.background,
            )
        restored = self.load_context()
        if restored is None:
            raise ValueError("candidate context did not contain importable data")
        return restored

    def save_job_search_profile(
        self,
        profile: JobSearchProfile,
    ) -> JobSearchProfileVersion:
        """Version the user's global job-search intentions and constraints."""

        payload = json.dumps(
            profile.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
        content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        existing = self._connection.execute(
            "SELECT * FROM job_search_profile_versions WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if existing is not None:
            return _search_profile_from_row(cast(sqlite3.Row, existing))
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS current FROM job_search_profile_versions"
        ).fetchone()
        version = int(row["current"]) + 1
        created_at = datetime.now(UTC)
        record = JobSearchProfileVersion(
            id=str(uuid4()),
            version=version,
            profile=profile,
            content_hash=content_hash,
            created_at=created_at,
        )
        with self._connection:
            self._connection.execute(
                """INSERT INTO job_search_profile_versions
                (id, version, profile_json, content_hash, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.version,
                    payload,
                    record.content_hash,
                    record.created_at.isoformat(),
                ),
            )
        return record

    def load_context(self) -> CandidateContext | None:
        """Restore the latest global candidate context for Agent startup."""

        resume_row = self._connection.execute(
            "SELECT * FROM candidate_resume_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        background_row = self._connection.execute(
            "SELECT * FROM candidate_background_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        search_row = self._connection.execute(
            "SELECT * FROM job_search_profile_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if resume_row is None and background_row is None and search_row is None:
            return None
        resume = (
            _resume_from_row(cast(sqlite3.Row, resume_row))
            if resume_row is not None
            else None
        )
        background = (
            _background_from_row(cast(sqlite3.Row, background_row)).background
            if background_row is not None
            else None
        )
        return CandidateContext(
            search_profile=(
                _search_profile_from_row(cast(sqlite3.Row, search_row)).profile
                if search_row is not None
                else None
            ),
            resume_path=None,
            background=background,
            resume_text=resume.content if resume else None,
        )

    def _migrate(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidate_resume_versions (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL UNIQUE,
                    source_name TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidate_background_versions (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL UNIQUE,
                    resume_version_id TEXT REFERENCES candidate_resume_versions(id),
                    background_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_search_profile_versions (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL UNIQUE,
                    profile_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                """
            )


class SQLiteCandidateContextProvider:
    """Load the latest candidate context on demand without holding a DB connection."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> CandidateContext | None:
        if not self._path.expanduser().resolve().is_file():
            return None
        with SQLiteCandidateProfileStore(self._path) as store:
            return store.load_context()


def _resume_from_row(row: sqlite3.Row) -> ResumeVersion:
    return ResumeVersion(
        id=row["id"],
        version=row["version"],
        source_name=row["source_name"],
        content_hash=row["content_hash"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _background_from_row(row: sqlite3.Row) -> CandidateBackgroundVersion:
    return CandidateBackgroundVersion(
        id=row["id"],
        version=row["version"],
        resume_version_id=row["resume_version_id"],
        background=CandidateBackground.model_validate(json.loads(row["background_json"])),
        confirmed_at=datetime.fromisoformat(row["confirmed_at"]),
    )


def _search_profile_from_row(row: sqlite3.Row) -> JobSearchProfileVersion:
    return JobSearchProfileVersion(
        id=row["id"],
        version=row["version"],
        profile=JobSearchProfile.model_validate(json.loads(row["profile_json"])),
        content_hash=row["content_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
