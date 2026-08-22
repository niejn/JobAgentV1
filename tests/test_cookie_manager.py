"""Tests for jobagent.auth.cookie_manager — priority logic."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jobagent.auth.browser_login import inspect_cookie_providers
from jobagent.auth.cookie_manager import CookieNotFoundError, get_cookies, inject_cookies


@pytest.fixture()
def tmp_cookie_dir(tmp_path: Path, monkeypatch):
    """Redirect COOKIE_DIR to a temp directory."""
    cookie_dir = tmp_path / "cookies"
    legacy_dir = tmp_path / "legacy-cookies"
    monkeypatch.setattr("jobagent.auth.browser_login.COOKIE_DIR", cookie_dir)
    monkeypatch.setattr("jobagent.auth.browser_login.LEGACY_COOKIE_DIR", legacy_dir)
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
            "cookies": [
                {
                    "name": "wt2",
                    "value": "from_file",
                    "domain": ".zhipin.com",
                    "path": "/",
                }
            ],
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
    async def test_legacy_cookie_export_is_matched_by_domain_not_filename(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        current_dir = tmp_path / "current"
        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir()
        monkeypatch.setattr("jobagent.auth.browser_login.COOKIE_DIR", current_dir)
        monkeypatch.setattr("jobagent.auth.browser_login.LEGACY_COOKIE_DIR", legacy_dir)
        exported = {
            "url": "https://www.zhipin.com",
            "cookies": [
                {
                    "name": "wt2",
                    "value": "from-domain-export",
                    "domain": ".zhipin.com",
                    "path": "/",
                }
            ],
        }
        (legacy_dir / "browser-export-2026.json").write_text(
            json.dumps(exported),
            encoding="utf-8",
        )

        cookies = await get_cookies("boss", _make_settings())

        assert [cookie["name"] for cookie in cookies] == ["wt2"]
        migrated = current_dir / "boss.json"
        assert migrated.is_file()
        migrated_payload = json.loads(migrated.read_text(encoding="utf-8"))
        assert migrated_payload["cookies"][0]["value"] == "from-domain-export"
        assert (legacy_dir / "browser-export-2026.json").is_file()

    def test_provider_inspection_reports_expired_cookie_without_rejecting_file(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        current_dir = tmp_path / "current"
        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir()
        monkeypatch.setattr("jobagent.auth.browser_login.COOKIE_DIR", current_dir)
        monkeypatch.setattr("jobagent.auth.browser_login.LEGACY_COOKIE_DIR", legacy_dir)
        exported = {
            "url": "https://www.zhipin.com",
            "cookies": [
                {
                    "name": "wt2",
                    "value": "secret",
                    "domain": ".zhipin.com",
                    "path": "/",
                    "expirationDate": 1_700_000_000.0,
                },
                {
                    "name": "session-only",
                    "value": "secret",
                    "domain": ".zhipin.com",
                    "path": "/",
                },
                {
                    "name": "wt2",
                    "value": "older-secret",
                    "domain": "www.zhipin.com",
                    "path": "/",
                    "expirationDate": 1_600_000_000.0,
                },
            ],
        }
        (legacy_dir / "random.json").write_text(json.dumps(exported), encoding="utf-8")

        statuses = inspect_cookie_providers(now_epoch=1_800_000_000.0)

        boss = next(status for status in statuses if status.platform == "boss")
        assert boss.matched is True
        assert boss.cookie_count == 3
        assert boss.expired_count == 2
        assert boss.key_cookie_expired is True
        assert (current_dir / "boss.json").is_file()

    @pytest.mark.asyncio
    async def test_no_cookies_raises(self, tmp_cookie_dir):
        """When no cookies are available, raise CookieNotFoundError."""
        settings = _make_settings()
        with pytest.raises(CookieNotFoundError, match="jobagent login"):
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

    @pytest.mark.asyncio
    async def test_normalizes_browser_extension_export_for_playwright(
        self,
        tmp_cookie_dir: Path,
    ) -> None:
        tmp_cookie_dir.mkdir(parents=True, exist_ok=True)
        exported = {
            "url": "https://www.zhipin.com",
            "cookies": [
                {
                    "name": "wt2",
                    "value": "secret",
                    "domain": ".zhipin.com",
                    "path": "/",
                    "sameSite": "unspecified",
                    "expirationDate": 1_900_000_000.5,
                    "hostOnly": False,
                    "session": False,
                    "storeId": "0",
                    "httpOnly": True,
                    "secure": True,
                }
            ],
        }
        (tmp_cookie_dir / "random-export.json").write_text(
            json.dumps(exported),
            encoding="utf-8",
        )
        context = AsyncMock()

        await inject_cookies(context, "boss", _make_settings())

        context.add_cookies.assert_awaited_once_with(
            [
                {
                    "name": "wt2",
                    "value": "secret",
                    "domain": ".zhipin.com",
                    "path": "/",
                    "expires": 1_900_000_000.5,
                    "httpOnly": True,
                    "secure": True,
                }
            ]
        )
