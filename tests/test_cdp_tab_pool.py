"""Tests for CdpTabPool: keeper tab, global cap, retirement, close."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jobagent.scraper.cdp_tab_pool import CdpTabPool


class FakeContext:
    """Mimics a CDP context: pages list is the Chrome-wide tab truth."""

    def __init__(self, existing: int = 0) -> None:
        self.pages: list[Any] = [FakePage() for _ in range(existing)]

    def __repr__(self) -> str:
        return f"FakeContext({len(self.pages)} tabs)"

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


class FakePage:
    def __init__(self, url: str = "https://www.zhipin.com/job") -> None:
        self.closed = False
        self.url = url

        async def _goto(*args: object, **kwargs: object) -> None:
            if args and isinstance(args[0], str):
                self.url = args[0]

        self.goto = AsyncMock(side_effect=_goto)
        self.close = AsyncMock(side_effect=self._close)

    def _close(self) -> None:
        self.closed = True
        # Chrome-wide list drops the tab with it.
        for ctx in _all_contexts:
            if self in ctx.pages:
                ctx.pages.remove(self)

    def is_closed(self) -> bool:
        return self.closed


_all_contexts: list[FakeContext] = []


@pytest.fixture(autouse=True)
def _track_contexts():
    _all_contexts.clear()
    yield
    _all_contexts.clear()


def _make(
    existing: int = 0, *, max_tabs: int = 4, max_reuses: int = 2
) -> tuple[CdpTabPool, FakeContext]:
    ctx = FakeContext(existing)
    _all_contexts.append(ctx)
    return CdpTabPool(ctx, max_tabs=max_tabs, max_reuses=max_reuses), ctx


@pytest.mark.asyncio
async def test_acquire_creates_keeper_plus_one_tab() -> None:
    pool, ctx = _make()
    page = await pool.acquire()
    # keeper + the acquired tab
    assert len(ctx.pages) == 2
    keeper = ctx.pages[0]
    assert keeper is not page
    keeper.goto.assert_awaited_once_with("about:blank", timeout=5_000)


@pytest.mark.asyncio
async def test_release_parks_tab_and_reuses_it() -> None:
    pool, ctx = _make()
    first = await pool.acquire()
    await pool.release(first)
    assert len(ctx.pages) == 2, "released tab stays parked (idle), not closed"
    second = await pool.acquire()
    assert second is first, "idle tab is reused instead of opening a new one"


@pytest.mark.asyncio
async def test_tab_retires_after_max_reuses() -> None:
    pool, ctx = _make(max_reuses=2)
    page = await pool.acquire()       # uses = 1
    await pool.release(page)          # parked with uses = 1
    page2 = await pool.acquire()      # reuse -> uses = 2 (at cap)
    assert page2 is page
    await pool.release(page2)         # uses >= cap -> retired (closed)
    assert page.closed
    page3 = await pool.acquire()      # fresh tab replaces it
    assert page3 is not page
    assert len(ctx.pages) == 2, "keeper + one fresh tab"

@pytest.mark.asyncio
async def test_second_pool_adopts_existing_keeper_no_accumulation() -> None:
    """Sequential pools on one Chrome must not each create their own keeper
    (verified leak: three discover calls grew tabs 1 -> 2 -> 3)."""

    pool1, ctx = _make()
    await pool1.acquire()
    await pool1.close()
    # Simulate Boss leaving a blank tab behind: the keeper survives close().
    assert len(ctx.pages) == 1, "keeper only"

    # A fresh pool (next backend/applier run) adopts that blank tab.
    pool2 = CdpTabPool(ctx, max_tabs=4, max_reuses=2)
    await pool2.acquire()
    await pool2.close()
    assert len(ctx.pages) == 1, "no second keeper was created"


@pytest.mark.asyncio
async def test_cap_blocks_new_tabs_until_release() -> None:
    pool, ctx = _make(max_tabs=2)     # keeper + 1 work tab only
    work = await pool.acquire()
    assert len(ctx.pages) == 2

    acquiring = asyncio.create_task(pool.acquire())

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(acquiring), timeout=0.2)
    assert len(ctx.pages) == 2, "no third tab while cap is reached"

    await pool.release(work)          # wakes the waiter
    got = await asyncio.wait_for(acquiring, timeout=1)
    assert got is work, "waiter received the released tab"


@pytest.mark.asyncio
async def test_close_closes_work_tabs_but_keeps_keeper() -> None:
    pool, ctx = _make()
    work = await pool.acquire()
    await pool.release(work)
    keeper = ctx.pages[0]

    await pool.close()

    assert work.closed
    assert not keeper.closed, "keeper survives to keep Chrome alive"
    with pytest.raises(RuntimeError):
        await pool.acquire()


@pytest.mark.asyncio
async def test_close_prunes_stray_blanks_not_opened_by_this_pool() -> None:
    """Task end must leave exactly one blank (the keeper): warlock
    victims and blanks left by other pool instances are closed too,
    even though this pool never opened them."""
    pool, ctx = _make(existing=1, max_tabs=6)
    work = await pool.acquire()
    await pool.release(work)
    keeper = pool._keeper
    # blanks this pool did NOT open: one from another pool instance,
    # two warlock redirect victims
    ctx.pages.append(FakePage(url="about:blank"))
    ctx.pages.append(FakePage(url="about:blank"))
    ctx.pages.append(FakePage(url="about:blank"))
    user_tab = ctx.pages[0]

    await pool.close()

    blanks = [p for p in ctx.pages if p.url == "about:blank"]
    assert len(blanks) == 1 and blanks[0] is keeper
    assert not user_tab.closed, "real user tab untouched"
    assert work.closed, "pool-owned tab closed by close()"



@pytest.mark.asyncio
async def test_user_tabs_do_not_deadlock_the_pool() -> None:
    """Regression (first live upload test hung on this): Chrome-wide tabs
    include the USER's own tabs; the cap must count only pool-owned ones,
    and a serial acquire with the cap seemingly reached must not wait."""
    # 5 pre-existing user tabs, pool cap 4 - chrome-wide already over cap
    pool, ctx = _make(existing=5, max_tabs=4)
    work = await pool.acquire()          # must NOT hang: keeper + 1 owned = 2
    assert len(ctx.pages) == 7           # 5 user + keeper + work
    await pool.release(work)
    work2 = await pool.acquire()         # idle reuse, still no hang
    assert work2 is work


@pytest.mark.asyncio
async def test_prune_blank_tabs_keeps_keeper_only() -> None:
    """Warlock victims pile up as about:blank tabs; pruning must close
    all blanks except the keeper (itself blank by design) and never
    touch real tabs."""
    pool, ctx = _make(existing=2, max_tabs=6)
    work = await pool.acquire()  # creates the (blank) keeper
    await pool.release(work)
    keeper = pool._keeper
    # two pre-existing blank tabs (user-left / warlock victims)
    for _ in range(2):
        ctx.pages.append(FakePage(url="about:blank"))

    closed = await pool.prune_blank_tabs()

    assert closed == 2
    blanks = [p for p in ctx.pages if p.url == "about:blank"]
    assert len(blanks) == 1 and blanks[0] is keeper
    # real tabs untouched: 2 existing + released work tab + keeper
    assert len(ctx.pages) == 4
