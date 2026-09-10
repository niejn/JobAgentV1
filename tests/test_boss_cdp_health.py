"""Boss CDP SPA health checks are bounded and return safe diagnostics."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from jobagent.config import Settings
from jobagent.scraper.boss import BossAccessError
from jobagent.scraper.boss_cdp import BossCdpBackend


class FakePage:
    def __init__(self, diagnostics: Iterator[dict[str, object]]) -> None:
        self.url = "https://www.zhipin.com/web/geek/job"
        self._diagnostics = diagnostics
        self.reload = AsyncMock()

    async def evaluate(self, _script: str) -> dict[str, object]:
        return next(self._diagnostics)


def _healthy(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "url": "https://www.zhipin.com/web/geek/job",
        "title": "Boss直聘",
        "body_chars": 120,
        "real_job_cards": 1,
        "login_wall": False,
        "captcha": False,
        "risk_control": False,
        "empty_result": False,
        "blank_or_data_url": False,
    }
    result.update(changes)
    return result


@pytest.mark.asyncio
async def test_health_wait_accepts_real_job_cards_without_reload() -> None:
    backend = BossCdpBackend(Settings(_env_file=None))
    page = FakePage(iter([_healthy()]))

    diagnostic = await backend._wait_for_search_content(page)

    assert diagnostic["real_job_cards"] == 1
    page.reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_page_lost_has_safe_structured_diagnostic() -> None:
    backend = BossCdpBackend(Settings(_env_file=None))
    page = FakePage(iter([_healthy(url="data:,", blank_or_data_url=True)]))

    with pytest.raises(BossAccessError) as caught:
        await backend._wait_for_search_content(page)

    assert caught.value.code == "page_lost"
    assert caught.value.details["url"] == "data:,"
    assert "body" not in caught.value.details
    page.reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_risk_stops_without_refresh(monkeypatch) -> None:
    backend = BossCdpBackend(Settings(_env_file=None))
    page = FakePage(iter([_healthy(captcha=True)]))
    cooldown = MagicMock()
    monkeypatch.setattr("jobagent.scraper.boss_cdp.get_boss_cooldown", lambda: cooldown)

    with pytest.raises(BossAccessError) as caught:
        await backend._wait_for_search_content(page)

    assert caught.value.code == "boss_risk_control"
    cooldown.trigger.assert_called_once_with("page_risk_control")
    page.reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_reloads_once_then_accepts_spa_content(monkeypatch) -> None:
    backend = BossCdpBackend(Settings(_env_file=None))
    page = FakePage(iter([_healthy(real_job_cards=0, body_chars=30), _healthy()]))
    monotonic = iter([0.0, 11.0, 12.0, 12.0])
    # ``time.monotonic`` is shared with asyncio; keep returning a stable
    # value after this test's four explicit calls so loop teardown is safe.
    monkeypatch.setattr(
        "jobagent.scraper.boss_cdp.time.monotonic", lambda: next(monotonic, 12.0)
    )
    monkeypatch.setattr("jobagent.scraper.boss_cdp.asyncio.sleep", AsyncMock())

    diagnostic = await backend._wait_for_search_content(page)

    assert diagnostic["real_job_cards"] == 1
    page.reload.assert_awaited_once()
