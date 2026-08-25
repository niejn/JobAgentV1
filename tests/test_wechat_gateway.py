"""Tests for the WeChat gateway channel (poll loop, command handler, sync-buf)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from jobagent.gateway.wechat_channel import (
    RegistryCommandHandler,
    WeChatChannel,
    _load_cursor,
    _save_cursor,
)
from jobagent.journey.job_registry import (
    JobProgressStatus,
    SQLiteJobRegistry,
)
from jobagent.wechat.ilink import (
    InboundMessage,
    MessageDeduplicator,
    WeixinAccount,
    WeixinBotClient,
)

# ---- sync-buf persistence ----------------------------------------------------


class TestSyncBuf:
    def test_roundtrip(self, tmp_path: Path) -> None:
        assert _load_cursor(tmp_path) == ""
        _save_cursor(tmp_path, "cursor-abc")
        assert _load_cursor(tmp_path) == "cursor-abc"

    def test_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "sync-buf.json").write_text("{{{", encoding="utf-8")
        assert _load_cursor(tmp_path) == ""


# ---- registry command handler ------------------------------------------------


class TestRegistryCommandHandler:
    def test_ping(self, registry_path: Path) -> None:
        h = RegistryCommandHandler(registry_path)
        reply = _sync(h.handle(_msg("/ping")))
        assert reply == "pong!"

    def test_help_contains_commands(self, registry_path: Path) -> None:
        h = RegistryCommandHandler(registry_path)
        reply = _sync(h.handle(_msg("/help")))
        assert reply is not None
        assert "/status" in reply
        assert "/progress" in reply
        assert "/ping" in reply

    def test_unknown_command_shows_help(self, registry_path: Path) -> None:
        h = RegistryCommandHandler(registry_path)
        reply = _sync(h.handle(_msg("/xyz")))
        assert reply is not None
        assert "未知指令" in reply

    def test_free_text_returns_none(self, registry_path: Path) -> None:
        h = RegistryCommandHandler(registry_path)
        assert _sync(h.handle(_msg("你好，什么情况"))) is None

    def test_empty_registry_status(self, registry_path: Path) -> None:
        h = RegistryCommandHandler(registry_path)
        reply = _sync(h.handle(_msg("/status")))
        assert reply is not None
        assert "暂无岗位记录" in reply

    def test_status_with_data(self, registry_path: Path) -> None:
        _seed_registry(registry_path, n=3)
        h = RegistryCommandHandler(registry_path)
        reply = _sync(h.handle(_msg("/status")))
        assert reply is not None
        assert "3" in reply  # total count
        assert "discovered" in reply

    def test_progress_by_job_id(self, registry_path: Path) -> None:
        _seed_registry(registry_path, n=1)
        h = RegistryCommandHandler(registry_path)
        reply = _sync(h.handle(_msg("/progress boss:job-1")))
        assert reply is not None
        assert "boss:job-1" in reply
        assert "discovered" in reply

    def test_progress_by_company(self, registry_path: Path) -> None:
        _seed_registry(registry_path, n=2)
        h = RegistryCommandHandler(registry_path)
        # job-1 has company "Company 1"
        reply = _sync(h.handle(_msg("/progress Company 1")))
        assert reply is not None
        assert "Company 1" in reply

    def test_progress_not_found(self, registry_path: Path) -> None:
        h = RegistryCommandHandler(registry_path)
        reply = _sync(h.handle(_msg("/progress nonexistent")))
        assert reply is not None
        assert "未找到" in reply

    def test_progress_multiple_results(self, registry_path: Path) -> None:
        _seed_registry(registry_path, n=5)
        h = RegistryCommandHandler(registry_path)
        # "Company" matches all 5
        reply = _sync(h.handle(_msg("/progress Company")))
        assert reply is not None
        assert "5 个匹配" in reply

    def test_event_history(self, registry_path: Path) -> None:
        _seed_registry(registry_path, n=1)
        # Mark one transition
        with SQLiteJobRegistry(registry_path) as r:
            r.mark("boss:job-1", JobProgressStatus.RECOMMENDED, note="推荐测试")
        h = RegistryCommandHandler(registry_path)
        reply = _sync(h.handle(_msg("/progress boss:job-1")))
        assert reply is not None
        assert "recognized" in reply or "recommended" in reply
        assert "推荐测试" in reply


# ---- WeChat channel poll loop ------------------------------------------------


class TestWeChatChannel:
    def test_requires_allowlist(self) -> None:
        account = WeixinAccount(bot_token="tok")
        with pytest.raises(RuntimeError, match="no allowed senders"):
            WeChatChannel(account=account)

    def test_allow_defaults_to_owner(self) -> None:
        account = WeixinAccount(bot_token="tok", owner_user_id="u-1")
        ch = WeChatChannel(account=account)
        assert ch._allow_user_ids == frozenset({"u-1"})

    def test_allow_explicit_override(self) -> None:
        account = WeixinAccount(bot_token="tok", owner_user_id="u-1")
        ch = WeChatChannel(account=account, allow_user_ids=frozenset({"u-2"}))
        assert ch._allow_user_ids == frozenset({"u-2"})

    @pytest.mark.asyncio
    async def test_poll_loop_exits_on_cancelled(self) -> None:
        """Poll loop handles CancelledError gracefully."""
        account = WeixinAccount(bot_token="tok", owner_user_id="u-1")

        async def handler(_m: InboundMessage) -> str | None:
            return None

        def _transport(request: httpx.Request) -> httpx.Response:
            return _updates_response("c-1", [])

        ch = WeChatChannel(
            account=account,
            handler=handler,
            client=WeixinBotClient(
                account=account, transport=httpx.MockTransport(_transport)
            ),
        )

        task = asyncio.create_task(ch.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_unknown_sender_ignored(self) -> None:
        """Messages from non-owner users are silently dropped."""
        account = WeixinAccount(bot_token="tok", owner_user_id="owner-1")
        handler = _capture_handler()
        dedup = MessageDeduplicator()
        transport = _mock_client(
            [
                _updates_response(
                    "cursor-1",
                    [_msg_payload("stranger-1", "hello", "ctx-1", "m-1")],
                ),
                _updates_response("cursor-2", []),
            ]
        )
        client = WeixinBotClient(account=account, transport=transport)
        ch = WeChatChannel(
            account=account,
            handler=handler,
            client=client,
            dedup=dedup,
            allow_user_ids=frozenset({"owner-1"}),
        )
        await _run_with_timeout(ch)
        assert handler.caught is None

    @pytest.mark.asyncio
    async def test_message_triggers_reply(self) -> None:
        """Test _handle_one directly: message → handler → send_text."""
        account = WeixinAccount(bot_token="tok", owner_user_id="owner-1")
        handler = _capture_handler(return_text="pong!")
        dedup = MessageDeduplicator()
        sent: list[str] = []

        def _transport(request: httpx.Request) -> httpx.Response:
            nonlocal sent
            if "sendmessage" in str(request.url):
                sent.append("sent")
                return httpx.Response(200, json={})
            return httpx.Response(200, json={})

        client = WeixinBotClient(
            account=account, transport=httpx.MockTransport(_transport)
        )
        ch = WeChatChannel(
            account=account,
            handler=handler,
            client=client,
            dedup=dedup,
            allow_user_ids=frozenset({"owner-1"}),
        )
        msg = InboundMessage(
            from_user_id="owner-1",
            text="/ping",
            context_token="ctx-1",
            message_id="m-1",
        )
        await ch._handle_one(
            client=client,
            store=ch._context_token_store,
            dedup=dedup,
            message=msg,
        )
        assert handler.caught is not None
        assert handler.caught.text == "/ping"
        assert len(sent) >= 1, "send_text was not called"

    @pytest.mark.asyncio
    async def test_no_context_token_no_reply(self) -> None:
        account = WeixinAccount(bot_token="tok", owner_user_id="owner-1")
        handler = _capture_handler(return_text="should-not-send")
        dedup = MessageDeduplicator()
        sent: list[str] = []

        def _transport(request: httpx.Request) -> httpx.Response:
            nonlocal sent
            if "getupdates" in str(request.url):
                return _updates_response(
                    "cursor-1",
                    [_msg_payload("owner-1", "hi", "", "m-1")],  # no context_token
                )
            if "sendmessage" in str(request.url):
                sent.append("sent")
                return httpx.Response(200, json={})
            return httpx.Response(200, json={})

        ch = WeChatChannel(
            account=account,
            handler=handler,
            client=WeixinBotClient(
                account=account, transport=httpx.MockTransport(_transport)
            ),
            dedup=dedup,
            allow_user_ids=frozenset({"owner-1"}),
        )
        await _run_with_timeout(ch)
        assert handler.caught is None

    @pytest.mark.asyncio
    async def test_session_expiry_pauses(self) -> None:
        """Session expiry sleeps for SESSION_EXPIRY_SLEEP_SECONDS then retries."""
        account = WeixinAccount(bot_token="tok", owner_user_id="owner-1")
        handler = _capture_handler(return_text="pong")
        dedup = MessageDeduplicator()
        events: list[str] = []

        def _transport(request: httpx.Request) -> httpx.Response:
            if "getupdates" in str(request.url):
                nonlocal events
                if not events:
                    events.append("expired")
                    return httpx.Response(
                        200,
                        json={"ret": -1, "errcode": -14, "errmsg": "expired"},
                    )
                events.append("ok")
                return _updates_response("cursor-1", [])
            if "sendmessage" in str(request.url):
                return httpx.Response(200, json={})
            return httpx.Response(200, json={})

        ch = WeChatChannel(
            account=account,
            handler=handler,
            client=WeixinBotClient(
                account=account, transport=httpx.MockTransport(_transport)
            ),
            dedup=dedup,
            allow_user_ids=frozenset({"owner-1"}),
            _max_iterations=2,
        )
        import jobagent.gateway.wechat_channel as mod

        old = mod.SESSION_EXPIRY_SLEEP_SECONDS
        mod.SESSION_EXPIRY_SLEEP_SECONDS = 0.01
        try:
            await ch.run()
        finally:
            mod.SESSION_EXPIRY_SLEEP_SECONDS = old
        assert len(events) == 2
        assert events[0] == "expired"
        assert events[1] == "ok"

    @pytest.mark.asyncio
    async def test_error_backoff_then_succeeds(self) -> None:
        """Consecutive errors trigger backoff, then recovery."""
        account = WeixinAccount(bot_token="tok", owner_user_id="owner-1")
        handler = _capture_handler(return_text="pong")
        dedup = MessageDeduplicator()
        attempts: list[int] = []

        def _transport(request: httpx.Request) -> httpx.Response:
            if "getupdates" in str(request.url):
                nonlocal attempts
                attempts.append(len(attempts))
                if len(attempts) <= 2:
                    raise httpx.ReadTimeout("timeout", request=request)
                return _updates_response(
                    "cursor-1",
                    [_msg_payload("owner-1", "/ping", "ctx-1", "m-1")],
                )
            if "sendmessage" in str(request.url):
                return httpx.Response(200, json={})
            return httpx.Response(200, json={})

        ch = WeChatChannel(
            account=account,
            handler=handler,
            client=WeixinBotClient(
                account=account, transport=httpx.MockTransport(_transport)
            ),
            dedup=dedup,
            allow_user_ids=frozenset({"owner-1"}),
            _max_iterations=3,
        )
        # Hijack backoff delays for test speed
        import jobagent.gateway.wechat_channel as mod

        old_backoff = mod.BACKOFF_DELAY
        old_retry = mod.RETRY_DELAY
        mod.BACKOFF_DELAY = 0.01
        mod.RETRY_DELAY = 0.02
        try:
            await ch.run()
        finally:
            mod.BACKOFF_DELAY = old_backoff
            mod.RETRY_DELAY = old_retry
        assert len(attempts) == 3
        assert handler.caught is not None

    @pytest.mark.asyncio
    async def test_no_downstream_write_when_client_errored(self) -> None:
        """Send failure doesn't crash the loop."""
        account = WeixinAccount(bot_token="tok", owner_user_id="owner-1")
        handler = _capture_handler(return_text="reply")
        dedup = MessageDeduplicator()
        send_attempts: list[int] = []

        def _transport(request: httpx.Request) -> httpx.Response:
            if "getupdates" in str(request.url):
                return _updates_response(
                    "cursor-1",
                    [_msg_payload("owner-1", "hi", "ctx-1", "m-1")],
                )
            if "sendmessage" in str(request.url):
                nonlocal send_attempts
                send_attempts.append(len(send_attempts))
                raise httpx.ReadTimeout("send timeout", request=request)
            return httpx.Response(200, json={})

        ch = WeChatChannel(
            account=account,
            handler=handler,
            client=WeixinBotClient(
                account=account, transport=httpx.MockTransport(_transport)
            ),
            dedup=dedup,
            allow_user_ids=frozenset({"owner-1"}),
            _max_iterations=2,
        )
        await ch.run()
        assert len(send_attempts) == 1
        assert handler.caught is not None


