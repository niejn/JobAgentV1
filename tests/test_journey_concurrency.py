"""Concurrency tests for journey store + job registry (code review HIGH H3).

Real threads with separate connections, exercising the exact interleavings
the review flagged: duplicate artifact versions, PK-collision crashes on
concurrent discovery, and divergent mark() histories.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from jobagent.journey.job_registry import (
    JobProgressStatus,
    JobTransitionError,
    SQLiteJobRegistry,
)
from jobagent.journey.store import SQLiteJourneyStore


def _mk_journey(store: SQLiteJourneyStore) -> tuple[str, str]:
    journey = store.create_journey(company="Example", role="SWE", job_description="jd")
    task = store.start_task(journey_id=journey.id, task_type="research")
    return journey.id, task.id


def test_concurrent_create_artifact_versions_are_unique(tmp_path: Path) -> None:
    """Writers racing create_artifact must produce 1..N versions, no dupes."""
    store = SQLiteJourneyStore(tmp_path / "journeys.db")
    journey_id, task_id = _mk_journey(store)
    store.close()

    def worker(n: int) -> int:
        s = SQLiteJourneyStore(tmp_path / "journeys.db")
        try:
            art = s.create_artifact(
                journey_id=journey_id,
                task_run_id=task_id,
                artifact_type="raw_source_snapshot",
                content_ref=f"ref-{n}",
                content_hash=f"hash-{n}",
                provenance_refs=[],
            )
            return art.version
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(worker, range(8)))

    assert sorted(versions) == list(range(1, 9)), f"duplicate versions: {sorted(versions)}"


def test_concurrent_upsert_discovered_never_crashes(tmp_path: Path) -> None:
    """Concurrent first discoveries of one job: exactly one insert, no exception."""
    db = tmp_path / "registry.db"

    def worker(n: int) -> object:
        r = SQLiteJobRegistry(db)
        try:
            return r.upsert_discovered(
                job_id="boss:same-job",
                source="boss",
                company=f"Co{n}",
                title="SWE",
            )
        finally:
            r.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))

    records = [r for r in results if r is not None]
    assert len(records) == 1, f"expected exactly one new-job record, got {len(records)}"

    final = SQLiteJobRegistry(db)
    try:
        row = final.get("boss:same-job")
        assert row is not None and row.status is JobProgressStatus.DISCOVERED
    finally:
        final.close()


def test_concurrent_mark_cannot_diverge_history(tmp_path: Path) -> None:
    """Same-source divergent mark() calls: guarded update lets one through.

    A barrier forces both workers to complete their validation snapshot
    (self.get) before either writes, reproducing the validated-then-raced
    interleaving the review flagged.
    """
    db = tmp_path / "registry.db"
    r = SQLiteJobRegistry(db)
    r.upsert_discovered(
        job_id="boss:j1", source="boss", company="Co", title="SWE", url="https://x/1"
    )
    r.mark("boss:j1", JobProgressStatus.RECOMMENDED)
    r.close()

    barrier = threading.Barrier(2)

    def worker(status: JobProgressStatus) -> tuple[bool, str]:
        reg = SQLiteJobRegistry(db)
        original_get = reg.get

        def synchronized_get(job_id: str):
            record = original_get(job_id)
            barrier.wait(timeout=10)
            return record

        try:
            with patch.object(reg, "get", side_effect=synchronized_get):
                reg.mark("boss:j1", status, note=f"n-{status.value}")
            return True, status.value
        except JobTransitionError:
            return False, status.value
        finally:
            reg.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(worker, (JobProgressStatus.GREETED, JobProgressStatus.CLOSED))
        )

    wins = [status for won, status in outcomes if won]
    assert len(wins) == 1, f"both divergent transitions succeeded: {outcomes}"

    final = SQLiteJobRegistry(db)
    try:
        record = final.get("boss:j1")
        assert record is not None
        assert record.status.value == wins[0]
        events = final.history("boss:j1")
        # One discovered + one recommended + exactly one divergent winner.
        assert len(events) == 3, f"impossible history recorded: {events}"
    finally:
        final.close()


def test_mark_same_status_is_still_idempotent(tmp_path: Path) -> None:
    r = SQLiteJobRegistry(tmp_path / "registry.db")
    r.upsert_discovered(job_id="boss:j2", source="boss", company="Co", title="SWE")
    first = r.mark("boss:j2", JobProgressStatus.RECOMMENDED)
    again = r.mark("boss:j2", JobProgressStatus.RECOMMENDED)
    assert first.status is again.status
    assert len(r.history("boss:j2")) == 2
    r.close()
