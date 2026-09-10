from jobagent.journey.xhs_query_planner import plan_queries
from jobagent.profile.context import JobSearchProfile


def test_plan_uses_only_profile_and_xhs_history_features():
    profile = JobSearchProfile(
        desired_roles=["AI Agent 工程师", "Python 开发"], preferred_locations=["上海"]
    )
    assert plan_queries(profile, ("LangGraph", "RAG")) == (
        "上海 AI Agent 工程师 招聘",
        "LangGraph 招聘",
        "Python 开发 招人",
    )
