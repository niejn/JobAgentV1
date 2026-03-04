"""Tests for Claude OAuth token auto-refresh."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobclaw.auth.token_refresh import ensure_valid_token


@pytest.fixture()
def creds_file(tmp_path: Path) -> Path:
    """Create a temporary credentials file with an expired token."""
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access-token",
                    "refreshToken": "test-refresh-token",
                    "expiresAt": int(time.time() * 1000) - 60_000,  # expired 1 min ago
                    "scopes": ["user:inference"],
                    "subscriptionType": "pro",
                    "rateLimitTier": "tier_4",
                }
            }
        )
    )
    return path


@pytest.fixture()
def valid_creds_file(tmp_path: Path) -> Path:
    """Create a temporary credentials file with a valid (non-expired) token."""
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "valid-access-token",
                    "refreshToken": "test-refresh-token",
                    "expiresAt": int(time.time() * 1000) + 3_600_000,  # 1 hour left
                    "scopes": ["user:inference"],
                }
            }
        )
    )
    return path


class TestEnsureValidToken:
    """Test ensure_valid_token() with various scenarios."""

    def test_valid_token_no_refresh(self, valid_creds_file: Path) -> None:
        """Token with plenty of time left should not trigger a refresh."""
        with patch("jobclaw.auth.token_refresh.httpx.post") as mock_post:
            result = ensure_valid_token(valid_creds_file)

        assert result is True
        mock_post.assert_not_called()

    def test_missing_credentials_file(self, tmp_path: Path) -> None:
        """Missing credentials file should return False."""
        result = ensure_valid_token(tmp_path / "nonexistent.json")
        assert result is False

    def test_refresh_success(self, creds_file: Path) -> None:
        """Expired token should be refreshed successfully."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 28800,
        }

        with patch("jobclaw.auth.token_refresh.httpx.post", return_value=mock_response) as mock_post:
            result = ensure_valid_token(creds_file)

        assert result is True
        mock_post.assert_called_once()

        # Verify credentials were written back
        updated = json.loads(creds_file.read_text())
        oauth = updated["claudeAiOauth"]
        assert oauth["accessToken"] == "new-access-token"
        assert oauth["refreshToken"] == "new-refresh-token"
        assert oauth["expiresAt"] > int(time.time() * 1000)

    def test_refresh_failure_http_error(self, creds_file: Path) -> None:
        """HTTP error during refresh should return False."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("jobclaw.auth.token_refresh.httpx.post", return_value=mock_response):
            result = ensure_valid_token(creds_file)

        assert result is False

    def test_refresh_failure_network_error(self, creds_file: Path) -> None:
        """Network error during refresh should return False."""
        with patch(
            "jobclaw.auth.token_refresh.httpx.post",
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            result = ensure_valid_token(creds_file)

        assert result is False

    def test_no_refresh_token(self, tmp_path: Path) -> None:
        """Missing refresh token should return False."""
        path = tmp_path / ".credentials.json"
        path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "old-token",
                        "expiresAt": int(time.time() * 1000) - 60_000,
                    }
                }
            )
        )
        result = ensure_valid_token(path)
        assert result is False

    def test_expires_at_in_seconds(self, tmp_path: Path) -> None:
        """expiresAt in seconds (not ms) should be handled correctly."""
        path = tmp_path / ".credentials.json"
        path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "valid-token",
                        "refreshToken": "refresh-token",
                        "expiresAt": int(time.time()) + 7200,  # 2 hours, in seconds
                    }
                }
            )
        )

        with patch("jobclaw.auth.token_refresh.httpx.post") as mock_post:
            result = ensure_valid_token(path)

        assert result is True
        mock_post.assert_not_called()


# Need to import httpx for the ConnectError in the test above
import httpx  # noqa: E402
