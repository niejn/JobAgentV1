from __future__ import annotations

from pathlib import Path

from jobagent.journey.boss_contact import BossContactRegistry


def test_contact_attempt_and_conversation_are_idempotent(tmp_path: Path) -> None:
    target = {
        "friend_id": 42,
        "friend_source": 0,
        "encrypt_boss_id": "enc-42",
        "name": "李女士",
        "company": "适宇科技",
        "job_title": "AI后端开发工程师（Python）",
    }
    with BossContactRegistry(tmp_path / "state.db") as registry:
        first = registry.begin_attempt(
            job_id="boss:job-1", action="greeting", requested_text="你好"
        )
        second = registry.begin_attempt(
            job_id="boss:job-1", action="greeting", requested_text="你好"
        )
        conversation_id = registry.save_conversation(job_id="boss:job-1", target=target)
        registry.finish_attempt(
            first.id,
            status="confirmed",
            result={"greeting_sent": True},
            conversation_id=conversation_id,
        )

        assert first.id == second.id
        assert conversation_id == "boss:42"
        assert registry.begin_attempt(
            job_id="boss:job-1", action="greeting", requested_text="你好"
        ).status == "confirmed"


def test_job_transport_metadata_survives_discovery_for_later_contact(
    tmp_path: Path,
) -> None:
    with BossContactRegistry(tmp_path / "state.db") as registry:
        registry.save_job_transport(
            job_id="boss:job-1",
            metadata={"security_id": "s", "lid": "l", "boss_name": "王媛"},
        )

        assert registry.get_job_transport("boss:job-1") == {
            "security_id": "s",
            "lid": "l",
            "boss_name": "王媛",
        }
