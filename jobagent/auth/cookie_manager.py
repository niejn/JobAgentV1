"""Unified cookie loading: config (.env) > persisted file > prompt login.

Provides a single entry point for all cookie needs across scraper and applier layers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

from jobagent.auth.browser_login import load_cookies, normalize_cookies_for_playwright

logger = logging.getLogger(__name__)

# Maps platform name to the Settings attribute for the cookie value
_PLATFORM_COOKIE_ATTR = {
    "boss": "boss_cookie",
    "linkedin": "linkedin_cookie",
    "xhs": "xhs_cookie",
}

# Maps platform name to the cookie name + domain used for injection
_PLATFORM_COOKIE_META = {
    "boss": {"name": "wt2", "domain": ".zhipin.com", "path": "/"},
    "linkedin": {"name": "li_at", "domain": ".linkedin.com", "path": "/"},
    "xhs": {"name": "web_session", "domain": ".xiaohongshu.com", "path": "/"},
}


class CookieNotFoundError(Exception):
    """Raised when no cookies are available for a platform."""


async def get_cookies(platform: str, settings: object) -> list[dict[str, Any]]:
    """Load cookies with priority: .env config > persisted file > raise.

    Args:
        platform: Platform name (``boss`` or ``linkedin``).
        settings: Settings instance with cookie attributes.

    Returns:
        List of Playwright-format cookie dicts.

    Raises:
        CookieNotFoundError: If no cookies are available from any source.
    """
    # Boss direct adapters need the complete browser session rather than only
    # the historical BOSS_COOKIE=wt2 shortcut.
    if platform == "boss":
        cookie_file = getattr(settings, "boss_cookie_file", None)
        if isinstance(cookie_file, (str, Path)) and str(cookie_file).strip():
            return load_cookie_export(Path(cookie_file))

    # Priority 1: .env / Settings
    attr = _PLATFORM_COOKIE_ATTR.get(platform)
    if attr:
        env_cookie = getattr(settings, attr, None)
        if env_cookie:
            meta = _PLATFORM_COOKIE_META[platform]
            logger.debug("Using %s cookie from .env config", platform)
            return [
                {
                    "name": meta["name"],
                    "value": env_cookie,
                    "domain": meta["domain"],
                    "path": meta["path"],
                }
            ]

    # Priority 2: Persisted cookie file
    persisted = await load_cookies(platform)
    if persisted:
        logger.debug("Using %s cookie from persisted file", platform)
        return persisted

    # Priority 3: No cookies — tell user to login
    raise CookieNotFoundError(
        f"No cookies found for {platform}. "
        f"Please run: jobagent login --platform {platform}"
    )


def load_cookie_export(path: Path) -> list[dict[str, Any]]:
    """Load a browser-exported JSON Cookie file without logging values."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CookieNotFoundError(f"Boss Cookie file not found: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CookieNotFoundError(f"Invalid Boss Cookie file: {resolved}") from exc
    items = payload.get("cookies") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise CookieNotFoundError("Boss Cookie export must contain a cookies list")
    cookies = [item for item in items if isinstance(item, dict) and item.get("name")]
    names = {str(item["name"]) for item in cookies}
    required = {"wt2", "bst", "__zp_stoken__"}
    missing = sorted(required - names)
    if missing:
        raise CookieNotFoundError(
            "Boss Cookie export is incomplete; missing: " + ", ".join(missing)
        )
    return cookies


async def inject_cookies(context: object, platform: str, settings: object) -> None:
    """Inject cookies into a Playwright browser context.

    Args:
        context: Playwright BrowserContext.
        platform: Platform name.
        settings: Settings instance.

    Raises:
        CookieNotFoundError: If no cookies are available.
    """
    cookies = await get_cookies(platform, settings)
    await cast(Any, context).add_cookies(normalize_cookies_for_playwright(cookies))
    logger.debug("Injected %d cookies for %s", len(cookies), platform)
