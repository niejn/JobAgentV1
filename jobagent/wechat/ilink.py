"""Text-only WeChat iLink Bot API client.

Ported from Hermes Agent's Weixin platform adapter (MIT License,
Copyright (c) 2025 Nous Research, ``gateway/platforms/weixin.py``) and the
``wechat-ilink-demo`` reference bot, reduced to the text-messaging core
JobAgent needs. Media CDN, typing indicators and Markdown chunking stay out
of scope until the notification use case demands them.

Protocol notes - iLink Bot API is Tencent's official personal-WeChat bot API:

- Pure HTTP/JSON against ``https://ilinkai.weixin.qq.com``; no WebSocket, no
  webhook, no public endpoint, no client hook - no account-ban risk.
- QR login: ``get_bot_qrcode`` -> scan with the WeChat mobile app ->
  ``get_qrcode_status`` returns a Bearer ``bot_token`` plus a bot identity
  (``...@im.bot``) that is separate from the scanning account.
- Inbound: ``getupdates`` long-polls (35 s server hold) with a cursor
  (``get_updates_buf``) advancing like Telegram's update offset.
- Outbound: ``sendmessage`` must echo the peer's latest ``context_token``;
  a bot cannot cold-start a conversation - the user messages first. This is
  why the JobAgent notification design pairs this channel (interactive,
  user-initiated) with local desktop toast notifications (always deliverable).
- Session expiry (``errcode -14``) requires re-running QR login.
"""

from __future__ import annotations

import base64
import json
import secrets
import struct
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
CHANNEL_VERSION = "2.2.0"

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_SECONDS = 40.0  # ~30 s server hold (measured) + client grace
API_TIMEOUT_SECONDS = 15.0

SESSION_EXPIRED_ERRCODE = -14
MSG_TYPE_BOT = 2
ITEM_TEXT = 1

#: Credentials live under the JobAgent home directory, next to cookies.
WECHAT_DIR = Path.home() / ".jobagent" / "wechat"
ACCOUNT_FILE = WECHAT_DIR / "account.json"
CONTEXT_TOKENS_FILE = WECHAT_DIR / "context-tokens.json"


