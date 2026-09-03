"""Small, explicit Boss chat MQTT-over-WebSocket transport.

The browser chat client uses MQTT 3.1.1 on ``/chatws`` and protobuf payloads.
Credentials are supplied by the caller and are never persisted or logged.
This module does not discover credentials and does not expose a default
application-level send path; callers must opt into it explicitly.
"""

from __future__ import annotations

import asyncio
import secrets
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_CONFIG_PATH = "/wapi/zpchat/config/ws"


@dataclass(frozen=True)
class BossWsProbeResult:
    status: str
    nodes: tuple[str, ...] = ()
    error_type: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class BossWsCredentials:
    """Ephemeral credentials required by the browser's MQTT CONNECT."""

    user_id: int
    page_token: str
    ws_password: str
    nodes: tuple[str, ...]


def _mqtt_remaining_length(value: int) -> bytes:
    if value < 0:
        raise ValueError("remaining length must be non-negative")
    out = bytearray()
    while True:
        digit = value % 128
        value //= 128
        if value:
            digit |= 0x80
        out.append(digit)
        if not value:
            return bytes(out)


def _mqtt_utf8(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise ValueError("MQTT UTF-8 field is too long")
    return struct.pack(">H", len(encoded)) + encoded


def encode_mqtt_connect(*, client_id: str, username: str, password: str) -> bytes:
    """Encode the exact MQTT CONNECT shape used by the web client."""

    variable = _mqtt_utf8("MQTT") + bytes((4, 0xC2)) + struct.pack(">H", 25)
    payload = _mqtt_utf8(client_id) + _mqtt_utf8(username) + _mqtt_utf8(password)
    body = variable + payload
    return bytes((0x10,)) + _mqtt_remaining_length(len(body)) + body


def encode_mqtt_publish(*, topic: str, payload: bytes, packet_id: int = 1) -> bytes:
    """Encode a QoS-1 retained PUBLISH, matching Paho's chat client call."""

    if not 1 <= packet_id <= 0xFFFF:
        raise ValueError("packet_id must be between 1 and 65535")
    body = _mqtt_utf8(topic) + struct.pack(">H", packet_id) + payload
    return bytes((0x32,)) + _mqtt_remaining_length(len(body)) + body


def mqtt_packet_type(packet: bytes) -> int:
    if not packet:
        raise ValueError("empty MQTT packet")
    return packet[0] >> 4


def mqtt_connack_code(packet: bytes) -> int:
    if len(packet) < 4 or mqtt_packet_type(packet) != 2:
        raise ValueError("not an MQTT CONNACK packet")
    return packet[3]


def mqtt_puback_packet_id(packet: bytes) -> int:
    if len(packet) < 4 or mqtt_packet_type(packet) != 4:
        raise ValueError("not an MQTT PUBACK packet")
    return int(struct.unpack(">H", packet[-2:])[0])


def _protobuf_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("only unsigned protobuf integers are supported")
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _protobuf_int(field_number: int, value: int) -> bytes:
    return _protobuf_varint(field_number << 3) + _protobuf_varint(value)


def _protobuf_bytes(field_number: int, value: bytes) -> bytes:
    return (
        _protobuf_varint((field_number << 3) | 2)
        + _protobuf_varint(len(value))
        + value
    )


def _protobuf_string(field_number: int, value: str) -> bytes:
    return _protobuf_bytes(field_number, value.encode("utf-8"))


def _protobuf_message(field_number: int, value: bytes) -> bytes:
    return _protobuf_bytes(field_number, value)


def encode_text_protocol(
    *, from_uid: int, to_uid: int, friend_source: int, encrypt_uid: str, text: str
) -> bytes:
    """Encode ``TechwolfChatProtocol`` containing one text message."""

    if not text:
        raise ValueError("text must not be empty")
    mid = int(time.time() * 1000)
    from_user = _protobuf_int(1, from_uid) + _protobuf_int(7, 0)
    to_user = _protobuf_int(1, to_uid) + _protobuf_int(7, friend_source)
    if encrypt_uid:
        to_user += _protobuf_string(2, encrypt_uid)
    body = _protobuf_int(1, 1) + _protobuf_int(2, 1) + _protobuf_string(3, text)
    message = (
        _protobuf_message(1, from_user)
        + _protobuf_message(2, to_user)
        + _protobuf_int(3, 1)
        + _protobuf_int(4, mid)
        + _protobuf_message(6, body)
        + _protobuf_int(11, mid)
    )
    return _protobuf_int(1, 1) + _protobuf_message(3, message)


class BossMqttWsClient:
    """Opt-in MQTT client for a single already-authorized Boss session."""

    def __init__(
        self,
        *,
        node: str,
        page_token: str,
        ws_password: str,
        cookies: dict[str, str] | None = None,
        user_agent: str = "Mozilla/5.0",
    ) -> None:
        self._node = node
        self._page_token = page_token
        self._ws_password = ws_password
        self._cookies = cookies or {}
        self._user_agent = user_agent
        self._socket: Any | None = None

    async def connect(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets dependency is required") from exc
        headers = {"User-Agent": self._user_agent}
        if self._cookies:
            headers["Cookie"] = "; ".join(
                f"{key}={value}" for key, value in self._cookies.items()
            )
        self._socket = await websockets.connect(
            f"wss://{self._node}/chatws",
            origin="https://www.zhipin.com",  # type: ignore[arg-type]
            additional_headers=headers,
            open_timeout=10,
            close_timeout=3,
        )
        await self._socket.send(
            encode_mqtt_connect(
                client_id="ws-" + secrets.token_urlsafe(12)[:16],
                username=f"{self._page_token}|0",
                password=self._ws_password,
            )
        )
        packet = await asyncio.wait_for(self._recv_bytes(), timeout=10)
        code = mqtt_connack_code(packet)
        if code != 0:
            await self.close()
            raise ConnectionError(f"Boss MQTT CONNACK rejected: {code}")

    async def publish_text(
        self,
        *,
        from_uid: int,
        to_uid: int,
        friend_source: int,
        encrypt_uid: str,
        text: str,
    ) -> None:
        if self._socket is None:
            raise RuntimeError("connect() must be called first")
        payload = encode_text_protocol(
            from_uid=from_uid,
            to_uid=to_uid,
            friend_source=friend_source,
            encrypt_uid=encrypt_uid,
            text=text,
        )
        await self._socket.send(encode_mqtt_publish(topic="chat", payload=payload))
        deadline = time.monotonic() + 10
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Boss MQTT publish acknowledgement timed out")
            packet = await asyncio.wait_for(self._recv_bytes(), timeout=remaining)
            packet_type = mqtt_packet_type(packet)
            if packet_type == 4 and mqtt_puback_packet_id(packet) == 1:
                return
            # The broker can deliver an inbound chat/event packet before the
            # PUBACK.  It is not evidence of failure; keep reading within the
            # bounded acknowledgement window.

    async def close(self) -> None:
        if self._socket is not None:
            await self._socket.close()
            self._socket = None

    async def _recv_bytes(self) -> bytes:
        if self._socket is None:
            raise RuntimeError("WebSocket is not connected")
        return bytes(await self._socket.recv())


def _node_host(value: str) -> str | None:
    """Return a host-only representation without leaking URL parameters."""

    raw = value if value.startswith(("ws://", "wss://")) else f"wss://{value}"
    parsed = urlsplit(raw)
    return parsed.hostname


def _extract_nodes(body: Any) -> tuple[str, ...]:
    if not isinstance(body, dict):
        return ()
    data = body.get("zpData")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list):
        return ()
    nodes = {_node_host(item) for item in result if isinstance(item, str)}
    return tuple(sorted(node for node in nodes if node))


async def fetch_ws_nodes(
    *, cookies: dict[str, str], user_agent: str, base_url: str = "https://www.zhipin.com"
) -> BossWsProbeResult:
    """Fetch the server's WS cluster list using an in-memory session only."""

    try:
        import httpx
    except ImportError:
        return BossWsProbeResult("failed", error_type="httpx_unavailable")

    bst = cookies.get("bst", "")
    headers = {
        "User-Agent": user_agent,
        "Origin": base_url,
        "Referer": f"{base_url}/web/geek/chat",
        "zp_token": bst,
    }
    try:
        async with httpx.AsyncClient(
            cookies=cookies, headers=headers, timeout=15, follow_redirects=True
        ) as client:
            response = await client.get(f"{base_url}{_CONFIG_PATH}")
            if response.status_code != 200:
                return BossWsProbeResult(
                    "failed", error_type="config_http_error", detail=str(response.status_code)
                )
            nodes = _extract_nodes(response.json())
    except Exception as exc:
        return BossWsProbeResult(
            "failed", error_type="config_request_failed", detail=type(exc).__name__
        )
    if not nodes:
        return BossWsProbeResult("failed", error_type="ws_nodes_missing")
    return BossWsProbeResult("ok", nodes=nodes)


async def fetch_ws_credentials(
    *, cookies: dict[str, str], user_agent: str, base_url: str = "https://www.zhipin.com"
) -> BossWsCredentials:
    """Fetch the same short-lived values used by the web chat client.

    Values remain in memory and are returned only to the caller.  The helper
    intentionally does not write them to Settings, files, logs, or artifacts.
    """

    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx dependency is required") from exc

    bst = cookies.get("bst", "")
    headers = {
        "User-Agent": user_agent,
        "Origin": base_url,
        "Referer": f"{base_url}/web/geek/chat",
        "X-Requested-With": "XMLHttpRequest",
        "zp_token": bst,
    }
    async with httpx.AsyncClient(
        cookies=cookies, headers=headers, timeout=15, follow_redirects=True
    ) as client:
        user_response = await client.get(f"{base_url}/wapi/zpuser/wap/getUserInfo.json")
        wt_response = await client.get(f"{base_url}/wapi/zppassport/get/wt")
        ws_response = await client.get(f"{base_url}{_CONFIG_PATH}")
    user_body = user_response.json()
    wt_body = wt_response.json()
    if user_response.status_code != 200 or wt_response.status_code != 200:
        raise ConnectionError("Boss WS credential request failed")
    user_data = user_body.get("zpData") if isinstance(user_body, dict) else None
    wt_data = wt_body.get("zpData") if isinstance(wt_body, dict) else None
    if not isinstance(user_data, dict) or not isinstance(wt_data, dict):
        raise ConnectionError("Boss WS credential response was malformed")
    user_id = user_data.get("userId")
    page_token = user_data.get("token")
    ws_password = wt_data.get("wt2")
    nodes = _extract_nodes(ws_response.json())
    if not isinstance(user_id, int) or not isinstance(page_token, str) or not page_token:
        raise ConnectionError("Boss page token was missing")
    if not isinstance(ws_password, str) or not ws_password or not nodes:
        raise ConnectionError("Boss WS credentials or nodes were missing")
    return BossWsCredentials(user_id, page_token, ws_password, nodes)


async def probe_ws_handshake(
    *, nodes: tuple[str, ...], cookies: dict[str, str], user_agent: str, path: str
) -> BossWsProbeResult:
    """Attempt a handshake only; never sends an application-level frame.

    ``path`` is required on purpose.  The config endpoint does not provide it,
    and guessing one would turn a diagnostic into uncontrolled probing.
    """

    if not path.startswith("/"):
        return BossWsProbeResult("failed", error_type="invalid_ws_path")
    try:
        import websockets
    except ImportError:
        return BossWsProbeResult("failed", error_type="websockets_unavailable")

    cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
    headers = {"Cookie": cookie_header, "User-Agent": user_agent}
    failures: list[str] = []
    for node in nodes:
        try:
            async with websockets.connect(
                f"wss://{node}{path}",
                origin="https://www.zhipin.com",  # type: ignore[arg-type]
                additional_headers=headers,
                open_timeout=10,
                close_timeout=3,
            ):
                return BossWsProbeResult("connected", nodes=(node,))
        except Exception as exc:
            failures.append(type(exc).__name__)
    return BossWsProbeResult(
        "failed", nodes=nodes, error_type="ws_handshake_failed", detail=",".join(failures)
    )
