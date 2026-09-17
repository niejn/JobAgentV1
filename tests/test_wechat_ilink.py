"""Tests for the WeChat iLink Bot client (ported from Hermes, MIT)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jobagent.wechat.ilink import (
    ContextTokenStore,
    ILinkApiError,
    MessageDeduplicator,
    WeixinAccount,
    WeixinAccountStore,
    WeixinBotClient,
    WeixinSessionExpired,
)


def _client(
    handler, *, account: WeixinAccount | None = None
) -> WeixinBotClient:
    return WeixinBotClient(
        account=account,
        base_url="https://ilink.test",
        transport=httpx.MockTransport(handler),
    )


class TestQRLogin:
    @pytest.mark.asyncio
    async def test_request_qr_code_parses_challenge(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["method"] = request.method
            return httpx.Response(
                200,
                json={"qrcode": "qr-key", "qrcode_img_content": "https://qr"},
            )

        async with _client(handler) as client:
            qr = await client.request_qr_code()

        assert seen["method"] == "GET"
        assert "ilink/bot/get_bot_qrcode" in seen["url"]
        assert "bot_type=3" in seen["url"]
        assert qr.key == "qr-key"
        assert qr.img_content == "https://qr"

    @pytest.mark.asyncio
    async def test_request_qr_code_requires_fields(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unrelated": 1})

        async with _client(handler) as client:
            with pytest.raises(ILinkApiError):
                await client.request_qr_code()

    @pytest.mark.asyncio
    async def test_poll_status_states(self) -> None:
        payloads = [
            {"status": "waiting"},
            {"status": "scanned"},
            {"status": "expired"},
        ]
        index = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal index
            assert "ilink/bot/get_qrcode_status" in str(request.url)
            assert "qrcode=qr-key" in str(request.url)
            payload = payloads[index]
            index += 1
            return httpx.Response(200, json=payload)

        async with _client(handler) as client:
            assert (await client.poll_qr_status("qr-key")).state.value == "waiting"
            assert (await client.poll_qr_status("qr-key")).state.value == "scanned"
            assert (await client.poll_qr_status("qr-key")).state.value == "expired"

    @pytest.mark.asyncio
    async def test_poll_status_confirmed_returns_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "bot_token": "tok-1",
                    "baseurl": "https://ilink.other",
                    "bot_id": "abc@im.bot",
                },
            )

        async with _client(handler) as client:
            status = await client.poll_qr_status("qr-key")

        assert status.state.value == "confirmed"
        assert status.bot_token == "tok-1"
        assert status.base_url == "https://ilink.other"
        assert status.bot_id == "abc@im.bot"

    @pytest.mark.asyncio
    async def test_poll_status_timeout_reads_as_waiting(self) -> None:
        """A long-poll hold past the read timeout must not kill the login flow.

        The real endpoint holds each poll ~30 s until the state changes
        (measured), so client timeouts during the scan/confirm wait are
        routine and must surface as WAITING, not raise.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated long-poll hold")

        async with _client(handler) as client:
            status = await client.poll_qr_status("qr-key")

        assert status.state.value == "waiting"


class TestGetUpdates:
    @pytest.mark.asyncio
    async def test_parses_messages_and_cursor(self) -> None:
        account = WeixinAccount(bot_token="tok", base_url="https://ilink.test")
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer tok"
            assert request.headers["AuthorizationType"] == "ilink_bot_token"
            assert "X-WECHAT-UIN" in request.headers
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "ret": 0,
                    "get_updates_buf": "cursor-2",
                    "msgs": [
                        {
                            "message_type": 1,
                            "from_user_id": "user-1",
                            "context_token": "ctx-1",
                            "message_id": "m-1",
                            "item_list": [
                                {"type": 1, "text_item": {"text": "你好"}},
                                {"type": 2, "image_item": {}},  # ignored
                            ],
                        },
                        {  # bot's own message - skipped
                            "message_type": 2,
                            "from_user_id": "user-1",
                            "item_list": [{"type": 1, "text_item": {"text": "x"}}],
                        },
                        {  # from another bot identity - skipped
                            "message_type": 1,
                            "from_user_id": "peer@im.bot",
                            "item_list": [{"type": 1, "text_item": {"text": "y"}}],
                        },
                    ],
                },
            )

        async with _client(handler, account=account) as client:
            messages, cursor = await client.get_updates("cursor-1")

        assert bodies[0]["get_updates_buf"] == "cursor-1"
        assert bodies[0]["base_info"] == {"channel_version": "2.2.0"}
        assert cursor == "cursor-2"
        assert len(messages) == 1
        assert messages[0].from_user_id == "user-1"
        assert messages[0].text == "你好"
        assert messages[0].context_token == "ctx-1"
        assert messages[0].message_id == "m-1"

    @pytest.mark.asyncio
    async def test_session_expired_raises(self) -> None:
        account = WeixinAccount(bot_token="tok", base_url="https://ilink.test")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"ret": -1, "errcode": -14, "errmsg": "expired"}
            )

        async with _client(handler, account=account) as client:
            with pytest.raises(WeixinSessionExpired):
                await client.get_updates("")

    @pytest.mark.asyncio
    async def test_error_payload_raises_typed_error(self) -> None:
        account = WeixinAccount(bot_token="tok", base_url="https://ilink.test")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ret": -1, "errcode": -3})

        async with _client(handler, account=account) as client:
            with pytest.raises(ILinkApiError) as exc_info:
                await client.get_updates("")
        assert exc_info.value.errcode == -3

    @pytest.mark.asyncio
    async def test_requires_account(self) -> None:
        async with _client(lambda request: httpx.Response(200, json={})) as client:
            with pytest.raises(RuntimeError):
                await client.get_updates("")


