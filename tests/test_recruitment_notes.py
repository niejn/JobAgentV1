from jobagent.journey.recruitment_notes import (
    CommentSnapshot,
    RecruitmentNoteRegistry,
    extract_note,
)


def test_snapshot_keeps_feature_position_and_public_email_provenance(tmp_path):
    note = extract_note(
        "n1",
        "https://www.xiaohongshu.com/explore/n1",
        "AI 团队招聘",
        "招聘 AI Agent 工程师\n职责：负责 Agent 平台\n要求：熟悉 Python\n投递 hr@example.com",
        "要求 LangGraph",
        (
            CommentSnapshot("owner-1", "author", "帖主", "联系 foo@example.com", True),
            CommentSnapshot("visitor-1", "visitor", "游客", "投递 bad@example.com", False),
        ),
    )
    assert note.features == ("Python", "AI Agent", "LangGraph")
    assert note.positions[0].title == "AI Agent 工程师"
    assert note.positions[0].responsibilities == ("职责：负责 Agent 平台",)
    assert note.positions[0].requirements == ("要求：熟悉 Python",)
    assert [(item.email, item.source, item.confidence) for item in note.contacts] == [
        ("hr@example.com", "body", "verified"),
        ("foo@example.com", "author_comment", "review_required"),
    ]
    assert "bad@example.com" not in {item.email for item in note.contacts}
    with RecruitmentNoteRegistry(tmp_path / "state.db") as registry:
        registry.save(note)
        assert registry.get("n1") == note
        assert (
            registry.select_position(note_id="n1", position_index=0, company="示例公司").title
            == "AI Agent 工程师"
        )
