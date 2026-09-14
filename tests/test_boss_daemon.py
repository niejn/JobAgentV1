from __future__ import annotations

import pytest

from jobagent.boss_daemon import (
    BossConversationDaemon,
    BossInboundMessage,
    BossMonitorStore,
)


def _message(mid: str, text: str = "可以先线上面试吗？") -> BossInboundMessage:
    return BossInboundMessage(
        conversation_id="c1",
        platform_message_id=mid,
        friend_id=42,
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

    async def poll(self):
        return next(self.batches)

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
