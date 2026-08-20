"""Discover recent, non-commercial XHS interview-experience notes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from jobclaw.scraper.xhs_backend import (
    DownloadedXhsNote,
    XhsFetchedNote,
    XhsNoteReference,
)

_SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
_PUBLISHED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"

# Explainable Phase-1 heuristics. A later classifier can replace the scoring
# without changing the Spider_XHS transport or discovery result contract.
_SELLER_SIGNALS: dict[str, int] = {
    "资料包": 2,
    "付费": 2,
    "课程": 2,
    "面试辅导": 2,
    "简历优化": 2,
    "简历代写": 2,
    "包过": 3,
    "保offer": 3,
    "私信领取": 2,
    "加微": 2,
    "加v": 2,
    "进群": 2,
    "评论区扣": 2,
    "学员": 2,
    "训练营": 2,
    "上岸资料": 2,
    "接单": 2,
    "咨询": 1,
}
_SELLER_THRESHOLD = 2


class XhsDiscoveryBackend(Protocol):
    """Backend capabilities required by the discovery workflow."""

    async def search_notes(
        self, query: str, *, limit: int | None = None, sort: int = 0
    ) -> list[XhsNoteReference]: ...

    async def list_user_notes(
        self, user_id: str, *, limit: int | None = None
    ) -> list[XhsNoteReference]: ...

    async def fetch_note(self, url: str) -> XhsFetchedNote: ...

    async def download_note(
        self, url: str, *, output_dir: Path | None = None
    ) -> DownloadedXhsNote: ...


@dataclass(frozen=True, slots=True)
class XhsDiscoveryRequest:
    """User-facing criteria for discovering interview experience posts."""

    company: str | None = None
    role: str | None = None
    city: str | None = None
    keyword: str | None = None
    user_id: str | None = None
    days: int = 90
    limit: int = 20
    download_limit: int = 0
    output_dir: Path | None = None

    def __post_init__(self) -> None:
        values = (self.company, self.role, self.city, self.keyword, self.user_id)
        if not any(value and value.strip() for value in values):
            raise ValueError("Provide company, role, city, keyword, or user_id")
        if self.days < 1 or self.limit < 1 or self.download_limit < 0:
            raise ValueError("days and limit must be positive; download_limit cannot be negative")

    @property
    def query(self) -> str:
        """Build a compact XHS query and bias results toward interview posts."""

        parts: list[str] = []
        for value in (self.company, self.role, self.city, self.keyword, "面经"):
            cleaned = value.strip() if value else ""
            if cleaned and cleaned not in parts:
                parts.append(cleaned)
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class XhsPostCandidate:
    """A fetched note with transparent freshness and seller-risk decisions."""

    note: XhsFetchedNote
    is_fresh: bool
    seller_risk: bool
    risk_score: int
    risk_flags: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.is_fresh and not self.seller_risk


@dataclass(frozen=True, slots=True)
class XhsDiscoveryResult:
    """All evaluated candidates, decisions, and optional downloads."""

    query: str | None
    candidates: tuple[XhsPostCandidate, ...]
    downloads: tuple[DownloadedXhsNote, ...]

    @property
    def accepted(self) -> tuple[XhsPostCandidate, ...]:
        return tuple(item for item in self.candidates if item.accepted)

    @property
    def rejected(self) -> tuple[XhsPostCandidate, ...]:
        return tuple(item for item in self.candidates if not item.accepted)


async def discover_xhs_notes(
    backend: XhsDiscoveryBackend,
    request: XhsDiscoveryRequest,
    *,
    now: datetime | None = None,
) -> XhsDiscoveryResult:
    """Search, inspect, filter, and optionally download matching XHS notes."""

    if request.user_id:
        references = await backend.list_user_notes(request.user_id.strip(), limit=request.limit)
        query: str | None = None
    else:
        query = request.query
        references = await backend.search_notes(query, limit=request.limit, sort=1)

    current = now or datetime.now(_SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_SHANGHAI)
    cutoff = current.astimezone(_SHANGHAI) - timedelta(days=request.days)

    fetched_notes: list[XhsFetchedNote] = []
    for reference in references:
        fetched_notes.append(await backend.fetch_note(reference.url))

    account_flags: dict[str, set[str]] = {}
    for note in fetched_notes:
        flags, _ = seller_risk(note)
        account_flags.setdefault(note.author_id, set()).update(flags)

    candidates: list[XhsPostCandidate] = []
    for note in fetched_notes:
        local_flags, _ = seller_risk(note)
        related_flags = tuple(sorted(account_flags.get(note.author_id, set())))
        flags = tuple(dict.fromkeys((*local_flags, *related_flags)))
        score = sum(_SELLER_SIGNALS[term] for term in flags)
        candidates.append(
            XhsPostCandidate(
                note=note,
                is_fresh=is_recent(note.published_at, cutoff),
                seller_risk=score >= _SELLER_THRESHOLD,
                risk_score=score,
                risk_flags=flags,
            )
        )
    candidates.sort(key=lambda item: published_sort_key(item.note.published_at), reverse=True)

    downloads: list[DownloadedXhsNote] = []
    accepted = (item for item in candidates if item.accepted)
    for candidate in accepted:
        if len(downloads) >= request.download_limit:
            break
        downloads.append(
            await backend.download_note(candidate.note.url, output_dir=request.output_dir)
        )

    return XhsDiscoveryResult(query, tuple(candidates), tuple(downloads))


def seller_risk(note: XhsFetchedNote) -> tuple[tuple[str, ...], int]:
    """Return matched commercial signals and their cumulative risk score."""

    text = f"{note.author_name}\n{note.title}\n{note.body}".lower()
    flags = tuple(term for term in _SELLER_SIGNALS if term in text)
    return flags, sum(_SELLER_SIGNALS[term] for term in flags)


def is_recent(published_at: str | None, cutoff: datetime) -> bool:
    """Treat missing or invalid platform timestamps as not fresh."""

    if not published_at:
        return False
    try:
        published = datetime.strptime(published_at, _PUBLISHED_AT_FORMAT).replace(
            tzinfo=_SHANGHAI
        )
    except ValueError:
        return False
    return published >= cutoff


def published_sort_key(published_at: str | None) -> datetime:
    """Return a sortable timestamp; unknown dates sort last."""

    if published_at:
        try:
            return datetime.strptime(published_at, _PUBLISHED_AT_FORMAT).replace(
                tzinfo=_SHANGHAI
            )
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=_SHANGHAI)
