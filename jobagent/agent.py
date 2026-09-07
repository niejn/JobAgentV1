"""Conversational JobAgent runtime."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import aiosqlite
from deepagents import create_deep_agent
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError

from jobagent.artifacts import (
    LocalOpportunityArtifacts,
    OpportunityStatusBoard,
    StatusPeriod,
)
from jobagent.config import Settings
from jobagent.crawl import (
    AsyncChannelLimiter,
    CrawlGate,
    SyncChannelLimiter,
    build_crawl_gate,
)
from jobagent.memory.conversation_log import ConversationLog
from jobagent.middleware import MessageCompatibilityMiddleware, ModelCapabilityRegistry
from jobagent.models.llm_client import build_agent_model
from jobagent.observability import (
    NodeTraceMiddleware,
    begin_trace,
    current_trace_id,
    log_decision,
    reset_trace,
)
from jobagent.profile import SQLiteCandidateContextProvider, SQLiteCandidateProfileStore
from jobagent.profile.context import CandidateContext
from jobagent.prompts import MAIN_AGENT_SYSTEM_PROMPT, build_system_prompt
from jobagent.scraper.xhs_backend import SpiderXhsBackend
from jobagent.skills import SkillManager
from jobagent.tools import (
    BossGreetingsManager,
    BossJobDiscovery,
    CandidateProfileManager,
    InterviewEvidenceDiscovery,
    JobDescriptionReader,
    SharedUrlSaver,
    UserDocumentReader,
    XhsAuthorPostsBrowser,
    build_boss_chat_history_tool,
    build_boss_chat_list_tool,
    build_boss_chat_reply_tool,
    build_boss_greet_jobs_tool,
    build_boss_job_discovery_tool,
    build_boss_resume_upload_tool,
    build_find_merge_candidates_tool,
    build_get_job_progress_tool,
    build_import_candidate_resume_tool,
    build_interview_evidence_tool,
    build_job_description_tool,
    build_list_job_records_tool,
    build_list_recent_emails_tool,
    build_merge_job_identities_tool,
    build_read_email_tool,
    build_save_candidate_background_tool,
    build_save_job_analysis_tool,
    build_save_job_search_profile_tool,
    build_save_user_fact_tool,
    build_search_history_tool,
    build_send_application_email_tool,
    build_shared_url_extract_tool,
    build_shared_url_save_tool,
    build_skill_tools,
    build_update_application_state_tool,
    build_update_job_progress_tool,
    build_user_document_tool,
    build_xhs_author_posts_tool,
    build_xhs_note_search_tool,
)
from jobagent.tools.xhs_note import XhsNoteSaver
from jobagent.tools.xhs_search import XhsNoteSearcher

# Backward-compatible import for callers that referenced the old constant.
SYSTEM_PROMPT = MAIN_AGENT_SYSTEM_PROMPT
logger = logging.getLogger(__name__)


# Shell commands run by the model get only these environment variables; everything
# else (notably provider keys and platform cookies loaded from .env) stays private.
_SHELL_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "LANG",
    "PYTHONIOENCODING",
)
# Redaction vocabulary lives in observability (single source of truth).
from jobagent.observability import _SENSITIVE_NAMES as _DEBUG_SENSITIVE_KEYS  # noqa: E402
from jobagent.observability import _SENSITIVE_QUERY as _DEBUG_SENSITIVE_QUERY  # noqa: E402

#: Tools whose execution is paused for explicit human approval before the
# call runs (physical interrupt - the model cannot bypass it). This replaces
# the former per-tool ``user_confirmed`` parameter gates.
_HITL_TOOLS: dict[str, str] = {
    "install_skill": "从本地文件或互联网下载并安装一个 Agent Skill",
    "send_application_email": "发送求职投递邮件（含简历附件）给外部 HR",
    "boss_greet_jobs": "向 Boss 招聘方批量发送打招呼消息",
    "upload_boss_resume_pdf": "向 Boss 账户上传/替换附件简历",
    "reply_boss_greeting": "在 Boss 聊天中向 HR 发送一条消息",
    "merge_job_identities": "合并两条岗位身份记录（不可自动撤销）",
}


def build_hitl_middleware() -> Any:
    """Assemble the framework HITL approval gate for external-write tools."""

    from langchain.agents.middleware import HumanInTheLoopMiddleware

    return HumanInTheLoopMiddleware(
        interrupt_on={
            name: {
                "allowed_decisions": ["approve", "reject"],
                "description": why,
            }
            for name, why in _HITL_TOOLS.items()
        },
        description_prefix="工具执行需要人工批准",
    )


@dataclass(frozen=True, slots=True)
class AgentStreamEvent:
    """Stable user-visible projection of internal LangGraph stream events."""

    kind: Literal["status", "token", "thinking", "tool", "interrupt", "done"]
    text: str


@dataclass(frozen=True, slots=True)
class ConversationEntry:
    """One safe user-visible message restored from a session."""

    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True, slots=True)
class ConversationHistory:
    """Bounded history projection returned when resuming a session."""

    summary: str | None
    recent: tuple[ConversationEntry, ...]
    compacted: bool


@dataclass(frozen=True, slots=True)
class ConversationSession:
    """One durable conversation session derived from checkpoint data."""

    session_id: str
    message_count: int
    last_used_at: str


def _decode_ulid_time(checkpoint_id: str) -> str:
    """Decode timestamp from UUID v6 checkpoint_id."""
    try:
        u = uuid.UUID(checkpoint_id)
        hex_str = u.hex
        time_high = int(hex_str[0:8], 16)
        time_mid = int(hex_str[8:12], 16)
        time_low = int(hex_str[13:16], 16)
        ts_100ns = (time_high << 28) | (time_mid << 12) | time_low
        uuid_epoch = datetime(1582, 10, 15, tzinfo=UTC)
        dt = uuid_epoch + timedelta(microseconds=ts_100ns // 10)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return checkpoint_id.split("-")[0] if "-" in checkpoint_id else "?"


class JobAgent:
    """Conversational interface with lazily initialized durable checkpoints."""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        tools: Sequence[BaseTool],
        system_prompt: str,
        checkpoint_db: Path,
        conversation_log: ConversationLog | None = None,
        # 每次调用的最大超步数；不传时回退 LangGraph 默认 25（仅约 6-12 轮工具循环）。
        # 超限时不裸抛 GraphRecursionError，而是无工具再调一次模型做总结收尾
        # （见 _graceful_budget_exhaustion，对应 Hermes _budget_grace_call）。
        recursion_limit: int = 90,
        opportunity_artifacts: LocalOpportunityArtifacts | None = None,
        filesystem_root: Path | None = None,
        debug_trace: bool = False,
        model_capability_registry: ModelCapabilityRegistry | None = None,
        model_capability_key: str = "",
    ) -> None:
        self._model = model
        self._tools = tuple(tools)
        self._system_prompt = system_prompt
        self._checkpoint_db = checkpoint_db
        self._conversation_log = conversation_log
        self._deep_agent: Any | None = None
        self._pending_hitl: Any | None = None
        self._connection: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        self._recursion_limit = recursion_limit
        self._opportunity_artifacts = opportunity_artifacts
        self._filesystem_root = (filesystem_root or Path.cwd()).expanduser().resolve()
        self._debug_trace = debug_trace
        self._model_capability_registry = model_capability_registry or ModelCapabilityRegistry(
            Path("data/model_capabilities.json")
        )
        self._model_capability_key = model_capability_key or _model_display_name(model)

    async def reply(self, message: str, *, session_id: str = "default") -> str:
        """Continue one conversation and collect its visible token stream."""

        parts = [
            event.text
            async for event in self.stream_reply(message, session_id=session_id)
            if event.kind == "token"
        ]
        response = "".join(parts)
        if response:
            return response
        raise RuntimeError("JobAgent returned no visible assistant message")

    async def stream_reply(
        self,
        message: str,
        *,
        session_id: str = "default",
    ) -> AsyncIterator[AgentStreamEvent]:
        if self._conversation_log is not None:
            self._conversation_log.append(session=session_id, role="user", text=message)
        async for event in self._reply_with_log(
            self._stream_reply_events(message, session_id=session_id),
            session_id=session_id,
        ):
            yield event

    async def resume_reply(
        self,
        approved: bool | Sequence[bool],
        *,
        session_id: str = "default",
        reject_reason: str = "",
    ) -> AsyncIterator[AgentStreamEvent]:
        """Resume after a HITL interrupt with the human's decision."""

        from langgraph.types import Command

        decisions = [approved] if isinstance(approved, bool) else list(approved)
        if not decisions:
            decisions = [False]
        decision_payloads: list[dict[str, Any]] = []
        for decision in decisions:
            item: dict[str, Any] = {"type": "approve" if decision else "reject"}
            if not decision and reject_reason:
                item["args"] = reject_reason
            decision_payloads.append(item)
        resume_input: Any = Command(resume={"decisions": decision_payloads})
        self._pending_hitl = None
        async for event in self._reply_with_log(
            self._stream_reply_events("", session_id=session_id, resume_input=resume_input),
            session_id=session_id,
        ):
            yield event

    async def _reply_with_log(
        self,
        events: AsyncIterator[AgentStreamEvent],
        *,
        session_id: str,
    ) -> AsyncIterator[AgentStreamEvent]:
        trace_token = begin_trace()
        log = self._conversation_log
        collected: list[str] = []
        try:
            async for event in events:
                if log is not None:
                    if event.kind == "token" and event.text:
                        collected.append(event.text)
                    elif event.kind == "tool" and event.text:
                        log.append(session=session_id, role="tool", text=event.text)
                yield event
        except GraphRecursionError:
            # 运行预算耗尽（recursion_limit 超步，见 settings 注释）：不裸抛异常，
            # 而是用已完成的 checkpoint 状态做一次无工具纯总结收尾，
            # 让用户拿到"目前为止的结论"而不是报错（Hermes _budget_grace_call 思路）。
            logger.warning(
                "jobagent.recursion_limit_exhausted",
                extra={"session_id": session_id, "recursion_limit": self._recursion_limit},
            )
            yield AgentStreamEvent("status", "已达最大运行步数，正在总结目前的结论…")
            deep_agent = await self._ensure_deep_agent()
            summary = await self._graceful_budget_exhaustion(deep_agent, session_id)
            if summary:
                yield AgentStreamEvent("token", summary)
            yield AgentStreamEvent("done", "")
        finally:
            reset_trace(trace_token)
            if log is not None and collected:
                log.append(session=session_id, role="assistant", text="".join(collected))

    async def _stream_reply_events(
        self,
        message: str,
        *,
        session_id: str,
        resume_input: Any | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Stream status/tool progress, thinking deltas, and final-answer tokens."""

        yield AgentStreamEvent("status", "正在分析你的请求…")
        request_started = time.perf_counter()
        if self._debug_trace:
            yield AgentStreamEvent(
                "status", f"[debug] Agent 请求开始 trace_id={current_trace_id()}"
            )
        deep_agent = await self._ensure_deep_agent()
        yield AgentStreamEvent(
            "status",
            f"Agent 已就绪（{time.perf_counter() - request_started:.1f}s）",
        )
        if self._debug_trace:
            yield AgentStreamEvent(
                "status",
                f"[debug] 模型请求开始（累计 {time.perf_counter() - request_started:.1f}s）",
            )
        emitted_visible_token = False
        complete_message_fallback = ""
        streamed_text: dict[str, str] = {}
        streamed_reasoning: dict[str, str] = {}
        announced_tool_calls: set[str] = set()
        latest_tool_name: str = ""
        latest_tool_args: dict[str, Any] = {}
        saw_tool_completion = False
        deterministic_tool_answer = ""
        last_finish_reason: str | None = None
        model_started = time.perf_counter()
        first_model_event_reported = False
        first_visible_token_reported = False
        tool_started = time.perf_counter()
        graph_input: Any = (
            resume_input
            if resume_input is not None
            else {"messages": [{"role": "user", "content": message}]}
        )
        async for part in deep_agent.astream(
            graph_input,
            config={
                "configurable": {"thread_id": session_id},
                # 运行预算：不传则 LangGraph 隐式默认 25 超步（≈6-12 轮工具循环），
                # 复合任务会中途裸崩。默认 90 ≈ 22-45 轮，见 settings 注释与 PS-4 设计。
                "recursion_limit": self._recursion_limit,
            },
            stream_mode=["messages", "updates"],
            version="v2",
        ):
            if not first_model_event_reported:
                first_model_event_reported = True
                yield AgentStreamEvent(
                    "status",
                    f"模型已返回事件（等待 {time.perf_counter() - model_started:.1f}s）",
                )
                if self._debug_trace:
                    yield AgentStreamEvent(
                        "status",
                        f"[debug] LangGraph 首事件（{time.perf_counter() - model_started:.1f}s）",
                    )
            if part.get("type") == "updates":
                for node, update in part["data"].items():
                    if self._debug_trace:
                        yield AgentStreamEvent("status", f"[debug] 节点：{node}")
                    if node == "__interrupt__":
                        # HITL: the framework paused before tool execution.
                        # Surface the pending action and stop this turn; the
                        # caller resumes with resume_reply(approved=...).
                        import json as _json

                        for interrupt_item in (
                            update if isinstance(update, tuple) else (update,)
                        ):
                            value = getattr(interrupt_item, "value", interrupt_item)
                            self._pending_hitl = value
                            yield AgentStreamEvent(
                                "interrupt",
                                _json.dumps(value, ensure_ascii=False, default=str),
                            )
                        continue
                    if node == "model" and isinstance(update, dict):
                        for updated_message in update.get("messages", []):
                            if not isinstance(updated_message, AIMessage):
                                continue
                            finish_reason = updated_message.response_metadata.get("finish_reason")
                            last_finish_reason = (
                                str(finish_reason) if finish_reason is not None else None
                            )
                            for tool_call in updated_message.tool_calls:
                                call_id = str(tool_call.get("id") or tool_call.get("name"))
                                if call_id in announced_tool_calls:
                                    continue
                                announced_tool_calls.add(call_id)
                                latest_tool_name = str(tool_call.get("name") or "unknown")
                                latest_tool_args = tool_call.get("args", {}) or {}
                                tool_started = time.perf_counter()
                                log_decision(
                                    logger,
                                    "agent.tool_selection",
                                    basis={
                                        "tool_name": tool_call.get("name"),
                                        "args": tool_call.get("args", {}),
                                        "message_count": len(update.get("messages", [])),
                                    },
                                    outcome=str(tool_call.get("name") or "unknown"),
                                )
                                if self._debug_trace:
                                    yield AgentStreamEvent(
                                        "status",
                                        "[debug] Tool 请求："
                                        f"{tool_call.get('name', '')} "
                                        f"{_safe_debug_args(tool_call.get('args'))}",
                                    )
                                yield AgentStreamEvent(
                                    "status",
                                    _tool_start_status(str(tool_call.get("name") or "")),
                                )
                    elif node == "tools":
                        saw_tool_completion = True
                        if isinstance(update, dict):
                            for tool_message in update.get("messages", []):
                                deterministic_tool_answer = (
                                    _deterministic_tool_answer(tool_message)
                                    or deterministic_tool_answer
                                )
                        tool_summary = _tool_result_summary(update) if update else ""
                        if tool_summary:
                            log_decision(
                                logger,
                                "agent.tool_completion",
                                basis={
                                    "tool_name": latest_tool_name,
                                    "args": _safe_debug_args(latest_tool_args),
                                    "duration_seconds": round(
                                        time.perf_counter() - tool_started, 3
                                    ),
                                    "result_summary": tool_summary,
                                },
                                outcome=latest_tool_name,
                            )
                            yield AgentStreamEvent("tool", tool_summary)
                        yield AgentStreamEvent(
                            "status",
                            "资料处理完成，正在生成回答…",
                        )
                        if self._debug_trace:
                            yield AgentStreamEvent(
                                "status",
                                f"[debug] Tool 完成：{time.perf_counter() - tool_started:.1f}s "
                                f"{_tool_result_summary(update)}",
                            )
                        yield AgentStreamEvent(
                            "status",
                            f"本次 Tool 耗时 {time.perf_counter() - tool_started:.1f}s",
                        )
                continue
            if part.get("type") != "messages":
                continue
            streamed_message, metadata = part["data"]
            if metadata.get("langgraph_node") != "model":
                continue
            reasoning = _reasoning_delta(streamed_message)
            if reasoning and isinstance(streamed_message, AIMessageChunk):
                message_key = str(
                    streamed_message.id
                    or metadata.get("langgraph_step")
                    or metadata.get("langgraph_node")
                    or "model"
                )
                accumulated_reasoning, novel_reasoning = _merge_streamed_text(
                    streamed_reasoning.get(message_key, ""),
                    reasoning,
                )
                streamed_reasoning[message_key] = accumulated_reasoning
                if novel_reasoning:
                    yield AgentStreamEvent("thinking", novel_reasoning)
            visible = _visible_text(streamed_message)
            if visible and isinstance(streamed_message, AIMessageChunk):
                message_key = str(
                    streamed_message.id
                    or metadata.get("langgraph_step")
                    or metadata.get("langgraph_node")
                    or "model"
                )
                accumulated, novel = _merge_streamed_text(
                    streamed_text.get(message_key, ""),
                    visible,
                )
                streamed_text[message_key] = accumulated
                if novel:
                    if not first_visible_token_reported:
                        first_visible_token_reported = True
                        yield AgentStreamEvent(
                            "status",
                            "正式答案开始输出（模型等待 "
                            f"{time.perf_counter() - model_started:.1f}s）",
                        )
                        if self._debug_trace:
                            yield AgentStreamEvent(
                                "status",
                                "[debug] 首个可见 token（累计 "
                                f"{time.perf_counter() - request_started:.1f}s）",
                            )
                    emitted_visible_token = True
                    yield AgentStreamEvent("token", novel)
            elif visible and isinstance(streamed_message, AIMessage):
                complete_message_fallback = visible
        if not emitted_visible_token and complete_message_fallback:
            yield AgentStreamEvent("token", complete_message_fallback)
            emitted_visible_token = True
        if not emitted_visible_token and saw_tool_completion:
            log_decision(
                logger,
                "agent.final_answer_recovery",
                basis={
                    "saw_tool_completion": saw_tool_completion,
                    "last_finish_reason": last_finish_reason,
                    "has_deterministic_result": bool(deterministic_tool_answer),
                },
                outcome=(
                    "deterministic_tool_result" if deterministic_tool_answer else "model_recovery"
                ),
            )
            status = (
                "模型输出被截断，正在恢复生成最终答复…"
                if last_finish_reason == "length"
                else "模型未生成最终答复，正在自动恢复…"
            )
            yield AgentStreamEvent("status", status)
            if deterministic_tool_answer:
                yield AgentStreamEvent("token", deterministic_tool_answer)
            else:
                recovered = await self._recover_final_answer(deep_agent, session_id)
                if recovered:
                    yield AgentStreamEvent("token", recovered)
        yield AgentStreamEvent("done", "")
        if self._debug_trace:
            logger.info(
                "jobagent.debug_trace.complete",
                extra={"elapsed_seconds": round(time.perf_counter() - request_started, 3)},
            )

    async def _graceful_budget_exhaustion(self, deep_agent: Any, session_id: str) -> str:
        """预算耗尽后的收尾：无工具再调一次模型，总结已有进展。

        与 `_recover_final_answer`（工具已完成但答案被截断）不同，这里面对的是
        递归上限内**未完成**的任务：模型循环中途被切新。收尾要求模型基于
        checkpoint 中已有的部分结果给出阶段性结论与未完成项，不得再调工具。
        结果写回 checkpoint（同恢复路径），保证下一轮对话上下文连贯。
        """

        config = {"configurable": {"thread_id": session_id}}
        try:
            snapshot = await deep_agent.aget_state(config)
            messages = tuple(snapshot.values.get("messages", ()))
            closing_messages: list[BaseMessage] = [
                SystemMessage(
                    content=(
                        f"{self._system_prompt}\n\n"
                        "<budget_exhaustion_policy>运行步数预算已耗尽，当前任务未完成。"
                        "基于以上已有进展，用中文给出阶段性总结：已完成什么、得到哪些"
                        "关键结论、还有什么未完成。不得调用任何工具，不要输出隐藏推理，"
                        "直接给出简洁、诚实、可执行的收尾说明，并建议用户如何继续"
                        "（例如换个更小的任务或分步进行）。</budget_exhaustion_policy>"
                    )
                ),
                *messages,
            ]
            closing_model = self._model.bind(max_tokens=4096, temperature=0.0)
            async with asyncio.timeout(60):
                response = await closing_model.ainvoke(closing_messages)
            summary = _visible_text(response).strip()
            if not summary or not isinstance(response, AIMessage):
                return ""
            await deep_agent.aupdate_state(config, {"messages": [response]})
            return summary
        except Exception:
            logger.warning("Budget-exhaustion closing summary failed", exc_info=True)
            return "本轮已达最大运行步数（未能在预算内完成）。建议拆分为更小的任务分别进行。"

    async def _recover_final_answer(self, deep_agent: Any, session_id: str) -> str:
        """Retry once without Tools when a completed Tool turn has no visible answer."""

        config = {"configurable": {"thread_id": session_id}}
        try:
            snapshot = await deep_agent.aget_state(config)
            messages = tuple(snapshot.values.get("messages", ()))
            recovery_messages: list[BaseMessage] = [
                SystemMessage(
                    content=(
                        f"{self._system_prompt}\n\n"
                        "<recovery_policy>恢复生成最终答复：上一轮工具已经完成，但模型输出"
                        "因长度限制或空响应而没有可见答案。不得再次调用工具，不要输出隐藏"
                        "推理过程。直接基于已有 Tool 结果，用中文给出简洁、完整、可执行的"
                        "最终答复；优先回答用户原始请求。</recovery_policy>"
                    )
                ),
                *messages,
            ]
            recovery_model = self._model.bind(max_tokens=4096, temperature=0.0)
            async with asyncio.timeout(60):
                response = await recovery_model.ainvoke(recovery_messages)
            recovered = _visible_text(response).strip()
            if not recovered or not isinstance(response, AIMessage):
                return ""
            await deep_agent.aupdate_state(config, {"messages": [response]})
            return recovered
        except Exception:
            logger.warning("Final-answer recovery failed", exc_info=True)
            return ""

    async def resume_session(self, session_id: str) -> ConversationHistory:
        """Restore one session and expose its safe visible history.

        Compaction is owned by deepagents' SummarizationMiddleware (fires at
        85% of the model context window during model calls); resuming only
        projects the stored messages, it no longer rewrites state.
        """

        deep_agent = await self._ensure_deep_agent()
        snapshot = await deep_agent.aget_state({"configurable": {"thread_id": session_id}})
        messages = tuple(snapshot.values.get("messages", ()))
        recent = tuple(
            entry for message in messages if (entry := _conversation_entry(message)) is not None
        )
        return ConversationHistory(summary=None, recent=recent, compacted=False)

    async def list_sessions(self, *, limit: int = 50) -> tuple[ConversationSession, ...]:
        """List durable sessions newest-first by decoding UUID v6 checkpoint_id.

        Self-contained: ``jobagent chat --sessions`` runs before any chat has
        initialized the agent, so open the checkpoint db on demand instead of
        silently reporting "no sessions" (bug: 5 stored sessions, CLI showed 0).
        """

        if self._connection is None:
            path = self._checkpoint_db.expanduser().resolve()
            if not path.is_file():
                return ()
            standalone = await aiosqlite.connect(str(path))
            try:
                return await self._list_sessions_on(standalone, limit)
            finally:
                await standalone.close()
        return await self._list_sessions_on(self._connection, limit)

    async def _list_sessions_on(
        self, connection: aiosqlite.Connection, limit: int
    ) -> tuple[ConversationSession, ...]:
        cursor = await connection.execute(
            """
            SELECT thread_id,
                   COUNT(*) AS total_cps,
                   SUM(CASE WHEN json_extract(CAST(metadata AS TEXT), '$.source') = 'input'
                        THEN 1 ELSE 0 END) AS user_msgs,
                   MAX(checkpoint_id) AS latest_cp
            FROM checkpoints
            WHERE checkpoint_ns = ''
            GROUP BY thread_id
            ORDER BY latest_cp DESC
            LIMIT ?
            """,
            (limit,),
        )
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
        sessions: list[ConversationSession] = []
        for row in rows:
            session_id = str(row[0])
            message_count = int(row[2])
            last_used_at = _decode_ulid_time(str(row[3]))
            sessions.append(
                ConversationSession(
                    session_id=session_id,
                    message_count=message_count,
                    last_used_at=last_used_at,
                )
            )
        return tuple(sessions)

    async def opportunity_status(self, period: StatusPeriod) -> OpportunityStatusBoard:
        """Return a local Opportunity status summary without invoking the LLM."""

        store = self._opportunity_artifacts
        if store is None:
            store = LocalOpportunityArtifacts(Path("data/opportunities"))
        return await asyncio.to_thread(store.status_board, period=period)

    async def close(self) -> None:
        """Flush and close the local checkpoint database connection."""

        async with self._init_lock:
            connection = self._connection
            self._connection = None
            self._deep_agent = None
            if connection is not None:
                await connection.close()

    async def _ensure_deep_agent(self) -> Any:
        if self._deep_agent is not None:
            return self._deep_agent
        async with self._init_lock:
            if self._deep_agent is not None:
                return self._deep_agent
            checkpoint_path = self._checkpoint_db.expanduser().resolve()
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(str(checkpoint_path))
            saver = AsyncSqliteSaver(
                connection,
                serde=JsonPlusSerializer(allowed_msgpack_modules=None),
            )
            try:
                await saver.setup()
                # LocalShellBackend extends FilesystemBackend with shell
                # execution; the user opted in to a local development agent.
                # The shell env is a minimal allowlist: process secrets
                # (OPENAI_API_KEY, *_COOKIE, bot tokens) live in os.environ
                # and must stay unreachable from model-driven commands.
                from deepagents.backends.local_shell import LocalShellBackend

                shell_env = {
                    name: os.environ[name] for name in _SHELL_ENV_ALLOWLIST if name in os.environ
                }
                shell_backend = LocalShellBackend(
                    root_dir=self._filesystem_root,
                    virtual_mode=True,
                    inherit_env=False,
                    env=shell_env,
                    timeout=120,
                )
                filesystem_middleware = FilesystemMiddleware(
                    backend=shell_backend,
                    tools=[
                        "ls",
                        "read_file",
                        "write_file",
                        "edit_file",
                        "glob",
                        "grep",
                        "execute",
                    ],
                )
                # deepagents harness: a ReAct loop (model <-> tools nodes)
                # compiled as a LangGraph CompiledStateGraph. Named 'deep_agent'
                # because orchestration lives in the model (harness mode),
                # not in hand-written graph nodes.
                deep_agent = create_deep_agent(
                    model=self._model,
                    tools=list(self._tools),
                    system_prompt=self._system_prompt,
                    middleware=[
                        filesystem_middleware,
                        MessageCompatibilityMiddleware(
                            self._model_capability_registry,
                            self._model_capability_key,
                        ),
                        build_hitl_middleware(),
                        cast(Any, NodeTraceMiddleware()),
                    ],
                    backend=shell_backend,
                    checkpointer=saver,
                    name="jobagent",
                )
            except Exception:
                await connection.close()
                raise
            self._connection = connection
            self._deep_agent = deep_agent
            return deep_agent


def _visible_text(message: Any) -> str:
    """Return only answer text blocks; never expose provider reasoning blocks."""

    blocks = getattr(message, "content_blocks", None)
    if isinstance(blocks, list):
        return "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else ""


def _reasoning_delta(message: Any) -> str:
    """Return recognized provider thinking deltas for the transient display.

    Only well-known reasoning transports are surfaced: deepseek-style
    ``reasoning_content`` additional kwargs and anthropic-style ``thinking``
    content blocks. Unrecognized block shapes stay hidden.
    """

    kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(kwargs, dict):
        for key in ("reasoning_content", "reasoning"):
            value = kwargs.get(key)
            if isinstance(value, str) and value:
                return value
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                text = block.get("thinking")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "".join(parts)
    return ""


def _deterministic_tool_answer(message: Any) -> str:
    """Return a safe final answer for tool results whose output is already definitive."""

    if not isinstance(message, ToolMessage):
        return ""
    name = str(getattr(message, "name", ""))
    if name != "write_file":
        return ""
    content = _visible_text(message)
    if not content.lower().startswith("successfully wrote to "):
        return ""
    return f"文件已写入：{content.removeprefix('Successfully wrote to ').strip()}"


def _safe_debug_args(value: Any) -> str:
    """Render bounded Tool args with credential-like fields redacted."""

    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): (
                    "[REDACTED]"
                    if any(term in str(key).lower() for term in _DEBUG_SENSITIVE_KEYS)
                    else scrub(child)
                )
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [scrub(child) for child in item[:10]]
        if isinstance(item, str):
            scrubbed = _DEBUG_SENSITIVE_QUERY.sub(r"\1=[REDACTED]", item)
            return scrubbed[:300]
        return item

    try:
        return json.dumps(scrub(value), ensure_ascii=False, default=str)[:800]
    except (TypeError, ValueError):
        return "[unserializable]"


def _tool_result_summary(update: Any) -> str:
    messages = update.get("messages", []) if isinstance(update, dict) else []
    summaries: list[str] = []
    for message in messages:
        name = str(getattr(message, "name", "tool"))
        content = _visible_text(message)
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            details = {
                key: payload[key]
                for key in ("status", "error_type", "image_count", "image_ocr_count", "file_path")
                if key in payload
            }
            summaries.append(f"{name} {_safe_debug_args(details)}")
        else:
            summaries.append(f"{name} ({len(content)} chars)")
    return "; ".join(summaries)[:1_200]


def _conversation_entry(message: BaseMessage) -> ConversationEntry | None:
    text = _visible_text(message).strip()
    if not text:
        return None
    if isinstance(message, HumanMessage):
        return ConversationEntry("user", text)
    if isinstance(message, AIMessage) and not message.tool_calls:
        return ConversationEntry("assistant", text)
    return None


def _merge_streamed_text(accumulated: str, chunk: str) -> tuple[str, str]:
    """Accept both delta and cumulative chunks without duplicating visible text."""

    if chunk.startswith(accumulated):
        return chunk, chunk[len(accumulated) :]
    if accumulated.startswith(chunk):
        return accumulated, ""
    overlap = min(len(accumulated), len(chunk))
    while overlap and not accumulated.endswith(chunk[:overlap]):
        overlap -= 1
    novel = chunk[overlap:]
    return accumulated + novel, novel


def _tool_start_status(tool_name: str) -> str:
    if tool_name == "save_shared_url":
        return "正在读取并保存分享链接中的资料…"
    if tool_name == "extract_shared_url":
        return "正在读取已保存内容并执行图片 OCR…"
    if tool_name == "search_xhs_notes":
        return "正在按关键词搜索小红书笔记…"
    if tool_name == "browse_xhs_author_posts":
        return "正在浏览作者公开帖子并筛选候选内容…"
    if tool_name == "discover_boss_jobs":
        return "正在 Boss 搜索并筛选岗位…"
    if tool_name == "discover_interview_evidence":
        return "正在搜索并整理面经资料…"
    if tool_name == "read_job_description":
        return "正在读取你指定的 JD 文本…"
    if tool_name == "read_user_document":
        return "正在读取你指定的候选人文档…"
    if tool_name == "import_candidate_resume":
        return "正在导入并版本化你的基础简历…"
    if tool_name == "save_candidate_background":
        return "正在保存你已确认的候选人背景…"
    if tool_name == "save_job_search_profile":
        return "正在保存你已确认的求职意向…"
    if tool_name == "save_job_analysis":
        return "正在保存 JD 与岗位分析报告…"
    if tool_name == "update_job_application_state":
        return "正在更新岗位投递状态…"
    if tool_name == "boss_greet_jobs":
        return "正在向 Boss 招聘方发送打招呼消息…"
    return "正在执行所需工具…"


def _xhs_backend_factory_for(
    settings: Settings,
    crawl_gate: CrawlGate | None = None,
) -> Callable[[Settings], Any]:
    """Return the XHS backend factory for this run.

    Spider_XHS stays the pure default. CDP composition is opt-in: the
    ``xhs_cdp`` module is imported lazily and only when the user enabled it,
    so a default deployment loads zero CDP code.

    ``crawl_gate`` 是进程级共享闸门（桶随进程存活，跨工具调用生效）。
    传入时 backend 的 api/media 限流器改接共享通道适配器；不传则退回
    backend 自带的实例级限流器（测试/独立使用）。CDP 组合路径同样把
    gate 传给 primary 与 CDP fallback（页面加载也吃同一个站桶）。
    """

    def _with_gate(target_settings: Settings) -> Any:
        if crawl_gate is None:
            return SpiderXhsBackend(target_settings)
        return SpiderXhsBackend(
            target_settings,
            api_limiter=SyncChannelLimiter(crawl_gate, "xhs-api"),
            media_limiter=AsyncChannelLimiter(crawl_gate, "xhs-media"),
        )

    if not settings.xhs_cdp_enabled:
        return _with_gate
    from jobagent.scraper.xhs_cdp import build_xhs_backend

    def _with_gate_and_fallback(target_settings: Settings) -> Any:
        if crawl_gate is None:
            return build_xhs_backend(target_settings)
        return build_xhs_backend(target_settings, crawl_gate=crawl_gate)

    return _with_gate_and_fallback


def _build_optional_tool(
    name: str,
    builder: Callable[[], BaseTool],
) -> BaseTool | None:
    """Build one tool registration, degrading gracefully on missing deps.

    Hermes 的 _discover_tools 优雅降级模式的显式提炼：某个工具的可选依赖
    （如 CDP 模块、playwright）缺失时，只跳过这一个工具并记 warning，
    agent 带着剩余能力集照常构建，而不是整体崩溃。

    刻意只捕 ImportError：依赖缺失是环境问题（可降级）；其他异常是代码
    bug，必须照常抛出 loudly--静默缩水能力集比崩溃更难排查。
    """

    try:
        return builder()
    except ImportError as exc:
        logger.warning(
            "jobagent.tool_degraded",
            extra={"tool": name, "reason": repr(exc)},
        )
        return None


def _model_display_name(model: BaseChatModel) -> str:
    """Best-effort model display name for the metadata layer."""

    for attr in ("model_name", "model", "model_id"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return type(model).__name__


def build_job_agent(
    settings: Settings,
    *,
    model: BaseChatModel | None = None,
    tools: Sequence[BaseTool] | None = None,
    candidate_context: CandidateContext | None = None,
    platform_hint: str = "",
    system_prompt_override: str | None = None,
) -> JobAgent:
    """Build a safe Agent with only explicitly registered job-search tools."""

    effective_context = candidate_context
    settings.jobagent_workspace_root.expanduser().resolve().mkdir(parents=True, exist_ok=True)
    state_db = settings.jobagent_state_db.expanduser().resolve()
    if effective_context is not None:
        with SQLiteCandidateProfileStore(state_db) as profile_store:
            effective_context = profile_store.import_context(effective_context)
    elif state_db.is_file():
        with SQLiteCandidateProfileStore(state_db) as profile_store:
            effective_context = profile_store.load_context()
    if tools is not None:
        registered_tools = list(tools)
    else:
        # 进程级爬取闸门：每个站点账号一个共享令牌桶（跨工具调用存活，
        # 修复“每次调用 new backend = 空桶”的速率失效 bug）+ 每次放行后
        # 按请求类别的 jitter。构造点是唯一组装处（build_crawl_gate）。
        crawl_gate = build_crawl_gate(settings)
        backend_factory = _xhs_backend_factory_for(settings, crawl_gate)
        shared_url_saver = SharedUrlSaver(
            settings,
            xhs_saver=XhsNoteSaver(settings, backend_factory=backend_factory),
        )
        # Job discovery reads the latest persisted context at tool-call time
        # so it refuses to crawl until the Job Search Profile and the resume /
        # confirmed background have been collected and saved by the Agent.
        context_provider = SQLiteCandidateContextProvider(state_db)
        profile_manager = CandidateProfileManager(
            workspace_root=settings.jobagent_workspace_root,
            database=settings.jobagent_state_db,
        )
        artifacts_store = LocalOpportunityArtifacts(settings.jobagent_opportunity_dir)
        skill_manager = SkillManager(settings.jobagent_skills_dir)
        # 声明式能力表：下面 (名字 -> 构造器) 对就是 agent 的能力集。
        # 统一经 _build_optional_tool 装配（Hermes 优雅降级模式：可选依赖
        # 缺失只降级该工具并记 warning，不炸整体构建；见其 docstring）。
        # 这个列表同时是 PS-1 条件 prompt 注入的事实源（工具集 -> policy 段）。
        tool_builders: list[tuple[str, Callable[[], BaseTool]]] = [
            (
                "discover_boss_jobs",
                lambda: build_boss_job_discovery_tool(
                    BossJobDiscovery(settings, crawl_gate=crawl_gate),
                    context_loader=context_provider.load,
                    registry_path=state_db,
                ),
            ),
            (
                "boss_greet_jobs",
                lambda: build_boss_greet_jobs_tool(
                    BossGreetingsManager(
                        settings,
                        registry_path=state_db,
                        crawl_gate=crawl_gate,
                    )
                ),
            ),
            (
                "upload_boss_resume_pdf",
                lambda: build_boss_resume_upload_tool(settings, crawl_gate=crawl_gate),
            ),
            ("list_boss_greetings", lambda: build_boss_chat_list_tool(settings)),
            ("save_user_fact", lambda: build_save_user_fact_tool(settings)),
            ("search_history", lambda: build_search_history_tool(settings)),
            (
                "read_boss_conversation",
                lambda: build_boss_chat_history_tool(settings),
            ),
            ("reply_boss_greeting", lambda: build_boss_chat_reply_tool(settings)),
            ("update_job_progress", lambda: build_update_job_progress_tool(state_db)),
            ("list_job_records", lambda: build_list_job_records_tool(state_db)),
            ("send_application_email", lambda: build_send_application_email_tool(settings)),
            ("list_recent_emails", lambda: build_list_recent_emails_tool(settings)),
            ("read_email", lambda: build_read_email_tool(settings)),
            ("get_job_progress", lambda: build_get_job_progress_tool(state_db)),
            (
                "find_job_merge_candidates",
                lambda: build_find_merge_candidates_tool(state_db),
            ),
            (
                "merge_job_identities",
                lambda: build_merge_job_identities_tool(state_db),
            ),
            (
                "import_candidate_resume",
                lambda: build_import_candidate_resume_tool(profile_manager),
            ),
            (
                "save_candidate_background",
                lambda: build_save_candidate_background_tool(profile_manager),
            ),
            (
                "save_job_search_profile",
                lambda: build_save_job_search_profile_tool(profile_manager),
            ),
            (
                "save_job_analysis",
                lambda: build_save_job_analysis_tool(artifacts_store),
            ),
            (
                "update_job_application_state",
                lambda: build_update_application_state_tool(artifacts_store),
            ),
            (
                "discover_interview_evidence",
                lambda: build_interview_evidence_tool(
                    InterviewEvidenceDiscovery(
                        settings,
                        backend_factory=backend_factory,
                        candidate_context=effective_context,
                    )
                ),
            ),
            ("save_shared_url", lambda: build_shared_url_save_tool(shared_url_saver)),
            (
                "extract_shared_url",
                lambda: build_shared_url_extract_tool(shared_url_saver),
            ),
            (
                "browse_xhs_author_posts",
                lambda: build_xhs_author_posts_tool(
                    XhsAuthorPostsBrowser(settings, backend_factory=backend_factory)
                ),
            ),
            (
                "search_xhs_notes",
                lambda: build_xhs_note_search_tool(
                    XhsNoteSearcher(settings, backend_factory=backend_factory)
                ),
            ),
            (
                "read_job_description",
                lambda: build_job_description_tool(
                    JobDescriptionReader(settings.jobagent_workspace_root)
                ),
            ),
            (
                "read_user_document",
                lambda: build_user_document_tool(
                    UserDocumentReader(settings.jobagent_workspace_root)
                ),
            ),
        ]
        registered_tools = [
            tool
            for name, build in tool_builders
            if (tool := _build_optional_tool(name, build)) is not None
        ]
        registered_tools.extend(build_skill_tools(skill_manager))
    effective_model = model or build_agent_model(settings)
    # PS-1 分层组装：条件注入（段/段落跟随 registered_tools）+ 元数据层
    # （日期冻结于构造时刻）+ candidate_context 不可信块。单一组装点在
    # prompts/builder.py，本处只传事实源。
    conversation_log = ConversationLog(state_db.parent / "conversations")
    memory_dir = state_db.parent / "memory"
    memory_markdown = ""
    try:
        from jobagent.memory.store import CandidateMemoryStore

        memory_markdown = CandidateMemoryStore(memory_dir / "candidate_memory.md").as_markdown()
    except Exception:
        logger.warning("candidate memory load failed", exc_info=True)
    system_prompt = system_prompt_override or build_system_prompt(
        registered_tools={tool.name for tool in registered_tools},
        candidate_context=effective_context,
        platform_hint=platform_hint,
        model_name=_model_display_name(effective_model),
        memory_markdown=memory_markdown,
    )
    return JobAgent(
        model=effective_model,
        tools=registered_tools,
        system_prompt=system_prompt,
        checkpoint_db=settings.jobagent_checkpoint_db,
        conversation_log=conversation_log,
        recursion_limit=settings.jobagent_recursion_limit,
        opportunity_artifacts=LocalOpportunityArtifacts(settings.jobagent_opportunity_dir),
        filesystem_root=settings.jobagent_artifact_dir,
        debug_trace=settings.jobagent_debug_trace,
        model_capability_registry=ModelCapabilityRegistry(
            settings.jobagent_model_capabilities_file
        ),
        model_capability_key=(
            f"{settings.jobagent_llm_model.strip()}@{settings.openai_base_url.rstrip('/')}"
        ),
    )
