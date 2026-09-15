"""Small, explicit Boss chat MQTT-over-WebSocket transport.

The browser chat client uses MQTT 3.1.1 on ``/chatws`` and protobuf payloads.
Credentials are supplied by the caller and are never persisted or logged.
This module does not discover credentials and does not expose a default
application-level send path; callers must opt into it explicitly.
"""

from __future__ import annotations

import asyncio
import base64
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


@dataclass(frozen=True)
class BossConversationTarget:
    friend_id: int
    friend_source: int
    encrypt_boss_id: str
    name: str
    company: str
    job_title: str


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


def mqtt_publish_payload(packet: bytes) -> bytes:
    """Extract the application payload from one MQTT PUBLISH packet."""

    if not packet or mqtt_packet_type(packet) != 3:
        raise ValueError("not an MQTT PUBLISH packet")
    index = 1
    multiplier = 1
    remaining = 0
    while True:
        if index >= len(packet) or multiplier > 128**3:
            raise ValueError("invalid MQTT remaining length")
        digit = packet[index]
        index += 1
        remaining += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            break
        multiplier *= 128
    if index + 2 > len(packet):
        raise ValueError("truncated MQTT topic")
    topic_length = struct.unpack(">H", packet[index : index + 2])[0]
    index += 2 + topic_length
    qos = (packet[0] >> 1) & 0x03
    if qos:
        index += 2
    if index > len(packet):
        raise ValueError("truncated MQTT PUBLISH packet")
    return packet[index:]


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


def _protobuf_fields(payload: bytes) -> dict[int, list[int | bytes]]:
    """Parse the protobuf wire types used by Boss chat messages."""

    fields: dict[int, list[int | bytes]] = {}
    index = 0
    while index < len(payload):
        key, index = _read_varint(payload, index)
        field_number, wire_type = key >> 3, key & 0x07
        if not field_number:
            raise ValueError("invalid protobuf field number")
        if wire_type == 0:
            int_value, index = _read_varint(payload, index)
            value: int | bytes = int_value
        elif wire_type == 2:
            size, index = _read_varint(payload, index)
            end = index + size
            if end > len(payload):
                raise ValueError("truncated protobuf field")
            value, index = payload[index:end], end
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
        fields.setdefault(field_number, []).append(value)
    return fields