# ---- helpers -----------------------------------------------------------------


def _msg(text: str, from_user: str = "test-user", ctx: str = "ctx-1") -> InboundMessage:
    return InboundMessage(
        from_user_id=from_user,
        text=text,
        context_token=ctx,
        message_id=f"m-{id(text)}",
    )


def _msg_payload(
    from_user: str, text: str, ctx: str, msg_id: str
) -> dict:
    return {
        "from_user_id": from_user,
        "message_type": 1,
        "context_token": ctx,
        "message_id": msg_id,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


def _updates_response(cursor: str, msgs: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ret": 0,
            "get_updates_buf": cursor,
            "msgs": msgs,
        },
    )


def _mock_client(responses: list[httpx.Response]) -> httpx.MockTransport:
    index = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        i = index[0]
        index[0] = i + 1
        if i < len(responses):
            return responses[i]
        return _updates_response("", [])

    return httpx.MockTransport(handler)


def _sync(coro: object) -> str | None:
    """Run a coroutine synchronously for tests that don't need async fixtures."""
    import asyncio

    assert hasattr(coro, "__await__")
    return asyncio.new_event_loop().run_until_complete(coro)  # type: ignore[arg-type]


async def _run_with_timeout(ch: WeChatChannel, *, timeout: float = 5.0) -> None:
    """Run a channel loop briefly, then cancel it.

    The channel's client must be injected (not created internally).
    """
    ch._owns_client = False  # test manages client lifecycle

    async def _run() -> None:
        try:
            await ch.run()
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _seed_registry(registry_path: Path, n: int) -> None:
    """Insert n jobs into the registry."""
    with SQLiteJobRegistry(registry_path) as r:
        for i in range(1, n + 1):
            r.upsert_discovered(
                job_id=f"boss:job-{i}",
                source="boss",
                company=f"Company {i}",
                title=f"Engineer {i}",
            )


class _capture_handler:
    """Handler that records the last message and returns a fixed reply."""

    def __init__(self, return_text: str | None = None) -> None:
        self.caught: InboundMessage | None = None
        self._return_text = return_text

    async def handle(self, message: InboundMessage) -> str | None:
        self.caught = message
        return self._return_text


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    p = tmp_path / "test_registry.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p