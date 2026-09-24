from __future__ import annotations

import time

import pytest

from jobagent.boss_reply_queue import _SCHEMA, BossReplyQueue


def test_queue_migrates_error_column_for_existing_databases(tmp_path) -> None:
    """The orchestration audit marker must not break an old queue database."""

    import sqlite3

    path = tmp_path / "state.db"
    legacy_schema = _SCHEMA.replace("    error TEXT\n", "    legacy_marker TEXT\n")
    with sqlite3.connect(path) as connection:
        connection.executescript(legacy_schema)
    queue = BossReplyQueue(path)
    try:
        columns = {
            row[1] for row in queue._connection.execute("PRAGMA table_info(boss_reply_queue)")
        }
        assert "error" in columns
    finally:
        queue.close()


def test_reply_queue_is_idempotent_and_version_checked(tmp_path) -> None:
    queue = BossReplyQueue(tmp_path / "state.db")
    try:
        item = {
            "reply_id": "r1",
            "conversation_id": "c1",
            "source_message_ids": ["m1"],
            "company": "示例公司",
            "title": "后端",
            "hr_name": "王女士",
            "hr_message": "可以线上面试吗？",
            "draft_text": "可以先线上沟通。",
            "risk_level": "low",
            "intent": "interview_mode",
            "confidence": 0.98,
        }
        assert queue.enqueue(**item) == "r1"
        replay = dict(item)
        replay.pop("reply_id")
        assert queue.enqueue(**replay) == "r1"
        assert len(queue.pending()) == 1
        assert not queue.decide("r1", "approve", draft_version=2)
        assert queue.decide("r1", "approve", draft_version=1)
        assert queue.pending() == []
    finally:
        queue.close()


def test_auto_ready_requires_current_verified_policy(tmp_path) -> None:
    queue = BossReplyQueue(tmp_path / "state.db")
    item = {
        "conversation_id": "c1",
        "source_message_ids": ["m1"],
        "company": "示例公司",
        "title": "后端",
        "hr_name": "王女士",
        "hr_message": "可以线上吗？",
        "draft_text": "可以先线上沟通。",
        "risk_level": "low",
        "intent": "interview_mode",
        "status": "auto_ready",
    }
    try:
        with pytest.raises(ValueError, match="policy"):
            queue.enqueue(**item)
        expiry = time.time() + 3600
        queue.upsert_policy(
            policy_id="online-interview",
            version=1,
            intent="interview_mode",
            authorized_until=expiry,
        )
        reply_id = queue.enqueue(
            **item,
            policy_id="online-interview",
            policy_version=1,
            authorized_until=expiry,
            risk_verified=True,
        )
        assert queue.claim_sendable(owner="worker-1")["reply_id"] == reply_id
    finally:
        queue.close()


def test_claim_is_atomic_and_sending_recovers_as_unverified(tmp_path) -> None:
    queue = BossReplyQueue(tmp_path / "state.db")
    try:
        reply_id = queue.enqueue(
            conversation_id="c1",
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
        claimed = queue.claim_sendable(owner="worker-1")
        assert claimed["reply_id"] == reply_id
        assert queue.claim_sendable(owner="worker-2") is None
        assert queue.recover_stale_sending() == 0
        queue._connection.execute(
            "UPDATE boss_reply_queue SET send_lease_until=0 WHERE reply_id=?",
            (reply_id,),
        )
        queue._connection.commit()
        assert queue.recover_stale_sending() == 1
        assert queue.get(reply_id)["status"] == "unverified"
    finally:
        queue.close()


def test_only_claim_owner_generation_can_finish(tmp_path) -> None:
    queue = BossReplyQueue(tmp_path / "state.db")
    try:
        reply_id = queue.enqueue(
            conversation_id="c1",
            source_message_ids=["m1"],
            company="示例公司",
            title="后端",
            hr_name="王女士",
            hr_message="您好",
            draft_text="您好",
            risk_level="high",
            intent="basic_interest",
        )
        assert queue.decide(reply_id, "approve", draft_version=1)
        item = queue.claim_sendable(owner="worker-1")
        assert not queue.finish(
            reply_id,
            owner="worker-2",
            generation=item["send_generation"],
            status="submitted",
        )
        assert queue.finish(
            reply_id,
            owner="worker-1",
            generation=item["send_generation"],
            status="submitted",
        )
    finally:
        queue.close()


def test_global_rate_limit_blocks_immediate_second_claim(tmp_path) -> None:
    queue = BossReplyQueue(tmp_path / "state.db")
    try:
        for index in range(2):
            reply_id = queue.enqueue(
                conversation_id=f"c{index}",
                source_message_ids=[f"m{index}"],
                company="示例公司",
                title="后端",
                hr_name=f"HR{index}",
                hr_message="您好",
                draft_text="您好",
                risk_level="high",
                intent="basic_interest",
            )
            assert queue.decide(reply_id, "approve", draft_version=1)
        first = queue.claim_sendable(owner="worker-1", global_interval_seconds=60)
        assert queue.finish(
            first["reply_id"],
            owner="worker-1",
            generation=first["send_generation"],
            status="submitted",
        )
        assert (
            queue.claim_sendable(owner="worker-2", global_interval_seconds=60) is None
        )
    finally:
        queue.close()
