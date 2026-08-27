"""UTC boundary + anchored path for ApplyHistory (code review MEDIUM M4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobagent.applier.history import ApplyHistory


def test_today_count_uses_utc_day_boundary(tmp_path: Path) -> None:
    """A UTC-timestamped apply must count on the UTC day, not the local one."""
    history = ApplyHistory(tmp_path / "h.json")

    # 2026-08-27T23:30Z: same UTC day "today" if now is 2026-08-27T23:59Z,
    # already next day in any UTC+ timezone. Both entries share the UTC date
    # 2026-08-27 and must both count when now() is UTC 2026-08-27.
    stamp = datetime(2026, 8, 27, 23, 30, tzinfo=UTC).isoformat()
    history._data.setdefault("jobs", {}).update(
        {
            "a": {"status": "submitted", "applied_at": stamp, "source": "boss"},
            "b": {"status": "submitted", "applied_at": stamp, "source": "boss"},
            "old": {
                "status": "submitted",
                "applied_at": (
                    datetime(2026, 8, 27, tzinfo=UTC) - timedelta(days=2)
                ).isoformat(),
                "source": "boss",
            },
        }
    )

    import jobagent.applier.history as h_mod

    original = h_mod.datetime

    class FixedDatetime(h_mod.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return datetime(2026, 8, 27, 23, 59, tzinfo=UTC)

    h_mod.datetime = FixedDatetime  # type: ignore[assignment]
    try:
        assert history.today_count() == 2
    finally:
        h_mod.datetime = original  # type: ignore[assignment]


def test_default_path_lives_next_to_state_db() -> None:
    """BossApplier must anchor the history beside jobagent_state_db, not CWD."""
    from jobagent.applier.boss import BossApplier
    from jobagent.config import Settings

    settings = Settings(_env_file=None, jobagent_state_db=Path("data/state/x.db"))
    applier = BossApplier(settings)
    assert str(applier._history._path).replace("\\", "/") == "data/state/apply_history.json"
