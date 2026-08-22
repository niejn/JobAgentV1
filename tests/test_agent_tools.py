"""Tests for safe JobAgent business Tool interfaces."""

from __future__ import annotations

from typing import Any

import pytest

from jobagent.tools.interview_evidence import (
    InterviewEvidenceTarget,
    build_interview_evidence_tool,
)


class FakeDiscovery:
    def __init__(self) -> None:
        self.target: InterviewEvidenceTarget | None = None

    async def discover(self, target: InterviewEvidenceTarget) -> dict[str, Any]:
        self.target = target
        return {"status": "completed", "accepted": []}


class TimeoutDiscovery:
    async def discover(self, target: InterviewEvidenceTarget) -> dict[str, Any]:
        raise TimeoutError("Interview research exceeded 300s total timeout")


@pytest.mark.asyncio
async def test_interview_tool_exposes_business_inputs_not_crawler_controls() -> None:
    discovery = FakeDiscovery()
    tool = build_interview_evidence_tool(discovery)  # type: ignore[arg-type]

    result = await tool.ainvoke(
        {
            "company": "字节跳动",
            "role": "后端开发",
            "city": "北京",
            "job_description": "负责分布式服务",
        }
    )

    assert result["status"] == "completed"
    assert discovery.target is not None
    assert discovery.target.company == "字节跳动"
    schema_fields = tool.args_schema.model_fields
    assert "limit" not in schema_fields
    assert "download" not in schema_fields
    assert "output_dir" not in schema_fields
    assert "url" not in schema_fields


@pytest.mark.asyncio
async def test_interview_tool_returns_structured_timeout_instead_of_crashing_agent() -> None:
    tool = build_interview_evidence_tool(TimeoutDiscovery())  # type: ignore[arg-type]

    result = await tool.ainvoke({"company": "示例公司", "role": "后端开发"})

    assert result == {
        "status": "failed",
        "error_type": "timeout",
        "message": "面经研究达到本轮时间预算，已安全停止；可以缩小岗位范围后继续。",
        "retryable": True,
    }
