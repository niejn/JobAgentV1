"""Behavior tests for the workspace-scoped JD text Tool."""

from pathlib import Path

import pytest

from jobagent.tools.job_description import (
    JobDescriptionReader,
    UserDocumentReader,
    build_job_description_tool,
    build_user_document_tool,
)


@pytest.mark.asyncio
async def test_job_description_tool_reads_named_workspace_text_without_exposing_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "example_job_jd.txt").write_text(
        "上海智擎云际信息技术\n大模型工程师\n负责 Agent 开发",
        encoding="utf-8",
    )
    tool = build_job_description_tool(JobDescriptionReader(tmp_path))

    result = await tool.ainvoke({"file_path": "example_job_jd.txt"})

    assert result["status"] == "completed"
    assert result["source_name"] == "example_job_jd.txt"
    assert "上海智擎云际" in result["content"]
    assert str(tmp_path) not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("file_path", ["../secret.txt", "resume.pdf", "missing.txt"])
async def test_job_description_tool_rejects_unsafe_or_invalid_files(
    tmp_path: Path,
    file_path: str,
) -> None:
    tool = build_job_description_tool(JobDescriptionReader(tmp_path))

    result = await tool.ainvoke({"file_path": file_path})

    assert result["status"] == "failed"
    assert result["error_type"] == "invalid_file"
    assert str(tmp_path) not in str(result)


@pytest.mark.asyncio
async def test_user_document_tool_reads_explicitly_named_resume_markdown(
    tmp_path: Path,
) -> None:
    (tmp_path / "resume-v5-四大项目版.md").write_text(
        "# 候选人简历\n\n项目：RAG 知识库",
        encoding="utf-8",
    )
    tool = build_user_document_tool(UserDocumentReader(tmp_path))

    result = await tool.ainvoke({"file_path": "resume-v5-四大项目版.md"})

    assert result["status"] == "completed"
    assert result["source_name"] == "resume-v5-四大项目版.md"
    assert "RAG 知识库" in result["content"]
    assert str(tmp_path) not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("file_path", ["../resume.md", ".env", "resume.pdf"])
async def test_user_document_tool_keeps_workspace_and_file_type_boundary(
    tmp_path: Path,
    file_path: str,
) -> None:
    tool = build_user_document_tool(UserDocumentReader(tmp_path))

    result = await tool.ainvoke({"file_path": file_path})

    assert result["status"] == "failed"
    assert result["error_type"] == "invalid_file"
    assert str(tmp_path) not in str(result)
