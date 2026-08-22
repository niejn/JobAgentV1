"""Local-file persistence for JD analysis artifacts and lightweight manifests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any


class FitDecision(StrEnum):
    SUITABLE = "suitable"
    UNCERTAIN = "uncertain"
    UNSUITABLE = "unsuitable"


class ApplicationState(StrEnum):
    NOT_APPLIED = "not_applied"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    CLOSED = "closed"


class StatusPeriod(StrEnum):
    ALL = "all"
    WEEK = "week"
    MONTH = "month"


@dataclass(frozen=True, slots=True)
class SavedOpportunityAnalysis:
    opportunity_id: str
    company: str
    role: str
    fit: FitDecision
    application_state: ApplicationState
    analysis_version: int
    jd_path: Path
    analysis_path: Path
    manifest_path: Path
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OpportunityStatusItem:
    opportunity_id: str
    company: str
    role: str
    fit: FitDecision
    application_state: ApplicationState
    analysis_version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OpportunityStatusBoard:
    period: StatusPeriod
    analyzed: int
    suitable: int
    uncertain: int
    unsuitable: int
    applied: int
    items: tuple[OpportunityStatusItem, ...]


class LocalOpportunityArtifacts:
    """Persist one JD and versioned analysis reports behind a small interface."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = root.expanduser().resolve()
        self._clock = clock or (lambda: datetime.now().astimezone())

    def save_analysis(
        self,
        *,
        company: str,
        role: str,
        job_description: str,
        analysis_report: str,
        fit: FitDecision,
        application_state: ApplicationState = ApplicationState.NOT_APPLIED,
    ) -> SavedOpportunityAnalysis:
        """Save immutable JD text and append one Markdown analysis version."""

        company = company.strip()
        role = role.strip()
        job_description = job_description.strip()
        analysis_report = analysis_report.strip()
        if not company or not role or not job_description or not analysis_report:
            raise ValueError("company, role, JD, and analysis report are required")
        opportunity_id = _opportunity_id(company, role, job_description)
        directory = self._root / opportunity_id
        manifest_path = directory / "manifest.json"
        jd_path = directory / "jd.md"
        directory.mkdir(parents=True, exist_ok=True)
        manifest = _read_manifest(manifest_path)
        now = self._clock()
        if manifest is None:
            manifest = {
                "schema_version": 1,
                "opportunity_id": opportunity_id,
                "company": company,
                "role": role,
                "fit": fit.value,
                "application_state": application_state.value,
                "jd": {
                    "path": "jd.md",
                    "sha256": _sha256(job_description),
                },
                "analyses": [],
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }
            _atomic_write_text(jd_path, job_description)
        analyses = manifest["analyses"]
        version = len(analyses) + 1
        analysis_name = f"analysis-v{version:03d}.md"
        analysis_path = directory / analysis_name
        _atomic_write_text(analysis_path, analysis_report)
        analyses.append(
            {
                "version": version,
                "path": analysis_name,
                "sha256": _sha256(analysis_report),
                "created_at": now.isoformat(),
            }
        )
        manifest["fit"] = fit.value
        manifest["application_state"] = application_state.value
        manifest["updated_at"] = now.isoformat()
        _atomic_write_json(manifest_path, manifest)
        return SavedOpportunityAnalysis(
            opportunity_id=opportunity_id,
            company=company,
            role=role,
            fit=fit,
            application_state=application_state,
            analysis_version=version,
            jd_path=jd_path,
            analysis_path=analysis_path,
            manifest_path=manifest_path,
            created_at=now,
        )

    def status_board(
        self,
        *,
        period: StatusPeriod = StatusPeriod.ALL,
        now: datetime | None = None,
    ) -> OpportunityStatusBoard:
        """Summarize analyzed opportunities for all time, current week, or month."""

        reference = now or self._clock()
        start = _period_start(period, reference)
        items: list[OpportunityStatusItem] = []
        if self._root.is_dir():
            for manifest_path in self._root.glob("*/manifest.json"):
                try:
                    manifest = _read_manifest(manifest_path)
                    if manifest is None:
                        continue
                    analyses = manifest["analyses"]
                    if not analyses:
                        continue
                    analyzed_at = datetime.fromisoformat(
                        str(analyses[-1]["created_at"])
                    )
                    if start is not None and analyzed_at < start:
                        continue
                    items.append(
                        OpportunityStatusItem(
                            opportunity_id=str(manifest["opportunity_id"]),
                            company=str(manifest["company"]),
                            role=str(manifest["role"]),
                            fit=FitDecision(str(manifest["fit"])),
                            application_state=ApplicationState(
                                str(manifest["application_state"])
                            ),
                            analysis_version=len(analyses),
                            updated_at=analyzed_at,
                        )
                    )
                except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return OpportunityStatusBoard(
            period=period,
            analyzed=len(items),
            suitable=sum(item.fit is FitDecision.SUITABLE for item in items),
            uncertain=sum(item.fit is FitDecision.UNCERTAIN for item in items),
            unsuitable=sum(item.fit is FitDecision.UNSUITABLE for item in items),
            applied=sum(
                item.application_state is not ApplicationState.NOT_APPLIED
                for item in items
            ),
            items=tuple(items),
        )

    def update_application_state(
        self,
        opportunity_id: str,
        state: ApplicationState,
    ) -> OpportunityStatusItem:
        """Update application progress for an existing analyzed opportunity."""

        if Path(opportunity_id).name != opportunity_id or not opportunity_id.strip():
            raise ValueError("invalid opportunity_id")
        manifest_path = (self._root / opportunity_id / "manifest.json").resolve()
        if not manifest_path.is_relative_to(self._root):
            raise ValueError("opportunity is outside artifact root")
        manifest = _read_manifest(manifest_path)
        if manifest is None:
            raise KeyError(f"opportunity not found: {opportunity_id}")
        updated_at = self._clock()
        manifest["application_state"] = state.value
        manifest["updated_at"] = updated_at.isoformat()
        _atomic_write_json(manifest_path, manifest)
        return OpportunityStatusItem(
            opportunity_id=str(manifest["opportunity_id"]),
            company=str(manifest["company"]),
            role=str(manifest["role"]),
            fit=FitDecision(str(manifest["fit"])),
            application_state=state,
            analysis_version=len(manifest["analyses"]),
            updated_at=updated_at,
        )


def _opportunity_id(company: str, role: str, job_description: str) -> str:
    readable = _slug(f"{company}-{role}")
    identity = _sha256(f"{company}\n{role}\n{job_description}")[:12]
    return f"{readable}-{identity}"


def _period_start(period: StatusPeriod, now: datetime) -> datetime | None:
    if period is StatusPeriod.ALL:
        return None
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period is StatusPeriod.WEEK:
        return day_start - timedelta(days=day_start.weekday())
    return day_start.replace(day=1)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-_.")
    return (normalized or "opportunity")[:64]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("analyses"), list):
        raise ValueError(f"invalid opportunity manifest: {path.name}")
    return payload


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
