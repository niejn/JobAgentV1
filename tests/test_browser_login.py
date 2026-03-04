"""Tests for jobclaw.auth.browser_login — cookie save/load/validation."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobclaw.auth.browser_login import (
    COOKIE_DIR,
    PLATFORM_CONFIG,
    _save_cookies,
    cookies_valid,
    get_cookie_age_hours,
    load_cookies,
)


@pytest.fixture()
def tmp_cookie_dir(tmp_path: Path, monkeypatch):
    """Redirect COOKIE_DIR to a temp directory."""
    cookie_dir = tmp_path / "cookies"
    monkeypatch.setattr("jobclaw.auth.browser_login.COOKIE_DIR", cookie_dir)
    return cookie_dir


# ---------------------------------------------------------------------------
# _save_cookies / load_cookies
# ---------------------------------------------------------------------------

class TestSaveLoadCookies:

    @pytest.mark.asyncio
    async def test_save_and_load_roundtrip(self, tmp_cookie_dir):
        """Cookies saved via _save_cookies can be loaded back."""
        cookies = [
            {"name": "wt2", "value": "abc123", "domain": ".zhipin.com", "path": "/"},
            {"name": "wbg", "value": "xyz", "domain": ".zhipin.com", "path": "/"},
        ]
        _save_cookies("boss", cookies)

        loaded = await load_cookies("boss")
        assert loaded is not None
        assert len(loaded) == 2
        assert loaded[0]["name"] == "wt2"
        assert loaded[0]["value"] == "abc123"

    @pytest.mark.asyncio
    async def test_load_nonexistent_returns_none(self, tmp_cookie_dir):
        result = await load_cookies("boss")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_corrupt_json_returns_none(self, tmp_cookie_dir):
        tmp_cookie_dir.mkdir(parents=True, exist_ok=True)
        (tmp_cookie_dir / "boss.json").write_text("NOT JSON")
        result = await load_cookies("boss")
        assert result is None

    def test_save_creates_directory(self, tmp_cookie_dir):
        assert not tmp_cookie_dir.exists()
        _save_cookies("boss", [{"name": "test", "value": "v"}])
        assert tmp_cookie_dir.exists()
        assert (tmp_cookie_dir / "boss.json").exists()

    def test_save_file_permissions(self, tmp_cookie_dir):
        _save_cookies("boss", [{"name": "a", "value": "b"}])
        f = tmp_cookie_dir / "boss.json"
        mode = f.stat().st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# get_cookie_age_hours
# ---------------------------------------------------------------------------

class TestCookieAge:

    def test_no_file_returns_none(self, tmp_cookie_dir):
        assert get_cookie_age_hours("boss") is None

    def test_returns_age(self, tmp_cookie_dir):
        tmp_cookie_dir.mkdir(parents=True, exist_ok=True)
        data = {"saved_at": time.time() - 3600, "cookies": []}
        (tmp_cookie_dir / "boss.json").write_text(json.dumps(data))
        age = get_cookie_age_hours("boss")
        assert age is not None
        assert 0.9 < age < 1.1  # ~1 hour


# ---------------------------------------------------------------------------
# cookies_valid (mocked Playwright)
# ---------------------------------------------------------------------------

class TestCookiesValid:

    @pytest.mark.asyncio
    async def test_no_cookies_returns_false(self, tmp_cookie_dir):
        result = await cookies_valid("boss")
        assert result is False

    @pytest.mark.asyncio
    async def test_unknown_platform_returns_false(self):
        result = await cookies_valid("unknown_platform")
        assert result is False


# ---------------------------------------------------------------------------
# PLATFORM_CONFIG sanity checks
# ---------------------------------------------------------------------------

class TestPlatformConfig:

    def test_boss_config_complete(self):
        cfg = PLATFORM_CONFIG["boss"]
        assert "login_url" in cfg
        assert "success_indicator" in cfg
        assert "key_cookies" in cfg
        assert "wt2" in cfg["key_cookies"]

    def test_linkedin_config_complete(self):
        cfg = PLATFORM_CONFIG["linkedin"]
        assert "login_url" in cfg
        assert "li_at" in cfg["key_cookies"]
