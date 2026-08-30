"""Tests for the FTS5 conversation index (BM25-ranked substring search)."""

from __future__ import annotations

import json
from pathlib import Path

from jobagent.memory.conversation_index import ConversationIndex


def _write_log(conv_dir: Path, name: str, lines: list[dict]) -> None:
    conv_dir.mkdir(parents=True, exist_ok=True)
    with (conv_dir / name).open("w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, ensure_ascii=False) + "\n")


def _sample(conv_dir: Path) -> None:
    _write_log(
        conv_dir,
        "2026-08-29.jsonl",
        [
            {"ts": "2026-08-29T10:00:00+08:00", "session": "s1", "role": "user",
             "text": "帮我准备字节的爬虫岗位面试"},
            {"ts": "2026-08-29T10:00:01+08:00", "session": "s1", "role": "assistant",
             "text": "建议复习爬虫反爬与分布式调度"},
            {"ts": "2026-08-29T11:00:00+08:00", "session": "s1", "role": "user",
             "text": "简历里写过大模型 Agent 项目"},
        ],
    )


def test_build_indexes_and_search_substring(tmp_path: Path) -> None:
    conv_dir = tmp_path / "conversations"
    _sample(conv_dir)
    idx = ConversationIndex(tmp_path / "memory" / "idx.db", conv_dir)
    try:
        assert idx.build() == 1  # one file indexed
        res = idx.search("爬虫")
        assert res["status"] == "ok"
        assert res["count"] >= 1
        texts = [r["match"] for r in res["results"]]
        # substring match: 爬虫 finds both lines containing it
        assert any("爬虫" in t for t in texts)
    finally:
        idx.close()


def test_search_is_idempotent_on_rebuild(tmp_path: Path) -> None:
    conv_dir = tmp_path / "conversations"
    _sample(conv_dir)
    idx = ConversationIndex(tmp_path / "memory" / "idx.db", conv_dir)
    try:
        assert idx.build() == 1
        # unchanged file -> no reindex
        assert idx.build() == 0
        res = idx.search("大模型")
        assert res["count"] == 1
        assert "大模型" in res["results"][0]["match"]
    finally:
        idx.close()


def test_day_filter(tmp_path: Path) -> None:
    conv_dir = tmp_path / "conversations"
    _sample(conv_dir)
    idx = ConversationIndex(tmp_path / "memory" / "idx.db", conv_dir)
    try:
        idx.build()
        hit = idx.search("爬虫", day="2026-08-29")
        assert hit["count"] >= 1
        miss = idx.search("爬虫", day="2020-01-01")
        assert miss["count"] == 0
    finally:
        idx.close()


def test_context_includes_neighbors(tmp_path: Path) -> None:
    conv_dir = tmp_path / "conversations"
    _sample(conv_dir)
    idx = ConversationIndex(tmp_path / "memory" / "idx.db", conv_dir)
    try:
        idx.build()
        res = idx.search("分布式调度")  # only in the assistant reply
        assert res["count"] == 1
        ctx = res["results"][0]["context"]
        # context carries the surrounding user/assistant lines
        assert any("爬虫" in c for c in ctx)
    finally:
        idx.close()
