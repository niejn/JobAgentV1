"""High-level XHS recruitment-note analysis Tool."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.journey.recruitment_notes import RecruitmentNoteRegistry, extract_note
from jobagent.journey.xhs_email_drafts import XhsEmailDraftService
from jobagent.journey.xhs_query_planner import plan_queries
from jobagent.profile.context import CandidateContext
from jobagent.tools.xhs_search import XhsNoteSearchRequest


class RecruitmentNoteSaver(Protocol):
    async def save(self, request: Any) -> dict[str, Any]: ...


class AnalyzeRecruitmentNoteRequest(BaseModel):
    url: str = Field(min_length=1, description="用户提供的完整小红书招人帖链接。")


class FindRecruitmentPostsRequest(BaseModel):
    limit_per_query: int = Field(default=10, ge=1, le=10)


class SelectRecruitmentPositionRequest(BaseModel):
    note_id: str = Field(min_length=1)
    position_index: int = Field(ge=0)
    company: str = Field(min_length=1, description="用户确认的招聘公司或团队名称。")


class PrepareRecruitmentEmailRequest(BaseModel):
    note_id: str = Field(min_length=1)
    resume_file_name: str = Field(min_length=1)
    sender_name: str = Field(min_length=1)
    subject: str | None = Field(
        default=None,
        description="用户逐字确认的邮件主题；提供时必须原样保存，不得改写。",
    )
    body_text: str | None = Field(
        default=None,
        description="用户逐字确认的邮件正文；提供时必须原样保存，不得改写。",
    )


class SendRecruitmentEmailRequest(BaseModel):
    draft_id: str = Field(min_length=1)


class XhsRecruitmentAnalyzer:
    """One deep business operation: save, extract, and persist an XHS note."""

    def __init__(self, saver: RecruitmentNoteSaver, registry_path: Any) -> None:
        self._saver = saver
        self._registry_path = registry_path

    async def analyze(self, request: AnalyzeRecruitmentNoteRequest) -> dict[str, Any]:
        from jobagent.tools.xhs_note import XhsNoteSaveRequest

        saved = await self._saver.save(XhsNoteSaveRequest(url=request.url))
        if saved.get("status") not in {"completed", "downloaded_ocr_pending"}:
            return saved
        note_id = str(saved.get("note_id") or "").strip()
        if not note_id:
            return {"status": "failed", "error_type": "missing_note_id"}
        note = extract_note(
            note_id,
            str(saved.get("source_url") or request.url),
            str(saved.get("title") or ""),
            str(saved.get("body_text") or ""),
            str(saved.get("image_ocr_text") or ""),
            tuple(saved.get("author_comments") or ()),
        )
        with RecruitmentNoteRegistry(self._registry_path) as registry:
            registry.save(note)
        return {
            "status": "analyzed",
            "note_id": note.note_id,
            "title": note.title,
            "features": list(note.features),
            "candidate_positions": [position.title for position in note.positions],
            "contacts": [
                {"email": item.email, "source": item.source, "confidence": item.confidence}
                for item in note.contacts
            ],
            "comment_capture_status": "not_available",
        }


class XhsRecruitmentFinder:
    """Hide query planning and bounded XHS searches behind one business operation."""

    def __init__(
        self,
        context_loader: Callable[[], CandidateContext | None],
        registry_path: Path,
        searcher: Any,
    ) -> None:
        self._context_loader = context_loader
        self._registry_path = registry_path
        self._searcher = searcher

    async def find(self, request: FindRecruitmentPostsRequest) -> dict[str, Any]:
        context = self._context_loader()
        if context is None or context.search_profile is None:
            return {"status": "missing_profile", "queries": [], "results": []}
        with RecruitmentNoteRegistry(self._registry_path) as registry:
            features = registry.recent_features()
        queries = plan_queries(context.search_profile, features)
        results = []
        for query in queries:
            result = await self._searcher.search(
                XhsNoteSearchRequest(query=query, limit=request.limit_per_query)
            )
            results.append(result)
            if result.get("status") != "ok":
                break
        return {"status": "ok", "queries": list(queries), "results": results}


class XhsPositionSelector:
    def __init__(self, registry_path: Path) -> None:
        self._registry_path = registry_path

    async def select(self, request: SelectRecruitmentPositionRequest) -> dict[str, Any]:
        with RecruitmentNoteRegistry(self._registry_path) as registry:
            try:
                position = registry.select_position(
                    note_id=request.note_id,
                    position_index=request.position_index,
                    company=request.company,
                )
            except (KeyError, IndexError, ValueError) as error:
                return {
                    "status": "failed",
                    "error_type": "invalid_selection",
                    "message": str(error),
                }
        jd = "\n".join((*position.responsibilities, *position.requirements))
        return {
            "status": "selected",
            "company": request.company,
            "role": position.title,
            "job_description": jd,
            "source_job_id": f"xhs:{request.note_id}",
        }


def build_analyze_recruitment_note_tool(analyzer: XhsRecruitmentAnalyzer) -> BaseTool:
    async def analyze_recruitment_note(url: str) -> dict[str, Any]:
        return await analyzer.analyze(AnalyzeRecruitmentNoteRequest(url=url))

    return StructuredTool.from_function(
        coroutine=analyze_recruitment_note,
        name="analyze_recruitment_note",
        description=(
            "读取并保存一条用户提供的小红书招人帖，提取可追溯的候选岗位、技术特征和公开邮箱证据。"
            "只做本地分析，不发送邮件。"
        ),
        args_schema=AnalyzeRecruitmentNoteRequest,
    )


def build_find_recruitment_posts_tool(finder: XhsRecruitmentFinder) -> BaseTool:
    async def find_recruitment_posts(limit_per_query: int = 10) -> dict[str, Any]:
        return await finder.find(FindRecruitmentPostsRequest(limit_per_query=limit_per_query))

    return StructuredTool.from_function(
        coroutine=find_recruitment_posts,
        name="find_recruitment_posts",
        description=(
            "依据已确认求职画像和已保存的小红书历史招聘贴特征，生成有界查询并搜索相似招人帖。"
            "不读取 Boss 职位或 JD。"
        ),
        args_schema=FindRecruitmentPostsRequest,
    )


def build_select_recruitment_position_tool(selector: XhsPositionSelector) -> BaseTool:
    async def select_recruitment_position(
        note_id: str, position_index: int, company: str
    ) -> dict[str, Any]:
        return await selector.select(
            SelectRecruitmentPositionRequest(
                note_id=note_id,
                position_index=position_index,
                company=company,
            )
        )

    return StructuredTool.from_function(
        coroutine=select_recruitment_position,
        name="select_recruitment_position",
        description=(
            "用户明确选择一条 XHS 招人帖中的候选岗位后，准备其 Journey 创建所需的已验证字段。"
        ),
        args_schema=SelectRecruitmentPositionRequest,
    )


def build_prepare_recruitment_email_tool(service: XhsEmailDraftService) -> BaseTool:
    async def prepare_recruitment_email(
        note_id: str,
        resume_file_name: str,
        sender_name: str,
        subject: str | None = None,
        body_text: str | None = None,
    ) -> dict[str, Any]:
        return service.prepare(note_id, resume_file_name, sender_name, subject, body_text)

    return StructuredTool.from_function(
        coroutine=prepare_recruitment_email,
        name="prepare_recruitment_email",
        description=(
            "为已选 XHS 岗位和用户选定 PDF 简历生成邮件草稿；用户已逐字确认主题/正文时"
            "原样传入 subject 和 body_text，未提供时使用默认模板；不发送邮件。"
        ),
        args_schema=PrepareRecruitmentEmailRequest,
    )


def build_send_recruitment_email_tool(service: XhsEmailDraftService) -> BaseTool:
    async def send_recruitment_email(draft_id: str) -> dict[str, Any]:
        try:
            return await service.send(draft_id)
        except KeyError:
            return {"status": "failed", "error_type": "draft_not_found"}

    return StructuredTool.from_function(
        coroutine=send_recruitment_email,
        name="send_recruitment_email",
        description="发送已保存的 XHS 邮件草稿；执行前必须人工批准。",
        args_schema=SendRecruitmentEmailRequest,
    )