def _read_varint(payload: bytes, index: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while index < len(payload):
        digit = payload[index]
        index += 1
        value |= (digit & 0x7F) << shift
        if not digit & 0x80:
            return value, index
        shift += 7
        if shift > 63:
            raise ValueError("protobuf varint is too large")
    raise ValueError("truncated protobuf varint")


def decode_chat_protocol(payload: bytes) -> dict[str, Any]:
    """Decode the stable envelope and message identity fields."""

    protocol = _protobuf_fields(payload)
    result: dict[str, Any] = {
        "type": int(protocol.get(1, [0])[0]),
        "messages": [],
    }
    for raw_message in protocol.get(3, []):
        if not isinstance(raw_message, bytes):
            continue
        message = _protobuf_fields(raw_message)
        item: dict[str, Any] = {
            "type": int(message.get(3, [0])[0]),
            "mid": int(message.get(4, [0])[0]),
            "from_uid": _nested_int(message.get(1, []), 1),
            "to_uid": _nested_int(message.get(2, []), 1),
        }
        bodies = message.get(6, [])
        if bodies and isinstance(bodies[0], bytes):
            body = _protobuf_fields(bodies[0])
            body_type = int(body.get(1, [0])[0])
            item["body_type"] = body_type
            item["text"] = _nested_text(body.get(3, []))
            item["body_fields"] = {
                str(number): [
                    base64.b64encode(value).decode("ascii")
                    if isinstance(value, bytes)
                    else value
                    for value in values
                ]
                for number, values in body.items()
            }
        result["messages"].append(item)
    return result


def find_resume_requests(payload: bytes) -> list[dict[str, Any]]:
    """Return only inbound messages representing resume request cards."""

    decoded = decode_chat_protocol(payload)
    return [message for message in decoded["messages"] if message.get("body_type") == 9]


def _nested_int(values: list[int | bytes], field_number: int) -> int:
    if not values or not isinstance(values[0], bytes):
        return 0
    nested = _protobuf_fields(values[0])
    return int(nested.get(field_number, [0])[0])


def _nested_text(values: list[int | bytes]) -> str:
    if not values or not isinstance(values[0], bytes):
        return ""
    return values[0].decode("utf-8", errors="replace")


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
        self._next_packet_id = 0

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
        self._next_packet_id = (self._next_packet_id % 0xFFFF) + 1
        packet_id = self._next_packet_id
        await self._socket.send(
            encode_mqtt_publish(topic="chat", payload=payload, packet_id=packet_id)
        )
        deadline = time.monotonic() + 10
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Boss MQTT publish acknowledgement timed out")
            try:
                packet = await asyncio.wait_for(self._recv_bytes(), timeout=remaining)
            except Exception as exc:
                # A broker may close the WebSocket cleanly immediately after
                # accepting the publish, before the PUBACK reaches us. Keep
                # this as an ambiguous transport result so callers perform
                # history verification instead of treating it as a hard
                # send failure.
                if type(exc).__name__.startswith("ConnectionClosed"):
                    raise ConnectionError(
                        "Boss WebSocket closed before PUBACK; delivery requires verification"
                    ) from exc
                raise
            packet_type = mqtt_packet_type(packet)
            if packet_type == 4 and mqtt_puback_packet_id(packet) == packet_id:
                return
            if packet_type == 3:
                # Some Boss nodes echo the accepted outgoing message before
                # the QoS-1 PUBACK. That echo is positive delivery evidence;
                # do not wait 10 seconds for an ACK that may be delayed.
                try:
                    echoed = decode_chat_protocol(mqtt_publish_payload(packet))
                    if any(item.get("text") == text for item in echoed["messages"]):
                        return
                except (TypeError, ValueError, KeyError):
                    pass
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
    *,
    cookies: dict[str, str],
    user_agent: str,
    base_url: str = "https://www.zhipin.com",
    page_token: str = "",
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
    response_page_token = user_data.get("token")
    ws_password = wt_data.get("wt2")
    nodes = _extract_nodes(ws_response.json())
    effective_page_token = page_token or response_page_token
    if (
        not isinstance(user_id, int)
        or not isinstance(effective_page_token, str)
        or not effective_page_token
    ):
        raise ConnectionError("Boss page token was missing")
    if not isinstance(ws_password, str) or not ws_password or not nodes:
        raise ConnectionError("Boss WS credentials or nodes were missing")
    return BossWsCredentials(user_id, effective_page_token, ws_password, nodes)


async def find_conversation_target(
    *,
    cookies: dict[str, str],
    company: str,
    job_title: str,
    friend_name: str | None = None,
    expected_encrypt_boss_id: str | None = None,
    user_agent: str = "Mozilla/5.0",
    base_url: str = "https://www.zhipin.com",
) -> BossConversationTarget:
    """Find the unique HR conversation created for a job card."""

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
        cookies=cookies, headers=headers, timeout=20, follow_redirects=True
    ) as client:
        response = await client.get(
            f"{base_url}/wapi/zprelation/friend/geekFilterByLabel",
            params={"labelId": 0, "_": int(time.time() * 1000)},
        )
    body = response.json()
    friends = ((body.get("zpData") or {}).get("friendList") or [])
    candidates = []
    for friend in friends:
        friend_company = str(friend.get("brandName") or "")
        friend_title = str(friend.get("jobName") or friend.get("positionName") or "")
        actual_encrypt_boss_id = str(
            friend.get("encryptBossId") or friend.get("encryptFriendId") or ""
        )
        if friend_company != company:
            continue
        exact_boss_match = bool(
            expected_encrypt_boss_id
            and expected_encrypt_boss_id == actual_encrypt_boss_id
        )
        if (
            not exact_boss_match
            and job_title not in friend_title
            and friend_title not in job_title
        ):
            continue
        if (
            friend_name
            and not expected_encrypt_boss_id
            and str(friend.get("name") or "") != friend_name
        ):
            continue
        if not friend.get("friendId"):
            continue
        if actual_encrypt_boss_id:
            if (
                expected_encrypt_boss_id
                and expected_encrypt_boss_id != actual_encrypt_boss_id
            ):
                continue
            candidates.append(
                BossConversationTarget(
                    friend_id=int(friend["friendId"]),
                    friend_source=int(friend.get("friendSource") or 0),
                    encrypt_boss_id=actual_encrypt_boss_id,
                    name=str(friend.get("name") or ""),
                    company=friend_company,
                    job_title=friend_title,
                )
            )
    if len(candidates) != 1:
        raise LookupError(f"expected one Boss conversation, found {len(candidates)}")
    return candidates[0]


