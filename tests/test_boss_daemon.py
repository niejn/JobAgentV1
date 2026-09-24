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


class FakeSink:
    def __init__(self) -> None:
        self.messages = []

    def publish_inbound(self, messages) -> None:
        self.messages.extend(messages)


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
    sink = FakeSink()
    daemon = BossConversationDaemon(adapter, store, event_sink=sink)

    first = await daemon.run_once()
    second = await daemon.run_once()

    assert first == {
        "status": "ok",
        "baseline": True,
        "fetched": 1,
        "new_messages": 0,
        "resume_requests": 0,
    }
    assert second == {
        "status": "ok",
        "baseline": False,
        "fetched": 2,
        "new_messages": 1,
        "resume_requests": 0,
    }
    rows = store.unclassified()
    assert [row["platform_message_id"] for row in rows] == ["m2"]
    assert [message.platform_message_id for message in sink.messages] == ["m2"]
    await daemon.close()


def _card_message(mid: str = "389163931374337") -> BossInboundMessage:
    """The TCL 2026-09-23 shape: HR resume-request card, no body text."""

    return BossInboundMessage(
        conversation_id="c1",
        platform_message_id=mid,
        friend_id=42,
        friend_source=0,
        encrypt_boss_id="boss-42",
        hr_name="康先生",
        company="TCL实业",
        title="AIGC应用开发工程师",
        text="[简历请求卡片]",
        sent_at=200,
        raw={"mid": mid, "type": 9, "text": "[简历请求卡片]", "direction": "boss"},
    )


@pytest.mark.asyncio
async def test_daemon_feeds_resume_cards_into_queue_idempotently(tmp_path) -> None:
    from jobagent.journey.resume_requests import ResumeRequestQueue

    db = tmp_path / "state.db"
    store = BossMonitorStore(db)
    adapter = FakeAdapter([[_card_message()], [_card_message()]])
    queue = ResumeRequestQueue(db)
    daemon = BossConversationDaemon(adapter, store, resume_queue=queue)

    first = await daemon.run_once()
    replay = await daemon.run_once()

    assert first["resume_requests"] == 1
    assert replay["resume_requests"] == 0  # mid dedupe: no second row, no crash
    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0].source_mid == 389163931374337
    assert pending[0].friend_name == "康先生"
    assert pending[0].company == "TCL实业"
    await daemon.close()
    queue.close()


@pytest.mark.asyncio
async def test_daemon_without_queue_ignores_resume_cards(tmp_path) -> None:
    store = BossMonitorStore(tmp_path / "state.db")
    adapter = FakeAdapter([[], [_card_message()]])
    daemon = BossConversationDaemon(adapter, store)  # resume_queue default None

    await daemon.run_once()  # baseline sweep settles the cold-start round
    result = await daemon.run_once()  # live poll delivers the card

    assert result["new_messages"] == 1
    assert result["resume_requests"] == 0  # no queue wired: nothing to feed
    assert [row["text"] for row in store.unclassified()] == ["[简历请求卡片]"]
    await daemon.close()


