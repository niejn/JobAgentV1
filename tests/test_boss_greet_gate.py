"""Tests for boss_greet_jobs approval semantics (code review CRITICAL-2).

Approval moved from a per-tool ``user_confirmed`` parameter to the agent's
``HumanInTheLoopMiddleware``: the tool call is physically interrupted before
the body runs, so the tool itself must have no confirmation parameter and the
middleware must cover it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jobagent.tools.boss_greet import (
    BossGreetingsManager,
    BossGreetJobsRequest,
    GreetingTarget,
)


def _request() -> BossGreetJobsRequest:
    return BossGreetJobsRequest(
        jobs=[
            GreetingTarget(
                url="https://www.zhipin.com/job_detail/abc.html",
                company="示例公司",
                title="后端工程师",
                job_id="boss:abc",
            )
        ],
        max_greetings=5,
    )


@pytest.mark.asyncio
async def test_greet_reaches_applier_without_confirmation_parameter(tmp_path) -> None:
    """No parameter gate: the call flows into BossApplier; the HITL
    middleware is the approval point (physical interrupt before this body)."""
    manager = BossGreetingsManager(_settings_stub(tmp_path))
    profile = SimpleNamespace(name="n", skills=[], years_experience=1, summary="s")

    with (
        patch("jobagent.tools.boss_greet.get_boss_cooldown") as cooldown,
        patch("jobagent.tools.boss_greet.BossApplier") as applier_cls,
        patch.object(BossGreetingsManager, "_load_profile", return_value=profile),
        patch.object(
            BossGreetingsManager, "_load_interview_preference", return_value=None
        ),
    ):
        cooldown.return_value.check.return_value = (True, 0, None)
        applier = applier_cls.return_value
        applier.__aenter__ = AsyncMock(return_value=applier)
        applier.__aexit__ = AsyncMock(return_value=None)
        applier.apply = AsyncMock(
            return_value=SimpleNamespace(
                status=SimpleNamespace(value="submitted"),
                extra={"greeting_sent": True, "reason": ""},
            )
        )

        result = await manager.greet(_request())

    assert applier_cls.call_count == 1, "applier must run without a parameter gate"
    applier.apply.assert_awaited_once()
    assert result["status"] == "completed"
    assert result["succeeded"] == 1


def test_greet_schema_has_no_user_confirmed_field() -> None:
    """The old weak gate is gone: user_confirmed is not part of the schema,
    so the model cannot bypass-or-satisfy approval through a flag."""
    assert "user_confirmed" not in BossGreetJobsRequest.model_fields
    assert "user_confirmed" not in GreetingTarget.model_fields


def _settings_stub(tmp_path) -> object:
    from jobagent.config import Settings

    return Settings(_env_file=None, jobagent_state_db=tmp_path / "state.db")
