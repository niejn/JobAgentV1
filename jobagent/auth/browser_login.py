"""Interactive browser login for job platforms.

Opens a headed browser, lets the user log in manually, then extracts
and persists cookies for future use by scraper / applier layers.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

COOKIE_DIR = Path.home() / ".jobagent" / "cookies"
LEGACY_COOKIE_DIR = Path.home() / ".jobclaw" / "cookies"

PLATFORM_CONFIG: dict[str, dict] = {
    "boss": {
        "login_url": "https://www.zhipin.com/web/user/?ka=header-login",
        "success_indicator": "/web/geek/job",
        "success_selectors": [".user-nav", ".nav-figure"],
        "domain": ".zhipin.com",
        "key_cookies": ["wt2", "wbg", "wd_guid"],
        "check_url": "https://www.zhipin.com/web/geek/job",
        "check_redirect_pattern": "/web/user",
    },
    "linkedin": {
        "login_url": "https://www.linkedin.com/login",
        "success_indicator": "/feed",
        "success_selectors": [".global-nav", "#global-nav"],
        "domain": ".linkedin.com",
        "key_cookies": ["li_at", "JSESSIONID"],
        "check_url": "https://www.linkedin.com/feed/",
        "check_redirect_pattern": "/login",
    },
    "xhs": {
        # Xiaohongshu: login modal on homepage; after login the sidebar
        # shows the user avatar. URL does not change -> rely on selectors.
        "login_url": "https://www.xiaohongshu.com",
        "success_indicator": "__never_matches__",  # force selector-based detection
        "success_selectors": [".side-bar-user", ".user .avatar", ".avatar-wrapper"],
        "domain": ".xiaohongshu.com",
        "key_cookies": ["web_session", "a1"],
        "check_url": "https://www.xiaohongshu.com/explore",
        "check_redirect_pattern": "__never_matches__",  # use selectors below
        "check_logged_in_selector": ".side-bar-user, .user .avatar",
        "check_logged_out_selector": ".login-container, .login-modal",
    },
}


@dataclass(frozen=True, slots=True)
class CookieProviderStatus:
    """Provider-level cookie health without exposing names or values."""

    platform: str
    matched: bool
    cookie_count: int
    expired_count: int
    key_cookie_expired: bool


def inspect_cookie_providers(
    *,
    now_epoch: float | None = None,
) -> tuple[CookieProviderStatus, ...]:
    """Match configured providers and report local expiry metadata without blocking."""

    reference = time.time() if now_epoch is None else now_epoch
    statuses: list[CookieProviderStatus] = []
    for platform, config in PLATFORM_CONFIG.items():
        cookie_file = _existing_cookie_file(platform)
        cookies = _read_cookie_export(cookie_file) if cookie_file is not None else None
        if cookies is None:
            statuses.append(CookieProviderStatus(platform, False, 0, 0, False))
            continue
        if cookie_file is not None and cookie_file.parent == LEGACY_COOKIE_DIR:
            _migrate_legacy_cookie_export(platform, cookies)
        expired_cookies = [
            cookie for cookie in cookies if _cookie_expired(cookie, reference)
        ]
        expired_names = {
            str(cookie.get("name", "")) for cookie in expired_cookies
        }
        statuses.append(
            CookieProviderStatus(
                platform=platform,
                matched=True,
                cookie_count=len(cookies),
                expired_count=len(expired_cookies),
                key_cookie_expired=bool(expired_names & set(config["key_cookies"])),
            )
        )
    return tuple(statuses)


def normalize_cookies_for_playwright(
    cookies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert browser-extension exports to Playwright's accepted cookie schema."""

    normalized: list[dict[str, Any]] = []
    same_site_values = {"strict": "Strict", "lax": "Lax", "none": "None"}
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        item: dict[str, Any] = {"name": name, "value": value}
        url = cookie.get("url")
        domain = cookie.get("domain")
        if isinstance(url, str) and url:
            item["url"] = url
        elif isinstance(domain, str) and domain:
            item["domain"] = domain
            item["path"] = str(cookie.get("path") or "/")
        else:
            continue
        expiry = cookie.get("expires", cookie.get("expirationDate"))
        if isinstance(expiry, (int, float)) and expiry > 0:
            item["expires"] = float(expiry)
        for boolean_field in ("httpOnly", "secure"):
            boolean_value = cookie.get(boolean_field)
            if isinstance(boolean_value, bool):
                item[boolean_field] = boolean_value
        same_site = cookie.get("sameSite")
        if isinstance(same_site, str):
            accepted = same_site_values.get(same_site.lower())
            if accepted is not None:
                item["sameSite"] = accepted
        normalized.append(item)
    return normalized


