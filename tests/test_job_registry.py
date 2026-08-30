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


def _discover(
    store: SQLiteJobRegistry,
    job_id: str = "boss:abc123",
    *,
    title: str = "AI Agent 工程师",
) -> None:
    store.upsert_discovered(
        job_id=job_id,
        source="boss",
        company="小而美科技",
        title=title,
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

    def test_applied_path_email_channel(self, registry: SQLiteJobRegistry) -> None:
        _discover(registry)
        applied = registry.mark("boss:abc123", JobProgressStatus.APPLIED, note="邮件投递")
        assert applied.status is JobProgressStatus.APPLIED
        # A resume submission can go straight to an interview invitation.
        record = registry.mark("boss:abc123", JobProgressStatus.INTERVIEWING)
        assert record.status is JobProgressStatus.INTERVIEWING

    def test_discovered_straight_to_applied(self, registry: SQLiteJobRegistry) -> None:
        _discover(registry)
        record = registry.mark("boss:abc123", JobProgressStatus.APPLIED)
        assert record.status is JobProgressStatus.APPLIED

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
        _discover(registry, "boss:two", title="爬虫工程师")
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


class TestCrossPlatformIdentity:
    """F1-R4: same job across platforms/re-posts shares one journey."""

    def test_repost_new_platform_id_merges_automatically(
        self, registry: SQLiteJobRegistry
    ) -> None:
        """Boss re-post (fresh encryptJobId, same company+title) lands on the
        existing identity and inherits its journey status (JI-4)."""

        _discover(registry, "boss:old-id")
        registry.mark("boss:old-id", JobProgressStatus.GREETED)

        # Same job re-posted: new platform id, slightly different writing.
        repost = registry.upsert_discovered(
            job_id="boss:new-id",
            source="boss",
            company="小而美科技有限公司",  # suffix variant
            title="资深 AI Agent 工程师",  # seniority variant
        )
        assert repost is not None  # a genuinely new posting
        assert repost.status is JobProgressStatus.GREETED  # inherited

    def test_xhs_posting_joins_boss_identity(
        self, registry: SQLiteJobRegistry
    ) -> None:
        """The same job seen on XHS joins the Boss identity's journey."""

        _discover(registry, "boss:xyz")
        registry.mark("boss:xyz", JobProgressStatus.APPLIED, note="邮件投递")

        xhs = registry.upsert_discovered(
            job_id="xhs:note-999",
            source="xhs",
            company="小而美科技",
            title="AI Agent 工程师",
        )
        assert xhs is not None
        assert xhs.status is JobProgressStatus.APPLIED  # shared journey

    def test_mark_syncs_all_postings_under_identity(
        self, registry: SQLiteJobRegistry
    ) -> None:
        """mark() on one posting moves every posting under the identity."""

        _discover(registry, "boss:one")
        _discover(registry, "boss:repost", title="AI Agent 工程师")
        registry.mark("boss:one", JobProgressStatus.GREETED)
        registry.mark("boss:one", JobProgressStatus.HR_REPLIED, note="HR 回复了")

        other = registry.get("boss:repost")
        assert other is not None
        assert other.status is JobProgressStatus.HR_REPLIED

    def test_distinct_jobs_stay_separate(
        self, registry: SQLiteJobRegistry
    ) -> None:
        _discover(registry, "boss:agent")
        _discover(registry, "boss:crawler", title="爬虫工程师")

        registry.mark("boss:agent", JobProgressStatus.GREETED)
        crawler = registry.get("boss:crawler")
        assert crawler is not None
        assert crawler.status is JobProgressStatus.DISCOVERED  # untouched


class TestLegacyMigration:
    """Old registries gain identity rows without losing journey state."""

    def test_legacy_rows_backfilled_with_identity(
        self, tmp_path: Path
    ) -> None:
        import sqlite3

        db = tmp_path / "legacy.db"
        # Build a pre-F1-R4 database (no identity_key column).
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE job_records (
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
            CREATE TABLE job_status_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES job_records(job_id),
                status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            INSERT INTO job_records VALUES (
                'boss:legacy1', 'boss', '老公司', 'AI 工程师', '上海', '',
                'greeted', '2026-01-01T00:00:00+08:00',
                '2026-01-02T00:00:00+08:00', NULL, '');
            """
        )
        conn.commit()
        conn.close()

        with SQLiteJobRegistry(db) as store:
            record = store.get("boss:legacy1")
            assert record is not None
            assert record.status is JobProgressStatus.GREETED

            # A same-job new posting must now join the legacy journey.
            repost = store.upsert_discovered(
                job_id="boss:legacy2",
                source="boss",
                company="老公司",
                title="AI 工程师",
            )
            assert repost is not None
            assert repost.status is JobProgressStatus.GREETED
