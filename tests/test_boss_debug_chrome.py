"""Dedicated Boss debug Chrome starts only when its local CDP endpoint is absent."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jobagent.auth.boss_debug_chrome import ensure_boss_debug_chrome
from jobagent.config import Settings


@pytest.mark.asyncio
async def test_existing_debug_chrome_is_reused(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("jobagent.auth.boss_debug_chrome._endpoint_ready", lambda _: True)
    popen = MagicMock()
    monkeypatch.setattr("jobagent.auth.boss_debug_chrome.subprocess.Popen", popen)

    started = await ensure_boss_debug_chrome(Settings(
        _env_file=None, boss_debug_chrome_profile_dir=tmp_path / "profile"
    ))

    assert started is False
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_missing_endpoint_launches_dedicated_profile(monkeypatch, tmp_path) -> None:
    readiness = iter([False, True])
    monkeypatch.setattr(
        "jobagent.auth.boss_debug_chrome._endpoint_ready", lambda _: next(readiness, True)
    )
    monkeypatch.setattr(
        "jobagent.auth.boss_debug_chrome._chrome_executable", lambda _: "chrome.exe"
    )
    popen = MagicMock()
    monkeypatch.setattr("jobagent.auth.boss_debug_chrome.subprocess.Popen", popen)

    profile = tmp_path / "profile"
    started = await ensure_boss_debug_chrome(Settings(
        _env_file=None, boss_debug_chrome_profile_dir=profile
    ))

    assert started is True
    assert profile.is_dir()
    command = popen.call_args.args[0]
    assert "--remote-debugging-port=9222" in command
    assert f"--user-data-dir={profile.resolve()}" in command