async def send_text_to_conversation(
    *,
    cookies: dict[str, str],
    company: str,
    job_title: str,
    text: str,
    user_agent: str = "Mozilla/5.0",
) -> BossConversationTarget:
    """Send one text to a job-created conversation over direct MQTT/WS."""

    target = await find_conversation_target(
        cookies=cookies, company=company, job_title=job_title, user_agent=user_agent
    )
    credentials = await fetch_ws_credentials(cookies=cookies, user_agent=user_agent)
    client = BossMqttWsClient(
        node=credentials.nodes[0],
        page_token=credentials.page_token,
        ws_password=credentials.ws_password,
        cookies=cookies,
        user_agent=user_agent,
    )
    await client.connect()
    try:
        await client.publish_text(
            from_uid=credentials.user_id,
            to_uid=target.friend_id,
            friend_source=target.friend_source,
            encrypt_uid=target.encrypt_boss_id,
            text=text,
        )
    finally:
        await client.close()
    return target


async def send_text_to_target(
    *,
    cookies: dict[str, str],
    target: BossConversationTarget,
    text: str,
    user_agent: str = "Mozilla/5.0",
    page_token: str = "",
) -> None:
    """Send text to an already resolved conversation without another lookup."""

    credentials = await fetch_ws_credentials(
        cookies=cookies, user_agent=user_agent, page_token=page_token
    )
    client = BossMqttWsClient(
        node=credentials.nodes[0],
        page_token=credentials.page_token,
        ws_password=credentials.ws_password,
        cookies=cookies,
        user_agent=user_agent,
    )
    await client.connect()
    try:
        await client.publish_text(
            from_uid=credentials.user_id,
            to_uid=target.friend_id,
            friend_source=target.friend_source,
            encrypt_uid=target.encrypt_boss_id,
            text=text,
        )
    finally:
        await client.close()


async def verify_text_in_conversation(
    *,
    cookies: dict[str, str],
    target: BossConversationTarget,
    text: str,
    user_agent: str = "Mozilla/5.0",
    not_before_ms: int = 0,
) -> bool:
    """Confirm an outgoing text through Boss history after an ACK ambiguity."""

    try:
        import httpx
    except ImportError:
        return False
    headers = {
        "User-Agent": user_agent,
        "Origin": "https://www.zhipin.com",
        "Referer": "https://www.zhipin.com/web/geek/chat",
        "X-Requested-With": "XMLHttpRequest",
        "zp_token": cookies.get("bst", ""),
    }
    async with httpx.AsyncClient(
        cookies=cookies, headers=headers, timeout=20, follow_redirects=True
    ) as client:
        form = (
            {"dzFriendIds": str(target.friend_id)}
            if target.friend_source == 1
            else {"friendIds": str(target.friend_id)}
        )
        full_response = await client.post(
            "https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json",
            params={"_": int(time.time() * 1000)},
            data=form,
        )
        full_body = full_response.json()
        full_items = ((full_body.get("zpData") or {}).get("result") or [])
        full: dict[str, Any] = next(
            (item for item in full_items if str(item.get("friendId")) == str(target.friend_id)),
            {},
        )
        security_id = str(full.get("securityId") or "")
        boss_id = str(
            full.get("encryptBossId")
            or full.get("encryptFriendId")
            or target.encrypt_boss_id
        )
        if not security_id or not boss_id:
            return False
        history_response = await client.get(
            "https://www.zhipin.com/wapi/zpchat/geek/historyMsg",
            params={
                "bossId": boss_id,
                "maxMsgId": 0,
                "c": 100,
                "page": 1,
                "src": 0,
                "securityId": security_id,
                "_": int(time.time() * 1000),
            },
        )
        history_body = history_response.json()
        messages = ((history_body.get("zpData") or {}).get("messages") or [])
    for message in messages:
        if not isinstance(message, dict):
            continue
        body = message.get("body")
        body_text = body.get("text") if isinstance(body, dict) else ""
        if str(body_text or message.get("text") or "") != text:
            continue
        try:
            sent_at = int(message.get("time") or message.get("createTime") or 0)
            from_id = int(message.get("fromId") or 0)
        except (TypeError, ValueError):
            continue
        sent_at_ms = sent_at if sent_at >= 1_000_000_000_000 else sent_at * 1000
        if from_id == target.friend_id:
            continue
        # Boss history timestamps can be second-precision while the local
        # attempt clock is milliseconds. Allow one timestamp bucket plus a
        # small clock-skew margin without matching genuinely old messages.
        if not_before_ms and sent_at_ms < not_before_ms - 2_000:
            continue
        return True
    return False


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