class TestSendText:
    @pytest.mark.asyncio
    async def test_builds_correct_body(self) -> None:
        account = WeixinAccount(bot_token="tok", base_url="https://ilink.test")
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer tok"
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={})

        async with _client(handler, account=account) as client:
            await client.send_text("user-1", "你好，我是候选人", "ctx-9")

        msg = bodies[0]["msg"]
        assert msg["to_user_id"] == "user-1"
        assert msg["from_user_id"] == ""
        assert msg["message_type"] == 2
        assert msg["message_state"] == 2
        assert msg["context_token"] == "ctx-9"
        assert msg["item_list"] == [
            {"type": 1, "text_item": {"text": "你好，我是候选人"}}
        ]
        assert bodies[0]["base_info"] == {"channel_version": "2.2.0"}
        assert msg["client_id"].startswith("jobagent-")

    @pytest.mark.asyncio
    async def test_empty_text_rejected(self) -> None:
        account = WeixinAccount(bot_token="tok", base_url="https://ilink.test")
        async with _client(
            lambda request: httpx.Response(200, json={}), account=account
        ) as client:
            with pytest.raises(ValueError):
                await client.send_text("user-1", "  ", "ctx")

    @pytest.mark.asyncio
    async def test_missing_context_token_rejected(self) -> None:
        account = WeixinAccount(bot_token="tok", base_url="https://ilink.test")
        async with _client(
            lambda request: httpx.Response(200, json={}), account=account
        ) as client:
            with pytest.raises(ValueError, match="cold-start"):
                await client.send_text("user-1", "hello", "")


class TestStores:
    def test_account_store_roundtrip(self, tmp_path: Path) -> None:
        store = WeixinAccountStore(tmp_path / "account.json")
        assert store.load() is None
        account = WeixinAccount(
            bot_token="tok", base_url="https://x", bot_id="b@im.bot", login_at="t"
        )
        store.save(account)
        assert store.load() == account
        assert store.path == tmp_path / "account.json"

    def test_account_store_tolerates_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "account.json"
        path.write_text("{invalid", encoding="utf-8")
        assert WeixinAccountStore(path).load() is None

    def test_context_token_store_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = ContextTokenStore(path)
        assert store.get("user-1") is None
        store.set("user-1", "ctx-1")
        assert store.get("user-1") == "ctx-1"
        # A new instance restores from disk (reply continuity).
        assert ContextTokenStore(path).get("user-1") == "ctx-1"

    def test_context_token_store_ignores_empty(self, tmp_path: Path) -> None:
        store = ContextTokenStore(tmp_path / "tokens.json")
        store.set("user-1", "")
        assert store.get("user-1") is None


class TestDedup:
    def test_seen_once(self) -> None:
        dedup = MessageDeduplicator(ttl_seconds=60)
        assert dedup.seen("m-1") is False
        assert dedup.seen("m-1") is True
        assert dedup.seen("m-2") is False

    def test_ttl_expiry(self) -> None:
        clock = {"now": 0.0}
        dedup = MessageDeduplicator(ttl_seconds=10)
        dedup._now = lambda: clock["now"]  # type: ignore[method-assign]
        assert dedup.seen("m-1") is False
        clock["now"] = 11.0
        assert dedup.seen("m-1") is False
