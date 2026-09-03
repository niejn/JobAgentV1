from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobagent.applier.boss_ws import (
    BossMqttWsClient,
    _extract_nodes,
    encode_mqtt_connect,
    encode_mqtt_publish,
    encode_text_protocol,
    fetch_ws_nodes,
    mqtt_connack_code,
    mqtt_packet_type,
    mqtt_puback_packet_id,
    probe_ws_handshake,
)


def test_extract_nodes_returns_hosts_only() -> None:
    body = {"zpData": {"result": ["ws6.zhipin.com", "wss://ws.zhipin.com?token=secret"]}}
    assert _extract_nodes(body) == ("ws.zhipin.com", "ws6.zhipin.com")


def test_mqtt_connect_and_publish_frames_have_expected_types() -> None:
    connect = encode_mqtt_connect(client_id="c", username="u", password="p")
    publish = encode_mqtt_publish(topic="chat", payload=b"abc")
    assert connect[0] >> 4 == 1
    assert publish[0] == 0x32
    assert mqtt_packet_type(bytes.fromhex("20020000")) == 2
    assert mqtt_connack_code(bytes.fromhex("20020000")) == 0
    assert mqtt_puback_packet_id(bytes.fromhex("40020001")) == 1


def test_text_protocol_contains_utf8_payload_and_protocol_type() -> None:
    payload = encode_text_protocol(
        from_uid=1,
        to_uid=2,
        friend_source=0,
        encrypt_uid="boss-id",
        text="测试消息",
    )
    assert payload[0] == 0x08 and payload[1] == 0x01
    assert "测试消息".encode() in payload


@pytest.mark.asyncio
async def test_probe_rejects_path_without_slash() -> None:
    result = await probe_ws_handshake(
        nodes=("ws.zhipin.com",), cookies={}, user_agent="ua", path="guess"
    )
    assert result.error_type == "invalid_ws_path"


@pytest.mark.asyncio
async def test_publish_waits_past_inbound_packet_for_puback() -> None:
    socket = MagicMock()
    socket.send = AsyncMock()
    socket.recv = AsyncMock(
        side_effect=[bytes.fromhex("3003000161"), bytes.fromhex("40020001")]
    )
    client = BossMqttWsClient(node="ws.zhipin.com", page_token="p", ws_password="w")
    client._socket = socket
    await client.publish_text(
        from_uid=1,
        to_uid=2,
        friend_source=0,
        encrypt_uid="boss",
        text="测试",
    )
    assert socket.recv.await_count == 2
    sent = socket.send.await_args.args[0]
    assert sent[0] == 0x32


@pytest.mark.asyncio
async def test_fetch_ws_nodes_redacts_url_parameters() -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "code": 0,
        "zpData": {"result": ["ws6.zhipin.com", "wss://ws.zhipin.com?token=secret"]},
    }
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.get.return_value = response
    with patch("httpx.AsyncClient", return_value=client):
        result = await fetch_ws_nodes(cookies={"bst": "secret"}, user_agent="ua")
    assert result.status == "ok"
    assert result.nodes == ("ws.zhipin.com", "ws6.zhipin.com")
    assert "secret" not in repr(result)
