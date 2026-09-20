from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.applier.boss_resume_delivery import BossResumeDelivery
from jobagent.config import Settings
from jobagent.tools.boss_resume_delivery import (
    build_prepare_boss_resume_after_hr_reply_tool,
    build_send_boss_resume_after_hr_reply_tool,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        jobagent_state_db=tmp_path / "state.db",
        jobagent_checkpoint_db=tmp_path / "checkpoint.db",
    )


class FakeDelivery(BossResumeDelivery):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(_settings(tmp_path))
        self.payloads: list[dict[str, str]] = []
        self.send_result = {
            "step": "done",
            "acceptStatus": "success",
            "refreshCode": 0,
            "historyCode": 0,
        }
        # When set, preflight evaluations return this resume list instead.
        self.refresh_resumes: list[dict] | None = None

    async def _evaluate(self, script: str, payload: dict[str, str]):  # type: ignore[override]
        self.payloads.append(payload)
        if "exchange/accept" in script:
            return self.send_result
        default_resumes = [
            {
                "optionId": "resume_0",
                "encryptResumeId": "encrypted-resume-id",
                "fileName": "李明_AI Agent工程师.pdf",
                "annexType": 0,
                "uploadTime": 1,
                "restricted": False,
                "restrictedLabel": "",
                "securityStatus": "normal",
            },
            {
                "optionId": "resume_1",
                "encryptResumeId": "restricted-id",
                "fileName": "李明_旧简历.pdf",
                "annexType": 0,
                "uploadTime": 1,
                "restricted": True,
                "restrictedLabel": "不可投递",
                "securityStatus": "restricted",
            },
        ]
        return {
            "step": "ready",
            "friendId": "20001",
            "hrName": "陈女士",
            "company": "四川影目",
            "jobTitle": "AI Agent 工程师",
            "securityId": "secret",
            "bossId": "boss-secret",
            "mid": "383439670067459",
            "type": "1",
            "resumes": self.refresh_resumes
            if self.refresh_resumes is not None
            else default_resumes,
        }


@pytest.mark.asyncio
async def test_prepare_lists_filenames_but_hides_platform_credentials(tmp_path: Path) -> None:
    manager = FakeDelivery(tmp_path)

    prepared = await manager.prepare("20001")

    assert prepared["status"] == "ready"
    assert prepared["hr_name"] == "陈女士"
    assert prepared["resume_options"] == [
        {
            "resume_option_id": "resume_0",
            "file_name": "李明_AI Agent工程师.pdf",
            "resume_type": "附件简历",
            "uploaded_at": 1,
            "restricted": False,
            "restricted_reason": "",
            "selectable": True,
        },
        {
            "resume_option_id": "resume_1",
            "file_name": "李明_旧简历.pdf",
            "resume_type": "附件简历",
            "uploaded_at": 1,
            "restricted": True,
            "restricted_reason": "不可投递",
            "selectable": False,
        },
    ]
    assert "securityId" not in str(prepared)
    assert "encrypted-resume-id" not in str(prepared)


@pytest.mark.asyncio
async def test_send_requires_exact_selected_filename_and_returns_receipt(tmp_path: Path) -> None:
    manager = FakeDelivery(tmp_path)
    prepared = await manager.prepare("20001")

    mismatched = await manager.send(
        delivery_id=prepared["delivery_id"],
        resume_option_id="resume_0",
        resume_file_name="另一份.pdf",
        hr_name="陈女士",
        company="四川影目",
        job_title="AI Agent 工程师",
    )
    assert mismatched["error_type"] == "resume_filename_mismatch"

    sent = await manager.send(
        delivery_id=prepared["delivery_id"],
        resume_option_id="resume_0",
        resume_file_name="李明_AI Agent工程师.pdf",
        hr_name="陈女士",
        company="四川影目",
        job_title="AI Agent 工程师",
    )
    assert sent["status"] == "confirmed"
    assert sent["resume_file_name"] == "李明_AI Agent工程师.pdf"
    assert manager.payloads[-1]["encryptResumeId"] == "encrypted-resume-id"


