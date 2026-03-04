"""Tests for jobclaw.auth.cookie_manager — priority logic."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jobclaw.auth.cookie_manager import CookieNotFoundError, get_cookies, inject_cookies


@pytest.fixture()
def tmp_cookie_dir(tmp_path: Path, monkeypatch):
    """Redirect COOKIE_DIR to a temp directory."""
    cookie_dir = tmp_path / "cookies"
    monkeypatch.setattr("jobclaw.auth.browser_login.COOKIE_DIR", cookie_dir)
    return cookie_dir


def _make_settings(boss_cookie=None, linkedin_cookie=None):
    s = MagicMock()
    s.boss_cookie = boss_cookie
    s.linkedin_cookie = linkedin_cookie
    return s


# ---------------------------------------------------------------------------
# get_cookies priority
# ---------------------------------------------------------------------------

class TestGetCookiesPriority:

    @pytest.mark.asyncio
    async def test_env_cookie_takes_priority(self, tmp_cookie_dir):
        """Settings .env cookie wins over persisted file."""
        # Write a persisted file
        tmp_cookie_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "saved_at": time.time(),
            "cookies": [{"name": "wt2", "value": "from_file", "domain": ".zhipin.com", "path": "/"}],
        }
        (tmp_cookie_dir / "boss.json").write_text(json.dumps(data))

        settings = _make_settings(boss_cookie="from_env")
        cookies = await get_cookies("boss", settings)

        assert len(cookies) == 1
        assert cookies[0]["value"] == "from_env"

    @pytest.mark.asyncio
    async def test_persisted_file_fallback(self, tmp_cookie_dir):
        """When .env cookie is empty, use persisted file."""
        tmp_cookie_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "saved_at": time.time(),
            "cookies": [
                {"name": "wt2", "value": "from_file", "domain": ".zhipin.com", "path": "/"},
                {"name": "wbg", "value": "extra", "domain": ".zhipin.com", "path": "/"},
            ],
        }
        (tmp_cookie_dir / "boss.json").write_text(json.dumps(data))

        settings = _make_settings(boss_cookie=None)
        cookies = await get_cookies("boss", settings)

        assert len(cookies) == 2
        assert cookies[0]["value"] == "from_file"

    @pytest.mark.asyncio
    async def test_no_cookies_raises(self, tmp_cookie_dir):
        """When no cookies are available, raise CookieNotFoundError."""
        settings = _make_settings()
        with pytest.raises(CookieNotFoundError, match="jobclaw login"):
            await get_cookies("boss", settings)

    @pytest.mark.asyncio
    async def test_linkedin_env_cookie(self, tmp_cookie_dir):
        settings = _make_settings(linkedin_cookie="li_at_value")
        cookies = await get_cookies("linkedin", settings)
        assert len(cookies) == 1
        assert cookies[0]["name"] == "li_at"
        assert cookies[0]["value"] == "li_at_value"


# ---------------------------------------------------------------------------
# inject_cookies
# ---------------------------------------------------------------------------

class TestInjectCookies:

    @pytest.mark.asyncio
    async def test_injects_into_context(self, tmp_cookie_dir):
        settings = _make_settings(boss_cookie="test_cookie")
        context = AsyncMock()

        await inject_cookies(context, "boss", settings)

        context.add_cookies.assert_called_once()
        injected = context.add_cookies.call_args[0][0]
        assert injected[0]["name"] == "wt2"
        assert injected[0]["value"] == "test_cookie"

    @pytest.mark.asyncio
    async def test_raises_when_no_cookies(self, tmp_cookie_dir):
        settings = _make_settings()
        context = AsyncMock()

        with pytest.raises(CookieNotFoundError):
            await inject_cookies(context, "boss", settings)
