from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobagent.applier.boss_ws import (
    BossConversationTarget,
    BossMqttWsClient,
    BossPublishAmbiguous,
    BossWsCredentials,
    _extract_nodes,
    encode_mqtt_connect,
    encode_mqtt_publish,
    encode_text_protocol,
    fetch_ws_nodes,
    find_resume_requests,
    mqtt_connack_code,
    mqtt_packet_type,
    mqtt_puback_packet_id,
    mqtt_publish_payload,
    probe_ws_handshake,
    send_text_to_conversation,
    send_text_to_target,
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


def test_resume_card_is_detected_from_chat_protocol() -> None:
    # TechwolfMessage: from.uid=7, to.uid=8, mid=99, body.type=9.
    from_user = bytes.fromhex("0807")
    to_user = bytes.fromhex("0808")
    body = bytes.fromhex("0809")
    message = (
        bytes.fromhex("0a02") + from_user
        + bytes.fromhex("1202") + to_user
        + bytes.fromhex("2063")
        + bytes.fromhex("3202") + body
    )
    protocol = bytes.fromhex("0801") + bytes.fromhex("1a") + bytes([len(message)]) + message
    requests = find_resume_requests(protocol)
    assert requests == [{
        "type": 0,
        "mid": 99,
        "from_uid": 7,
        "to_uid": 8,
        "body_type": 9,
        "text": "",
        "body_fields": {"1": [9]},
    }]


def test_mqtt_publish_payload_strips_topic_and_packet_id() -> None:
    packet = encode_mqtt_publish(topic="chat", payload=b"payload", packet_id=7)
    assert mqtt_publish_payload(packet) == b"payload"


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
async def test_publish_accepts_matching_outgoing_echo_before_puback() -> None:
    socket = MagicMock()
    socket.send = AsyncMock()
    echoed = encode_mqtt_publish(
        topic="chat",
        payload=encode_text_protocol(
            from_uid=1, to_uid=2, friend_source=0, encrypt_uid="boss", text="测试"
        ),
        packet_id=77,
    )
    socket.recv = AsyncMock(return_value=echoed)
    client = BossMqttWsClient(node="ws.zhipin.com", page_token="p", ws_password="w")
    client._socket = socket

    await client.publish_text(
        from_uid=1,
        to_uid=2,
        friend_source=0,
        encrypt_uid="boss",
        text="测试",
    )

    assert socket.recv.await_count == 1


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


def _target() -> BossConversationTarget:
    return BossConversationTarget(
        friend_id=7,
        friend_source=0,
        encrypt_boss_id="e",
        name="HR",
        company="c",
        job_title="t",
    )


def _client(publish_side_effect: BaseException | None = None) -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock()
    client.publish_text = AsyncMock(side_effect=publish_side_effect)
    client.close = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_ack_loss_after_publish_is_ambiguous_and_keeps_target() -> None:
    """Field finding 2026-09-22: PUBACK loss while the message still lands.

    A publish that already left the socket must surface as
    BossPublishAmbiguous carrying the resolved target, so callers verify by
    history instead of recording a definite failure or re-sending blind.
    """
    target = _target()
    credentials = BossWsCredentials(1, "p", "w", ("ws.zhipin.com",))
    client = _client(TimeoutError("ack window elapsed"))

    with (
        patch(
            "jobagent.applier.boss_ws.fetch_ws_credentials",
            new=AsyncMock(return_value=credentials),
        ),
        patch("jobagent.applier.boss_ws.BossMqttWsClient", return_value=client),
    ):
        with pytest.raises(BossPublishAmbiguous) as caught:
            await send_text_to_target(cookies={}, target=target, text="hi")

    assert caught.value.target == target
    assert isinstance(caught.value.original, TimeoutError)
    client.publish_text.assert_awaited_once()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_conversation_send_ambiguity_carries_resolved_target() -> None:
    """send_text_to_conversation resolves the target internally; on ack loss
    the caller would otherwise have nothing to verify with (the gap that
    forced the 2026-09-22 workspace-script workaround)."""
    target = _target()
    credentials = BossWsCredentials(1, "p", "w", ("ws.zhipin.com",))
    client = _client(ConnectionError("Boss WebSocket closed before PUBACK"))

    with (
        patch(
            "jobagent.applier.boss_ws.find_conversation_target",
            new=AsyncMock(return_value=target),
        ),
        patch(
            "jobagent.applier.boss_ws.fetch_ws_credentials",
            new=AsyncMock(return_value=credentials),
        ),
        patch("jobagent.applier.boss_ws.BossMqttWsClient", return_value=client),
    ):
        with pytest.raises(BossPublishAmbiguous) as caught:
            await send_text_to_conversation(
                cookies={}, company="c", job_title="t", text="hi"
            )

    assert caught.value.target == target
    assert isinstance(caught.value.original, ConnectionError)


@pytest.mark.asyncio
async def test_connect_failure_before_publish_is_not_ambiguous() -> None:
    """Boundary: nothing was sent yet, so the failure stays definite."""
    target = _target()
    credentials = BossWsCredentials(1, "p", "w", ("ws.zhipin.com",))
    client = MagicMock()
    client.connect = AsyncMock(side_effect=ConnectionError("CONNACK rejected"))
    client.publish_text = AsyncMock()
    client.close = AsyncMock()

    with (
        patch(
            "jobagent.applier.boss_ws.fetch_ws_credentials",
            new=AsyncMock(return_value=credentials),
        ),
        patch("jobagent.applier.boss_ws.BossMqttWsClient", return_value=client),
    ):
        with pytest.raises(ConnectionError) as caught:
            await send_text_to_target(cookies={}, target=target, text="hi")

    assert not isinstance(caught.value, BossPublishAmbiguous)
    client.publish_text.assert_not_awaited()
