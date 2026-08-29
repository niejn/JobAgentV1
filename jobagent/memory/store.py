"""Candidate memory: a plain Markdown file the agent reads at startup.

Design follows the converged pattern of OpenClaw / Hermes Agent
(MEMORY.md + USER.md - plain files, human-editable, "no hidden state"),
distilled 2026-08-29:

- Sections map fact kinds; every entry carries an observed date and
  active/superseded state (supersession in place, history preserved).
- Curated core: only stable facts/preferences and recent status live
  here; conversation detail stays in the JSONL episodic log
  (search_history), never in this file.
- The file IS the truth: the user may edit it by hand and the agent
  honours what it reads.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_SECTIONS: dict[str, str] = {
    "experience": "## 经历事实",
    "preference": "## 偏好",
    "status": "## 最近状态",
    "expired": "## 已过期（保留可追溯）",
}
_SECTION_BY_HEADING = {v: k for k, v in _SECTIONS.items()}
_HEADING_RE = re.compile(r"^(## .+)$", re.M)

_TEMPLATE = """# 候选人记忆

> 由 Agent 维护、用户可随时手动编辑。每条格式：
> `- [YYYY-MM-DD] 内容（active/superseded）`

## 经历事实

## 偏好

## 最近状态

## 已过期（保留可追溯）
"""


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    kind: str
    text: str
    active: bool


class CandidateMemoryStore:
    """Append/supersede facts in a sectioned Markdown file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text(_TEMPLATE, encoding="utf-8")

    # -- read -------------------------------------------------------------------

    def as_markdown(self) -> str:
        """The whole file (injected verbatim into the system prompt)."""

        try:
            return self._path.read_text(encoding="utf-8")
        except OSError:
            return _TEMPLATE

    def entries(self, kind: str | None = None) -> list[MemoryEntry]:
        """Parse entries; used by tests and supersede matching."""

        result: list[MemoryEntry] = []
        current: str | None = None
        for line in self.as_markdown().splitlines():
            if line.startswith("## "):
                current = _SECTION_BY_HEADING.get(line.strip())
                continue
            m = re.match(r"^- \[(\d{4}-\d{2}-\d{2})\]\s+(.+)$", line.strip())
            if m and current and (kind is None or current == kind):
                text = m.group(2)
                active = "superseded" not in text
                result.append(
                    MemoryEntry(kind=current, text=text.strip(), active=active)
                )
        return result

    # -- write ------------------------------------------------------------------

    def append_fact(self, kind: str, content: str) -> str:
        """Append one dated, active fact under its section."""

        if kind not in _SECTIONS or kind == "expired":
            raise ValueError(f"unsupported fact kind: {kind}")
        content = content.strip()
        if not content:
            raise ValueError("empty fact")
        entry = f"- [{date.today():%Y-%m-%d}] {content}（active）"
        self._insert_under_heading(_SECTIONS[kind], entry)
        logger.info("Memory fact saved [%s]: %s", kind, content[:60])
        return entry

    def supersede(self, kind: str, content: str, superseded_by: str) -> bool:
        """Mark an existing active fact superseded, keep it for history."""

        facts = self.entries(kind)
        match = next(
            (e for e in facts if e.active and content[:12] in e.text), None
        )
        if match is None:
            return False
        self.append_fact(kind, superseded_by)
        # Rewrite the matched LINE, preserving its `- [date]` prefix (the
        # parser only sees lines that carry one).
        lines = self.as_markdown().splitlines()
        needle = content[:12]
        for i, line in enumerate(lines):
            if line.startswith("- [") and needle in line and "superseded" not in line:
                prefix_end = line.index("] ") + 2  # keep "- [YYYY-MM-DD] "
                lines[i] = (
                    f"{line[:prefix_end]}~~{line[prefix_end:]}~~ （superseded）"
                )
                break
        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    # -- internals ----------------------------------------------------------------

    def _insert_under_heading(self, heading: str, entry: str) -> None:
        text = self.as_markdown()
        parts = _HEADING_RE.split(text)
        # split keeps [before, heading1, body1, heading2, body2, ...]
        for i in range(1, len(parts), 2):
            if parts[i].strip() == heading:
                body = parts[i + 1].rstrip("\n")
                body = (body + "\n" + entry + "\n\n") if body.strip() else (
                    "\n" + entry + "\n\n"
                )
                parts[i + 1] = body
                break
        else:  # heading missing (user edited the file): append a section
            parts.extend([heading, "\n" + entry + "\n\n"])
        self._path.write_text("".join(parts), encoding="utf-8")
