from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jobagent.boss_reply_service import BossReplyApplicationService
from jobagent.gateway.wechat_channel import BossReplyCommandHandler, WeChatChannel
from jobagent.wechat.ilink import InboundMessage, WeixinAccount


def _draft() -> dict[str, object]:
    return {
        "conversation_id": "c1",
        "friend_id": 42,
        "friend_source": 0,
        "encrypt_boss_id": "boss-42",
        "source_message_ids": ["m1"],
        "company": "示例公司",
        "title": "后端",
        "hr_name": "王女士",
        "hr_message": "最近什么时间方便面试？",
        "draft_text": "我确认后回复您。",
        "risk_level": "high",
        "intent": "interview_time",
    }


@pytest.mark.asyncio
async def test_wechat_and_cli_service_share_versioned_decision(tmp_path) -> None:
    service = BossReplyApplicationService(tmp_path / "state.db")
    reply_id = service.create_draft(**_draft())
    handler = BossReplyCommandHandler(service)
    message = InboundMessage(
        from_user_id="owner",
        text=f"/boss approve {reply_id[:12]} V1",
        context_token="ctx",
        message_id="wechat-1",
    )

    reply = await handler.handle(message)

    assert reply == "已批准并加入发送队列。"
    assert service.list_pending() == []


def test_outbox_is_idempotent_for_one_reply(tmp_path) -> None:
    service = BossReplyApplicationService(tmp_path / "state.db")
    reply_id = service.create_draft(**_draft())
    assert service.sync_pending_notifications() == 0
    notifications = service.pending_notifications("wechat")
    assert len(notifications) == 1
    service.mark_notification(notifications[0].event_id, delivered=True)
    assert service.pending_notifications("wechat") == []
    assert service.resolve(reply_id) is not None


@pytest.mark.asyncio
async def test_wechat_channel_delivers_pending_outbox_to_owner(tmp_path) -> None:
    service = BossReplyApplicationService(tmp_path / "state.db")
    service.create_draft(**_draft())
    account = WeixinAccount(bot_token="token", owner_user_id="owner")
    channel = WeChatChannel(
        account=account,
        allow_user_ids=frozenset({"owner"}),
        notification_source=service,
    )
    channel._context_token_store.set("owner", "ctx-token")
    client = AsyncMock()

    await channel._deliver_notifications(client, channel._context_token_store)

    client.send_text.assert_awaited_once()
    assert service.pending_notifications("wechat") == []
