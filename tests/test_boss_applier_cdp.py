"""Regression tests for the CDP-based BossApplier.

Locks two behaviors:
- apply() refuses immediately while the shared Boss cooldown is active
  (previously each job in a --apply batch kept hitting Boss and deepening
  the block for the whole 60-minute window);
- tabs opened for greetings are closed on __aexit__, leaving the user's
  own tabs alone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobagent.applier.boss import BossApplier
from jobagent.applier.history import ApplyHistory
from jobagent.config import Settings
from jobagent.models import ApplicationStatus, Job, JobSource, Profile
from jobagent.scraper.boss import get_boss_cooldown


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        jobagent_state_db=tmp_path / "state.db",
    )


def _job() -> Job:
    return Job(
        id="boss:cdp-test",
        source=JobSource.BOSS,
        title="后端工程师",
        company="示例公司",
        location="北京",
        url="https://www.zhipin.com/job_detail/cdp-test.html",
        description="",
    )


def _profile() -> Profile:
    return Profile(name="张三", resume_text="Python 后端")


@pytest.mark.asyncio
async def test_apply_refuses_during_cooldown_without_touching_browser(
    tmp_path: Path,
) -> None:
    """Cooldown active -> FAILED(cooldown_active) before any page is opened."""

    applier = BossApplier(_settings(tmp_path), history=ApplyHistory(tmp_path / "h.json"))
    applier._context = MagicMock()
    applier._context.new_page = AsyncMock()

    with patch.object(
        get_boss_cooldown().__class__,
        "check",
        return_value=(False, 42, None),
    ):
        result = await applier.apply(_job(), _profile())

    assert result.status is ApplicationStatus.FAILED
    assert result.extra["reason"] == "cooldown_active"
    assert result.extra["remaining_minutes"] == 42
    applier._context.new_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_closes_only_applier_opened_tabs(tmp_path: Path) -> None:
    """__aexit__ closes the tabs this applier opened and nothing else."""

    applier = BossApplier(_settings(tmp_path), history=ApplyHistory(tmp_path / "h.json"))

    ours = MagicMock()
    ours.close = AsyncMock()
    also_ours = MagicMock()
    also_ours.close = AsyncMock()
    applier._opened_pages = [ours, also_ours]
    applier._context = MagicMock()

    await applier.__aexit__(None, None, None)

    ours.close.assert_awaited_once()
    also_ours.close.assert_awaited_once()
    assert applier._opened_pages == []
    assert applier._context is None


@pytest.mark.asyncio
async def test_crawl_gate_paces_page_creation(tmp_path: Path) -> None:
    """A configured gate is acquired before each greeting tab is opened."""

    applier = BossApplier(_settings(tmp_path), history=ApplyHistory(tmp_path / "h.json"))
    gate = MagicMock()
    gate.acquire = AsyncMock()
    applier._crawl_gate = gate
    applier._context = MagicMock()
    applier._context.new_page = AsyncMock(return_value=MagicMock())

    async def noop_do_apply(*args: Any, **kwargs: Any) -> Any:
        from jobagent.models import Application

        return Application(
            job_id="boss:cdp-test",
            status=ApplicationStatus.SUBMITTED,
            extra={"reason": "test"},
        )

    with (
        patch.object(BossApplier, "_do_apply", side_effect=noop_do_apply),
        patch(
            "jobagent.applier.boss.get_boss_cooldown",
            return_value=MagicMock(check=MagicMock(return_value=(True, 0, None))),
        ),
    ):
        await applier.apply(_job(), _profile())

    gate.acquire.assert_awaited_once_with("boss-cdp")
    applier._context.new_page.assert_awaited_once()
    assert len(applier._opened_pages) == 1
    asyncio.get_running_loop()  # sanity: loop still healthy
