from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.journey.resume_requests import ResumeRequestQueue


def test_receive_is_idempotent_and_lists_pending_request(tmp_path: Path) -> None:
    with ResumeRequestQueue(tmp_path / "state.db") as queue:
        first = queue.receive(
            conversation_id="boss:42",
            source_mid=1001,
            friend_name="李女士",
            company="外企德科数字",
            job_title="Python",
            card_payload={"type": 9},
        )
        second = queue.receive(
            conversation_id="boss:42",
            source_mid=1001,
            friend_name="李女士",
            company="外企德科数字",
            job_title="Python",
            card_payload={"type": 9},
        )

        assert first.id == second.id
        assert first.status == "received"
        assert queue.list_pending()[0].source_mid == 1001


def test_transition_requires_valid_order_and_keeps_resume_selection(tmp_path: Path) -> None:
    with ResumeRequestQueue(tmp_path / "state.db") as queue:
        request = queue.receive(
            conversation_id="boss:42",
            source_mid=1001,
            friend_name="李女士",
            company="外企德科数字",
            job_title="Python",
            card_payload={"type": 9},
        )
        waiting = queue.transition(request.id, "waiting_approval")
        approved = queue.transition(
            waiting.id, "approved", selected_resume_id="resume-v001"
        )

        assert approved.selected_resume_id == "resume-v001"
        with pytest.raises(ValueError, match="invalid resume request transition"):
            queue.transition(approved.id, "confirmed")
