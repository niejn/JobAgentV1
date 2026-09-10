import pytest

from jobagent.config import Settings
from jobagent.journey.creation import CreateJourneyRequest, create_journey
from jobagent.journey.recruitment_notes import RecruitmentNoteRegistry, extract_note
from jobagent.journey.xhs_email_drafts import XhsEmailDraftService
from jobagent.tools.resume_library import ResumeLibrary


def test_prepare_draft_requires_selected_position_verified_email_and_pdf(tmp_path):
    database = tmp_path / "state.db"
    with RecruitmentNoteRegistry(database) as notes:
        notes.save(
            extract_note(
                "n", "https://xhs/n", "招聘", "招聘 AI Agent 工程师\n投递 hr@example.com", ""
            )
        )
        notes.select_position(note_id="n", position_index=0, company="示例公司")
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF-1.7\nresume")
    resumes = ResumeLibrary(tmp_path / "resumes")
    resumes.register(str(source))
    service = XhsEmailDraftService(database, resumes, Settings(_env_file=None))
    draft = service.prepare("n", "resume.pdf", "张三")
    assert draft["status"] == "drafted"
    assert draft["to"] == "hr@example.com"
    preview = service.approval_preview(draft["draft_id"])
    assert "收件人：hr@example.com" in preview
    assert "resume.pdf" in preview



def test_prepare_draft_preserves_user_confirmed_subject_and_body_verbatim(tmp_path):
    database = tmp_path / "state.db"
    with RecruitmentNoteRegistry(database) as notes:
        notes.save(
            extract_note(
                "n", "https://xhs/n", "招聘", "招聘 AI Agent 工程师\n投递 hr@example.com", ""
            )
        )
        notes.select_position(note_id="n", position_index=0, company="示例公司")
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF-1.7\nresume")
    resumes = ResumeLibrary(tmp_path / "resumes")
    resumes.register(str(source))
    service = XhsEmailDraftService(database, resumes, Settings(_env_file=None))
    confirmed_subject = "应聘 AI Agent 工程师｜张三"
    confirmed_body = "您好，\n\n正文由用户逐字确认，含换行与  空格，不得改写。\n\n张三"

    draft = service.prepare(
        "n",
        "resume.pdf",
        "张三",
        subject=confirmed_subject,
        body_text=confirmed_body,
    )
    stored = service.get(draft["draft_id"])

    assert draft["status"] == "drafted"
    assert draft["content_origin"] == "user_confirmed"
    assert stored["subject"] == confirmed_subject
    assert stored["body_text"] == confirmed_body
    assert confirmed_body in service.approval_preview(draft["draft_id"])


def test_prepare_draft_falls_back_to_template_when_content_not_confirmed(tmp_path):
    database = tmp_path / "state.db"
    with RecruitmentNoteRegistry(database) as notes:
        notes.save(
            extract_note(
                "n", "https://xhs/n", "招聘", "招聘 AI Agent 工程师\n投递 hr@example.com", ""
            )
        )
        notes.select_position(note_id="n", position_index=0, company="示例公司")
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF-1.7\nresume")
    resumes = ResumeLibrary(tmp_path / "resumes")
    resumes.register(str(source))
    service = XhsEmailDraftService(database, resumes, Settings(_env_file=None))

    draft = service.prepare("n", "resume.pdf", "张三")

    assert draft["subject"] == "应聘 AI Agent 工程师｜张三"
    assert draft["body_text"] == (
        "您好，\n\n我想应聘贵司 AI Agent 工程师 岗位，附件为我的简历，期待交流。\n\n张三"
    )
    assert draft["content_origin"] == "template"


def test_prepare_draft_requires_note_saved_before_position_selected_before_draft(tmp_path):
    """Pipeline order is fail-closed: each step needs the previous step's artifact."""

    database = tmp_path / "state.db"
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF-1.7\nresume")
    resumes = ResumeLibrary(tmp_path / "resumes")
    resumes.register(str(source))
    service = XhsEmailDraftService(database, resumes, Settings(_env_file=None))

    unsaved = service.prepare("never-saved", "resume.pdf", "张三")
    assert unsaved == {"status": "failed", "error_type": "note_not_saved"}

    with RecruitmentNoteRegistry(database) as notes:
        notes.save(
            extract_note(
                "n", "https://xhs/n", "招聘", "招聘 AI Agent 工程师\n投递 hr@example.com", ""
            )
        )
    unselected = service.prepare("n", "resume.pdf", "张三")
    assert unselected == {"status": "failed", "error_type": "position_not_selected"}


