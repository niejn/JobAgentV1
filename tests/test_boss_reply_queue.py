from __future__ import annotations

from jobagent.boss_reply_queue import BossReplyQueue


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
        assert queue.enqueue(**item) == "r1"
        assert len(queue.pending()) == 1
        assert not queue.decide("r1", "approve", draft_version=2)
        assert queue.decide("r1", "approve", draft_version=1)
        assert queue.pending() == []
    finally:
        queue.close()
