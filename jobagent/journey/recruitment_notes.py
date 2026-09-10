"""Durable XHS recruitment-note snapshots and deterministic first-pass extraction."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from jobagent.journey.store import _enable_wal

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_ROLE = re.compile(
    r"(?m)^(?:招聘|招募|岗位|职位)\s*[:：\-]?\s*([^\n，。；;]{2,40}(?:工程师|开发|运营|产品经理|设计师|研究员))"
)
_BULLET_ROLE = re.compile(
    r"^(?:[-•]\s*)?([^：:\n]{2,40}(?:工程师|开发|运营|产品经理|设计师|研究员))"
)
_TECH = ("Python", "Golang", "Java", "AI Agent", "LangGraph", "RAG", "MCP", "大模型")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recruitment_notes (
 note_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, title TEXT NOT NULL,
 body TEXT NOT NULL, image_ocr TEXT NOT NULL, comments_json TEXT NOT NULL,
 content_hash TEXT NOT NULL, features_json TEXT NOT NULL, positions_json TEXT NOT NULL,
 contacts_json TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recruitment_note_selections (
 note_id TEXT PRIMARY KEY, position_index INTEGER NOT NULL, company TEXT NOT NULL,
 selected_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class CandidatePosition:
    title: str
    responsibilities: tuple[str, ...]
    requirements: tuple[str, ...]
    evidence: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ContactEvidence:
    email: str
    source: str
    confidence: str


@dataclass(frozen=True, slots=True)
class CommentSnapshot:
    comment_id: str
    author_id: str
    author_name: str
    text: str
    is_note_author: bool


@dataclass(frozen=True, slots=True)
class RecruitmentNote:
    note_id: str
    source_url: str
    title: str
    body: str
    image_ocr: str
    comments: tuple[CommentSnapshot, ...]
    features: tuple[str, ...]
    positions: tuple[CandidatePosition, ...]
    contacts: tuple[ContactEvidence, ...]


def extract_note(
    note_id: str,
    source_url: str,
    title: str,
    body: str,
    image_ocr: str,
    comments: tuple[CommentSnapshot, ...] = (),
) -> RecruitmentNote:
    """Create a deterministic, provenance-preserving first-pass snapshot."""

    sources: list[tuple[str, str, str]] = [
        ("body", body, "verified"),
        ("image_ocr", image_ocr, "verified"),
    ]
    # Only the note author's comments can be recruitment evidence. Visitor
    # comments are intentionally not sent to the extraction model.
    sources.extend(
        ("author_comment", comment.text, "review_required")
        for comment in comments
        if comment.is_note_author
    )
    contacts: list[ContactEvidence] = []
    seen_emails: set[str] = set()
    for source, text, confidence in sources:
        for email in _EMAIL.findall(text):
            normalized = email.lower()
            if normalized not in seen_emails:
                contacts.append(ContactEvidence(normalized, source, confidence))
                seen_emails.add(normalized)
    combined = "\n".join((title, body, image_ocr))
    positions = _extract_candidate_positions(body, image_ocr)
    if not positions and "招聘" in combined:
        positions = (
            CandidatePosition(title.strip() or "未命名招聘岗位", (), (), (("body", body[:500]),)),
        )
    features = tuple(item for item in _TECH if item.lower() in combined.lower())
    return RecruitmentNote(
        note_id, source_url, title, body, image_ocr, comments, features, positions, tuple(contacts)
    )


def _extract_candidate_positions(body: str, image_ocr: str) -> tuple[CandidatePosition, ...]:
    """Split source lines into positions without inventing JD fields."""

    positions: list[CandidatePosition] = []
    for source, text in (("body", body), ("image_ocr", image_ocr)):
        current = ""
        responsibilities: list[str] = []
        requirements: list[str] = []
        evidence: list[tuple[str, str]] = []
        section = ""

        for line in (item.strip(" -•\t") for item in text.splitlines() if item.strip()):
            match = _ROLE.match(line) or _BULLET_ROLE.match(line)
            if match:
                if current:
                    positions.append(
                        CandidatePosition(
                            current,
                            tuple(responsibilities),
                            tuple(requirements),
                            tuple(evidence),
                        )
                    )
                current, responsibilities, requirements = match.group(1).strip(), [], []
                evidence, section = [(source, line)], ""
                continue
            if not current:
                continue
            if any(token in line for token in ("投递", "简历", "邮箱", "邮件")):
                section = ""
                continue
            if any(token in line for token in ("职责", "负责", "工作内容")):
                section = "responsibilities"
            elif any(token in line for token in ("要求", "任职", "熟悉", "经验", "能力")):
                section = "requirements"
            if section == "responsibilities":
                responsibilities.append(line)
                evidence.append((source, line))
            elif section == "requirements":
                requirements.append(line)
                evidence.append((source, line))
        if current:
            positions.append(
                CandidatePosition(
                    current,
                    tuple(responsibilities),
                    tuple(requirements),
                    tuple(evidence),
                )
            )
    return tuple(positions)


class RecruitmentNoteRegistry:
    """Small persistence seam for source snapshots; replacing a note keeps its ID stable."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path.expanduser().resolve())
        self._connection.execute("PRAGMA busy_timeout = 5000")
        _enable_wal(self._connection)
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> RecruitmentNoteRegistry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def save(self, note: RecruitmentNote) -> None:
        digest = hashlib.sha256(
            json.dumps(asdict(note), ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        now = int(time.time() * 1000)
        self._connection.execute(
            """INSERT INTO recruitment_notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET source_url=excluded.source_url, title=excluded.title,
            body=excluded.body, image_ocr=excluded.image_ocr, comments_json=excluded.comments_json,
            content_hash=excluded.content_hash, features_json=excluded.features_json,
            positions_json=excluded.positions_json,
            contacts_json=excluded.contacts_json, updated_at=excluded.updated_at""",
            (
                note.note_id,
                note.source_url,
                note.title,
                note.body,
                note.image_ocr,
                json.dumps([asdict(comment) for comment in note.comments], ensure_ascii=False),
                digest,
                json.dumps(note.features, ensure_ascii=False),
                json.dumps([asdict(p) for p in note.positions], ensure_ascii=False),
                json.dumps([asdict(c) for c in note.contacts], ensure_ascii=False),
                now,
                now,
            ),
        )
        self._connection.commit()

    def get(self, note_id: str) -> RecruitmentNote:
        row = self._connection.execute(
            "SELECT * FROM recruitment_notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        if row is None:
            raise KeyError(note_id)
        return RecruitmentNote(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            tuple(CommentSnapshot(**value) for value in json.loads(row[5])),
            tuple(json.loads(row[7])),
            tuple(
                CandidatePosition(
                    value["title"],
                    tuple(value.get("responsibilities", [])),
                    tuple(value.get("requirements", [])),
                    tuple(tuple(item) for item in value.get("evidence", [])),
                )
                for value in json.loads(row[8])
            ),
            tuple(ContactEvidence(**v) for v in json.loads(row[9])),
        )

    def recent_features(self, *, limit: int = 20) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT features_json FROM recruitment_notes ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return tuple(dict.fromkeys(feature for row in rows for feature in json.loads(row[0])))

    def select_position(
        self, *, note_id: str, position_index: int, company: str
    ) -> CandidatePosition:
        note = self.get(note_id)
        if position_index < 0 or position_index >= len(note.positions):
            raise IndexError("candidate position index is out of range")
        cleaned_company = company.strip()
        if not cleaned_company:
            raise ValueError("company is required before creating a Journey")
        self._connection.execute(
            """INSERT INTO recruitment_note_selections VALUES (?, ?, ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET position_index=excluded.position_index,
            company=excluded.company, selected_at=excluded.selected_at""",
            (note_id, position_index, cleaned_company, int(time.time() * 1000)),
        )
        self._connection.commit()
        return note.positions[position_index]

    def selected_position(self, note_id: str) -> tuple[str, CandidatePosition]:
        row = self._connection.execute(
            "SELECT position_index, company FROM recruitment_note_selections WHERE note_id = ?",
            (note_id,),
        ).fetchone()
        if row is None:
            raise KeyError("candidate position has not been selected")
        return str(row[1]), self.get(note_id).positions[int(row[0])]
