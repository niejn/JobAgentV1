"""Tests for the Boss chat archive (idempotent rows, funnel base)."""

from __future__ import annotations

from pathlib import Path

from jobagent.journey.chat_archive import BossChatArchive


def _messages() -> list[dict]:
    return [
        {
            "mid": 38030625355435,
            "direction": "geek",
            "fromId": 49478717,
            "time": 1787987406420,
            "type": 1,
            "text": "贵司的AI Agent岗位还在招人么？",
        },
        {
            "mid": 380306253546242,
            "direction": "boss",
            "fromId": 1490239,
            "time": 1787987406408,
            "type": 4,
            "text": "",
        },
    ]


def test_upsert_history_is_idempotent(tmp_path: Path) -> None:
    with BossChatArchive(tmp_path / "state.db") as archive:
        first = archive.upsert_history(
            friend_id=1490239, friend_name="陈女士", messages=_messages()
        )
        second = archive.upsert_history(
            friend_id=1490239, friend_name="陈女士", messages=_messages()
        )

        assert first == 2  # both new
        assert second == 0  # deduped by msg_id


def test_append_sent_has_no_msg_id(tmp_path: Path) -> None:
    with BossChatArchive(tmp_path / "state.db") as archive:
        archive.append_sent(
            friend_id=1490239,
            friend_name="陈女士",
            text="你好",
            source="ws_sent",
        )
        archive.append_sent(
            friend_id=1490239,
            friend_name="陈女士",
            text="补充",
            source="ws_sent",
        )
        # NULL msg_id rows coexist (no unique clash)
        latest = archive.latest_per_friend()
        assert len(latest) == 1
        assert latest[0]["friend_name"] == "陈女士"
        assert latest[0]["last_direction"] == "geek"


def test_latest_per_friend_orders_by_recency(tmp_path: Path) -> None:
    with BossChatArchive(tmp_path / "state.db") as archive:
        archive.upsert_history(
            friend_id=1, friend_name="A", messages=_messages()
        )
        archive.append_sent(friend_id=2, friend_name="B", text="hi", source="ui_sent")
        latest = archive.latest_per_friend()
        # B's send (now) is newer than A's history (yesterday's timestamps)
        assert latest[0]["friend_name"] == "B"
        assert latest[1]["friend_name"] == "A"


def test_messages_without_mid_are_skipped(tmp_path: Path) -> None:
    with BossChatArchive(tmp_path / "state.db") as archive:
        added = archive.upsert_history(
            friend_id=3,
            friend_name="C",
            messages=[{"mid": 0, "text": "no id", "time": 1}],
        )
        assert added == 0