@pytest.mark.asyncio
async def test_daemon_skips_resume_card_without_numeric_mid(tmp_path) -> None:
    from jobagent.journey.resume_requests import ResumeRequestQueue

    db = tmp_path / "state.db"
    store = BossMonitorStore(db)
    adapter = FakeAdapter([[_card_message(mid="")]])
    queue = ResumeRequestQueue(db)
    daemon = BossConversationDaemon(adapter, store, resume_queue=queue)

    result = await daemon.run_once()

    assert result["resume_requests"] == 0
    assert queue.list_pending() == []
    await daemon.close()
    queue.close()


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
async def test_live_adapter_tracks_update_time_when_last_message_absent() -> None:
    """HTTP transport listings carry no lastMessage; updateTime is the signal."""
    listing = {
        "status": "ok",
        "greetings": [
            {
                "friendId": 42,
                "name": "陈女士",
                "brandName": "云片",
                "updateTime": 200,
            }
        ],
    }
    history = {
        "status": "ok",
        "messages": [
            {"mid": "old", "fromId": 42, "time": 100, "text": "历史消息"},
            {"mid": "new", "fromId": 42, "time": 200, "text": "新回复"},
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
            {"42": BossConversationCursor("boss:42:t100", 100)}, baseline=False
        )

    assert [message.platform_message_id for message in batch.messages] == ["new"]
    assert all(not message.baseline for message in batch.messages)
    assert batch.cursors["42"] == BossConversationCursor("boss:42:t200", 200)


@pytest.mark.asyncio
async def test_live_adapter_marks_first_seen_history_as_baseline() -> None:
    """First-seen conversations carry pre-monitor history → cold-start rows."""
    listing = {
        "status": "ok",
        "greetings": [
            {"friendId": 42, "name": "陈女士", "updateTime": 200},
            {"friendId": 43, "name": "康先生", "updateTime": 100},
        ],
    }

    class Reader:
        def __init__(self, messages):
            self.messages = messages
            self.read_friend_ids = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def read_conversation(self, *, friend_id, **kwargs):
            self.read_friend_ids.append(friend_id)
            return {
                "status": "ok",
                "messages": self.messages.get(friend_id, []),
            }

    reader = Reader(
        {
            42: [{"mid": "h1", "fromId": 42, "time": 200, "text": "老消息"}],
            43: [{"mid": "h2", "fromId": 43, "time": 100, "text": "另一条老消息"}],
        }
    )
    with (
        patch(
            "jobagent.applier.boss_chat.list_boss_greetings_http",
            new=AsyncMock(return_value=listing),
        ),
        patch("jobagent.applier.boss_chat.BossChatReader", return_value=reader),
    ):
        adapter = LiveBossConversationAdapter(
            Settings(_env_file=None), history_pull_cap=1
        )
        batch = await adapter.poll({}, baseline=False)

    # Cap 1: only the newest conversation was read this poll.
    assert reader.read_friend_ids == [42]
    assert [message.platform_message_id for message in batch.messages] == ["h1"]
    assert all(message.baseline for message in batch.messages)
    assert set(batch.cursors) == {"42"}


@pytest.mark.asyncio
async def test_live_adapter_stops_sweep_after_api_rejection() -> None:
    """One api_rejected means the page budget is gone; don't hammer the rest."""
    listing = {
        "status": "ok",
        "greetings": [
            {"friendId": 51, "name": "甲", "updateTime": 300},
            {"friendId": 52, "name": "乙", "updateTime": 200},
            {"friendId": 53, "name": "丙", "updateTime": 100},
        ],
    }

    class RejectingReader:
        def __init__(self):
            self.read_count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def read_conversation(self, **kwargs):
            self.read_count += 1
            if self.read_count == 1:
                return {"status": "failed", "error_type": "api_rejected"}
            return {"status": "ok", "messages": []}

    reader = RejectingReader()
    with (
        patch(
            "jobagent.applier.boss_chat.list_boss_greetings_http",
            new=AsyncMock(return_value=listing),
        ),
        patch("jobagent.applier.boss_chat.BossChatReader", return_value=reader),
    ):
        adapter = LiveBossConversationAdapter(Settings(_env_file=None))
        batch = await adapter.poll({}, baseline=False)

    assert reader.read_count == 1
    assert batch.messages == []
    assert batch.cursors == {}


@pytest.mark.asyncio
async def test_cold_start_rows_do_not_notify_or_count_as_new(tmp_path) -> None:
    store = BossMonitorStore(tmp_path / "state.db")
    cold = BossInboundMessage(
        conversation_id="c9",
        platform_message_id="old1",
        friend_id=9,
        friend_source=0,
        encrypt_boss_id="boss-9",
        hr_name="陈女士",
        company="云片",
        title="后端",
        text="历史消息",
        sent_at=100,
        raw={"mid": "old1"},
        baseline=True,
    )
    live = BossInboundMessage(
        conversation_id="c9",
        platform_message_id="new1",
        friend_id=9,
        friend_source=0,
        encrypt_boss_id="boss-9",
        hr_name="陈女士",
        company="云片",
        title="后端",
        text="新消息",
        sent_at=200,
        raw={"mid": "new1"},
    )

    class MixedAdapter:
        async def poll(self, cursors, *, baseline):
            return BossPollBatch(
                messages=[cold, live],
                cursors={
                    "c9": BossConversationCursor("head", 200),
                },
            )

        async def close(self):
            return None

    sink = FakeSink()
    daemon = BossConversationDaemon(MixedAdapter(), store, event_sink=sink)
    # Seed the store as an already-initialized monitor (baseline done) without
    # holding the lease the daemon must acquire.
    store._connection.execute(  # noqa: SLF001 - seed baseline_complete
        "INSERT OR REPLACE INTO boss_monitor_state(key, value) "
        "VALUES('baseline_complete', CURRENT_TIMESTAMP)"
    )
    store._connection.commit()

    result = await daemon.run_once()

    assert result["new_messages"] == 1
    assert [m.platform_message_id for m in sink.messages] == ["new1"]
    assert [row["platform_message_id"] for row in store.unclassified()] == ["new1"]
    baseline_rows = store._connection.execute(  # noqa: SLF001 - verify cold-start rows
        "SELECT platform_message_id FROM boss_inbound_messages WHERE baseline=1"
    ).fetchall()
    assert [row["platform_message_id"] for row in baseline_rows] == ["old1"]
    await daemon.close()


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


def test_monitor_status_distinguishes_uninitialized_store(tmp_path) -> None:
    from jobagent.boss_reply_service import BossReplyApplicationService

    service = BossReplyApplicationService(tmp_path / "state.db")
    assert service.monitor_status() == {
        "initialized": False,
        "baseline_completed_at": None,
    }
    assert service.list_inbox() == []

    store = BossMonitorStore(tmp_path / "state.db")
    store._connection.execute(  # noqa: SLF001 - what the daemon persists on baseline
        "INSERT OR REPLACE INTO boss_monitor_state(key, value) "
        "VALUES('baseline_complete', CURRENT_TIMESTAMP)"
    )
    store._connection.commit()
    store.close()
    assert service.monitor_status()["initialized"] is True
