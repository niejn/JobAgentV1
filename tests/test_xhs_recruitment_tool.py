import pytest

from jobagent.journey.recruitment_notes import RecruitmentNoteRegistry
from jobagent.profile.context import CandidateContext, JobSearchProfile
from jobagent.tools.xhs_recruitment import (
    AnalyzeRecruitmentNoteRequest,
    XhsRecruitmentAnalyzer,
    XhsRecruitmentFinder,
    build_analyze_recruitment_note_tool,
)


class FakeSaver:
    async def save(self, request):
        return {
            "status": "completed",
            "note_id": "xhs-1",
            "source_url": request.url,
            "title": "AI 团队招聘",
            "body_text": "招聘 AI Agent 工程师\n要求：Python\n投递 hr@example.com",
            "image_ocr_text": "熟悉 LangGraph",
        }


@pytest.mark.asyncio
async def test_analyze_note_persists_the_existing_xhs_snapshot(tmp_path):
    analyzer = XhsRecruitmentAnalyzer(FakeSaver(), tmp_path / "state.db")
    result = await analyzer.analyze(
        AnalyzeRecruitmentNoteRequest(url="https://www.xiaohongshu.com/explore/xhs-1")
    )

    assert result["status"] == "analyzed"
    assert result["candidate_positions"] == ["AI Agent 工程师"]
    assert result["contacts"][0]["email"] == "hr@example.com"
    with RecruitmentNoteRegistry(tmp_path / "state.db") as registry:
        assert registry.get("xhs-1").features == ("Python", "AI Agent", "LangGraph")


@pytest.mark.asyncio
async def test_analyze_tool_accepts_schema_keyword_arguments(tmp_path):
    tool = build_analyze_recruitment_note_tool(
        XhsRecruitmentAnalyzer(FakeSaver(), tmp_path / "state.db")
    )

    result = await tool.ainvoke({"url": "https://www.xiaohongshu.com/explore/xhs-1"})

    assert result["status"] == "analyzed"


@pytest.mark.asyncio
async def test_finder_uses_profile_and_history_not_caller_keywords(tmp_path):
    class Searcher:
        def __init__(self):
            self.queries = []

        async def search(self, request):
            self.queries.append(request.query)
            return {"status": "ok", "query": request.query, "notes": []}

    searcher = Searcher()
    context = CandidateContext(
        JobSearchProfile(desired_roles=["AI Agent 工程师"], preferred_locations=["上海"]),
        None,
        None,
        None,
    )
    finder = XhsRecruitmentFinder(lambda: context, tmp_path / "state.db", searcher)
    result = await finder.find(type("Request", (), {"limit_per_query": 3})())

    assert result["queries"] == ["上海 AI Agent 工程师 招聘"]
    assert searcher.queries == ["上海 AI Agent 工程师 招聘"]
