"""Worker that atomically sends approved Boss HR reply drafts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from typing import Any, Protocol

from jobagent.boss_reply_queue import BossReplyQueue

logger = logging.getLogger(__name__)


class BossReplySender(Protocol):
    async def send(
        self,
        *,
        conversation_id: str,
        friend_id: int,
        friend_source: int,
        encrypt_boss_id: str,
        hr_name: str,
        text: str,
    ) -> dict[str, Any]: ...


class LiveBossReplySender:
    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def send(
        self,
        *,
        conversation_id: str,
        friend_id: int,
        friend_source: int,
        encrypt_boss_id: str,
        hr_name: str,
        text: str,
    ) -> dict[str, Any]:
        from jobagent.applier.boss_ws import (
            BossConversationTarget,
            send_text_to_target,
            verify_text_in_conversation,
        )
        from jobagent.auth.cookie_manager import get_cookies

        items = await get_cookies("boss", self._settings)
        cookies = {
            str(item["name"]): str(item["value"])
            for item in items
            if item.get("name") and item.get("value")
        }
        target = BossConversationTarget(
            friend_id=friend_id,
            friend_source=friend_source,
            encrypt_boss_id=encrypt_boss_id,
            name=hr_name,
            company="",
            job_title="",
        )
        started_ms = int(time.time() * 1000)
        try:
            await send_text_to_target(cookies=cookies, target=target, text=text)
            return {"status": "ok", "conversation_id": conversation_id}
        except Exception as exc:
            for attempt in range(3):
                if await verify_text_in_conversation(
                    cookies=cookies,
                    target=target,
                    text=text,
                    not_before_ms=started_ms,
                ):
                    return {"status": "ok", "conversation_id": conversation_id}
                if attempt < 2:
                    await asyncio.sleep(2)
            return {
                "status": "unverified",
                "error_type": type(exc).__name__,
                "conversation_id": conversation_id,
            }


class BossReplyWorker:
    def __init__(
        self,
        queue: BossReplyQueue,
        sender: BossReplySender,
        *,
        poll_interval_seconds: float = 2,
        daily_limit: int = 30,
        post_send_delay: tuple[float, float] = (5, 10),
    ) -> None:
        self._queue = queue
        self._sender = sender
        self._poll_interval = max(0.2, poll_interval_seconds)
        self._daily_limit = max(1, daily_limit)
        low, high = post_send_delay
        self._post_send_delay = (max(0, low), max(low, high))
        self._owner = uuid.uuid4().hex
        self._stop = asyncio.Event()

    async def run_once(self) -> dict[str, Any]:
        self._queue.recover_stale_sending()
        item = self._queue.claim_sendable(
            owner=self._owner,
            daily_limit=self._daily_limit,
        )
        if item is None:
            return {"status": "idle"}
        friend_id = int(item.get("friend_id") or 0)
        encrypt_boss_id = str(item.get("encrypt_boss_id") or "")
        generation = int(item["send_generation"])
        if friend_id <= 0 or not encrypt_boss_id:
            self._queue.finish(
                str(item["reply_id"]),
                owner=self._owner,
                generation=generation,
                status="failed",
                error="stable_conversation_target_missing",
            )
            return {"status": "failed", "reply_id": item["reply_id"]}
        try:
            heartbeat = asyncio.create_task(
                self._lease_heartbeat(str(item["reply_id"]), generation)
            )
            try:
                result = await self._sender.send(
                    conversation_id=str(item["conversation_id"]),
                    friend_id=friend_id,
                    friend_source=int(item.get("friend_source") or 0),
                    encrypt_boss_id=encrypt_boss_id,
                    hr_name=str(item["hr_name"]),
                    text=str(item["draft_text"]),
                )
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
        except Exception as exc:
            self._queue.finish(
                str(item["reply_id"]),
                owner=self._owner,
                generation=generation,
                status="unverified",
                error=type(exc).__name__,
            )
            return {"status": "unverified", "reply_id": item["reply_id"]}
        result_status = str(result.get("status") or "failed")
        terminal = (
            "submitted"
            if result_status == "ok"
            else "unverified"
            if result_status == "unverified"
            else "failed"
        )
        self._queue.finish(
            str(item["reply_id"]),
            owner=self._owner,
            generation=generation,
            status=terminal,
            error=str(result.get("error_type") or result.get("message") or "")[:300],
        )
        return {"status": terminal, "reply_id": item["reply_id"]}

    async def _lease_heartbeat(self, reply_id: str, generation: int) -> None:
        while True:
            await asyncio.sleep(20)
            if not self._queue.renew_send_lease(
                reply_id,
                owner=self._owner,
                generation=generation,
                lease_seconds=60,
            ):
                return

    async def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                result = await self.run_once()
                failures = 0
            except Exception:
                failures += 1
                delay = min(self._poll_interval * (2 ** min(failures, 5)), 60)
                logger.exception("Boss reply worker failed; retrying in %.1fs", delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                continue
            if result["status"] != "idle":
                low, high = self._post_send_delay
                delay = random.uniform(low, high)
                if delay:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
