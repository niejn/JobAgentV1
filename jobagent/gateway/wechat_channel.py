"""WeChat iLink Bot channel with registration-command handling.

Follows Hermes ``gateway/platforms/weixin.py`` production patterns:

- Persistent long-poll cursor (sync-buf.json survives restarts)
- Session-expiry (errcode -14 / ret=-2) → 10-minute pause
- Consecutive-failure backoff (2 s → 30 s after 3)
- Message dedup (message_id + content fingerprint)
- Sender allowlist = account ``owner_user_id`` (the QR-scanning user only)
- Context-token persistence for cross-restart reply continuity
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Protocol

import httpx

from jobagent.journey.job_registry import (
    JobProgressStatus,
    SQLiteJobRegistry,
)
from jobagent.wechat.ilink import (
    WECHAT_DIR,
    ContextTokenStore,
    InboundMessage,
    MessageDeduplicator,
    WeixinAccount,
    WeixinBotClient,
    WeixinSessionExpired,
)

# ---- Production backoff constants from Hermes --------------------------------

#: Delay (seconds) between retries when get_updates errors out.
BACKOFF_DELAY: float = 2.0
#: Delay after MAX_CONSECUTIVE_FAILURES, also used for session-expiry pause.
RETRY_DELAY: float = 30.0
#: How many consecutive failures before switching from BACKOFF_DELAY to RETRY_DELAY.
MAX_CONSECUTIVE_FAILURES: int = 3
#: How long (seconds) to pause on session expiry (errcode -14).
SESSION_EXPIRY_SLEEP_SECONDS: float = 600.0


# ---- Message handler protocol ------------------------------------------------


class MessageHandler(Protocol):
    """Protocol for processing one inbound WeChat message.

    Return a reply string, or ``None`` to leave the message unanswered.
    """

    async def handle(self, message: InboundMessage) -> str | None: ...


# ---- Registry command handler ------------------------------------------------


class RegistryCommandHandler:
    """Answers ``/status``, ``/progress``, ``/ping``, ``/help`` from the registry.

    Free-form text returns ``None`` (no reply from the gateway), prompting the
    user to switch to ``jobagent chat`` on their terminal.
    """

    def __init__(self, registry_path: Path) -> None:
        self._registry_path = registry_path

    async def handle(self, message: InboundMessage) -> str | None:
        text = message.text.strip()
        if text == "/help":
            return self._help()
        if text == "/ping":
            return "pong!"
        if text == "/status":
            return self._format_status()
        if text.startswith("/progress "):
            keyword = text[len("/progress "):].strip()
            return self._format_progress(keyword)
        if text.startswith("/"):
            # Unknown command: show help
            return (
                f"未知指令 '{text.split()[0]}'。\n"
                f"{self._help()}"
            )
        return None  # free-form text → user should use terminal chat

    def _help(self) -> str:
        return (
            "JobAgent Gateway 指令：\n"
            "/status   - 所有岗位进度概览（按状态统计 + 活跃岗位）\n"
            "/progress <岗位ID 或 公司名>  - 查看岗位完整事件时间线\n"
            "/ping     - 检查 Bot 是否在线\n\n"
            "其他自由文本消息请打开终端运行 jobagent chat 进行 AI 对话，当前网关不处理。"
        )

    def _format_status(self) -> str:
        with SQLiteJobRegistry(self._registry_path) as registry:
            records = registry.list_records(limit=500)
        if not records:
            return "登记册中暂无岗位记录。"
        counts: dict[str, int] = {}
        active: list[dict[str, str]] = []
        for r in records:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
            if r.status not in (
                JobProgressStatus.CLOSED,
                JobProgressStatus.OFFER,
                JobProgressStatus.REJECTED,
            ):
                if r.company and r.title:
                    active.append({
                        "status": r.status.value,
                        "company": r.company,
                        "title": r.title[:24],
                    })
        status_lines = "\n".join(
            f"{s}: {c}" for s, c in sorted(counts.items())
        )
        active_lines = (
            "\n".join(
                f"  {i + 1}. [{a['status']}] {a['company']} - {a['title']}"
                for i, a in enumerate(active[:10])
            )
            if active
            else "  (无)"
        )
        return (
            f"===== 岗位进度概览 =====\n"
            f"总跟踪: {len(records)} 个\n\n"
            f"按状态统计:\n{status_lines}\n\n"
            f"活跃岗位:\n{active_lines}"
        )

    def _format_progress(self, keyword: str) -> str:
        with SQLiteJobRegistry(self._registry_path) as registry:
            record = registry.get(keyword)
            if record is None:
                hits = list(registry.list_records(company=keyword, limit=5))
                if not hits:
                    all_records = registry.list_records(limit=500)
                    hits = [r for r in all_records if r.job_id.startswith(keyword)]
                if not hits:
                    return f"未找到匹配 '{keyword}' 的岗位。用 /status 看全部。"
                if len(hits) > 1:
                    return (
                        f"找到 {len(hits)} 个匹配：\n"
                        + "\n".join(
                            f"  {r.job_id} - {r.company[:16]} {r.title[:20]}"
                            for r in hits[:8]
                        )
                        + "\n请用完整 job_id 查询。"
                    )
                record = hits[0]
            events = registry.history(record.job_id)
        event_lines = (
            "\n".join(
                f"  {e.created_at.strftime('%m-%d %H:%M')} → {e.status.value}: "
                f"{e.note or '-'}"
                for e in events
            )
            if events
            else "  (无)"
        )
        return (
            f"===== {record.company[:16]} - {record.title[:16]} =====\n"
            f"岗位ID: {record.job_id}\n"
            f"位置: {record.location or '-'}\n"
            f"网址: {record.url or '-'}\n"
            f"当前状态: {record.status.value}\n"
            f"备注: {record.note or '-'}\n"
            f"首次发现: {record.first_seen_at.strftime('%m-%d %H:%M')}\n\n"
            f"事件时间线:\n{event_lines}"
        )


def build_registry_command_handler(registry_path: Path) -> RegistryCommandHandler:
    """Factory that builds a production registry handler for the gateway."""
    return RegistryCommandHandler(registry_path)


# ---- Sync-buf persistence (cursor survives restarts) -------------------------

_SYNC_BUF_FILE = "sync-buf.json"


def _load_cursor(state_dir: Path) -> str:
    path = state_dir / _SYNC_BUF_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("cursor", ""))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return ""


def _save_cursor(state_dir: Path, cursor: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / _SYNC_BUF_FILE
    path.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")


# ---- WeChat channel ----------------------------------------------------------


class WeChatChannel:
    """Long-lived WeChat iLink bot channel for the ``jobagent watch`` gateway.

    Runs a single long-poll session; reaps itself on
    :exc:`~asyncio.CancelledError` / :exc:`KeyboardInterrupt`.
    """

    def __init__(  # noqa: PLR0913 - constructor concentration
        self,
        *,
        account: WeixinAccount,
        handler: MessageHandler | None = None,
        state_dir: Path = WECHAT_DIR,
        client: WeixinBotClient | None = None,
        allow_user_ids: frozenset[str] | None = None,
        dedup: MessageDeduplicator | None = None,
        _max_iterations: int = 0,  # test-only: exit after N iterations
    ) -> None:
        self._account = account
        self._handler = handler
        self._state_dir = state_dir
        self._client = client
        self._dedup = dedup or MessageDeduplicator()
        self._context_token_store = ContextTokenStore()
        self._owns_client = client is None
        self._max_iterations = _max_iterations

        # Default allowlist: only the account owner (QR-scanning user).
        if allow_user_ids is not None:
            self._allow_user_ids: frozenset[str] = allow_user_ids
        elif account.owner_user_id:
            self._allow_user_ids = frozenset({account.owner_user_id})
        else:
            raise RuntimeError(
                "WeChatChannel: no allowed senders configured; "
                "set owner_user_id in the account"
            )

    async def run(self) -> None:  # noqa: C901, PLR0912 - poll loop complexity
        """Enter the long-poll loop. Runs until interrupted."""
        client = self._client or WeixinBotClient(account=self._account)
        self._owns_client = self._client is None
        try:
            store = self._context_token_store
            dedup = self._dedup
            cursor = _load_cursor(self._state_dir)
            consecutive_failures = 0
            iteration = 0

            while True:
                iteration += 1
                if self._max_iterations and iteration > self._max_iterations:
                    break
                try:
                    messages, new_cursor = await client.get_updates(cursor)
                except WeixinSessionExpired:
                    consecutive_failures = 0
                    await asyncio.sleep(SESSION_EXPIRY_SLEEP_SECONDS)
                    continue
                except httpx.ReadTimeout:
                    # Normal for long-poll candidate: server didn't respond
                    # within 40 s. Retry immediately.
                    continue
                except BaseException as exc:
                    # KeyboardInterrupt, CancelledError → re-raise
                    if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                        raise
                    consecutive_failures += 1
                    await self._backoff(consecutive_failures)
                    continue

                consecutive_failures = 0
                if new_cursor:
                    cursor = new_cursor
                    _save_cursor(self._state_dir, cursor)

                for message in messages:
                    await self._handle_one(client, store, dedup, message)

                # Yield control so CancelledError can be delivered
                # (MockTransport in tests returns synchronously)
                await asyncio.sleep(0)
        finally:
            if self._owns_client:
                await client.aclose()
            # - dedup     → ephemeral (recreated on next run, drops old keys)
            # - cursor    → persisted on disk, reloaded on next run
            # - context   → persisted on disk by ContextTokenStore

    async def aclose(self) -> None:
        """Release resources. Safe to call even if :meth:`run` wasn't called."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _backoff(self, consecutive_failures: int) -> None:  # noqa: PLR6301
        """Apply Hermes-style exponential backoff, re-raising on fatal errors."""
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            await asyncio.sleep(RETRY_DELAY)
        else:
            await asyncio.sleep(BACKOFF_DELAY)

    async def _handle_one(  # noqa: PLR6301
        self,
        client: WeixinBotClient,
        store: ContextTokenStore,
        dedup: MessageDeduplicator,
        message: InboundMessage,
    ) -> None:
        """Process one inbound message: allowlist → dedup → reply."""
        if self._allow_user_ids and message.from_user_id not in self._allow_user_ids:
            return
        if message.message_id and dedup.seen(message.message_id):
            return

        # Content-fingerprint dedup (Hermes pattern)
        if message.text:
            key = (
                f"content:{message.from_user_id}:"
                f"{hashlib.md5(message.text.encode()).hexdigest()}"
            )
            if dedup.seen(key):
                return

        if message.context_token:
            store.set(message.from_user_id, message.context_token)
        if not message.context_token:
            return  # cannot reply without a context_token

        if self._handler is None:
            return

        reply = await self._handler.handle(message)
        if reply:
            try:
                await client.send_text(
                    message.from_user_id,
                    reply[:2048],
                    message.context_token,
                )
            except Exception:
                pass  # best-effort; non-critical

    @property
    def handler(self) -> MessageHandler | None:
        return self._handler

    @property
    def account(self) -> WeixinAccount:
        return self._account