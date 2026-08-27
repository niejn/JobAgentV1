"""Tests for BossCdpBackend.dispose() teardown (code review HIGH H2).

Every discover_boss_jobs call constructs a fresh backend and disposes it;
dispose() must release the CDP browser connection AND stop the Playwright
driver, otherwise each call leaks a node process and a websocket.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jobagent.config import Settings
from jobagent.scraper.boss_cdp import BossCdpBackend


@pytest.mark.asyncio
async def test_dispose_tears_down_connection_and_driver() -> None:
    backend = BossCdpBackend(Settings(_env_file=None))
    browser = AsyncMock()
    driver = AsyncMock()
    backend._connection = (browser, object(), driver)

    await backend.dispose()

    browser.close.assert_awaited_once()
    driver.stop.assert_awaited_once()
    assert backend._connection is None


@pytest.mark.asyncio
async def test_dispose_without_connection_is_a_noop() -> None:
    backend = BossCdpBackend(Settings(_env_file=None))
    await backend.dispose()  # must not raise


@pytest.mark.asyncio
async def test_dispose_survives_teardown_errors() -> None:
    """dispose() runs in a finally block; teardown failures must not escape."""
    backend = BossCdpBackend(Settings(_env_file=None))
    browser = AsyncMock()
    browser.close.side_effect = RuntimeError("transport gone")
    driver = AsyncMock()
    driver.stop.side_effect = RuntimeError("driver gone")
    backend._connection = (browser, object(), driver)

    await backend.dispose()  # must not raise

    browser.close.assert_awaited_once()
    driver.stop.assert_awaited_once()
    assert backend._connection is None
