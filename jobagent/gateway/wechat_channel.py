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
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx

from jobagent.journey.job_registry import (
    JobProgressStatus,
    SQLiteJobRegistry,
)
from jobagent.wechat.ilink import (
    CONTEXT_TOKENS_FILE,
    WECHAT_DIR,
    ContextTokenStore,
    ILinkApiError,
    InboundMessage,
    MessageDeduplicator,
    WeixinAccount,
    WeixinBotClient,
    WeixinSessionExpired,
)

if TYPE_CHECKING:
    from jobagent.boss_reply_service import BossReplyApplicationService

logger = logging.getLogger(__name__)

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


class NotificationSource(Protocol):
    def sync_pending_notifications(self) -> int: ...

    def pending_notifications(self, channel: str, limit: int = 5) -> Sequence[object]: ...

    def mark_notification(
        self, event_id: str, *, delivered: bool, error: str = ""
    ) -> None: ...


# ---- Registry command handler ------------------------------------------------
class RegistryCommandHandler:
    """Answers ``/status``, ``/progress``, ``/ping``, ``/help`` from the registry.

    Free-form text gets a short pointer reply (to ``/help`` and the terminal
    ``jobagent chat``) — silence reads as "bot dead".
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
        return (
            "当前网关只处理指令，发送 /help 查看全部指令；"
            "自由文本对话请打开终端运行 jobagent chat。"
        )

    def _help(self) -> str:
        return (
            "JobAgent Gateway 指令：\n"
            "/status   - 所有岗位进度概览（按状态统计 + 活跃岗位）\n"
            "/progress <岗位ID 或 公司名>  - 查看岗位完整事件时间线\n"
            "/boss     - 查看 HR 新消息和待审批回复\n"
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
        for record in records:
            counts[record.status.value] = counts.get(record.status.value, 0) + 1
            if record.status not in (
                JobProgressStatus.CLOSED,
                JobProgressStatus.OFFER,
                JobProgressStatus.REJECTED,
            ) and record.company and record.title:
                active.append(
                    {
                        "status": record.status.value,
                        "company": record.company,
                        "title": record.title[:24],
                    }
                )
        status_lines = "\n".join(f"{key}: {value}" for key, value in sorted(counts.items()))
        active_lines = (
            "\n".join(
                f"  {index + 1}. [{item['status']}] {item['company']} - {item['title']}"
                for index, item in enumerate(active[:10])
            )
            if active
            else "  (无)"
        )
        return (
            f"===== 岗位进度概览 =====\n总跟踪: {len(records)} 个\n\n"
            f"按状态统计:\n{status_lines}\n\n活跃岗位:\n{active_lines}"
        )

    def _format_progress(self, keyword: str) -> str:
        with SQLiteJobRegistry(self._registry_path) as registry:
            record = registry.get(keyword)
            if record is None:
                hits = list(registry.list_records(company=keyword, limit=5))
                if not hits:
                    hits = [
                        item
                        for item in registry.list_records(limit=500)
                        if item.job_id.startswith(keyword)
                    ]
                if not hits:
                    return f"未找到匹配 '{keyword}' 的岗位。用 /status 看全部。"
                if len(hits) > 1:
                    return (
                        f"找到 {len(hits)} 个匹配：\n"
                        + "\n".join(
                            f"  {item.job_id} - {item.company[:16]} {item.title[:20]}"
                            for item in hits[:8]
                        )
                        + "\n请用完整 job_id 查询。"
                    )
                record = hits[0]
            events = registry.history(record.job_id)
        event_lines = (
            "\n".join(
                f"  {event.created_at.strftime('%m-%d %H:%M')} → "
                f"{event.status.value}: {event.note or '-'}"
                for event in events
            )
            if events
            else "  (无)"
        )
        return (
            f"===== {record.company[:16]} - {record.title[:16]} =====\n"
            f"岗位ID: {record.job_id}\n位置: {record.location or '-'}\n"
            f"网址: {record.url or '-'}\n当前状态: {record.status.value}\n"
            f"备注: {record.note or '-'}\n"
            f"首次发现: {record.first_seen_at.strftime('%m-%d %H:%M')}\n\n"
            f"事件时间线:\n{event_lines}"
        )


class BossReplyCommandHandler:
    """Deterministic WeChat commands over the shared Boss reply module."""

    def __init__(self, service: BossReplyApplicationService) -> None:
        self._service = service

    async def handle(self, message: InboundMessage) -> str | None:
        text = message.text.strip()
        if not (text == "/boss" or text.startswith("/boss ")):
            return None
        parts = text.split(maxsplit=4)
        action = parts[1].lower() if len(parts) > 1 else "list"
        if action == "inbox":
            messages = self._service.list_inbox(5)
            if not messages:
                if not self._service.monitor_status()["initialized"]:
                    return (
                        "Boss 暂无未处理的新 HR 消息。\n"
                        "注意：Boss 监控未启动或未完成基线扫描，"
                        "请先运行 jobagent watch。"
                    )
                return "Boss 暂无未处理的新 HR 消息。"
            return "Boss 新 HR 消息：\n" + "\n".join(
                f"{row['company']} / {row['hr_name']}：{row['text']}"
                for row in messages
            )
        if action in {"list", "next"}:
            pending = self._service.list_pending(5)
            if not pending:
                return "Boss 暂无待人工确认的回复。"
            if action == "next":
                return self._render_one(pending[0])
            return "Boss 待确认回复：\n" + "\n".join(
                f"{row['reply_id'][:12]} V{row['draft_version']} "
                f"{row['company']} / {row['hr_name']}"
                for row in pending
            ) + "\n发送 /boss next 查看下一条。"
        if action not in {"approve", "skip", "edit"}:
            return self._help()
        if len(parts) < 4:
            return self._help()
        reply_ref = parts[2]
        version = self._parse_version(parts[3])
        if version is None:
            return "草稿版本格式错误，请使用 V2 这样的格式。"
        draft_text = parts[4].strip() if action == "edit" and len(parts) > 4 else None
        if action == "edit" and not draft_text:
            return "编辑后发送需要提供新的回复正文。"
        result = self._service.decide(
            reply_ref,
            decision="approve" if action in {"approve", "edit"} else "skip",
            draft_version=version,
            draft_text=draft_text,
        )
        if result["status"] == "updated":
            return "已批准并加入发送队列。" if action != "skip" else "已忽略该回复。"
        if result["status"] == "not_found_or_ambiguous":
            return "未找到唯一匹配的回复编号，请使用 /boss list 重新查看。"
        return "草稿版本或状态已变化，请使用 /boss next 查看最新内容。"

    @staticmethod
    def _parse_version(value: str) -> int | None:
        normalized = value.strip().lower().removeprefix("v")
        return int(normalized) if normalized.isdigit() and int(normalized) > 0 else None

    @staticmethod
    def _render_one(row: dict[str, object]) -> str:
        return (
            f"{row['company']} / {row['title']} / HR {row['hr_name']}\n"
            f"风险：{row['risk_level']} · {row['intent']}\n"
            f"HR：{row['hr_message']}\n"
            f"草稿：{row['draft_text']}\n"
            f"编号：{str(row['reply_id'])[:12]} V{row['draft_version']}\n"
            "批准：/boss approve <编号> <版本>\n"
            "编辑：/boss edit <编号> <版本> <新正文>\n"
            "忽略：/boss skip <编号> <版本>"
        )

    @staticmethod
    def _help() -> str:
        return (
            "Boss 回复指令：\n"
            "/boss inbox\n/boss list\n/boss next\n"
            "/boss approve <编号> <版本>\n"
            "/boss edit <编号> <版本> <新正文>\n"
            "/boss skip <编号> <版本>"
        )


class CompositeMessageHandler:
    def __init__(self, *handlers: MessageHandler) -> None:
        self._handlers = handlers

    async def handle(self, message: InboundMessage) -> str | None:
        for handler in self._handlers:
            reply = await handler.handle(message)
            if reply is not None:
                return reply
        return None


def build_registry_command_handler(registry_path: Path) -> MessageHandler:
    """Factory that builds a production registry handler for the gateway."""
    from jobagent.boss_reply_service import BossReplyApplicationService

    return CompositeMessageHandler(
        BossReplyCommandHandler(BossReplyApplicationService(registry_path)),
        RegistryCommandHandler(registry_path),
    )


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
        notification_source: NotificationSource | None = None,
        _max_iterations: int = 0,  # test-only: exit after N iterations
    ) -> None:
        self._account = account
        self._handler = handler
        self._state_dir = state_dir
        self._client = client
        self._dedup = dedup or MessageDeduplicator()
        self._notification_source = notification_source
        self._context_token_store = ContextTokenStore(
            path=state_dir / CONTEXT_TOKENS_FILE.name
        )
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
                    # Normal for long-poll: server didn't respond within the
                    # 40 s budget. Deliver queued management notifications
                    # before reopening the long poll.
                    await self._deliver_notifications(client, store)
                    continue
                except ILinkApiError as exc:
                    if cursor:
                        # A bogus/stale persisted cursor makes iLink reject
                        # every poll (ret=-1) forever; reset once and re-poll
                        # from scratch instead of silently backing off until
                        # the user kills the process.
                        logger.warning(
                            "get_updates rejected with cursor %r (ret=%s "
                            "errcode=%s); resetting cursor and re-polling",
                            cursor[:12],
                            exc.ret,
                            exc.errcode,
                        )
                        cursor = ""
                        _save_cursor(self._state_dir, "")
                        continue
                    consecutive_failures += 1
                    logger.warning(
                        "get_updates failed (%s); consecutive failures: %d",
                        exc,
                        consecutive_failures,
                    )
                    await self._backoff(consecutive_failures)
                    continue
                except BaseException as exc:
                    # KeyboardInterrupt, CancelledError → re-raise
                    if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                        raise
                    consecutive_failures += 1
                    logger.warning(
                        "get_updates error (%s); consecutive failures: %d",
                        exc,
                        consecutive_failures,
                    )
                    await self._backoff(consecutive_failures)
                    continue

                consecutive_failures = 0
                if new_cursor:
                    cursor = new_cursor
                    _save_cursor(self._state_dir, cursor)

                for message in messages:
                    await self._handle_one(client, store, dedup, message)

                await self._deliver_notifications(client, store)

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

        # Content-fingerprint dedup (Hermes pattern), free text only: a
        # repeated identical slash-command (/ping liveness check) is
        # legitimate traffic; message-id dedup above already guards the
        # overlapping-poll duplicates this was ported to fight.
        if message.text and not message.text.startswith("/"):
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

    async def _deliver_notifications(
        self, client: WeixinBotClient, store: ContextTokenStore
    ) -> None:
        source = self._notification_source
        owner = self._account.owner_user_id
        if source is None or not owner:
            return
        context_token = store.get(owner)
        if not context_token:
            return
        source.sync_pending_notifications()
        for notification in source.pending_notifications("wechat", limit=5):
            event_id = str(getattr(notification, "event_id", ""))
            text = str(getattr(notification, "text", ""))
            if not event_id or not text:
                continue
            try:
                await client.send_text(owner, text[:2048], context_token)
            except Exception as exc:
                source.mark_notification(
                    event_id, delivered=False, error=type(exc).__name__
                )
                continue
            source.mark_notification(event_id, delivered=True)

    @property
    def handler(self) -> MessageHandler | None:
        return self._handler

    @property
    def account(self) -> WeixinAccount:
        return self._account
