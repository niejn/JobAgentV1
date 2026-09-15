"""Boss circuit breaker: stop feeding warlock negative samples.

Why (2026-08-28, four live incidents in one day): every blank/reject the
tools hit deepens the account's risk score, and retrying on a flagged
account makes things worse. The breaker trips after N consecutive
"page-killed" failures and refuses ALL Boss tool calls for a cooldown
window, returning a structured "cooling down" result instead.

Trip conditions (error_type seen in tool results):
  chat_page_blocked / resume_page_blocked / page_lost / tab_pool_timeout

Success resets the consecutive counter. State lives in a tiny JSON file
under the state dir so short-lived `chat -c` processes share it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TRIP_ERRORS = frozenset(
    {
        "chat_page_blocked",
        "resume_page_blocked",
        "page_lost",
        "tab_pool_timeout",
    }
)
_THRESHOLD = 3
_BASE_COOLDOWN_SECONDS = 30
_MAX_COOLDOWN_SECONDS = 60


class BossCircuit:
    """File-backed consecutive-failure breaker shared across processes."""

    def __init__(
        self,
        state_path: Path,
        *,
        cooldown_seconds: int = _BASE_COOLDOWN_SECONDS,
        max_cooldown_seconds: int = _MAX_COOLDOWN_SECONDS,
    ) -> None:
        self._path = state_path
        self._max_cooldown_seconds = max(1, min(max_cooldown_seconds, 60))
        self._cooldown_seconds = min(max(1, cooldown_seconds), self._max_cooldown_seconds)

    # -- public API ---------------------------------------------------------

    def check(self) -> dict[str, Any] | None:
        """Return the refusal dict when tripped, else None.

        Setting JOBAGENT_BOSS_IGNORE_CIRCUIT=1 bypasses the breaker for
        debugging - each forced call still runs (and still feeds warlock
        negative samples while the account is flagged, so use it knowing
        the breaker exists precisely to prevent that).
        """

        if os.environ.get("JOBAGENT_BOSS_IGNORE_CIRCUIT", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            logger.warning(
                "Boss circuit BYPASSED via JOBAGENT_BOSS_IGNORE_CIRCUIT"
            )
            return None
        state = self._load()
        until = state.get("until")
        if not until:
            return None
        try:
            until_dt = datetime.fromisoformat(until)
        except ValueError:
            return None
        now = datetime.now().astimezone()
        if now >= until_dt:
            # Keep the trip count for exponential backoff across repeated
            # cooldown windows; a successful Boss operation resets it.
            trip_count = int(state.get("trip_count", 0))
            self._save({"failures": 0, "trip_count": trip_count} if trip_count else {})
            return None
        # Clamp every persisted legacy deadline, including states that already
        # contain trip_count from the former 60-minute exponential policy.
        if until_dt - now > timedelta(seconds=self._max_cooldown_seconds):
            until_dt = now + timedelta(seconds=self._max_cooldown_seconds)
            state = {
                "failures": int(state.get("failures", _THRESHOLD)),
                "trip_count": int(state.get("trip_count", 1)),
                "cooldown_seconds": self._max_cooldown_seconds,
                "until": until_dt.isoformat(),
            }
            self._save(state)
            until = until_dt.isoformat()
        return {
            "status": "failed",
            "error_type": "circuit_open",
            "message": (
                f"Boss 熔断中（连续被反爬拦截），{until_dt:%H:%M} 后自动恢复。"
                "冷却期间请勿手动访问自动化页面；人工浏览不受影响。"
            ),
            "retry_after": until,
        }

    def record(self, result: dict[str, Any]) -> None:
        """Fold one tool result into the breaker state."""

        if result.get("status") == "ok":
            if self._load().get("failures"):
                self._save({})
            return
        if result.get("error_type") not in _TRIP_ERRORS:
            return
        state = self._load()
        failures = int(state.get("failures", 0)) + 1
        if failures >= _THRESHOLD:
            trip_count = int(state.get("trip_count", 0)) + 1
            cooldown = min(
                self._cooldown_seconds * (2 ** (trip_count - 1)),
                self._max_cooldown_seconds,
            )
            until = datetime.now().astimezone() + timedelta(seconds=cooldown)
            self._save({
                "failures": failures,
                "trip_count": trip_count,
                "cooldown_seconds": cooldown,
                "until": until.isoformat(),
            })
            logger.warning(
                "Boss circuit OPEN until %s after %d failures",
                until,
                failures,
            )
        else:
            self._save({"failures": failures, "trip_count": int(state.get("trip_count", 0))})

    # -- storage -------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _save(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )


def boss_circuit_path(state_db: Path) -> Path:
    """Stable path for the breaker file next to the state db."""

    return state_db.parent / "boss_circuit.json"
