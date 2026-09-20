from __future__ import annotations

import pytest

from jobagent.boss_preflight import BossSessionPreflight
from jobagent.config import Settings
from jobagent.scraper.boss import get_boss_cooldown


@pytest.mark.asyncio
async def test_preflight_blocks_missing_boss_session(tmp_path) -> None:
    cooldown = get_boss_cooldown()
    cooldown.reset()
    try:
        status = await BossSessionPreflight(
            Settings(
                jobagent_state_db=tmp_path / "state.db",
                boss_cookie=None,
                boss_cookie_file=tmp_path / "missing-cookies.json",
            )
        ).check()
        assert not status.ready
        assert status.reason == "boss_login_required"
    finally:
        cooldown.reset()