async def interactive_login(platform: str, timeout_minutes: int = 5) -> dict:
    """Open a headed browser for manual login, then extract and persist cookies.

    Args:
        platform: One of the keys in PLATFORM_CONFIG.
        timeout_minutes: How long to wait before giving up.

    Returns:
        Dict mapping cookie name -> value for the key cookies.

    Raises:
        TimeoutError: If login is not detected within the timeout.
        ValueError: If platform is not supported.
    """
    if platform not in PLATFORM_CONFIG:
        raise ValueError(f"Unsupported platform: {platform}")

    config = PLATFORM_CONFIG[platform]
    deadline = time.monotonic() + timeout_minutes * 60

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        logger.info("Navigating to %s login page: %s", platform, config["login_url"])
        try:
            await page.goto(config["login_url"], wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            logger.error("Failed to load login page: %s", e)
            await browser.close()
            raise

        logger.info("Waiting for manual login (timeout=%dm)...", timeout_minutes)

        login_detected = False
        while time.monotonic() < deadline:
            try:
                # Check if browser was closed by user
                if page.is_closed():
                    break

                # Check 1: URL contains success indicator
                current_url = page.url
                if config["success_indicator"] in current_url:
                    login_detected = True
                    break

                # Check 2: Page has success selectors
                for selector in config["success_selectors"]:
                    try:
                        el = await page.query_selector(selector)
                        if el and await el.is_visible():
                            login_detected = True
                            break
                    except Exception:
                        continue

                if login_detected:
                    break

                await page.wait_for_timeout(1500)

            except Exception as e:
                # Browser may have been closed
                logger.debug("Poll error (browser closed?): %s", e)
                break

        if not login_detected:
            try:
                await browser.close()
            except Exception:
                pass
            raise TimeoutError(
                f"Login not detected within {timeout_minutes} minutes. "
                f"Please try again with `jobagent login --platform {platform}`."
            )

        # Extract cookies
        all_cookies = cast(list[dict[str, Any]], await context.cookies())
        _save_cookies(platform, all_cookies)

        # Build key cookie dict
        key_names = set(config["key_cookies"])
        key_cookies = {
            c["name"]: c["value"]
            for c in all_cookies
            if c["name"] in key_names
        }

        logger.info(
            "Login successful for %s. Saved %d cookies (%d key cookies).",
            platform, len(all_cookies), len(key_cookies),
        )

        await browser.close()

    return key_cookies


def _save_cookies(platform: str, cookies: list[dict[str, Any]]) -> Path:
    """Save cookies to ~/.jobagent/cookies/{platform}.json with 0600 permissions."""
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    cookie_file = COOKIE_DIR / f"{platform}.json"

    # Add saved_at timestamp for expiry tracking
    data = {
        "saved_at": time.time(),
        "cookies": cookies,
    }
    cookie_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Set file permissions to 600 (owner read/write only)
    try:
        os.chmod(cookie_file, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        logger.warning("Could not set cookie file permissions to 600")

    logger.info("Cookies saved to %s", cookie_file)
    return cookie_file


async def load_cookies(platform: str) -> list[dict[str, Any]] | None:
    """Load persisted cookies from ~/.jobagent/cookies/{platform}.json.

    Returns:
        List of Playwright cookie dicts, or None if file doesn't exist.
    """
    cookie_file = _existing_cookie_file(platform)
    if cookie_file is None:
        return None
    if cookie_file.parent == LEGACY_COOKIE_DIR:
        logger.info(
            "Using legacy JobClaw cookie file for %s; the next login will save under JobAgent",
            platform,
        )
    cookies = _read_cookie_export(cookie_file)
    if cookies is not None and cookie_file.parent == LEGACY_COOKIE_DIR:
        _migrate_legacy_cookie_export(platform, cookies)
    return cookies


def get_cookie_age_hours(platform: str) -> float | None:
    """Return the age of the saved cookie file in hours, or None if not found."""
    cookie_file = _existing_cookie_file(platform)
    if cookie_file is None:
        return None
    try:
        data = json.loads(cookie_file.read_text(encoding="utf-8"))
        saved_at = data.get("saved_at")
        if isinstance(saved_at, (int, float)):
            return (time.time() - float(saved_at)) / 3600
    except Exception:
        pass
    return None


def _existing_cookie_file(platform: str) -> Path | None:
    for directory in (COOKIE_DIR, LEGACY_COOKIE_DIR):
        if not directory.is_dir():
            continue
        candidates = sorted(
            directory.glob("*.json"),
            key=lambda path: (
                path.name != f"{platform}.json",
                -path.stat().st_mtime,
            ),
        )
        for candidate in candidates:
            if _cookie_export_matches_platform(candidate, platform):
                return candidate
    return None


def _read_cookie_export(path: Path) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load cookie export metadata: %s", type(exc).__name__)
        return None
    cookies = payload.get("cookies", payload) if isinstance(payload, dict) else payload
    if not isinstance(cookies, list) or not all(
        isinstance(cookie, dict) for cookie in cookies
    ):
        return None
    return cast(list[dict[str, Any]], cookies)


def _migrate_legacy_cookie_export(
    platform: str,
    cookies: list[dict[str, Any]],
) -> None:
    """Copy a matched legacy export to the canonical JobAgent directory atomically."""

    destination = COOKIE_DIR / f"{platform}.json"
    if destination.is_file():
        return
    temporary = destination.with_suffix(".json.tmp")
    try:
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": time.time(),
            "migrated_from": "jobclaw",
            "cookies": cookies,
        }
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(destination)
        try:
            os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            logger.warning("Could not set migrated cookie permissions to 600")
        logger.info("Migrated %s cookies to the JobAgent configuration directory", platform)
    except OSError as exc:
        logger.warning("Cookie migration for %s failed: %s", platform, type(exc).__name__)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _cookie_expired(cookie: dict[str, Any], now_epoch: float) -> bool:
    raw_expiry = cookie.get("expirationDate", cookie.get("expires"))
    if not isinstance(raw_expiry, (int, float)) or raw_expiry <= 0:
        return False
    return float(raw_expiry) <= now_epoch


def _cookie_export_matches_platform(path: Path, platform: str) -> bool:
    """Match a bounded browser export by provider facts, never by its filename."""

    config = PLATFORM_CONFIG.get(platform)
    if config is None or path.is_symlink():
        return False
    try:
        if path.stat().st_size > 5_000_000:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    cookies = payload.get("cookies", payload) if isinstance(payload, dict) else payload
    if not isinstance(cookies, list):
        return False
    if path.name == f"{platform}.json":
        return True
    expected_domain = str(config["domain"]).lstrip(".").lower()
    expected_names = set(config["key_cookies"])
    cookie_domains = {
        str(cookie.get("domain", "")).lstrip(".").lower()
        for cookie in cookies
        if isinstance(cookie, dict)
    }
    cookie_names = {
        str(cookie.get("name", ""))
        for cookie in cookies
        if isinstance(cookie, dict)
    }
    url_host = ""
    if isinstance(payload, dict):
        url_host = (urlparse(str(payload.get("url", ""))).hostname or "").lower()
    provider_matches = any(
        domain == expected_domain or domain.endswith(f".{expected_domain}")
        for domain in cookie_domains
    ) or url_host == expected_domain or url_host.endswith(f".{expected_domain}")
    return provider_matches and bool(cookie_names & expected_names)


async def cookies_valid(platform: str) -> bool:
    """Check if saved cookies are still valid by visiting a protected page headlessly.

    Returns True if the page loads without redirecting to login.
    For platforms with ``check_logged_in_selector`` configured (e.g. XHS,
    which shows a login modal without changing URL), element presence is
    checked instead of / in addition to the redirect pattern.
    """
    if platform not in PLATFORM_CONFIG:
        return False

    cookies = await load_cookies(platform)
    if not cookies:
        return False

    config = PLATFORM_CONFIG[platform]
    check_url = config.get("check_url")
    redirect_pattern = config.get("check_redirect_pattern")
    logged_in_selector = config.get("check_logged_in_selector")
    logged_out_selector = config.get("check_logged_out_selector")

    if not check_url:
        # Can't validate without check config - assume valid if cookies exist
        return True

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.add_cookies(
                cast(Any, normalize_cookies_for_playwright(cookies))
            )
            page = await context.new_page()

            await page.goto(check_url, wait_until="domcontentloaded", timeout=15_000)
            await page.wait_for_timeout(2000)  # let SPA render
            current_url = page.url

            # Selector-based check (SPA login modals that don't redirect)
            if logged_in_selector or logged_out_selector:
                logged_in = False
                logged_out = False
                if logged_in_selector:
                    el = await page.query_selector(logged_in_selector)
                    logged_in = bool(el)
                if logged_out_selector:
                    el = await page.query_selector(logged_out_selector)
                    logged_out = bool(el and await el.is_visible())

                await browser.close()

                if logged_out:
                    logger.info("Cookies for %s are expired (login UI visible)", platform)
                    return False
                if logged_in:
                    logger.info("Cookies for %s are valid", platform)
                    return True
                # Neither signal found - fall through to redirect check
            else:
                await browser.close()

            # If redirected to login page, cookies are invalid
            if redirect_pattern and redirect_pattern in current_url:
                logger.info("Cookies for %s are expired (redirected to login)", platform)
                return False

            logger.info("Cookies for %s are valid", platform)
            return True

    except Exception as e:
        logger.warning("Cookie validation failed for %s: %s", platform, e)
        return False
