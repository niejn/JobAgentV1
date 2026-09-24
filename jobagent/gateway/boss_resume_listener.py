"""Ingress adapter that turns Boss WS packets into durable resume requests."""

from __future__ import annotations

from jobagent.applier.boss_ws import find_resume_requests, mqtt_publish_payload
from jobagent.journey.resume_requests import ResumeRequest, ResumeRequestQueue


class BossResumeRequestListener:
    """Keep transport parsing separate from durable request state."""

    #: conversation_id must be the daemon-format id, str(friend.conversationId
    #: or friendId) - resume_requests dedupes on UNIQUE(conversation_id,
    #: source_mid), so the polling feed in boss_daemon and this realtime feed
    #: collapse onto one row per card only when both use the same format.

    def __init__(self, queue: ResumeRequestQueue) -> None:
        self._queue = queue

    def ingest_packet(
        self,
        packet: bytes,
        *,
        conversation_id: str,
        friend_name: str,
        company: str,
        job_title: str,
    ) -> list[ResumeRequest]:
        """Ingest one MQTT packet and return newly observed request rows.

        Duplicate packets are returned as the same durable row, allowing the
        caller to acknowledge transport delivery without creating duplicate
        approval work.
        """

        if not packet or packet[0] >> 4 != 3:
            return []
        payload = mqtt_publish_payload(packet)
        requests = find_resume_requests(payload)
        return [
            self._queue.receive(
                conversation_id=conversation_id,
                source_mid=int(request["mid"]),
                friend_name=friend_name,
                company=company,
                job_title=job_title,
                card_payload=request,
            )
            for request in requests
        ]
