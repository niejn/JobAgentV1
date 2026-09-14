"""Bounded tab pool over one CDP browser context (thread-pool semantics).

Design constraints, all from real-machine Boss behavior (2026-08-27):

1. KEEPER TAB — when the anti-bot frontend closes the only remaining site
   tab in a single-window debug Chrome, the window disappears and the whole
   Chrome process exits (verified: 29 processes gone). The pool therefore
   keeps one ``about:blank`` tab of its own that never navigates anywhere.

2. POOL-OWNED CAP — the cap counts only tabs this pool opened (idle +
   active + keeper). Chrome-wide ``context.pages`` includes the user's own
   tabs; gating on those deadlocks serial callers (verified: first upload
   test hung with 3 restored user tabs + keeper already at the cap).

3. RETIREMENT — Boss flags pages that get re-navigated repeatedly
   (boss-zhipin-scraper's "reused pages" observation), so a tab is retired
   (closed and replaced by a fresh one) after ``max_reuses`` uses. The pool
   bounds *concurrency*, not total creation.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, cast

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_KEEPER_URL = "about:blank"
_ACQUIRE_TIMEOUT_S = 30.0
_KEEPER_LOCKS: dict[int, asyncio.Lock] = {}
_KEEPER_LOCKS_GUARD = threading.Lock()
_MANAGED_CONTEXTS: dict[int, Any] = {}


class CdpTabPool:
    """Acquire/release tabs with a hard cap, recycling, and a keeper tab."""

    def __init__(
        self,
        context: Any,
        *,
        max_tabs: int = 4,
        max_reuses: int = 2,
    ) -> None:
        self._context = context
        _MANAGED_CONTEXTS[id(context)] = context
        self._max_tabs = max_tabs
        self._max_reuses = max_reuses
        self._keeper: Page | None = None
        # id(page) -> uses so far; a page is in exactly one of the two.
        self._idle: dict[int, int] = {}
        self._active: dict[int, int] = {}
        self._released = asyncio.Event()
        self._closed = False

    # -- keeper ---------------------------------------------------------------

    async def _ensure_keeper(self) -> None:
        """Guarantee one non-site tab so anti-bot tab kills cannot end Chrome."""

        if self._keeper is not None and not self._keeper.is_closed():
            return
        context_key = id(self._context)
        with _KEEPER_LOCKS_GUARD:
            lock = _KEEPER_LOCKS.setdefault(context_key, asyncio.Lock())
        async with lock:
            # Another pool sharing this context may have created/adopted the
            # keeper while we waited.  Re-scan before opening anything.
            if self._keeper is not None and not self._keeper.is_closed():
                return
            tracked = {*self._idle, *self._active}
            for page in cast("list[Page]", self._context.pages):
                if id(page) in tracked or page.is_closed():
                    continue
                if str(page.url or "") == _KEEPER_URL:
                    self._keeper = page
                    return
            # No existing blank page is available, so create exactly one.
            # The subsequent acquire creates the work page; this is the only
            # intentional two-tab startup shape for a pool.
            self._keeper = await self._context.new_page()
            try:
                await self._keeper.goto(_KEEPER_URL, timeout=5_000)
            except Exception:
                logger.debug("TabPool: keeper goto failed (already blank?)", exc_info=True)

    # -- pool API --------------------------------------------------------------

    async def acquire(self) -> Page:
        """Return a usable tab: reuse an idle one, else open a new one."""

        if self._closed:
            raise RuntimeError("CdpTabPool already closed")
        await self._ensure_keeper()

        while True:
            retired: list[Page] = []
            reused: Page | None = None
            # 1) Reuse an idle tab that still has reuse budget left.
            while self._idle:
                page_id, uses = next(iter(self._idle.items()))
                del self._idle[page_id]
                page = self._page_by_id(page_id)
                if page is None or page.is_closed():
                    continue
                # This acquire would be use number uses+1; allow it unless
                # it exceeds the budget (release closes at uses >= max).
                if uses + 1 > self._max_reuses:
                    retired.append(page)
                    continue
                self._active[id(page)] = uses + 1
                reused = page
                break
            for page in retired:
                await self._quiet_close(page)
            if reused is not None:
                return reused
            # Reuse an already-open Boss page, including one left by a
            # previous short-lived tool context. All tabs in the debug Chrome
            # are JobAgent-managed, so adopting it is intentional.
            tracked = {*self._idle, *self._active}
            for candidate in cast("list[Page]", self._context.pages):
                if (
                    id(candidate) in tracked
                    or candidate.is_closed()
                    or candidate is self._keeper
                ):
                    continue
                if (
                    "zhipin.com" in str(candidate.url or "")
                    and callable(getattr(candidate, "goto", None))
                    and callable(getattr(candidate, "on", None))
                ):
                    self._active[id(candidate)] = 1
                    return candidate
            # 2) Room for a fresh tab? Count ONLY pool-owned tabs (idle +
            #    active + keeper). Chrome-wide pages include the user's own
            #    tabs, and waiting on those deadlocks a serial caller: the
            #    user's tabs are never released (verified hang during the
            #    first upload test with 3 restored tabs + keeper).
            owned = len(self._idle) + len(self._active) + (1 if self._keeper else 0)
            if owned < self._max_tabs:
                page = cast("Page", await self._context.new_page())
                self._active[id(page)] = 1
                return page
            # 3) Cap reached: a concurrent holder must release. Bound the
            #    wait so a serial caller fails loudly instead of hanging.
            logger.info(
                "TabPool: cap %d reached (%d pool-owned), waiting for a release",
                self._max_tabs,
                owned,
            )
            self._released.clear()
            try:
                await asyncio.wait_for(self._released.wait(), timeout=_ACQUIRE_TIMEOUT_S)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"CdpTabPool: no tab became available within "
                    f"{_ACQUIRE_TIMEOUT_S}s (cap {self._max_tabs}). "
                    "Close leftover tabs this pool opened and retry."
                ) from exc

    async def release(self, page: Page) -> None:
        """Return a tab: park it as idle, or retire it past the reuse cap."""

        if page.is_closed():
            self._active.pop(id(page), None)
            self._wake()
            return
        uses = self._active.pop(id(page), 1)
        if uses >= self._max_reuses:
            await self._quiet_close(page)
        else:
            self._idle[id(page)] = uses
        self._wake()

    async def close(self) -> None:
        """Close every tab the pool opened, then prune stray blank tabs.

        Only tabs opened by this pool are closed here. The process-level
        ``close_all_managed_debug_tabs`` hook closes every debug-Chrome tab
        at JobAgent shutdown, with blank pages handled first.
        """

        self._closed = True
        for page in self._owned_pages():
            await self._quiet_close(page)
        self._idle.clear()
        self._active.clear()
        # Process-lifetime cleanup is the only unconditional blank-tab sweep.
        pruned = await self.prune_blank_tabs()
        if pruned:
            logger.info("TabPool: close() pruned %d stray blank tab(s)", pruned)
        self._wake()

    async def park_all(self) -> None:
        """Return active pages to idle without closing them."""

        for page_id, uses in list(self._active.items()):
            page = self._page_by_id(page_id)
            if page is not None and not page.is_closed():
                self._idle[page_id] = uses
        self._active.clear()
        self._wake()

    async def close_all_tabs(self) -> int:
        """Close every tab in this debug context, blank pages first."""

        pages = [p for p in cast("list[Page]", self._context.pages) if not p.is_closed()]
        pages.sort(key=lambda p: str(p.url or "") != _KEEPER_URL)
        closed = 0
        for page in pages:
            try:
                await page.close()
                closed += 1
            except Exception:
                logger.debug("TabPool: tab already gone during shutdown", exc_info=True)
        return closed

    @property
    def keeper(self) -> Page | None:
        """The blank tab keeping the browser alive (may be None)."""

        return self._keeper

    async def prune_blank_tabs(self) -> int:
        """Close leftover about:blank tabs (warlock victims), keeping one.

        The keeper IS a blank tab by design, so exactly one blank always
        survives; user tabs (any real URL, incl. the new-tab page) are
        never touched.
        """

        blanks = [
            p
            for p in cast("list[Page]", self._context.pages)
            if str(p.url or "") == "about:blank"
        ]
        keeper = self._keeper if self._keeper else blanks[0] if blanks else None
        closed = 0
        for page in blanks:
            if keeper is not None and page is keeper:
                continue
            try:
                await page.close()
                closed += 1
            except Exception:
                pass
        if closed:
            logger.info("TabPool: pruned %d blank tab(s)", closed)
        return closed

    async def prune_excess_blank_tabs(self, *, max_pages: int = 10) -> int:
        """Trim blank tabs only when Chrome has accumulated too many pages.

        Normal operation deliberately leaves blank tabs alone: a blank page
        can be the user's/debug session keeper, and removing it while a CDP
        operation is starting can turn a transient page loss into
        ``TargetClosedError``.  Callers may use this bounded guard during a
        long-running process; unconditional cleanup remains owned by
        :meth:`close`.
        """

        if len(cast("list[Page]", self._context.pages)) <= max_pages:
            return 0
        return await self.prune_blank_tabs()

    async def detach(self, page: Page) -> None:
        """Stop tracking a tab: it stays open, outside pool management.

        Used for long-lived workhorse tabs (e.g. the persistent Boss chat
        page) that must survive pool.close() and be reused across tool
        calls - loading that page repeatedly is what warlock flags.
        """

        self._active.pop(id(page), None)
        self._idle.pop(id(page), None)
        self._wake()

    # -- helpers -----------------------------------------------------------------

    def _wake(self) -> None:
        self._released.set()

    def _owned_pages(self) -> list[Page]:
        known = {*self._idle, *self._active}
        return [p for p in self._context.pages if id(p) in known]

    def _page_by_id(self, page_id: int) -> Page | None:
        for page in cast("list[Page]", self._context.pages):
            if id(page) == page_id:
                return page
        return None

    @staticmethod
    async def _quiet_close(page: Page) -> None:
        try:
            await page.close()
        except Exception:
            logger.debug("TabPool: tab already gone", exc_info=True)


async def close_all_managed_debug_tabs() -> int:
    """Close all tabs in every debug context used by JobAgent.

    This is intentionally a process-lifetime operation. During normal tool
    calls tabs remain open and reusable; JobAgent shutdown owns the final
    cleanup and does not distinguish user-created from automation-created
    tabs.
    """

    contexts = list(_MANAGED_CONTEXTS.values())
    _MANAGED_CONTEXTS.clear()
    closed = 0
    for context in contexts:
        pages = [p for p in cast("list[Page]", context.pages) if not p.is_closed()]
        pages.sort(key=lambda p: str(p.url or "") != _KEEPER_URL)
        for page in pages:
            try:
                await page.close()
                closed += 1
            except Exception:
                logger.debug("TabPool: tab already gone during global shutdown", exc_info=True)
    return closed