def test_approval_preview_shows_post_url_and_contact_provenance(tmp_path):
    database = tmp_path / "state.db"
    with RecruitmentNoteRegistry(database) as notes:
        notes.save(
            extract_note(
                "n",
                "https://www.xiaohongshu.com/explore/n",
                "招聘",
                "招聘 AI Agent 工程师\n投递 zhiqiu.lin@moodio.art",
                "",
            )
        )
        notes.select_position(note_id="n", position_index=0, company="Moodio")
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF-1.7\nresume")
    resumes = ResumeLibrary(tmp_path / "resumes")
    resumes.register(str(source))
    service = XhsEmailDraftService(database, resumes, Settings(_env_file=None))

    draft = service.prepare("n", "resume.pdf", "张三")
    preview = service.approval_preview(draft["draft_id"])

    assert draft["to"] == "zhiqiu.lin@moodio.art"
    assert draft["contact_evidence"] == {"source": "body", "confidence": "verified"}
    assert "帖子链接：https://www.xiaohongshu.com/explore/n" in preview
    assert "邮箱出处：body（置信 verified）" in preview


class FakeSender:
    def __init__(self, result):
        self.result, self.calls = result, []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.mark.asyncio
async def test_submitted_xhs_draft_updates_stable_registry_and_blocks_repeat(tmp_path):
    database = tmp_path / "state.db"
    with RecruitmentNoteRegistry(database) as notes:
        notes.save(
            extract_note(
                "n",
                "https://xhs/n",
                "招聘",
                "招聘 AI Agent 工程师\n投递 hr@example.com",
                "",
            )
        )
        notes.select_position(note_id="n", position_index=0, company="示例公司")
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF-1.7\nresume")
    resumes = ResumeLibrary(tmp_path / "resumes")
    resumes.register(str(source))
    sender = FakeSender({"status": "ok", "message_id": "<test@example>"})
    service = XhsEmailDraftService(database, resumes, Settings(_env_file=None), sender=sender)
    journey, _ = create_journey(
        database,
        CreateJourneyRequest(company="示例公司", role="AI Agent 工程师", source_job_id="xhs:n"),
    )
    draft = service.prepare("n", "resume.pdf", "张三")
    sent = await service.send(draft["draft_id"])
    repeated = await service.send(draft["draft_id"])
    assert sent["status"] == "submitted"
    assert sent["receipt"]["message_id"] == sender.calls[0]["message_id"]
    assert repeated["error_type"] == "already_submitted"
    assert len(sender.calls) == 1
    from jobagent.journey.store import SQLiteJourneyStore
    with SQLiteJourneyStore(database) as store:
        assert store.get_journey(journey.id).stage == "applied"


@pytest.mark.asyncio
async def test_unverified_delivery_does_not_mark_applied(tmp_path):
    database = tmp_path / "state.db"
    with RecruitmentNoteRegistry(database) as notes:
        notes.save(
            extract_note(
                "n", "https://xhs/n", "招聘", "招聘 AI Agent 工程师\n投递 hr@example.com", ""
            )
        )
        notes.select_position(note_id="n", position_index=0, company="示例公司")
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF-1.7\nresume")
    resumes = ResumeLibrary(tmp_path / "resumes")
    resumes.register(str(source))
    sender = FakeSender({"status": "unverified", "error_type": "network_uncertain"})
    service = XhsEmailDraftService(database, resumes, Settings(_env_file=None), sender=sender)
    draft = service.prepare("n", "resume.pdf", "张三")
    result = await service.send(draft["draft_id"])
    assert result["status"] == "unverified"


@pytest.mark.asyncio
async def test_failed_delivery_does_not_mark_applied(tmp_path):
    database = tmp_path / "state.db"
    with RecruitmentNoteRegistry(database) as notes:
        notes.save(
            extract_note(
                "n",
                "https://xhs/n",
                "招聘",
                "招聘 AI Agent 工程师\n投递 hr@example.com",
                "",
            )
        )
        notes.select_position(note_id="n", position_index=0, company="示例公司")
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF-1.7\nresume")
    resumes = ResumeLibrary(tmp_path / "resumes")
    resumes.register(str(source))
    service = XhsEmailDraftService(
        database,
        resumes,
        Settings(_env_file=None),
        sender=FakeSender({"status": "failed", "error_type": "auth_failed"}),
    )
    draft = service.prepare("n", "resume.pdf", "张三")
    assert (await service.send(draft["draft_id"]))["status"] == "failed"
