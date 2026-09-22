"""Agent Tool for batch-sending Boss greetings with HITL confirmation."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, field_validator

from jobagent.boss_outbound_guard import guard_refusal, screen_outbound_text
from jobagent.config import Settings
from jobagent.crawl import CrawlGate
from jobagent.domain import HttpUrl, Job, JobSource, Profile
from jobagent.journey.job_registry import (
    JobProgressStatus,
    JobTransitionError,
    SQLiteJobRegistry,
)
from jobagent.profile import SQLiteCandidateContextProvider
from jobagent.scraper.boss import get_boss_cooldown

logger = logging.getLogger(__name__)


def _bare_boss_job_id(job_id: str) -> str:
    """Chat-list metadata carries the bare encryptJobId; registry ids add boss:."""

    return job_id.removeprefix("boss:").strip()


class GreetingTarget(BaseModel):
    """One Boss job to greet - minimal fields the agent already has."""

    url: str = Field(min_length=1, description="Full Boss job-detail URL")
    company: str = Field(min_length=1, description="Company name")
    title: str = Field(min_length=1, description="Job title")
    job_id: str = Field(default="", description="Unique job ID if known")
    greeting: str | None = Field(
        default=None,
        description=(
            "Per-JD personalized greeting text (recommended), written in the "
            "job seeker's own first-person voice. Must never mention AI, "
            "automation, bots, or test/ignore phrasing - an outbound guard "
            "refuses to send such text. When omitted, the configured template "
            "or Boss's default greeting is sent."
        ),
    )

    @field_validator("url")
    @classmethod
    def validate_boss_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        hostname = parsed.hostname or ""
        if "zhipin.com" not in hostname or not parsed.path:
            raise ValueError("Provide a valid Boss job detail URL (zhipin.com)")
        return value.strip()


class BossGreetJobsRequest(BaseModel):
    """Jobs to greet and batch controls."""

    jobs: list[GreetingTarget] = Field(
        min_length=1,
        max_length=20,
        description=(
            "List of Boss jobs to greet. The Agent must present these to the user "
            "and obtain explicit confirmation before calling this tool."
        ),
    )

    max_greetings: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum greetings to send in this batch",
    )


class BossGreetingsManager:
    """Batch-send greetings on Boss using the existing Playwright applier."""

    def __init__(
        self,
        settings: Settings,
        *,
        registry_path: Path | None = None,
        crawl_gate: CrawlGate | None = None,
    ) -> None:
        self._settings = settings
        # When set, every successfully submitted greeting is recorded in the
        # job registry so future discovery runs never re-recommend the job.
        self._registry_path = registry_path
        self._crawl_gate = crawl_gate

    async def greet(self, request: BossGreetJobsRequest) -> dict[str, Any]:
        """Execute greetings for up to ``max_greetings`` jobs."""

        # Approval is enforced by the HITL middleware (physical interrupt
        # before this body runs); no parameter gate here.

        # Greeting while rate-limited would fail and deepen the block.
        allowed, remaining_min, _ = get_boss_cooldown().check()
        if not allowed:
            return {
                "status": "blocked",
                "error_type": "cooldown_active",
                "message": (
                    f"Boss 风控冷却中（约剩 {remaining_min} 分钟），本次未发送任何招呼。"
                    "请稍后再试。"
                ),
            }
        profile = self._load_profile()
        if profile is None:
            return {
                "status": "failed",
                "message": "候选人背景未就绪；请先导入简历并确认候选背景。",
            }
        # The legacy CDP/Playwright greeting transport was removed 2026-09-19:
        # it lacked the per-HR dedup, the outbound-guard send seam and the
        # post-batch delivery re-verification. HTTP direct contact is the only
        # path; BossApplier itself stays for the standalone CLI apply flow.
        return await self._greet_direct(request)

    async def _greet_direct(self, request: BossGreetJobsRequest) -> dict[str, Any]:
        """Create conversations with friend/add and greet through MQTT/WS."""

        from jobagent.applier.boss_direct_contact import BossDirectContactAdapter
        from jobagent.applier.boss_ws import send_text_to_target
        from jobagent.auth.cookie_manager import get_cookies
        from jobagent.journey.boss_contact import BossContactRegistry

        cookie_items = await get_cookies("boss", self._settings)
        cookies = {
            str(item["name"]): str(item["value"])
            for item in cookie_items
            if item.get("name") and item.get("value")
        }
        targets = request.jobs[: request.max_greetings]
        history_index = await self._load_history_job_index()
        results: list[dict[str, Any]] = []
        # (results_index, conversation target, text, send timestamp, request target)
        # for entries left unverified — re-checked read-only after the batch.
        pending_verifies: list[tuple[int, Any, str, int, GreetingTarget]] = []
        registry = (
            SQLiteJobRegistry(self._registry_path)
            if self._registry_path is not None
            else None
        )
        contact_registry = (
            BossContactRegistry(self._registry_path)
            if self._registry_path is not None
            else None
        )
        try:
            contact = BossDirectContactAdapter(self._settings, cookies=cookies)
            for target in targets:
                job_id = target.job_id or target.url
                attempt = (
                    contact_registry.begin_attempt(
                        job_id=job_id,
                        action="greeting",
                        requested_text=target.greeting,
                    )
                    if contact_registry is not None
                    else None
                )
                if attempt is not None and attempt.status in {"confirmed", "submitted"}:
                    results.append(
                        {
                            "job_id": job_id,
                            "company": target.company,
                            "title": target.title,
                            "url": target.url,
                            "status": "submitted",
                            "reason": "already_contacted",
                            "greeting_sent": attempt.result.get("greeting_sent"),
                        }
                    )
                    continue
                if violations := screen_outbound_text(target.greeting or ""):
                    entry = {
                        "job_id": job_id,
                        "company": target.company,
                        "title": target.title,
                        "url": target.url,
                        "reason": "outbound_guard",
                        "greeting_sent": False,
                        **guard_refusal(violations),
                    }
                    if contact_registry is not None and attempt is not None:
                        contact_registry.finish_attempt(
                            attempt.id, status="failed", result=entry
                        )
                    results.append(entry)
                    continue
                hist = history_index.get(_bare_boss_job_id(job_id))
                if hist is not None:
                    results.append(
                        self._record_history_skip(
                            registry=registry,
                            contact_registry=contact_registry,
                            attempt=attempt,
                            target=target,
                            job_id=job_id,
                            hist=hist,
                        )
                    )
                    continue
                transport = (
                    contact_registry.get_job_transport(job_id)
                    if contact_registry is not None
                    else {}
                )
                if not transport:
                    entry = {
                        "job_id": job_id,
                        "company": target.company,
                        "title": target.title,
                        "url": target.url,
                        "status": "failed",
                        "reason": "job_transport_data_missing",
                        "greeting_sent": False,
                    }
                    if contact_registry is not None and attempt is not None:
                        contact_registry.finish_attempt(
                            attempt.id, status="failed", result=entry
                        )
                    results.append(entry)
                    continue
                job = Job(
                    id=job_id,
                    source=JobSource.BOSS,
                    title=target.title,
                    company=target.company,
                    location="",
                    url=HttpUrl(target.url),
                    description="",
                    metadata=transport,
                )
                created = await contact.enter(job)
                prior_conversation = (
                    contact_registry.find_conversation_by_friend(created.target.friend_id)
                    if contact_registry is not None and created.target is not None
                    else None
                )
                same_hr_previously_contacted = prior_conversation is not None
                custom_sent = False
                send_error = ""
                delivery_unverified = False
                if (
                    created.status == "confirmed"
                    and created.target
                    and target.greeting
                    and not same_hr_previously_contacted
                ):
                    attempt_started_ms = int(time.time() * 1000)
                    try:
                        await send_text_to_target(
                            cookies=cookies,
                            target=created.target,
                            text=target.greeting,
                            page_token=contact.page_token,
                        )
                        custom_sent = True
                    except TimeoutError:
                        if await self._verify_with_retry(
                            cookies=cookies,
                            target=created.target,
                            text=target.greeting,
                            not_before_ms=attempt_started_ms,
                        ):
                            custom_sent = True
                            send_error = "ack_timeout_but_history_confirmed"
                        else:
                            delivery_unverified = True
                            send_error = "ack_timeout_history_not_visible"
                            pending_verifies.append(
                                (
                                    len(results),
                                    created.target,
                                    target.greeting,
                                    attempt_started_ms,
                                    target,
                                )
                            )
                    except Exception as exc:
                        send_error = type(exc).__name__
                        if await self._verify_with_retry(
                            cookies=cookies,
                            target=created.target,
                            text=target.greeting,
                            not_before_ms=attempt_started_ms,
                        ):
                            custom_sent = True
                            send_error = "ack_timeout_but_history_confirmed"
                        else:
                            # WebSocket libraries use their own close/error
                            # classes (for example ConnectionClosedOK),
                            # which are not ConnectionError/OSError. Once the
                            # publish may have been accepted, any transport
                            # exception is ambiguous and must not be recorded
                            # as a definite failure or retried blindly.
                            delivery_unverified = True
                            logger.warning(
                                "Direct Boss greeting delivery unverified: %s",
                                send_error,
                            )
                            pending_verifies.append(
                                (
                                    len(results),
                                    created.target,
                                    target.greeting,
                                    attempt_started_ms,
                                    target,
                                )
                            )
                status = (
                    "submitted"
                    if created.status == "confirmed"
                    and (custom_sent or same_hr_previously_contacted)
                    else "unverified"
                    if created.status == "confirmed" and delivery_unverified
                    else "failed"
                )
                entry = {
                    "job_id": job_id,
                    "company": target.company,
                    "title": target.title,
                    "url": target.url,
                    "status": status,
                    "reason": "already_contacted_same_hr"
                    if status == "submitted" and same_hr_previously_contacted
                    else "direct_contact_confirmed"
                    if status == "submitted"
                    else "greeting_unverified"
                    if status == "unverified"
                    else created.error_type or send_error or "greeting_unconfirmed",
                    "greeting_sent": custom_sent,
                    "default_greeting_present": created.default_greeting is not None,
                    "platform_error_code": getattr(created, "platform_code", None),
                    "platform_error_message": getattr(created, "platform_message", ""),
                }
                if same_hr_previously_contacted and prior_conversation is not None:
                    entry["previously_contacted_job"] = prior_conversation["job_id"]
                conversation_id = None
                if contact_registry is not None and created.target is not None:
                    conversation_id = contact_registry.save_conversation(
                        job_id=job_id,
                        target={
                            "friend_id": created.target.friend_id,
                            "friend_source": created.target.friend_source,
                            "encrypt_boss_id": created.target.encrypt_boss_id,
                            "name": created.target.name,
                            "company": created.target.company,
                            "job_title": created.target.job_title,
                        },
                    )
                if contact_registry is not None and attempt is not None:
                    contact_registry.finish_attempt(
                        attempt.id,
                        status=status,
                        result=entry,
                        conversation_id=conversation_id,
                    )
                if registry is not None and status == "submitted":
                    entry["progress_recorded"] = self._record_greeted(registry, target)
                results.append(entry)

            # Deferred read-only re-verification (channel fact, measured
            # 2026-09-19: WS/MQTT ack loss is routine and messages surface in
            # history ~6 s late, so unverified usually means delivered).
            # By now the whole batch has elapsed; one more history pass
            # corrects the status without ever re-sending.
            for index, verify_target, text, started_ms, target_model in pending_verifies:
                if await self._verify_with_retry(
                    cookies=cookies,
                    target=verify_target,
                    text=text,
                    not_before_ms=started_ms,
                    initial_delay=0.0,
                ):
                    entry = results[index]
                    entry["status"] = "submitted"
                    entry["reason"] = "history_confirmed_after_batch"
                    entry["greeting_sent"] = True
                    if registry is not None:
                        entry["progress_recorded"] = self._record_greeted(
                            registry, target_model
                        )
        finally:
            if registry is not None:
                registry.close()
            if contact_registry is not None:
                contact_registry.close()
        succeeded = sum(1 for item in results if item["status"] == "submitted")
        has_unverified = any(item["status"] == "unverified" for item in results)
        return {
            "status": "completed" if succeeded else "unverified" if has_unverified else "failed",
            "total": len(targets),
            "succeeded": succeeded,
            "results": results,
        }

    async def _load_history_job_index(self) -> dict[str, dict[str, Any]]:
        """Read-only chat list indexed by bare encryptJobId.

        Failures degrade to an empty index: greeting must not be blocked by a
        read outage; the registry-only checks still apply.
        """

        try:
            from jobagent.applier.boss_chat import list_boss_greetings_http

            listing = await list_boss_greetings_http(self._settings, label_id=0)
        except Exception:
            logger.info("Boss history pre-check unavailable", exc_info=True)
            return {}
        if listing.get("status") != "ok":
            logger.info(
                "Boss history pre-check failed: %s", listing.get("error_type")
            )
            return {}
        index: dict[str, dict[str, Any]] = {}
        for friend in listing.get("greetings") or []:
            if not isinstance(friend, dict):
                continue
            bare = str((friend.get("job_metadata") or {}).get("job_id") or "").strip()
            if bare:
                index[bare] = friend
        return index

    def _record_history_skip(
        self,
        *,
        registry: SQLiteJobRegistry | None,
        contact_registry: Any,
        attempt: Any,
        target: GreetingTarget,
        job_id: str,
        hist: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an already-greeted-in-history skip (registry + attempt +
        conversation), so the next run takes the already_contacted fast path."""

        entry = {
            "job_id": job_id,
            "company": target.company,
            "title": target.title,
            "url": target.url,
            "status": "submitted",
            "reason": "already_greeted_in_history",
            "greeting_sent": False,
            "history_hr": str(hist.get("name") or ""),
        }
        conversation_id: str | None = None
        if contact_registry is not None:
            friend_id = int(hist.get("friendId") or 0)
            if friend_id > 0:
                meta = hist.get("job_metadata") or {}
                conversation_id = contact_registry.save_conversation(
                    job_id=job_id,
                    target={
                        "friend_id": friend_id,
                        "friend_source": int(hist.get("friendSource") or 0),
                        "encrypt_boss_id": str(
                            hist.get("encryptBossId")
                            or hist.get("encryptFriendId")
                            or ""
                        ),
                        "name": str(hist.get("name") or ""),
                        "company": str(
                            hist.get("brandName") or meta.get("company") or ""
                        ),
                        "job_title": str(meta.get("title") or target.title),
                    },
                )
        if contact_registry is not None and attempt is not None:
            contact_registry.finish_attempt(
                attempt.id,
                status="submitted",
                result=entry,
                conversation_id=conversation_id,
            )
        if registry is not None:
            entry["progress_recorded"] = self._record_greeted(registry, target)
        return entry

    @staticmethod
    async def _verify_with_retry(
        *,
        cookies: dict[str, str],
        target: Any,
        text: str,
        not_before_ms: int = 0,
        initial_delay: float = 3.0,
    ) -> bool:
        """Poll Boss history after an ACK ambiguity; never send a second message.

        Channel fact (measured 2026-09-19 live): Boss WS/MQTT acks are
        routinely lost and the connection may close mid-send while the
        message still delivers — it only surfaces in conversation history
        ~6 s after the publish. Wait out that visibility lag before the
        first read-only check and span ~10 s total before giving up.
        ``initial_delay=0`` is for deferred re-checks long after the send.
        """

        from jobagent.applier.boss_ws import verify_text_in_conversation

        if initial_delay:
            await asyncio.sleep(initial_delay)
        for attempt in range(3):
            try:
                if await verify_text_in_conversation(
                    cookies=cookies,
                    target=target,
                    text=text,
                    not_before_ms=not_before_ms,
                ):
                    return True
            except Exception:
                logger.info("Boss greeting history check failed (attempt %d)", attempt + 1)
            if attempt < 2:
                await asyncio.sleep(3.0)
        return False

    def _record_greeted(
        self, registry: SQLiteJobRegistry, target: GreetingTarget
    ) -> bool:
        """Persist one submitted greeting; never let registry errors fail the batch."""

        job_id = target.job_id or target.url
        try:
            registry.upsert_discovered(
                job_id=job_id,
                source="boss",
                company=target.company,
                title=target.title,
                url=target.url,
            )
            registry.mark(job_id, JobProgressStatus.GREETED)
            return True
        except (JobTransitionError, KeyError, ValueError) as exc:
            logger.warning("Failed to record greeted status for %s: %s", job_id, exc)
            return False

    def _load_interview_preference(self) -> str | None:
        """Return a note about the user's interview-mode preference, if set."""

        provider = SQLiteCandidateContextProvider(
            self._settings.jobagent_state_db
        )
        context = provider.load()
        if context is None or context.search_profile is None:
            return None
        sp = context.search_profile
        if sp.prefer_online_interview:
            return (
                "候选人优先在线面试（视频/电话），"
                "如果 HR 回复了招呼，请确认面试方式能否在线上进行。"
            )
        return None

    def _load_profile(self) -> Profile | None:
        """Build a ``Profile`` from the candidate background stored in SQLite."""

        provider = SQLiteCandidateContextProvider(
            self._settings.jobagent_state_db
        )
        context = provider.load()
        if context is None:
            return None
        bg = context.background
        if bg is None:
            return None
        return Profile(
            name=bg.name,
            skills=bg.skills,
            years_experience=bg.years_experience,
            summary=bg.summary,
            desired_roles=(
                context.search_profile.desired_roles
                if context.search_profile
                else []
            ),
            preferred_locations=(
                context.search_profile.preferred_locations
                if context.search_profile
                else []
            ),
        )


