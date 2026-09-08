from __future__ import annotations

import sqlite3
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
        assert conversation_id == "boss:job-1:42"
        assert registry.begin_attempt(
            job_id="boss:job-1", action="greeting", requested_text="你好"
        ).status == "confirmed"


def test_same_friend_linking_two_jobs_does_not_collide(tmp_path: Path) -> None:
    """One HR talking about two jobs: both links must persist (PK bug)."""

    target = {
        "friend_id": 42,
        "friend_source": 0,
        "encrypt_boss_id": "enc-42",
        "name": "李女士",
        "company": "适宇科技",
        "job_title": "AI后端开发工程师（Python）",
    }
    db = tmp_path / "state.db"
    with BossContactRegistry(db) as registry:
        first = registry.save_conversation(job_id="boss:job-1", target=target)
        second = registry.save_conversation(job_id="boss:job-2", target=target)
        # re-saving the first link stays idempotent
        again = registry.save_conversation(job_id="boss:job-1", target=target)

    assert first == "boss:job-1:42"
    assert second == "boss:job-2:42"
    assert again == first
    rows = sqlite3.connect(db).execute(
        "SELECT job_id, id FROM boss_conversations WHERE friend_id = 42 ORDER BY job_id"
    ).fetchall()
    assert rows == [
        ("boss:job-1", "boss:job-1:42"),
        ("boss:job-2", "boss:job-2:42"),
    ]


def test_legacy_friend_only_id_row_keeps_its_stored_id(tmp_path: Path) -> None:
    """Rows written by older versions (id=boss:{friend_id}) keep that id."""

    with sqlite3.connect(tmp_path / "state.db") as legacy:
        legacy.execute(
            """
            CREATE TABLE boss_conversations (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                friend_id INTEGER NOT NULL, friend_source INTEGER NOT NULL,
                encrypt_boss_id TEXT NOT NULL, friend_name TEXT NOT NULL,
                company TEXT NOT NULL, job_title TEXT NOT NULL,
                status TEXT NOT NULL, first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL, UNIQUE(job_id, friend_id)
            )
            """
        )
        legacy.execute(
            "INSERT INTO boss_conversations VALUES"
            " ('boss:42', 'boss:job-1', 42, 0, 'enc-42', '李女士',"
            " '适宇科技', 'AI后端开发工程师', 'active', 1, 1)"
        )

    target = {
        "friend_id": 42,
        "friend_source": 0,
        "encrypt_boss_id": "enc-42",
        "name": "李女士",
        "company": "适宇科技",
        "job_title": "AI后端开发工程师（Python）",
    }
    with BossContactRegistry(tmp_path / "state.db") as registry:
        # the existing link refreshes under its legacy id ...
        assert registry.save_conversation(job_id="boss:job-1", target=target) == "boss:42"
        # ... and a second job no longer trips the legacy PK
        assert registry.save_conversation(job_id="boss:job-2", target=target) == "boss:job-2:42"


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