class ILinkApiError(RuntimeError):
    """iLink API returned an error payload; ret/errcode attached."""

    def __init__(
        self,
        message: str,
        *,
        ret: int | None = None,
        errcode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.ret = ret
        self.errcode = errcode


class WeixinSessionExpired(ILinkApiError):
    """Bearer token expired (errcode -14); QR login must be re-run."""


class QRLoginState(StrEnum):
    WAITING = "waiting"
    SCANNED = "scanned"
    REDIRECT = "scaned_but_redirect"  # API's own typo; carries redirect_host
    EXPIRED = "expired"
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class QRCodeLogin:
    """One pending QR login challenge."""

    img_content: str  # the scannable liteapp URL - render as QR code
    key: str  # polling key for get_qrcode_status


@dataclass(frozen=True, slots=True)
class QRLoginStatus:
    """Latest state of one QR login challenge."""

    state: QRLoginState
    bot_token: str = ""
    base_url: str = ""
    bot_id: str = ""
    owner_user_id: str = ""  # the WeChat user who scanned (the bot owner)
    redirect_host: str = ""  # for REDIRECT: poll this host from now on


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One text message received from a WeChat user."""

    from_user_id: str
    text: str
    context_token: str
    message_id: str


class WeixinAccount(BaseModel):
    """Persisted iLink bot credentials."""

    bot_token: str
    base_url: str = ILINK_BASE_URL
    bot_id: str = ""
    owner_user_id: str = ""  # the scanning user; the only allowed sender
    login_at: str = ""


def _random_wechat_uin() -> str:
    """Anti-replay header: random uint32 -> decimal string -> base64."""

    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _raise_for_payload(payload: dict[str, Any], endpoint: str) -> None:
    """Turn iLink error payloads into typed exceptions."""

    ret = payload.get("ret")
    errcode = payload.get("errcode")
    if (ret in (None, 0)) and errcode in (None, 0):
        return
    if errcode == SESSION_EXPIRED_ERRCODE:
        raise WeixinSessionExpired(
            f"iLink {endpoint}: session expired (errcode -14); "
            "re-run `jobagent login --platform wechat`",
            ret=ret if isinstance(ret, int) else None,
            errcode=errcode if isinstance(errcode, int) else None,
        )
    raise ILinkApiError(
        f"iLink {endpoint}: ret={ret} errcode={errcode} "
        f"errmsg={payload.get('errmsg', '')}",
        ret=ret if isinstance(ret, int) else None,
        errcode=errcode if isinstance(errcode, int) else None,
    )


class WeixinBotClient:
    """Async text-messaging client for one iLink bot account."""

    def __init__(
        self,
        *,
        account: WeixinAccount | None = None,
        base_url: str = ILINK_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._account = account
        self._base_url = (account.base_url if account else base_url).rstrip("/")
        self._client = httpx.AsyncClient(transport=transport)

    async def __aenter__(self) -> WeixinBotClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def token(self) -> str:
        if self._account is None:
            raise RuntimeError("WeixinBotClient has no account; log in first")
        return self._account.bot_token

    # -- login phase (unauthenticated) --------------------------------------

    async def request_qr_code(self) -> QRCodeLogin:
        """Fetch a fresh QR login challenge."""

        response = await self._client.get(
            f"{self._base_url}/{EP_GET_BOT_QR}",
            params={"bot_type": 3},
            headers=_headers(None),
            timeout=API_TIMEOUT_SECONDS,
        )
        payload = _json_response(response, EP_GET_BOT_QR)
        img_content = str(payload.get("qrcode_img_content") or "")
        key = str(payload.get("qrcode") or "")
        if not img_content or not key:
            raise ILinkApiError(
                f"iLink {EP_GET_BOT_QR}: response missing QR fields: {payload}"
            )
        return QRCodeLogin(img_content=img_content, key=key)

    async def poll_qr_status(
        self, qrcode_key: str, *, base_url: str | None = None
    ) -> QRLoginStatus:
        """Long-poll one QR challenge until its state changes.

        The server holds each request ~30 s (scan/confirm wait included), so
        this uses the long-poll budget; a client-side timeout just means
        "no news yet" and reads as WAITING.

        ``base_url`` overrides the client default because a REDIRECT state
        moves polling to ``redirect_host`` mid-login.
        """

        effective_base = (base_url or self._base_url).rstrip("/")
        try:
            response = await self._client.get(
                f"{effective_base}/{EP_GET_QR_STATUS}",
                params={"qrcode": qrcode_key},
                headers=_headers(None),
                timeout=LONG_POLL_TIMEOUT_SECONDS,
            )
        except httpx.TransportError:
            # Same policy as hermes qr_login: transport failures during the
            # scan/confirm wait (timeout, reset, refused redirect host) are
            # retried by the caller until the login deadline, not fatal.
            return QRLoginStatus(state=QRLoginState.WAITING)
        payload = _json_response(response, EP_GET_QR_STATUS)
        bot_token = str(payload.get("bot_token") or "")
        if bot_token:
            return QRLoginStatus(
                state=QRLoginState.CONFIRMED,
                bot_token=bot_token,
                base_url=str(payload.get("baseurl") or ILINK_BASE_URL),
                bot_id=str(
                    payload.get("ilink_bot_id") or payload.get("bot_id") or ""
                ),
                owner_user_id=str(payload.get("ilink_user_id") or ""),
            )
        raw_state = str(payload.get("status") or "").lower()
        if raw_state in {"scanned", "scaned"}:  # the API itself misspells it
            return QRLoginStatus(state=QRLoginState.SCANNED)
        if raw_state == "scaned_but_redirect":
            return QRLoginStatus(
                state=QRLoginState.REDIRECT,
                redirect_host=str(payload.get("redirect_host") or ""),
            )
        if raw_state == "expired":
            return QRLoginStatus(state=QRLoginState.EXPIRED)
        return QRLoginStatus(state=QRLoginState.WAITING)

    # -- messaging phase (authenticated) ------------------------------------

    async def get_updates(
        self, cursor: str = ""
    ) -> tuple[list[InboundMessage], str]:
        """Long-poll inbound messages; returns ``(messages, next_cursor)``.

        A long-poll timeout is normal and yields an empty result with the
        unchanged cursor.
        """

        try:
            response = await self._client.post(
                f"{self._base_url}/{EP_GET_UPDATES}",
                json={
                    "get_updates_buf": cursor,
                    "base_info": {"channel_version": CHANNEL_VERSION},
                },
                headers=_headers(self.token),
                timeout=LONG_POLL_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            return [], cursor
        payload = _json_response(response, EP_GET_UPDATES)
        _raise_for_payload(payload, EP_GET_UPDATES)
        next_cursor = str(payload.get("get_updates_buf") or cursor)
        messages: list[InboundMessage] = []
        for raw in payload.get("msgs") or []:
            if not isinstance(raw, dict):
                continue
            message = _parse_inbound(raw)
            if message is not None:
                messages.append(message)
        return messages, next_cursor

    async def send_text(
        self, to_user_id: str, text: str, context_token: str
    ) -> None:
        """Send one text message; must echo the peer's latest context_token."""

        if not text or not text.strip():
            raise ValueError("send_text: text must not be empty")
        if not context_token:
            raise ValueError(
                "send_text: context_token is required - the user must have "
                "messaged the bot first (iLink bots cannot cold-start chats)"
            )
        response = await self._client.post(
            f"{self._base_url}/{EP_SEND_MESSAGE}",
            json={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": f"jobagent-{secrets.token_hex(8)}",
                    "message_type": MSG_TYPE_BOT,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
                },
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
            headers=_headers(self.token),
            timeout=API_TIMEOUT_SECONDS,
        )
        payload = _json_response(response, EP_SEND_MESSAGE)
        _raise_for_payload(payload, EP_SEND_MESSAGE)


def _json_response(response: httpx.Response, endpoint: str) -> dict[str, Any]:
    """Parse one iLink JSON response body (empty body means success)."""

    if response.status_code != 200:
        raise ILinkApiError(
            f"iLink {endpoint}: HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )
    raw = response.text.strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ILinkApiError(f"iLink {endpoint}: invalid JSON: {raw[:200]}") from exc
    if not isinstance(payload, dict):
        raise ILinkApiError(f"iLink {endpoint}: unexpected payload: {raw[:200]}")
    return payload


def _parse_inbound(raw: dict[str, Any]) -> InboundMessage | None:
    """Normalize one raw message; returns None for bot-self/system messages."""

    if raw.get("message_type") == MSG_TYPE_BOT:
        return None
    from_user_id = str(raw.get("from_user_id") or "")
    if from_user_id.endswith("@im.bot"):
        return None
    context_token = str(raw.get("context_token") or "")
    text = "".join(
        str(item.get("text_item", {}).get("text") or "")
        for item in raw.get("item_list") or []
        if isinstance(item, dict) and item.get("type") == ITEM_TEXT
    )
    message_id = str(raw.get("message_id") or raw.get("client_id") or "")
    return InboundMessage(
        from_user_id=from_user_id,
        text=text,
        context_token=context_token,
        message_id=message_id,
    )


class WeixinAccountStore:
    """Persist the iLink bot account under ``~/.jobagent/wechat``."""

    def __init__(self, path: Path = ACCOUNT_FILE) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def save(self, account: WeixinAccount) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(
            account.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self._path)
        try:
            self._path.chmod(0o600)
        except OSError:
            pass  # Windows/NTFS does not enforce POSIX modes

    def load(self) -> WeixinAccount | None:
        if not self._path.is_file():
            return None
        try:
            return WeixinAccount.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except ValueError:
            return None


class ContextTokenStore:
    """Disk-backed per-peer context_token cache for reply continuity."""

    def __init__(self, path: Path = CONTEXT_TOKENS_FILE) -> None:
        self._path = path
        self._cache: dict[str, str] = {}
        self._restore()

    def get(self, user_id: str) -> str | None:
        return self._cache.get(user_id)

    def set(self, user_id: str, token: str) -> None:
        if not token:
            return
        self._cache[user_id] = token
        self._persist()

    def _restore(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self._cache = {
                str(key): str(value)
                for key, value in data.items()
                if isinstance(value, str) and value
            }

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._path)


class MessageDeduplicator:
    """Sliding-window dedup by message id (overlapping poll responses)."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}
        self._now = time.monotonic

    def seen(self, message_id: str) -> bool:
        """Mark ``message_id``; return True when it was already seen."""

        now = self._now()
        # Evict expired entries to keep the window bounded.
        self._seen = {
            key: stamp for key, stamp in self._seen.items()
            if now - stamp < self._ttl_seconds
        }
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False
