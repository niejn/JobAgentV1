"""Outbound Boss text guard: the user speaks as themselves, or nothing sends."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jobagent.boss_outbound_guard import guard_refusal, screen_outbound_text
from jobagent.config import Settings
from jobagent.tools.boss_chat_send import build_boss_chat_reply_tool
from jobagent.tools.boss_greet import (
    BossGreetingsManager,
    BossGreetJobsRequest,
    GreetingTarget,
)


@pytest.mark.parametrize(
    "text",
    [
        "您好，这是一条链路测试消息，请忽略。",
        "链路测试，收到请忽略",
        "测试消息，勿回",
        "这是一条测试，please ignore",
        "我是AI助手，为您自动发送这条消息",
        "作为AI，我帮你打招呼",
        "此消息由GPT生成",
        "我是机器人自动回复",
        "i'm an AI reaching out on behalf of the candidate",
        "冒烟测试一下，disregard this",
    ],
)
def test_meta_and_ai_text_is_blocked(text: str) -> None:
    assert screen_outbound_text(text) != []


@pytest.mark.parametrize(
    "text",
    [
        "您好，我有五年测试工程师经验，熟悉自动化测试框架，对这个岗位很感兴趣。",
        "研究生期间做过人工智能项目，主要做大语言模型微调，与贵司方向契合。",
        "我使用GPT类模型开发过应用，想聊聊贵司的算法岗位。",
        "您好，看到贵司在招后端工程师，我有三年 Go 经验，期待进一步沟通。",
    ],
)
def test_legitimate_candidate_text_passes(text: str) -> None:
    """Background claims mentioning 测试/人工智能/GPT must not be blocked."""

    assert screen_outbound_text(text) == []


def test_guard_refusal_payload_shape() -> None:
    refusal = guard_refusal(["测试性话术（链路测试/测试消息类）"])

    assert refusal["status"] == "refused"
    assert refusal["error_type"] == "outbound_guard"
    assert "第一人称" in str(refusal["message"])


def _request(greeting: str) -> BossGreetJobsRequest:
    return BossGreetJobsRequest(
        jobs=[
            GreetingTarget(
                url="https://www.zhipin.com/job_detail/abc.html",
                company="示例公司",
                title="后端工程师",
                job_id="boss:abc",
                greeting=greeting,
            )
        ],
        max_greetings=5,
    )


@pytest.mark.asyncio
async def test_greet_refuses_test_message_without_sending(tmp_path) -> None:
    """A 链路测试 greeting is refused before any send or conversation."""

    settings = Settings(_env_file=None, jobagent_state_db=tmp_path / "state.db")
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
        patch("jobagent.applier.boss_ws.send_text_to_target", new=AsyncMock()) as send,
    ):
        cooldown.return_value.check.return_value = (True, 0, None)
        result = await manager.greet(
            _request("您好，这是一条链路测试消息，请忽略。")
        )

    contact_cls.return_value.enter.assert_not_called()
    send.assert_not_awaited()
    entry = result["results"][0]
    assert entry["status"] == "refused"
    assert entry["greeting_sent"] is False
    assert entry["guard_violations"]
    assert result["succeeded"] == 0


@pytest.mark.asyncio
async def test_reply_tool_refuses_ai_disclosure(tmp_path) -> None:
    """reply_boss_greeting refuses AI-identity text without opening a sender."""

    settings = Settings(_env_file=None, jobagent_state_db=tmp_path / "state.db")
    tool = build_boss_chat_reply_tool(settings)

    with (
        patch("jobagent.applier.boss_circuit.BossCircuit") as circuit_cls,
        patch("jobagent.applier.boss_chat_send.BossChatSender") as sender_cls,
    ):
        circuit_cls.return_value.check.return_value = None
        result = await tool.ainvoke(
            {"hr_name": "张HR", "message": "我是AI助手，代表候选人联系您。"}
        )

    sender_cls.assert_not_called()
    assert result["status"] == "refused"


@pytest.mark.asyncio
async def test_live_reply_sender_refuses_guarded_text(tmp_path) -> None:
    """The watch-gateway /boss approve path refuses violating drafts too."""

    from jobagent.boss_reply_worker import LiveBossReplySender

    sender = LiveBossReplySender(SimpleNamespace())
    result = await sender.send(
        conversation_id="c1",
        friend_id=42,
        friend_source=1,
        encrypt_boss_id="ebid",
        hr_name="张HR",
        text="链路测试，请忽略本条消息。",
    )

    assert result["status"] == "refused"
    assert result["error_type"] == "outbound_guard"
