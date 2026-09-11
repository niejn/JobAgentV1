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

from jobagent.applier.boss_ws import BossConversationTarget
from jobagent.journey.boss_contact import BossContactRegistry
from jobagent.models import Job, JobSource
from jobagent.tools.boss_greet import (
    BossGreetingsManager,
    BossGreetJobsRequest,
    GreetingTarget,
    build_boss_greet_jobs_tool,
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
    settings = _settings_stub(tmp_path)
    settings.boss_contact_transport = "cdp"
    manager = BossGreetingsManager(settings)
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


def test_default_boss_transports_use_cdp_reads_and_direct_contact(tmp_path) -> None:
    """Job discovery needs the logged-in Chrome; contact must avoid job-page DOM."""

    settings = _settings_stub(tmp_path)

    assert settings.boss_search_transport == "cdp"
    assert settings.boss_contact_transport == "http"


@pytest.mark.asyncio
async def test_http_contact_creates_conversation_then_sends_greeting(tmp_path) -> None:
    settings = _settings_stub(tmp_path)
    settings.boss_contact_transport = "http"
    request = BossGreetJobsRequest(
        jobs=[
            GreetingTarget(
                url="https://www.zhipin.com/job_detail/abc.html",
                company="际数科技",
                title="标注工程师实习岗",
                job_id="boss:abc",
                greeting="您好，想进一步沟通。",
            )
        ]
    )
    job = Job(
        id="boss:abc",
        source=JobSource.BOSS,
        title="标注工程师实习岗",
        company="际数科技",
        location="上海",
        url="https://www.zhipin.com/job_detail/abc.html",
        description="",
        metadata={"security_id": "security", "lid": "lid"},
    )
    target = BossConversationTarget(42, 0, "enc", "王媛", "际数科技", job.title)
    profile = SimpleNamespace(name="n", skills=[], years_experience=1, summary="s")
    with BossContactRegistry(settings.jobagent_state_db) as contact_registry:
        contact_registry.save_job_transport(job_id="boss:abc", metadata=job.metadata)

    with (
        patch("jobagent.tools.boss_greet.get_boss_cooldown") as cooldown,
        patch.object(BossGreetingsManager, "_load_profile", return_value=profile),
        patch("jobagent.auth.cookie_manager.get_cookies", new=AsyncMock(return_value=[
            {"name": "bst", "value": "b"},
            {"name": "wt2", "value": "w"},
            {"name": "__zp_stoken__", "value": "s"},
        ])),
        patch("jobagent.applier.boss_direct_contact.BossDirectContactAdapter") as contact_cls,
        patch("jobagent.applier.boss_ws.send_text_to_target", new=AsyncMock()) as send,
    ):
        cooldown.return_value.check.return_value = (True, 0, None)
        contact_cls.return_value.enter = AsyncMock(
            return_value=SimpleNamespace(
                status="confirmed",
                target=target,
                default_greeting="Boss 默认招呼",
                error_type=None,
            )
        )
        result = await BossGreetingsManager(
            settings, registry_path=settings.jobagent_state_db
        ).greet(request)

    assert result["status"] == "completed"
    assert result["succeeded"] == 1
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_ack_timeout_history_check_is_bounded_and_never_resends(monkeypatch) -> None:
    history = AsyncMock(side_effect=[False, False, True])
    monkeypatch.setattr("jobagent.applier.boss_ws.verify_text_in_conversation", history)
    monkeypatch.setattr("jobagent.tools.boss_greet.asyncio.sleep", AsyncMock())

    result = await BossGreetingsManager._verify_with_retry(
        cookies={},
        target=BossConversationTarget(42, 0, "enc", "王媛", "际数科技", "岗位"),
        text="定制招呼",
    )

    assert result is True
    assert history.await_count == 3


@pytest.mark.asyncio
async def test_tool_accepts_schema_parsed_greeting_targets(tmp_path) -> None:
    manager = SimpleNamespace(greet=AsyncMock(return_value={"status": "completed"}))
    tool = build_boss_greet_jobs_tool(manager)

    result = await tool.ainvoke(
        {
            "jobs": [
                {
                    "url": "https://www.zhipin.com/job_detail/abc.html",
                    "company": "示例公司",
                    "title": "后端工程师",
                    "greeting": "你好",
                }
            ]
        }
    )

    assert result["status"] == "completed"
    manager.greet.assert_awaited_once()
    assert isinstance(manager.greet.await_args.args[0].jobs[0], GreetingTarget)


def _settings_stub(tmp_path) -> object:
    from jobagent.config import Settings

    return Settings(_env_file=None, jobagent_state_db=tmp_path / "state.db")
