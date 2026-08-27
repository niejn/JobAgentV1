"""Telegram notification channel via Bot API."""

from __future__ import annotations

import html
import logging

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send notifications via Telegram Bot API."""

    BASE_URL = "https://api.telegram.org/bot{token}"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token = bot_token
        self._chat_id = chat_id

    async def send_text(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a text message to the configured chat.

        Args:
            text: Message content.
            parse_mode: Telegram parse mode (HTML or Markdown).

        Returns:
            True if message was sent successfully.
        """
        # Telegram limit is 4096; HTML parse_mode requires escaping
        text = html.escape(text)
        if len(text) > 4096:
            text = text[:4093] + "…"
        url = f"{self.BASE_URL.format(token=self._token)}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=10)
                resp.raise_for_status()
                logger.info("Telegram message sent to %s", self._chat_id)
                return True
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
            return False