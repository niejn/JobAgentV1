"""Bounded tab pool over one CDP browser context (thread-pool semantics).

Design constraints, all from real-machine Boss behavior (2026-08-27):

1. KEEPER TAB — when the anti-bot frontend closes the only remaining site
   tab in a single-window debug Chrome, the window disappears and the whole
   Chrome process exits (verified: 29 processes gone). The pool therefore
   keeps one ``about:blank`` tab of its own that never navigates anywhere.

2. GLOBAL CAP — ``context.pages`` on a ``connect_over_cdp`` connection
   reports the Chrome-wide tab list, so every pool attached to this Chrome
   counts the same truth. The cap includes the keeper.

3. RETIREMENT — Boss flags pages that get re-navigated repeatedly
   (boss-zhipin-scraper's "reused pages" observation), so a tab is retired
   (closed and replaced by a fresh one) after ``max_reuses`` uses. The pool
   bounds *concurrency*, not total creation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_KEEPER_URL = "about:blank"


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
        # Adopt any existing blank tab first: Boss's anti-bot redirect
        # produces about:blank tabs routinely, and one of them serves the
        # keeper purpose just as well. Adopting keeps multiple pool
        # instances (backend + applier, or sequential runs) from each
        # creating - and never closing - their own keeper (verified leak:
        # tabs grew 1 -> 2 -> 3 across three discover calls).
        tracked = {*self._idle, *self._active}
        for page in cast("list[Page]", self._context.pages):
            if id(page) in tracked or page.is_closed():
                continue
            if str(page.url or "") == _KEEPER_URL:
                self._keeper = page
                return
        self._keeper = await self._context.new_page()
        try:
            # about:blank issues no network request; the keeper never
            # navigates to a target site afterwards.
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
            # 2) Room for a fresh tab? (context.pages = Chrome-wide truth:
            #    includes the keeper and tabs opened by any other pool.)
            pages = self._context.pages
            if len(pages) < self._max_tabs:
                page = cast("Page", await self._context.new_page())
                self._active[id(page)] = 1
                return page
            # 3) Cap reached: wait for a release/close event, then retry.
            logger.info(
                "TabPool: cap %d reached (%d live), waiting for a release",
                self._max_tabs,
                len(pages),
            )
            self._released.clear()
            await self._released.wait()

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
        """Close every tab the pool opened except the keeper (it protects
        the browser lifecycle and is reused by the next pool)."""

        self._closed = True
        for page in self._owned_pages():
            await self._quiet_close(page)
        self._idle.clear()
        self._active.clear()
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
