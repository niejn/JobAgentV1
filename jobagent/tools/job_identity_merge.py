"""Agent tools for cross-platform job identity merge review (F1-R4 L3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field


class MergeCandidatesRequest(BaseModel):
    """Find identities under the same company that may need an L3 merge."""

    company: str = Field(min_length=1, description="公司名（任意写法，内部会归一化）")
    title: str = Field(default="", description="当前岗位标题（用于排除自身）")
    limit: int = Field(default=10, ge=1, le=50)


class MergeIdentitiesRequest(BaseModel):
    """Merge two identities after the user confirmed (HITL)."""

    source_key: str = Field(min_length=1, description="被合并的 identity_key")
    target_key: str = Field(
        min_length=1,
        description="合并后保留的 identity_key（journey 状态以更深的一侧为准）",
    )
    rationale: str = Field(
        default="",
        max_length=500,
        description="合并依据（同一岗位的证据：JD 相同/同一招聘方/同一业务线等）",
    )





def build_find_merge_candidates_tool(state_db: Path) -> BaseTool:
    """Expose L3 candidate discovery (read-only, no HITL needed)."""

    async def _run(company: str, title: str = "", limit: int = 10) -> dict[str, Any]:
        from jobagent.journey.job_registry import SQLiteJobRegistry

        with SQLiteJobRegistry(state_db) as registry:
            candidates = registry.find_merge_candidates(company, title, limit=limit)
        return {
            "status": "ok",
            "company": company,
            "count": len(candidates),
            "candidates": list(candidates),
            "hint": (
                "同公司但 identity_key 不同的岗位。若判定为同一岗位，调用 "
                "merge_job_identities 并先向用户展示两侧信息与依据。"
            ),
        }

    return StructuredTool.from_function(
        coroutine=_run,
        name="find_job_merge_candidates",
        description=(
            "查找同公司下可能需要合并的岗位身份（跨平台同岗位/方向写法差异）。"
            "只读操作。返回候选 identity_key 列表供人工审查。"
        ),
        args_schema=MergeCandidatesRequest,
    )


def build_merge_job_identities_tool(state_db: Path) -> BaseTool:
    """Merge two identities behind a hard HITL gate."""

    async def _run(
        source_key: str,
        target_key: str,
        rationale: str = "",
    ) -> dict[str, Any]:
        from jobagent.journey.job_registry import SQLiteJobRegistry

        with SQLiteJobRegistry(state_db) as registry:
            return dict(
                registry.merge_identities(
                    source_key,
                    target_key,
                    user_confirmed=True,  # middleware approved before this runs
                    rationale=rationale,
                )
            )

    return StructuredTool.from_function(
        coroutine=_run,
        name="merge_job_identities",
        description=(
            "合并两个岗位身份（L3 人工确认）：同一岗位在不同平台/不同写法被拆成"
            "两条记录时使用。合并后状态取更深一侧，所有 postings 挂到保留身份下。"
            "执行前暂停等待人工批准。先用 find_job_merge_candidates 找候选。"
        ),
        args_schema=MergeIdentitiesRequest,
    )
