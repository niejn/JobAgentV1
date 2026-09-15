from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jobagent.boss_reply_queue import BossReplyQueue
from jobagent.boss_reply_worker import BossReplyWorker


@pytest.mark.asyncio
async def test_worker_sends_one_approved_reply(tmp_path) -> None:
    queue = BossReplyQueue(tmp_path / "state.db")
    reply_id = queue.enqueue(
        conversation_id="c1",
        friend_id=42,
        friend_source=0,
        encrypt_boss_id="boss-42",
        source_message_ids=["m1"],
        company="示例公司",
        title="后端",
        hr_name="王女士",
        hr_message="可以线上吗？",
        draft_text="可以先线上沟通。",
        risk_level="low",
        intent="interview_mode",
    )
    assert queue.decide(reply_id, "approve", draft_version=1)
    sender = AsyncMock()
    sender.send.return_value = {"status": "ok"}
    worker = BossReplyWorker(queue, sender)

    assert await worker.run_once() == {"status": "submitted", "reply_id": reply_id}
    sender.send.assert_awaited_once_with(
        conversation_id="c1",
        friend_id=42,
        friend_source=0,
        encrypt_boss_id="boss-42",
        hr_name="王女士",
        text="可以先线上沟通。",
    )
    assert queue.get(reply_id)["status"] == "submitted"
    queue.close()


@pytest.mark.asyncio
async def test_worker_exception_is_unverified_not_retried(tmp_path) -> None:
    queue = BossReplyQueue(tmp_path / "state.db")
    reply_id = queue.enqueue(
        conversation_id="c1",
        friend_id=42,
        friend_source=0,
        encrypt_boss_id="boss-42",
        source_message_ids=["m1"],
        company="示例公司",
        title="后端",
        hr_name="王女士",
        hr_message="可以线上吗？",
        draft_text="可以。",
        risk_level="low",
        intent="interview_mode",
    )
    assert queue.decide(reply_id, "approve", draft_version=1)
    sender = AsyncMock()
    sender.send.side_effect = ConnectionError("closed after publish")
    worker = BossReplyWorker(queue, sender)

    assert (await worker.run_once())["status"] == "unverified"
    assert await worker.run_once() == {"status": "idle"}
    queue.close()
