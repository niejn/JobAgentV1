"""Tests for the agent memory layer (curated Markdown + episodic JSONL)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobagent.config import Settings
from jobagent.memory.conversation_log import ConversationLog
from jobagent.memory.store import CandidateMemoryStore
from jobagent.prompts import build_system_prompt
from jobagent.tools.memory import (
    build_save_user_fact_tool,
    build_search_history_tool,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        jobagent_state_db=tmp_path / "state.db",
        jobagent_checkpoint_db=tmp_path / "checkpoints.db",
    )


def test_memory_store_creates_template_and_appends(tmp_path: Path) -> None:
    store = CandidateMemoryStore(tmp_path / "memory" / "candidate_memory.md")
    store.append_fact("experience", "曾在中信期货做交易所行情爬虫")
    store.append_fact("preference", "主攻 Agent 方向，爬虫岗仅作备选")

    md = store.as_markdown()
    assert "曾在中信期货" in md and "主攻 Agent 方向" in md
    entries = store.entries()
    assert len(entries) == 2
    assert entries[0].kind == "experience" and entries[0].active


def test_memory_store_supersede_keeps_history(tmp_path: Path) -> None:
    store = CandidateMemoryStore(tmp_path / "m.md")
    store.append_fact("preference", "不想看外企")
    ok = store.supersede("preference", "不想看外企", "外企也可以考虑")

    assert ok
    prefs = store.entries("preference")
    assert len(prefs) == 2
    assert prefs[0].active is False  # old kept, marked superseded
    assert prefs[1].active is True
    assert "superseded" in prefs[0].text


def test_memory_store_rejects_bad_kind(tmp_path: Path) -> None:
    store = CandidateMemoryStore(tmp_path / "m.md")
    with pytest.raises(ValueError):
        store.append_fact("expired", "不该直接写过期区")


def test_conversation_log_append_and_search(tmp_path: Path) -> None:
    log = ConversationLog(tmp_path / "conversations")
    log.append(session="s1", role="user", text="帮我找上海的大模型岗位")
    log.append(
        session="s1", role="assistant", text="推荐5个：①字节-大模型应用 ②美团"
    )
    log.append(session="s1", role="tool", text="discover_boss_jobs: 12 results")

    result = log.search("推荐")
    assert result["count"] == 1
    hit = result["results"][0]
    assert "字节" in hit["match"]
    # context carries the user question on the previous line
    assert any("大模型岗位" in c for c in hit["context"])


def test_conversation_log_day_filter_and_terms(tmp_path: Path) -> None:
    log = ConversationLog(tmp_path / "conversations")
    log.append(session="s1", role="user", text="测试今天的内容")

    from datetime import date

    today = f"{date.today():%Y-%m-%d}"
    assert log.search("不存在词")["count"] == 0
    assert log.search("内容", day=today)["count"] == 1
    assert log.search("内容", day="2020-01-01")["count"] == 0  # no such file
    assert log.search("今天 内容")["count"] == 1  # OR terms


@pytest.mark.asyncio
async def test_save_user_fact_tool_persists(tmp_path: Path) -> None:
    tool = build_save_user_fact_tool(_settings(tmp_path))
    result = await tool.coroutine(kind="status", content="梭翱(夏女士) 已手动婉拒")
    assert result["status"] == "completed"
    md = (tmp_path / "memory" / "candidate_memory.md").read_text(encoding="utf-8")
    assert "梭翱" in md


@pytest.mark.asyncio
async def test_search_history_tool_reads_log(tmp_path: Path) -> None:
    log_dir = tmp_path / "conversations"
    log_dir.mkdir()
    from datetime import date

    row = {
        "ts": "2026-08-29T10:00:00",
        "session": "s1",
        "role": "assistant",
        "text": "推荐批次：字节/美团/拼多多",
    }
    (log_dir / f"{date.today():%Y-%m-%d}.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tool = build_search_history_tool(_settings(tmp_path))
    result = await tool.coroutine(query="推荐批次")
    assert result["count"] == 1


def test_system_prompt_includes_memory(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        registered_tools=set(),
        memory_markdown="# 候选人记忆\n\n- [2026-08-29] 主攻 Agent（active）",
    )
    assert "候选人记忆" in prompt and "主攻 Agent" in prompt
