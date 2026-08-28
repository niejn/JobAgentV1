"""Persistent Boss chat-page session shared across chat tools.

Why (live finding, 2026-08-28): warlock tolerates a chat-page load or two
per short window; rapid open -> scan -> close cycles blank it fast - three
probe tabs in a row killed the third. Real usage must look like a human:
open the chat page ONCE, keep it parked in Chrome, and do everything else
(list refresh via the wapi fetch, searching, typing, sending) on that
already-loaded page.

`get_chat_page(pool)` returns (page, fresh):
- fresh=True  -> caller must navigate + settle (first use, or the parked
  tab was closed and a replacement was acquired).
- fresh=False -> the parked tab is alive on /web/geek/chat; reuse it with
  NO navigation, no reload.

The page is detached from the pool (survives pool.close) and intentionally
left open for the process lifetime - the user can keep chatting in it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from jobagent.scraper.cdp_tab_pool import CdpTabPool

_CHAT_PAGE_PATH = "/web/geek/chat"

_parked: Any = None


async def get_chat_page(pool: CdpTabPool) -> tuple[Any, bool]:
    """Return the parked chat tab, acquiring a fresh one only if needed."""

    global _parked
    await pool.prune_blank_tabs()  # warlock victims pile up otherwise
    if _parked is not None and not _parked.is_closed():
        try:
            if urlsplit(str(_parked.url or "")).path == _CHAT_PAGE_PATH:
                return _parked, False
        except Exception:
            pass  # page dying mid-check: fall through to a fresh acquire
    page = await pool.acquire()
    await pool.detach(page)  # survives pool.close(); parked for the process
    _parked = page
    return page, True


def chat_page_url_path() -> str:
    """The path a live parked chat tab must be on (for tests/settling)."""

    return _CHAT_PAGE_PATH
