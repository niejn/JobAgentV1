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
async def test_greet_flows_to_direct_transport_without_confirmation_parameter(
    tmp_path,
) -> None:
    """No parameter gate: the call flows straight into the HTTP direct
    transport (the only path since the CDP branch was removed); the HITL
    middleware is the approval point (physical interrupt before this body)."""

    settings = _settings_stub(tmp_path)
    manager = BossGreetingsManager(settings)
    profile = SimpleNamespace(name="n", skills=[], years_experience=1, summary="s")

    with (
        patch("jobagent.tools.boss_greet.get_boss_cooldown") as cooldown,
        patch.object(BossGreetingsManager, "_load_profile", return_value=profile),
        patch.object(
            BossGreetingsManager, "_load_interview_preference", return_value=None
        ),
        patch("jobagent.auth.cookie_manager.get_cookies", new=AsyncMock(return_value=[])),
        patch(
            "jobagent.applier.boss_chat.list_boss_greetings_http",
            new=AsyncMock(return_value={"status": "ok", "greetings": []}),
        ),
        patch("jobagent.applier.boss_direct_contact.BossDirectContactAdapter") as contact_cls,
    ):
        cooldown.return_value.check.return_value = (True, 0, None)
        result = await manager.greet(_request())

    contact_cls.assert_called_once()
    entry = result["results"][0]
    assert entry["reason"] == "job_transport_data_missing"  # no gate, real flow


def test_greet_schema_has_no_user_confirmed_field() -> None:
    """The old weak gate is gone: user_confirmed is not part of the schema,
    so the model cannot bypass-or-satisfy approval through a flag."""
    assert "user_confirmed" not in BossGreetJobsRequest.model_fields
    assert "user_confirmed" not in GreetingTarget.model_fields


def test_default_boss_transports_use_cdp_reads(tmp_path) -> None:
    """Job discovery needs the logged-in Chrome; the CDP greeting transport
    was removed 2026-09-19, HTTP direct contact is the only contact path."""

    settings = _settings_stub(tmp_path)

    assert settings.boss_search_transport == "cdp"


@pytest.mark.asyncio
async def test_http_contact_creates_conversation_then_sends_greeting(tmp_path) -> None:
    settings = _settings_stub(tmp_path)
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
async def test_unverified_greeting_reverified_after_batch(tmp_path) -> None:
    """Channel fact (2026-09-19): ack loss is routine and messages surface in
    history ~6 s late. An entry left unverified by the in-flight checks is
    re-verified read-only after the batch and flipped to submitted — never
    re-sent."""

    settings = _settings_stub(tmp_path)
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
    target = BossConversationTarget(42, 0, "enc", "王媛", "际数科技", "后端")
    profile = SimpleNamespace(name="n", skills=[], years_experience=1, summary="s")
    with BossContactRegistry(settings.jobagent_state_db) as contact_registry:
        contact_registry.save_job_transport(
            job_id="boss:abc", metadata={"security_id": "s", "lid": "l"}
        )

    send = AsyncMock(side_effect=RuntimeError("ConnectionClosedOK"))
    history = AsyncMock(side_effect=[False, False, False, True])
    with (
        patch("jobagent.tools.boss_greet.get_boss_cooldown") as cooldown,
        patch.object(BossGreetingsManager, "_load_profile", return_value=profile),
        patch.object(
            BossGreetingsManager, "_load_interview_preference", return_value=None
        ),
        patch("jobagent.auth.cookie_manager.get_cookies", new=AsyncMock(return_value=[])),
        patch("jobagent.applier.boss_direct_contact.BossDirectContactAdapter") as contact_cls,
        patch("jobagent.applier.boss_ws.send_text_to_target", new=send),
        patch("jobagent.applier.boss_ws.verify_text_in_conversation", new=history),
        patch("jobagent.tools.boss_greet.asyncio.sleep", new=AsyncMock()),
    ):
        cooldown.return_value.check.return_value = (True, 0, None)
        contact_cls.return_value.enter = AsyncMock(
            return_value=SimpleNamespace(
                status="confirmed",
                target=target,
                default_greeting=None,
                error_type=None,
            )
        )
        result = await BossGreetingsManager(
            settings, registry_path=settings.jobagent_state_db
        ).greet(request)

    entry = result["results"][0]
    assert entry["status"] == "submitted"
    assert entry["reason"] == "history_confirmed_after_batch"
    assert entry["greeting_sent"] is True
    # 3 in-flight checks failed, the 4th (post-batch) confirmed; send ran once.
    assert history.await_count == 4
    send.assert_awaited_once()
    assert result["succeeded"] == 1

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


