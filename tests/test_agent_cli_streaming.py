"""CLI behavior tests for visible Agent streaming."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from jobagent.agent import (
    AgentStreamEvent,
    ConversationEntry,
    ConversationHistory,
    ConversationSession,
)
from jobagent.artifacts import (
    ApplicationState,
    FitDecision,
    OpportunityStatusBoard,
    OpportunityStatusItem,
    StatusPeriod,
)
from jobagent.auth.browser_login import CookieProviderStatus
from jobagent.cli import main
from jobagent.config import Settings
from jobagent.profile import JobSearchProfile, SQLiteCandidateProfileStore


class FakeStreamingAgent:
    closed = False

    async def stream_reply(
        self,
        message: str,
        *,
        session_id: str,
    ) -> AsyncIterator[AgentStreamEvent]:
        assert message == "你好"
        assert session_id == "stream-test"
        yield AgentStreamEvent("status", "正在分析你的请求…")
        yield AgentStreamEvent("token", "你")
        yield AgentStreamEvent("token", "好")
        yield AgentStreamEvent("done", "")

    async def close(self) -> None:
        self.closed = True

    async def resume_session(self, session_id: str) -> ConversationHistory:
        assert session_id == "stream-test"
        return ConversationHistory(
            summary="较早对话已归纳。",
            recent=(
                ConversationEntry("user", "之前的问题"),
                ConversationEntry("assistant", "之前的回答"),
            ),
            compacted=True,
        )


class ThinkingStreamingAgent(FakeStreamingAgent):
    async def stream_reply(
        self,
        message: str,
        *,
        session_id: str,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent("status", "正在分析你的请求…")
        yield AgentStreamEvent("thinking", "模型思考：先理解")
        yield AgentStreamEvent("thinking", "用户意图")
        yield AgentStreamEvent("tool", "save_shared_url {'status': 'completed'}")
        yield AgentStreamEvent("token", "你")
        yield AgentStreamEvent("token", "好")
        yield AgentStreamEvent("done", "")


class FailingStreamingAgent(FakeStreamingAgent):
    async def stream_reply(
        self,
        message: str,
        *,
        session_id: str,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent("status", "正在分析你的请求…")
        raise RuntimeError("internal secret traceback")


class MissingFinalAnswerFakeAgent(FakeStreamingAgent):
    async def stream_reply(
        self,
        message: str,
        *,
        session_id: str,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent("status", "正在导入并版本化你的基础简历…")
        yield AgentStreamEvent("status", "资料处理完成，正在生成回答…")
        yield AgentStreamEvent("done", "")


class AutoSessionFakeAgent(FakeStreamingAgent):
    active_session_ids: list[str]

    def __init__(self) -> None:
        self.active_session_ids = []

    async def resume_session(self, session_id: str) -> ConversationHistory:
        self.active_session_ids.append(session_id)
        return ConversationHistory(summary=None, recent=(), compacted=False)

    async def stream_reply(
        self,
        message: str,
        *,
        session_id: str,
    ) -> AsyncIterator[AgentStreamEvent]:
        self.active_session_ids.append(session_id)
        yield AgentStreamEvent("token", "你好")
        yield AgentStreamEvent("done", "")


class SwitchingSessionFakeAgent(AutoSessionFakeAgent):
    async def list_sessions(self, *, limit: int = 50) -> tuple[ConversationSession, ...]:
        return (
            ConversationSession("previous-session", 4, "2026-08-24 10:00"),
            ConversationSession("older-session", 2, "2026-08-20 09:00"),
        )

    async def resume_session(self, session_id: str) -> ConversationHistory:
        self.active_session_ids.append(session_id)
        if session_id == "previous-session":
            return ConversationHistory(
                summary=None,
                recent=(ConversationEntry("assistant", "这是之前的会话"),),
                compacted=False,
            )
        return ConversationHistory(summary=None, recent=(), compacted=False)


class StatusBoardFakeAgent(AutoSessionFakeAgent):
    async def opportunity_status(self, period: StatusPeriod) -> OpportunityStatusBoard:
        return OpportunityStatusBoard(
            period=period,
            analyzed=2,
            suitable=1,
            uncertain=1,
            unsuitable=0,
            applied=1,
            items=(
                OpportunityStatusItem(
                    opportunity_id="opp-1",
                    company="示例科技",
                    role="AI Engineer",
                    fit=FitDecision.SUITABLE,
                    application_state=ApplicationState.APPLIED,
                    analysis_version=2,
                    updated_at=datetime(2026, 8, 21, tzinfo=UTC),
                ),
            ),
        )


def test_chat_cli_renders_status_then_tokens_incrementally(
    tmp_path: Path,
    monkeypatch,
) -> None:
    startup = tmp_path / "agent.yaml"
    startup.write_text("unused: true", encoding="utf-8")
    agent = FakeStreamingAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr("jobagent.profile.load_candidate_context", lambda path: None)

    result = CliRunner().invoke(
        main,
        ["chat", "--config", str(startup), "--session-id", "stream-test"],
        input="你好\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    status_at = result.output.index("正在分析你的请求")
    answer_at = result.output.index("JobAgent> 你好")
    assert status_at < answer_at
    assert "已恢复历史会话" in result.output
    assert "较早对话已归纳" in result.output
    assert "You · 之前的问题" in result.output
    assert "JobAgent · 之前的回答" in result.output
    assert agent.closed is True


def test_chat_cli_does_not_write_production_log(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Chat CLI runs under pytest must never touch data/logs/jobagent.log.

    Regression guard: ``_chat`` calls ``setup_logging()`` whose default file
    sink is the production log; the pytest conftest redirects that call into
    ``data/logs/tests/cli.log``. Without the redirect, this test fails and
    test tracebacks (e.g. fake "internal secret traceback" exceptions) leak
    into the production log.
    """

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "jobagent.cli.get_settings",
        lambda: Settings(_env_file=None, jobagent_debug_trace=False),
    )
    startup = tmp_path / "agent.yaml"
    startup.write_text("unused: true", encoding="utf-8")
    agent = FailingStreamingAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr("jobagent.profile.load_candidate_context", lambda path: None)

    result = CliRunner().invoke(
        main,
        ["chat", "--config", str(startup), "--session-id", "stream-test"],
        input="开始研究\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "本轮处理失败" in result.output
    production_log = tmp_path / "data" / "logs" / "jobagent.log"
    assert not production_log.exists()
    assert not any(tmp_path.glob("data/logs/jobagent.log*"))


def test_chat_cli_keeps_session_alive_after_one_turn_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "jobagent.cli.get_settings",
        lambda: Settings(_env_file=None, jobagent_debug_trace=False),
    )
    startup = tmp_path / "agent.yaml"
    startup.write_text("unused: true", encoding="utf-8")
    agent = FailingStreamingAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr("jobagent.profile.load_candidate_context", lambda path: None)

    result = CliRunner().invoke(
        main,
        ["chat", "--config", str(startup), "--session-id", "stream-test"],
        input="开始研究\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "本轮处理失败" in result.output
    assert "可以继续输入" in result.output
    assert "internal secret traceback" not in result.output
    assert result.output.count("You>") == 2
    assert agent.closed is True


def test_chat_cli_does_not_silently_return_when_agent_has_no_final_answer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    startup = tmp_path / "agent.yaml"
    startup.write_text("unused: true", encoding="utf-8")
    agent = MissingFinalAnswerFakeAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr("jobagent.profile.load_candidate_context", lambda path: None)

    result = CliRunner().invoke(
        main,
        ["chat", "--config", str(startup), "--session-id", "stream-test"],
        input="读取简历并给出建议\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "没有生成最终回答" in result.output
    assert result.output.count("You>") == 2


def test_chat_cli_starts_without_yaml_configuration(tmp_path: Path, monkeypatch) -> None:
    agent = FakeStreamingAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(
        "jobagent.cli.get_settings",
        lambda: Settings(
            _env_file=None,
            jobagent_state_db=tmp_path / "empty-profile.db",
            jobagent_checkpoint_db=tmp_path / "checkpoints.db",
        ),
    )

    result = CliRunner().invoke(
        main,
        ["chat", "--session-id", "stream-test"],
        input="你好\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "请先提供基础简历" in result.output
    assert "期望岗位、城市、薪资、公司特征、职位特征" in result.output
    assert "也可以直接提供一个目标 JD" in result.output
    assert "JobAgent> 你好" in result.output
    assert agent.closed is True


def test_chat_cli_recognizes_persisted_resume_and_search_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "candidate.db"
    with SQLiteCandidateProfileStore(database) as store:
        store.import_resume(source_name="resume.md", content="# Resume\nPython Agent")
        store.save_job_search_profile(
            JobSearchProfile(
                desired_roles=["AI Agent Engineer"],
                preferred_locations=["上海"],
                preferred_company_sizes=["成长型", "大型"],
            )
        )
    agent = FakeStreamingAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(
        "jobagent.cli.get_settings",
        lambda: Settings(
            _env_file=None,
            jobagent_state_db=database,
            jobagent_checkpoint_db=tmp_path / "checkpoints.db",
        ),
    )

    result = CliRunner().invoke(
        main,
        ["chat", "--session-id", "stream-test"],
        input="你好\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "候选人资料与求职意向已就绪" in result.output
    assert "请先提供基础简历" not in result.output


def test_chat_warns_about_expired_cookie_but_remains_usable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = FakeStreamingAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(
        "jobagent.cli.get_settings",
        lambda: Settings(
            _env_file=None,
            jobagent_state_db=tmp_path / "profile.db",
            jobagent_checkpoint_db=tmp_path / "checkpoints.db",
        ),
    )
    monkeypatch.setattr(
        "jobagent.cli.inspect_cookie_providers",
        lambda: (
            CookieProviderStatus(
                platform="boss",
                matched=True,
                cookie_count=10,
                expired_count=2,
                key_cookie_expired=True,
            ),
        ),
    )

    result = CliRunner().invoke(
        main,
        ["chat", "--session-id", "stream-test"],
        input="你好\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "Boss Cookie 中有 2 项已过期" in result.output
    assert "不会阻止继续使用" in result.output
    assert "JobAgent> 你好" in result.output


def test_chat_cli_creates_named_session_when_session_id_is_omitted(monkeypatch) -> None:
    agent = AutoSessionFakeAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)

    result = CliRunner().invoke(main, ["chat"], input="你好\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "当前会话：session-" in result.output
    assert len(set(agent.active_session_ids)) == 1
    assert agent.active_session_ids[0].startswith("session-")


def test_sessions_command_lists_and_switches_saved_conversation(monkeypatch) -> None:
    agent = SwitchingSessionFakeAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)

    result = CliRunner().invoke(
        main,
        ["chat"],
        input="/sessions\n1\n你好\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "1. previous-session" in result.output
    assert "已切换会话：previous-session" in result.output
    assert "这是之前的会话" in result.output
    assert agent.active_session_ids[-1] == "previous-session"


def test_status_command_renders_weekly_opportunity_summary_without_llm(monkeypatch) -> None:
    agent = StatusBoardFakeAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)

    result = CliRunner().invoke(main, ["chat"], input="/status week\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "本周状态" in result.output
    assert "已分析 2" in result.output
    assert "适合 1" in result.output
    assert "已投递 1" in result.output
    assert "示例科技 · AI Engineer" in result.output


def test_chat_cli_prints_thinking_and_tool_lines_before_final_answer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = ThinkingStreamingAgent()
    monkeypatch.setattr("jobagent.agent.build_job_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(
        "jobagent.cli.get_settings",
        lambda: Settings(
            _env_file=None,
            jobagent_state_db=tmp_path / "profile.db",
            jobagent_checkpoint_db=tmp_path / "checkpoints.db",
        ),
    )

    result = CliRunner().invoke(
        main,
        ["chat", "--session-id", "stream-test"],
        input="你好\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    thinking_at = result.output.index("模型思考：先理解用户意图")
    tool_at = result.output.index("工具结果 save_shared_url")
    answer_at = result.output.index("JobAgent> 你好")
    assert thinking_at < answer_at
    assert tool_at < answer_at
