"""Episodic conversation log: append-only daily JSONL + keyword search.

The JSONL files are the source of truth (full history, tool calls,
summaries); the Markdown memory file holds only curated facts. This
mirrors OpenClaw's curated-vs-episodic split - the log is never
injected wholesale, only searched on demand via the search_history
tool.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONTEXT_LINES = 2  # lines before/after a match (dialogue is a stream:
# the question sits on the previous line, the answer on the next)
_MAX_TEXT = 2000
_MAX_CONTEXT_CHARS = 120


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ConversationLog:
    """Append conversation turns to data/conversations/YYYY-MM-DD.jsonl."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    # -- write ------------------------------------------------------------------

    def append(
        self,
        *,
        session: str,
        role: str,
        text: str,
        tool: str | None = None,
    ) -> None:
        """One line per turn: user / assistant / tool."""

        record = {
            "ts": _now_iso(),
            "session": session,
            "role": role,
            "text": text[:_MAX_TEXT],
        }
        if tool:
            record["tool"] = tool
        path = self._root / f"{date.today():%Y-%m-%d}.jsonl"
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("conversation log append failed", exc_info=True)

    # -- search -------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        day: str | None = None,
        days: int = 7,
        limit: int = 15,
    ) -> dict[str, Any]:
        """Keyword search across daily logs with line context.

        Terms are OR-matched (whitespace-split, case-insensitive); the
        LLM filters relevance from the returned snippets.
        """

        terms = [t.lower() for t in query.split() if t.strip()]
        if not terms:
            return {"status": "ok", "query": query, "count": 0, "results": []}
        files = self._select_files(day, days)
        hits: list[dict[str, Any]] = []
        for path in files:
            entries = self._read(path)
            for i, entry in enumerate(entries):
                hay = str(entry.get("text", "")).lower()
                if any(t in hay for t in terms):
                    ctx = entries[max(0, i - _CONTEXT_LINES) : i + _CONTEXT_LINES + 1]
                    hits.append(
                        {
                            "file": path.name,
                            "ts": entry.get("ts", ""),
                            "role": entry.get("role", ""),
                            "match": str(entry.get("text", ""))[:200],
                            "context": [
                                f'{c.get("role", "?")}: '
                                f'{str(c.get("text", ""))[:_MAX_CONTEXT_CHARS]}'
                                for c in ctx
                            ],
                        }
                    )
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        return {
            "status": "ok",
            "query": query,
            "files_scanned": [p.name for p in files],
            "count": len(hits),
            "results": hits,
        }

    # -- internals -----------------------------------------------------------------

    def _select_files(self, day: str | None, days: int) -> list[Path]:
        if day:
            try:
                date.fromisoformat(day)  # validates; blocks path tricks
            except ValueError:
                return []
            p = self._root / f"{day}.jsonl"
            return [p] if p.exists() else []
        files = sorted(self._root.glob("*.jsonl"))
        return files[-days:]

    def _read(self, path: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            for line in path.open(encoding="utf-8"):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
        except OSError:
            logger.warning("conversation log read failed: %s", path)
        return out


def yesterday_iso() -> str:
    return f"{date.today() - timedelta(days=1):%Y-%m-%d}"
