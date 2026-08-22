"""Durable cross-Journey index of reusable Raw Source Snapshots."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from jobagent.interview.snapshot import SnapshotBundle, load_snapshot_bundle

logger = logging.getLogger(__name__)


class SQLiteSourceCorpus:
    """Index immutable source manifests without owning or rewriting their files."""

    def __init__(self, database_path: Path) -> None:
        self.path = database_path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteSourceCorpus:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def preserve(self, source_type: str, bundle: SnapshotBundle) -> bool:
        """Idempotently index one immutable snapshot version."""

        source = source_type.strip().lower()
        if not source:
            raise ValueError("source_type is required")
        searchable_text = "\n".join(
            (
                bundle.snapshot.title,
                bundle.snapshot.body,
                " ".join(bundle.snapshot.tags),
                *(item.text for item in bundle.extractions),
            )
        )
        with self._connection:
            result = self._connection.execute(
                """INSERT OR IGNORE INTO source_corpus
                (source_type, source_id, content_hash, manifest_path, searchable_text,
                 captured_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    source,
                    bundle.snapshot.note_id,
                    bundle.snapshot.content_hash,
                    str(bundle.manifest_path.resolve()),
                    searchable_text,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return result.rowcount == 1

    def backfill_manifests(self, artifact_root: Path, source_type: str = "xhs") -> int:
        """Index legacy manifests once so pre-corpus downloads become reusable."""

        root = artifact_root.expanduser().resolve()
        migration_key = f"legacy-snapshot-backfill-v1:{root}"
        migrated = self._connection.execute(
            "SELECT 1 FROM source_corpus_migrations WHERE migration_key = ?",
            (migration_key,),
        ).fetchone()
        if migrated is not None:
            return 0

        indexed = 0
        had_errors = False
        if root.is_dir():
            for manifest_path in root.rglob("snapshot.json"):
                try:
                    bundle = load_snapshot_bundle(manifest_path)
                    indexed += int(self.preserve(source_type, bundle))
                except (FileNotFoundError, KeyError, TypeError, ValueError):
                    had_errors = True
                    logger.warning(
                        "Unable to backfill Source Corpus manifest",
                        extra={"manifest_path": str(manifest_path)},
                        exc_info=True,
                    )
        if not had_errors:
            with self._connection:
                self._connection.execute(
                    """INSERT OR IGNORE INTO source_corpus_migrations
                    (migration_key, completed_at) VALUES (?, ?)""",
                    (migration_key, datetime.now(UTC).isoformat()),
                )
        return indexed

    def get_latest(self, source_type: str, source_id: str) -> SnapshotBundle | None:
        """Return the newest locally available version for one platform source ID."""

        rows = self._connection.execute(
            """SELECT manifest_path FROM source_corpus
            WHERE source_type = ? AND source_id = ?
            ORDER BY captured_at DESC, rowid DESC""",
            (source_type.strip().lower(), source_id.strip()),
        ).fetchall()
        for row in rows:
            try:
                return load_snapshot_bundle(Path(row["manifest_path"]))
            except (FileNotFoundError, KeyError, TypeError, ValueError):
                logger.warning(
                    "Ignoring unavailable Source Corpus manifest",
                    extra={"source_type": source_type, "source_id": source_id},
                    exc_info=True,
                )
        return None

    def count_versions(self, source_type: str, source_id: str) -> int:
        row = self._connection.execute(
            """SELECT COUNT(*) AS count FROM source_corpus
            WHERE source_type = ? AND source_id = ?""",
            (source_type.strip().lower(), source_id.strip()),
        ).fetchone()
        return int(row["count"])

    def _migrate(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_corpus (
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    searchable_text TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY (source_type, source_id, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_source_corpus_latest
                ON source_corpus(source_type, source_id, captured_at DESC);
                CREATE TABLE IF NOT EXISTS source_corpus_migrations (
                    migration_key TEXT PRIMARY KEY,
                    completed_at TEXT NOT NULL
                );
                """
            )
