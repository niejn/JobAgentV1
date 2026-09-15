from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from jobagent.boss_daemon import (
    BossConversationCursor,
    BossConversationDaemon,
    BossInboundMessage,
    BossMonitorStore,
    BossPollBatch,
    LiveBossConversationAdapter,
)
from jobagent.config import Settings


def _message(mid: str, text: str = "可以先线上面试吗？") -> BossInboundMessage:
    return BossInboundMessage(
        conversation_id="c1",
        platform_message_id=mid,
        friend_id=42,
        friend_source=0,
        encrypt_boss_id="boss-42",
        hr_name="王女士",
        company="示例公司",
        title="后端工程师",
        text=text,
        sent_at=100,
        raw={"mid": mid, "text": text, "direction": "boss"},
    )


class FakeAdapter:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.closed = False

    async def poll(self, cursors, *, baseline):
        messages = next(self.batches)
        return BossPollBatch(
            messages=messages,
            cursors={
                message.conversation_id: BossConversationCursor(
                    message.message_key, message.sent_at
                )
                for message in messages
            },
        )

    async def close(self):
        self.closed = True


def test_monitor_store_lease_is_single_owner(tmp_path) -> None:
    first = BossMonitorStore(tmp_path / "state.db")
    second = BossMonitorStore(tmp_path / "state.db")
    try:
        assert first.acquire_lease("one", ttl_seconds=60)
        assert not second.acquire_lease("two", ttl_seconds=60)
        first.release_lease("one")
        assert second.acquire_lease("two", ttl_seconds=60)
    finally:
        first.close()
        second.close()


def test_expired_owner_cannot_commit_after_lease_takeover(tmp_path) -> None:
    first = BossMonitorStore(tmp_path / "state.db")
    second = BossMonitorStore(tmp_path / "state.db")
    try:
        generation_one = first.acquire_lease("one", ttl_seconds=60)
        assert generation_one is not None
        first._connection.execute(
            "UPDATE boss_monitor_leases SET expires_at=0 WHERE name='monitor'"
        )
        generation_two = second.acquire_lease("two", ttl_seconds=60)
        assert generation_two is not None and generation_two > generation_one
        with pytest.raises(RuntimeError, match="lease lost"):
            first.record(
                [_message("m1")],
                cursors={},
                baseline=False,
                owner="one",
                generation=generation_one,
            )
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_first_scan_is_baseline_and_second_scan_emits_only_new(tmp_path) -> None:
    store = BossMonitorStore(tmp_path / "state.db")
    adapter = FakeAdapter([[_message("m1")], [_message("m1"), _message("m2")]])
    daemon = BossConversationDaemon(adapter, store)

    first = await daemon.run_once()
    second = await daemon.run_once()

    assert first == {"status": "ok", "baseline": True, "fetched": 1, "new_messages": 0}
    assert second == {"status": "ok", "baseline": False, "fetched": 2, "new_messages": 1}
    rows = store.unclassified()
    assert [row["platform_message_id"] for row in rows] == ["m2"]
    await daemon.close()


@pytest.mark.asyncio
async def test_replayed_message_is_not_inserted_twice(tmp_path) -> None:
    store = BossMonitorStore(tmp_path / "state.db")
    adapter = FakeAdapter([[], [_message("m1")], [_message("m1")]])
    daemon = BossConversationDaemon(adapter, store)

    await daemon.run_once()
    assert (await daemon.run_once())["new_messages"] == 1
    assert (await daemon.run_once())["new_messages"] == 0
    await daemon.close()


@pytest.mark.asyncio
async def test_live_adapter_fetches_all_new_history_messages_after_head_changes(
    tmp_path,
) -> None:
    listing = {
        "status": "ok",
        "greetings": [
            {
                "friendId": 42,
                "name": "王女士",
                "brandName": "示例公司",
                "jobName": "后端",
                "lastMessage": {"mid": "m3", "fromId": 42, "time": 103, "text": "第三条"},
            }
        ],
    }
    history = {
        "status": "ok",
        "messages": [
            {"mid": "m1", "fromId": 42, "time": 101, "text": "第一条"},
            {"mid": "m2", "fromId": 42, "time": 102, "text": "第二条"},
            {"mid": "self", "fromId": 1, "time": 102, "text": "我的回复"},
            {"mid": "m3", "fromId": 42, "time": 103, "text": "第三条"},
        ],
    }

    class Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def read_conversation(self, **kwargs):
            return history

    with (
        patch(
            "jobagent.applier.boss_chat.list_boss_greetings_http",
            new=AsyncMock(return_value=listing),
        ),
        patch("jobagent.applier.boss_chat.BossChatReader", return_value=Reader()),
    ):
        adapter = LiveBossConversationAdapter(Settings(_env_file=None))
        batch = await adapter.poll(
            {"42": BossConversationCursor("old", 100)}, baseline=False
        )

    assert [message.platform_message_id for message in batch.messages] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_live_adapter_drops_message_with_unknown_direction() -> None:
    listing = {
        "status": "ok",
        "greetings": [
            {
                "friendId": 42,
                "name": "王女士",
                "lastMessage": {"mid": "m1", "time": 101, "text": "方向未知"},
            }
        ],
    }
    with patch(
        "jobagent.applier.boss_chat.list_boss_greetings_http",
        new=AsyncMock(return_value=listing),
    ):
        adapter = LiveBossConversationAdapter(Settings(_env_file=None))
        batch = await adapter.poll({}, baseline=True)
    assert batch.messages == []
