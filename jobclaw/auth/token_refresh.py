"""Claude OAuth token auto-refresh."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

_DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


def ensure_valid_token(
    credentials_path: str | Path | None = None,
    *,
    min_remaining_minutes: int = 10,
) -> bool:
    """Check if Claude OAuth token is valid; refresh if expired or expiring soon.

    Parameters
    ----------
    credentials_path:
        Override path for the credentials JSON file.
    min_remaining_minutes:
        Refresh if fewer than this many minutes remain.

    Returns
    -------
    bool
        ``True`` if token is valid (or was successfully refreshed),
        ``False`` otherwise.
    """
    creds_path = Path(credentials_path or _DEFAULT_CREDENTIALS_PATH)

    if not creds_path.exists():
        logger.warning("No credentials file at %s", creds_path)
        return False

    creds = json.loads(creds_path.read_text(encoding="utf-8"))

    # Handle nested structure: {"claudeAiOauth": {...}}
    oauth = creds.get("claudeAiOauth", creds)

    expires_at = oauth.get("expiresAt", 0)
    # expiresAt can be in milliseconds or seconds
    now_ms = int(time.time() * 1000)
    if expires_at < 1e12:  # looks like seconds
        expires_at *= 1000

    remaining_ms = expires_at - now_ms
    remaining_min = remaining_ms / 1000 / 60

    if remaining_min > min_remaining_minutes:
        logger.debug("Token valid for %.0f more minutes", remaining_min)
        return True

    # Need to refresh
    refresh_token = oauth.get("refreshToken") or oauth.get("refresh_token")
    if not refresh_token:
        logger.error("No refresh token available")
        return False

    logger.info("Token expires in %.0f min, refreshing...", remaining_min)

    try:
        resp = httpx.post(
            ANTHROPIC_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": ANTHROPIC_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            timeout=30.0,
        )

        if resp.status_code != 200:
            logger.error(
                "Token refresh failed: %s %s",
                resp.status_code,
                resp.text[:200],
            )
            return False

        data = resp.json()
        oauth["accessToken"] = data["access_token"]
        if data.get("refresh_token"):
            oauth["refreshToken"] = data["refresh_token"]
        oauth["expiresAt"] = (
            int(time.time() * 1000) + data.get("expires_in", 28800) * 1000
        )

        # Write back
        if "claudeAiOauth" in creds:
            creds["claudeAiOauth"] = oauth
        else:
            creds = oauth

        creds_path.write_text(json.dumps(creds, indent=2), encoding="utf-8")
        logger.info(
            "Token refreshed, valid for %.0f minutes",
            data.get("expires_in", 0) / 60,
        )
        return True

    except Exception as exc:
        logger.error("Token refresh error: %s", exc)
        return False