@pytest.mark.asyncio
async def test_expired_delivery_reprepares_and_sends(tmp_path: Path) -> None:
    """HITL approval routinely outlives the 10-min preflight TTL: send must
    re-run the read-only preflight itself instead of failing (which drove
    agents to script prepare+send as one bypass)."""

    manager = FakeDelivery(tmp_path)
    prepared = await manager.prepare("20001")
    # Age the preflight past the TTL without waiting.
    for record in manager._prepared.values():
        object.__setattr__(record, "created_at", record.created_at - 700)

    sent = await manager.send(
        delivery_id=prepared["delivery_id"],
        resume_option_id="resume_0",
        resume_file_name="李明_AI Agent工程师.pdf",
        hr_name="陈女士",
        company="四川影目",
        job_title="AI Agent 工程师",
    )

    assert sent["status"] == "confirmed"
    assert sent["reprepared_after_expiry"] is True
    # Two prepare evaluations (initial + refresh) plus one send evaluation.
    assert len([p for p in manager.payloads if "friendId" in p]) == 2
    assert manager.payloads[-1]["encryptResumeId"] == "encrypted-resume-id"


@pytest.mark.asyncio
async def test_expired_delivery_reports_when_selection_became_unsendable(
    tmp_path: Path,
) -> None:
    manager = FakeDelivery(tmp_path)
    prepared = await manager.prepare("20001")
    for record in manager._prepared.values():
        object.__setattr__(record, "created_at", record.created_at - 700)
    # The refresh drops the previously selected resume entirely.
    manager.refresh_resumes = [
        {
            "optionId": "resume_x",
            "encryptResumeId": "other-id",
            "fileName": "新简历.pdf",
            "annexType": 1,
            "restricted": False,
        }
    ]

    sent = await manager.send(
        delivery_id=prepared["delivery_id"],
        resume_option_id="resume_0",
        resume_file_name="李明_AI Agent工程师.pdf",
        hr_name="陈女士",
        company="四川影目",
        job_title="AI Agent 工程师",
    )

    assert sent["status"] == "failed"
    assert sent["error_type"] == "delivery_expired_options_changed"
    assert sent["resume_options"]


@pytest.mark.asyncio
async def test_send_marks_unverified_when_refresh_does_not_confirm(tmp_path: Path) -> None:
    manager = FakeDelivery(tmp_path)
    prepared = await manager.prepare("20001")
    manager.send_result = {
        "step": "done",
        "acceptStatus": "success",
        "refreshCode": 1,
        "historyCode": 0,
    }

    result = await manager.send(
        delivery_id=prepared["delivery_id"],
        resume_option_id="resume_0",
        resume_file_name="李明_AI Agent工程师.pdf",
        hr_name="陈女士",
        company="四川影目",
        job_title="AI Agent 工程师",
    )

    assert result["status"] == "unverified"
    assert result["error_type"] == "refresh_unconfirmed"


@pytest.mark.asyncio
async def test_confirmed_delivery_is_idempotent_for_same_hr_reply_and_resume(
    tmp_path: Path,
) -> None:
    manager = FakeDelivery(tmp_path)
    first = await manager.prepare("20001")
    first_result = await manager.send(
        delivery_id=first["delivery_id"],
        resume_option_id="resume_0",
        resume_file_name="李明_AI Agent工程师.pdf",
        hr_name="陈女士",
        company="四川影目",
        job_title="AI Agent 工程师",
    )
    assert first_result["status"] == "confirmed"

    repeated = await manager.prepare("20001")
    repeated_result = await manager.send(
        delivery_id=repeated["delivery_id"],
        resume_option_id="resume_0",
        resume_file_name="李明_AI Agent工程师.pdf",
        hr_name="陈女士",
        company="四川影目",
        job_title="AI Agent 工程师",
    )

    assert repeated_result == {
        "status": "blocked",
        "error_type": "resume_already_delivered",
    }


@pytest.mark.asyncio
async def test_tools_keep_prepare_read_only_and_send_separate(tmp_path: Path) -> None:
    manager = FakeDelivery(tmp_path)
    prepare_tool = build_prepare_boss_resume_after_hr_reply_tool(manager)
    send_tool = build_send_boss_resume_after_hr_reply_tool(manager)

    prepared = await prepare_tool.ainvoke({"conversation_id": "20001"})

    assert prepare_tool.name == "prepare_boss_resume_after_hr_reply"
    assert send_tool.name == "send_boss_resume_after_hr_reply"
    assert prepared["status"] == "ready"
