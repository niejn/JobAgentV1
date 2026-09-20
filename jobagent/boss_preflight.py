"""Fresh, fail-closed Boss session checks for the Boss subagent.

The check intentionally lives outside prompts and checkpoints.  A model may
remember an old "ready" answer; platform credentials and circuit state are
dynamic and must be checked again at the tool boundary.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from jobagent.auth.cookie_manager import CookieNotFoundError, get_cookies
from jobagent.config import Settings
from jobagent.scraper.boss import get_boss_cooldown

_INVALIDATING_ERRORS = {
    "boss_login_required", "code=7", "page_lost", "page_token_missing",
    "boss_cookie_sync_failed",
}


@dataclass(frozen=True, slots=True)
class BossSessionStatus:
    ready: bool
    reason: str = ""


class BossSessionPreflight:
    """Cache a successful credential/circuit check only for a short TTL."""

    def __init__(self, settings: Settings, *, ttl_seconds: float = 180.0) -> None:
        self._settings = settings
        self._ttl_seconds = ttl_seconds
        self._ready_until = 0.0

    def invalidate(self) -> None:
        self._ready_until = 0.0

    async def check(self) -> BossSessionStatus:
        allowed, _, reason = get_boss_cooldown().check()
        if not allowed:
            self.invalidate()
            return BossSessionStatus(False, "circuit_open" if reason else "cooldown_active")
        if time.monotonic() < self._ready_until:
            return BossSessionStatus(True)
        try:
            cookies = await get_cookies("boss", self._settings)
        except CookieNotFoundError:
            self.invalidate()
            return BossSessionStatus(False, "boss_login_required")
        names = {str(item.get("name") or "") for item in cookies}
        # A direct Boss session needs all three; accepting a lone wt2 would let
        # a stale export pass the guard and fail later in a write tool.
        if not {"wt2", "bst", "__zp_stoken__"}.issubset(names):
            self.invalidate()
            return BossSessionStatus(False, "boss_cookie_sync_failed")
        self._ready_until = time.monotonic() + self._ttl_seconds
        return BossSessionStatus(True)


class BossSessionPreflightMiddleware(AgentMiddleware):
    """Hard-gate every Boss tool call; never persist live status in messages."""

    def __init__(self, preflight: BossSessionPreflight) -> None:
        super().__init__()
        self._preflight = preflight

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        status = await self._preflight.check()
        if not status.ready:
            return ToolMessage(
                content=(
                    '{"status":"blocked","error_type":"'
                    + status.reason
                    + '"}'
                ),
                name=request.tool_call["name"],
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        response = await handler(request)
        content = str(getattr(response, "content", ""))
        if any(marker in content for marker in _INVALIDATING_ERRORS):
            self._preflight.invalidate()
        return response
