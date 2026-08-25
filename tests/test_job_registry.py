"""Tests for the job progress registry (dedup + journey tracking)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jobagent.journey.job_registry import (
    JobProgressStatus,
    JobTransitionError,
    SQLiteJobRegistry,
)


@pytest.fixture()
def registry(tmp_path: Path) -> SQLiteJobRegistry:
    store = SQLiteJobRegistry(tmp_path / "registry.db")
    yield store
    store.close()


def _discover(store: SQLiteJobRegistry, job_id: str = "boss:abc123") -> None:
    store.upsert_discovered(
        job_id=job_id,
        source="boss",
        company="小而美科技",
        title="AI Agent 工程师",
        location="上海·杨浦区",
        url="https://www.zhipin.com/job_detail/abc123.html",
    )


class TestUpsertDiscovered:
    def test_new_job_recorded_as_discovered(self, registry: SQLiteJobRegistry) -> None:
        created = registry.upsert_discovered(
            job_id="boss:abc123",
            source="boss",
            company="小而美科技",
            title="AI Agent 工程师",
            location="上海",
            url="https://www.zhipin.com/job_detail/abc123.html",
        )
        assert created is not None
        assert created.status is JobProgressStatus.DISCOVERED
        assert created.company == "小而美科技"

    def test_known_job_returns_none_and_refreshes_last_seen(
        self, registry: SQLiteJobRegistry
    ) -> None:
        _discover(registry)
        first = registry.get("boss:abc123")
        assert first is not None
        # Advance the registry clock by re-inserting later.
        later = datetime.now().astimezone() + timedelta(hours=2)
        registry_after = SQLiteJobRegistry(
            registry.path, clock=lambda: later
        )
        try:
            result = registry_after.upsert_discovered(
                job_id="boss:abc123",
                source="boss",
                company="小而美科技",
                title="AI Agent 工程师",
            )
        finally:
            registry_after.close()
        assert result is None  # already known - not new this run
        refreshed = registry.get("boss:abc123")
        assert refreshed is not None
        assert refreshed.status is JobProgressStatus.DISCOVERED  # no regression
        assert refreshed.last_seen_at > first.last_seen_at

    def test_known_job_status_never_regresses_on_re_discovery(
        self, registry: SQLiteJobRegistry
    ) -> None:
        _discover(registry)
        registry.mark("boss:abc123", JobProgressStatus.GREETED)
        registry.upsert_discovered(
            job_id="boss:abc123",
            source="boss",
            company="小而美科技",
            title="AI Agent 工程师",
        )
        record = registry.get("boss:abc123")
        assert record is not None
        assert record.status is JobProgressStatus.GREETED

    def test_blank_job_id_rejected(self, registry: SQLiteJobRegistry) -> None:
        with pytest.raises(ValueError):
            registry.upsert_discovered(
                job_id="  ", source="boss", company="c", title="t"
            )


class TestJourneyTransitions:
    def test_full_happy_path(self, registry: SQLiteJobRegistry) -> None:
        _discover(registry)
        registry.mark("boss:abc123", JobProgressStatus.RECOMMENDED)
        greeted = registry.mark("boss:abc123", JobProgressStatus.GREETED)
        assert greeted.greeted_at is not None
        registry.mark("boss:abc123", JobProgressStatus.HR_REPLIED, note="约了周三面试")
        registry.mark("boss:abc123", JobProgressStatus.INTERVIEWING, note="视频一面")
        record = registry.mark("boss:abc123", JobProgressStatus.OFFER)
        assert record.status is JobProgressStatus.OFFER

    def test_no_response_then_late_reply(self, registry: SQLiteJobRegistry) -> None:
        _discover(registry)
        registry.mark("boss:abc123", JobProgressStatus.GREETED)
        registry.mark("boss:abc123", JobProgressStatus.NO_RESPONSE)
        record = registry.mark("boss:abc123", JobProgressStatus.HR_REPLIED, note="一个月后回复")
        assert record.status is JobProgressStatus.HR_REPLIED

    def test_illegal_transition_rejected(self, registry: SQLiteJobRegistry) -> None:
        _discover(registry)
        with pytest.raises(JobTransitionError):
            # discovered -> interviewing skips greeted/replied entirely
            registry.mark("boss:abc123", JobProgressStatus.INTERVIEWING)

    def test_greeted_twice_is_idempotent(self, registry: SQLiteJobRegistry) -> None:
        _discover(registry)
        first = registry.mark("boss:abc123", JobProgressStatus.GREETED)
        again = registry.mark("boss:abc123", JobProgressStatus.GREETED)
        assert again.greeted_at == first.greeted_at
        assert len(registry.history("boss:abc123")) == 2  # discovered + greeted only

    def test_closed_can_reopen(self, registry: SQLiteJobRegistry) -> None:
        _discover(registry)
        registry.mark("boss:abc123", JobProgressStatus.GREETED)
        registry.mark("boss:abc123", JobProgressStatus.CLOSED, note="放弃")
        record = registry.mark("boss:abc123", JobProgressStatus.HR_REPLIED, note="HR又联系了")
        assert record.status is JobProgressStatus.HR_REPLIED

    def test_unknown_job_rejected(self, registry: SQLiteJobRegistry) -> None:
        with pytest.raises(KeyError):
            registry.mark("boss:missing", JobProgressStatus.GREETED)


class TestHistoryAndListing:
    def test_history_records_events_in_order(
        self, registry: SQLiteJobRegistry
    ) -> None:
        _discover(registry)
        registry.mark("boss:abc123", JobProgressStatus.GREETED)
        events = registry.history("boss:abc123")
        assert [event.status for event in events] == [
            JobProgressStatus.DISCOVERED,
            JobProgressStatus.GREETED,
        ]

    def test_list_filters_by_status_and_company(
        self, registry: SQLiteJobRegistry
    ) -> None:
        _discover(registry, "boss:one")
        _discover(registry, "boss:two")
        registry.upsert_discovered(
            job_id="boss:three",
            source="boss",
            company="另一家公司",
            title="后端",
        )
        registry.mark("boss:one", JobProgressStatus.GREETED)

        greeted = registry.list_records(status=JobProgressStatus.GREETED)
        assert [record.job_id for record in greeted] == ["boss:one"]

        matched = registry.list_records(company="另一家")
        assert [record.job_id for record in matched] == ["boss:three"]

        assert len(registry.list_records()) == 3


class TestPersistence:
    def test_state_survives_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        with SQLiteJobRegistry(db) as store:
            _discover(store)
            store.mark("boss:abc123", JobProgressStatus.GREETED)
        with SQLiteJobRegistry(db) as store:
            record = store.get("boss:abc123")
            assert record is not None
            assert record.status is JobProgressStatus.GREETED
            assert record.greeted_at is not None
            assert len(store.history("boss:abc123")) == 2
