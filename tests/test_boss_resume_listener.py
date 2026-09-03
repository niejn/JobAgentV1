from __future__ import annotations

from pathlib import Path

from jobagent.applier.boss_ws import encode_mqtt_publish
from jobagent.gateway.boss_resume_listener import BossResumeRequestListener
from jobagent.journey.resume_requests import ResumeRequestQueue


def _resume_payload() -> bytes:
    # TechwolfMessage: from.uid=7, to.uid=8, mid=99, body.type=9.
    message = (
        bytes.fromhex("0a020807")
        + bytes.fromhex("12020808")
        + bytes.fromhex("2063")
        + bytes.fromhex("32020809")
    )
    return bytes.fromhex("0801") + bytes.fromhex("1a") + bytes([len(message)]) + message


def test_listener_persists_resume_card_idempotently(tmp_path: Path) -> None:
    packet = encode_mqtt_publish(topic="chat", payload=_resume_payload())
    with ResumeRequestQueue(tmp_path / "state.db") as queue:
        listener = BossResumeRequestListener(queue)
        first = listener.ingest_packet(
            packet,
            conversation_id="boss:42",
            friend_name="李女士",
            company="外企德科数字",
            job_title="Python",
        )
        second = listener.ingest_packet(
            packet,
            conversation_id="boss:42",
            friend_name="李女士",
            company="外企德科数字",
            job_title="Python",
        )

        assert [r.id for r in first] == [r.id for r in second]
        assert len(queue.list_pending()) == 1