@pytest.mark.asyncio
async def test_history_precheck_skips_already_greeted_job(tmp_path) -> None:
    """A job with an existing platform conversation (chat history) is skipped
    without entering or sending, recorded as greeted, and the NEXT run takes
    the already_contacted fast path."""

    settings = _settings_stub(tmp_path)
    request = BossGreetJobsRequest(
        jobs=[
            GreetingTarget(
                url="https://www.zhipin.com/job_detail/abc.html",
                company="掘新科技",
                title="AI Agent 工程师",
                job_id="boss:abc",
                greeting="您好，想进一步沟通。",
            )
        ]
    )
    listing = AsyncMock(
        return_value={
            "status": "ok",
            "greetings": [
                {
                    "friendId": 4242,
                    "friendSource": 1,
                    "encryptBossId": "enc-boss",
                    "name": "赖立高",
                    "brandName": "掘新科技",
                    "job_metadata": {
                        "job_id": "abc",
                        "title": "AI Agent 工程师",
                        "company": "掘新科技",
                    },
                }
            ],
        }
    )
    with (
        patch("jobagent.tools.boss_greet.get_boss_cooldown") as cooldown,
        patch.object(
            BossGreetingsManager,
            "_load_profile",
            return_value=SimpleNamespace(name="n", skills=[], years_experience=1, summary="s"),
        ),
        patch.object(BossGreetingsManager, "_load_interview_preference", return_value=None),
        patch("jobagent.auth.cookie_manager.get_cookies", new=AsyncMock(return_value=[])),
        patch("jobagent.applier.boss_chat.list_boss_greetings_http", new=listing),
        patch("jobagent.applier.boss_direct_contact.BossDirectContactAdapter") as contact_cls,
        patch("jobagent.applier.boss_ws.send_text_to_target", new=AsyncMock()) as send,
    ):
        cooldown.return_value.check.return_value = (True, 0, None)
        manager = BossGreetingsManager(settings, registry_path=settings.jobagent_state_db)
        result = await manager.greet(request)

        entry = result["results"][0]
        assert entry["status"] == "submitted"
        assert entry["reason"] == "already_greeted_in_history"
        assert entry["greeting_sent"] is False
        contact_cls.return_value.enter.assert_not_called()
        send.assert_not_awaited()

        result2 = await manager.greet(request)

    assert result2["results"][0]["reason"] == "already_contacted"


@pytest.mark.asyncio
async def test_same_hr_different_job_skips_custom_send(tmp_path) -> None:
    """An HR we already talked to under another job gets no new greeting:
    the entry is submitted (contact stands) with already_contacted_same_hr."""

    settings = _settings_stub(tmp_path)
    request = BossGreetJobsRequest(
        jobs=[
            GreetingTarget(
                url="https://www.zhipin.com/job_detail/new.html",
                company="掘新科技",
                title="大模型后端",
                job_id="boss:new",
                greeting="您好，想进一步沟通。",
            )
        ]
    )
    target = BossConversationTarget(42, 0, "enc", "赖立高", "掘新科技", "大模型后端")
    profile = SimpleNamespace(name="n", skills=[], years_experience=1, summary="s")
    with BossContactRegistry(settings.jobagent_state_db) as contact_registry:
        contact_registry.save_job_transport(
            job_id="boss:new", metadata={"security_id": "s", "lid": "l"}
        )
        contact_registry.save_conversation(
            job_id="boss:old",
            target={
                "friend_id": 42,
                "friend_source": 0,
                "encrypt_boss_id": "enc",
                "name": "赖立高",
                "company": "掘新科技",
                "job_title": "旧岗位",
            },
        )

    with (
        patch("jobagent.tools.boss_greet.get_boss_cooldown") as cooldown,
        patch.object(BossGreetingsManager, "_load_profile", return_value=profile),
        patch.object(BossGreetingsManager, "_load_interview_preference", return_value=None),
        patch("jobagent.auth.cookie_manager.get_cookies", new=AsyncMock(return_value=[])),
        patch(
            "jobagent.applier.boss_chat.list_boss_greetings_http",
            new=AsyncMock(return_value={"status": "ok", "greetings": []}),
        ),
        patch("jobagent.applier.boss_direct_contact.BossDirectContactAdapter") as contact_cls,
        patch("jobagent.applier.boss_ws.send_text_to_target", new=AsyncMock()) as send,
    ):
        cooldown.return_value.check.return_value = (True, 0, None)
        contact_cls.return_value.enter = AsyncMock(
            return_value=SimpleNamespace(
                status="confirmed",
                target=target,
                default_greeting=None,
                error_type=None,
            )
        )
        result = await BossGreetingsManager(
            settings, registry_path=settings.jobagent_state_db
        ).greet(request)

    entry = result["results"][0]
    assert entry["status"] == "submitted"
    assert entry["reason"] == "already_contacted_same_hr"
    assert entry["previously_contacted_job"] == "boss:old"
    send.assert_not_awaited()


def _settings_stub(tmp_path) -> object:
    from jobagent.config import Settings

    return Settings(_env_file=None, jobagent_state_db=tmp_path / "state.db")
