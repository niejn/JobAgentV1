from __future__ import annotations

from jobagent.tools.tailored_resume import TailoredResumeDraft, TailoredResumeStore


def test_tailored_resume_is_versioned_and_requires_explicit_confirmation(tmp_path) -> None:
    store = TailoredResumeStore(tmp_path, chrome_executable=tmp_path / "no-chrome")
    draft = TailoredResumeDraft(
        journey_id="journey-1", company="示例", title="后端",
        resume_markdown="# 简历\n" + "经历\n" * 30,
        fact_source_ids=["candidate-background-v1"],
    )
    first = store.save(draft)
    second = store.save(draft)
    assert first["status"] == "draft"
    assert first["version"] == 1
    assert second["version"] == 2
    assert first["render_status"] == "pdf_renderer_unavailable"
    assert store.confirm(first["artifact_id"])["status"] == "confirmed"
