"""Discord notification channel via webhook."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Send notifications via Discord webhook."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send_text(self, text: str) -> bool:
        """Send a text message to the configured Discord webhook.

        Args:
            text: Message content (Discord markdown supported).

        Returns:
            True if message was sent successfully.
        """
        # Discord limit is 2000; truncate with ellipsis so receiver knows
        if len(text) > 2000:
            text = text[:1997] + "…"
        payload = {"content": text}

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._webhook_url, json=payload, timeout=10,
                )
                resp.raise_for_status()
                logger.info("Discord message sent")
                return True
        except Exception as e:
            logger.error("Discord send failed: %s", e)
            return False
