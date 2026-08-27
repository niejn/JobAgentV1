"""Tests for the boss_greet_jobs hard HITL gate (code review CRITICAL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from jobagent.tools.boss_greet import (
    BossGreetingsManager,
    BossGreetJobsRequest,
    GreetingTarget,
)


def _request(user_confirmed: bool) -> BossGreetJobsRequest:
    return BossGreetJobsRequest(
        jobs=[
            GreetingTarget(
                url="https://www.zhipin.com/job_detail/abc.html",
                company="示例公司",
                title="后端工程师",
                job_id="boss:abc",
            )
        ],
        user_confirmed=user_confirmed,
        max_greetings=5,
    )


@pytest.mark.asyncio
async def test_greet_refuses_without_user_confirmation() -> None:
    """No confirmation -> structured refusal; BossApplier must never start."""
    manager = BossGreetingsManager(_settings_stub())

    with patch("jobagent.tools.boss_greet.BossApplier") as applier_cls:
        applier_cls.return_value.__aenter__ = AsyncMock(return_value=None)
        result = await manager.greet(_request(user_confirmed=False))

    assert result["status"] == "waiting_user_confirmation"
    assert applier_cls.call_count == 0, "applier constructed despite no confirmation"
    assert "未发送任何消息" in result["message"]


@pytest.mark.asyncio
async def test_greet_missing_field_is_rejected_by_schema() -> None:
    """user_confirmed is required: omitting it must fail validation, not default."""
    with pytest.raises(ValueError, match="user_confirmed"):
        BossGreetJobsRequest(
            jobs=[
                GreetingTarget(
                    url="https://www.zhipin.com/job_detail/abc.html",
                    company="示例公司",
                    title="后端工程师",
                )
            ],
            max_greetings=5,
        )


def _settings_stub():
    from jobagent.config import Settings

    return Settings(_env_file=None)