def build_boss_greet_jobs_tool(manager: BossGreetingsManager) -> BaseTool:
    """Expose batch greeting; approval handled by the HITL middleware."""

    async def boss_greet_jobs(
        jobs: list[dict[str, str] | GreetingTarget],
        max_greetings: int = 5,
    ) -> dict[str, Any]:
        """批量向 Boss 招聘方发送打招呼消息（执行前暂停等待人工批准）。

        使用说明：
        1. 先用 discover_boss_jobs 找到合适的岗位
        2. 向用户展示岗位列表（公司/职位/招呼语）
        3. 调用本工具后执行会暂停，等待用户批准后才真正发送
        4. 工具会在后台逐一点击「立即沟通」并发送招呼语
        5. 招呼语使用 BOSS_GREETING 模板（支持 $company / $title / $name 变量）
        """

        normalized_jobs = [
            item if isinstance(item, GreetingTarget) else GreetingTarget(**item)
            for item in jobs
        ]
        return await manager.greet(
            BossGreetJobsRequest(
                jobs=normalized_jobs,
                max_greetings=max_greetings,
            )
        )

    return StructuredTool.from_function(
        coroutine=boss_greet_jobs,
        name="boss_greet_jobs",
        description=(
            "Send greetings to Boss HR for listed jobs (batch). "
            "Uses an existing logged-in Boss session with Playwright. "
            "Execution pauses for explicit user approval (HITL middleware). "
            "The greeter logs and enforces daily Boss limits. "
            "Dedup before sending: jobs already greeted are skipped with "
            "already_contacted / already_greeted_in_history (chat history "
            "shows a conversation for that job), and an HR we already talked "
            "to under another job is skipped with already_contacted_same_hr "
            "instead of re-sending. "
            "Receipts can drop while messages still deliver — unverified "
            "means 'no receipt', usually delivered; "
            "the batch re-verifies via read-only history at the end and flips "
            "confirmed entries to submitted (reason=history_confirmed_after_batch). "
            "Never re-send on unverified; report such entries as pending "
            "verification in the task result — the root agent routes history "
            "verification to boss_verification and records application state "
            "itself. Do not call tools you do not hold."
        ),
        args_schema=BossGreetJobsRequest,
    )
