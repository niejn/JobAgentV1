"""FTS5-backed retrieval over conversation JSONL logs.

Replaces the naive per-line ``in hay`` grep in ``ConversationLog.search``
with a proper indexed search. Mirrors CareerDesk's memory design
(FTS5-first conversation index): BM25-ranked keyword/phrase retrieval
over the episodic store, with optional day scoping.

Tokenizer choice: ``trigram`` gives **substring** matching, which matches
how users actually search Chinese (``面试`` should find ``帮我准备面试``).
A ``LIKE`` fallback covers queries shorter than 3 chars that trigram
cannot index. Day scoping is applied in Python (FTS5 virtual tables do
not reliably honour ``LIKE`` on UNINDEXED columns), with an enlarged
fetch cap so the post-filter still reaches ``limit`` hits.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CONTEXT_CHARS = 200
_TERM = re.compile(r"[一-鿿A-Za-z0-9_]+")


class ConversationIndex:
    """BM25-ranked, substring-aware search over daily conversation logs."""

    def __init__(self, db_path: Path, conv_dir: Path) -> None:
        self._db: Path = Path(db_path).expanduser().resolve()
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._conv_dir: Path = Path(conv_dir).expanduser().resolve()
        self._conn: sqlite3.Connection = sqlite3.connect(self._db)
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
                text,
                ts UNINDEXED,
                session UNINDEXED,
                role UNINDEXED,
                file UNINDEXED,
                tokenize = 'trigram'
            );
            CREATE TABLE IF NOT EXISTS indexed_files (
                name TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                size INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ConversationIndex:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- indexing -------------------------------------------------------------

    def build(self) -> int:
        """Index any changed daily JSONL files. Returns count re-indexed."""

        if not self._conv_dir.exists():
            return 0
        reindexed = 0
        for f in sorted(self._conv_dir.glob("*.jsonl")):
            st = f.stat()
            row = self._conn.execute(
                "SELECT mtime, size FROM indexed_files WHERE name = ?", (str(f),)
            ).fetchone()
            if row and row[0] == st.st_mtime and row[1] == st.st_size:
                continue
            self._conn.execute("DELETE FROM messages WHERE file = ?", (str(f),))
            count = 0
            with f.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._conn.execute(
                        "INSERT INTO messages(text, ts, session, role, file) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            str(obj.get("text", "")),
                            str(obj.get("ts", "")),
                            str(obj.get("session", "")),
                            str(obj.get("role", "")),
                            str(f),
                        ),
                    )
                    count += 1
            self._conn.execute(
                "INSERT OR REPLACE INTO indexed_files(name, mtime, size) "
                "VALUES (?, ?, ?)",
                (str(f), st.st_mtime, st.st_size),
            )
            reindexed += 1
            logger.debug("indexed %d lines from %s", count, f.name)
        self._conn.commit()
        return reindexed

    # -- search ---------------------------------------------------------------

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = _TERM.findall(query)
        if not terms:
            return '"' + query.replace('"', '""') + '"'
        return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)

    def search(
        self, query: str, *, day: str | None = None, limit: int = 15
    ) -> dict[str, Any]:
        """Return results shaped like ``ConversationLog.search``."""

        self.build()
        cap = limit * 5 if day else limit
        q = self._fts_query(query)
        sql = (
            "SELECT rowid, text, ts, role FROM messages "
            "WHERE messages MATCH ? ORDER BY bm25(messages) LIMIT ?"
        )
        hits = self._conn.execute(sql, (q, cap)).fetchall()

        # Trigram cannot index sub-3-char terms; fall back to a LIKE scan.
        if not hits and any(len(t) < 3 for t in _TERM.findall(query)):
            like = "%" + query.replace("%", "") + "%"
            hits = self._conn.execute(
                "SELECT rowid, text, ts, role FROM messages "
                "WHERE text LIKE ? LIMIT ?",
                (like, cap),
            ).fetchall()

        results = [
            {
                "ts": ts,
                "role": role,
                "match": text[:_MAX_CONTEXT_CHARS],
                "context": self._context(rowid),
            }
            for rowid, text, ts, role in hits
        ]

        # Day scoping in Python (FTS5 UNINDEXED-column LIKE is unreliable).
        if day:
            results = [r for r in results if str(r["ts"]).startswith(day)]
            results = results[:limit]

        return {"status": "ok", "query": query, "count": len(results), "results": results}

    def _context(self, rowid: int, span: int = 2) -> list[str]:
        rows = self._conn.execute(
            "SELECT role, text FROM messages "
            "WHERE rowid BETWEEN ? AND ? ORDER BY rowid",
            (rowid - span, rowid + span),
        ).fetchall()
        return [
            f"{role}: {text[:_MAX_CONTEXT_CHARS]}" for role, text in rows
        ]
