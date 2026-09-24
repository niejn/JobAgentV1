"""Boss直聘 auto-apply adapter — 打招呼 (chat greeting) flow."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import UTC, datetime
from string import Template
from typing import Any, Self

from playwright.async_api import ElementHandle, Page, Playwright, async_playwright

from jobagent.applier.base import BaseApplier
from jobagent.applier.boss_ws import (
    BossPublishAmbiguous,
    send_text_to_conversation,
    verify_text_in_conversation,
)
from jobagent.applier.captcha import detect_captcha, notify_captcha
from jobagent.applier.history import ApplyHistory
from jobagent.auth.boss_debug_chrome import BossDebugChromeError, ensure_boss_debug_chrome
from jobagent.config import Settings
from jobagent.crawl import CrawlGate
from jobagent.models import Application, ApplicationStatus, Job, JobSource, Profile
from jobagent.scraper.boss import BossAccessError, get_boss_cooldown
from jobagent.scraper.cdp_tab_pool import CdpTabPool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selectors — multiple fallbacks for resilience
# ---------------------------------------------------------------------------

_BTN_START_CHAT = [
    "a.btn-startchat",                          # primary CTA
    ".job-detail-box .btn-startchat",
    "a[ka='job_detail_chat']",
    "a:has-text('立即沟通')",
    "div.job-op a.btn",                          # broad fallback
]

_BTN_CONTINUE_CHAT = [
    "a:has-text('继续沟通')",
    "a.btn-startchat:has-text('继续沟通')",
]

_CHAT_INPUT = [
    "#chat-input",
    ".chat-input textarea",
    "div.chat-editor textarea",
    "[contenteditable='true'].chat-input",
    "div.edit-area [contenteditable='true']",
    "textarea[name='msg']",
]

_BTN_SEND = [
    "button.btn-send",
    "button:has-text('发送')",
    ".chat-op button[type='submit']",
    "div.message-controls button.btn-v2",
]

_DAILY_LIMIT_TEXT = [
    "今日沟通人数已达上限",
    "今日投递次数已用完",
    "今天的机会已用完",
]

class BossApplier(BaseApplier):
    """Auto-apply to jobs on Boss直聘 via the 打招呼 chat flow.

    Usage::

        async with BossApplier(settings) as applier:
            result = await applier.apply(job, profile)

    Rides the user's already-logged-in Chrome over CDP (same transport as
    BossCdpBackend.discover): real browser fingerprint, real login session,
    no cookie injection into a fresh profile - the combination that kept
    triggering Boss risk control on the previous self-launched browser.

    Tabs opened for greetings stay open during the batch (Boss detects
    reused/blank tabs); they are all closed on __aexit__, leaving the
    user's own tabs untouched.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        notifier: object | None = None,
        history: ApplyHistory | None = None,
        crawl_gate: CrawlGate | None = None,
    ) -> None:
        self._settings = settings
        self._notifier = notifier
        # Anchor the apply history next to the state DB so the dedup ledger
        # and daily limit survive CWD changes (code review MEDIUM).
        self._history = history or ApplyHistory(
            settings.jobagent_state_db.parent / "apply_history.json"
        )
        self._crawl_gate = crawl_gate
        self._playwright: Playwright | None = None
        self._context: Any | None = None
        self._tab_pool: CdpTabPool | None = None

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> Self:
        self._playwright = await async_playwright().start()
        try:
            await ensure_boss_debug_chrome(self._settings)
            browser = await self._playwright.chromium.connect_over_cdp(
                self._settings.debug_chrome_cdp_endpoint,
                timeout=10_000,
            )
        except BossDebugChromeError as exc:
            await self._playwright.stop()
            raise BossAccessError(str(exc), code="cdp_not_ready") from exc
        except Exception as exc:
            await self._playwright.stop()
            raise BossAccessError(
                "Boss CDP: 无法连接 Chrome--Chrome 调试端口未就绪。请按以下步骤设置:\n"
                "1. 读取 skills/chrome-cdp-setup/SKILL.md\n"
                "2. 按 SKILL.md 中的 4 个步骤启动 Chrome 调试模式\n"
                "3. 重试本次操作",
                code="cdp_not_ready",
            ) from exc

        # Reuse the context that already has pages (= persistent login).
        context = next((c for c in browser.contexts if c.pages), None)
        if context is None:
            try:
                await browser.close()
            finally:
                await self._playwright.stop()
            raise BossAccessError(
                "Boss CDP: 未找到已打开页面的浏览器上下文（登录态可能丢失），"
                "请在 Chrome 窗口中确认 Boss 已登录。",
                code="boss_access_denied",
            )
        self._context = context
        logger.info("BossApplier attached to user Chrome via CDP")
        return self

    async def __aexit__(self, *args: object) -> None:
        # The pool closes every tab this applier opened; the keeper tab
        # stays (it keeps the debug Chrome alive) and the user's own tabs
        # are untouched.
        if self._tab_pool is not None:
            await self._tab_pool.close()
            self._tab_pool = None
        self._context = None
        # On a connect_over_cdp() browser, close() only detaches the
        # connection - the externally-owned Chrome keeps running (verified
        # against playwright 1.62.0).
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("BossApplier detached; greeting tabs returned to pool")

    # -- public API ---------------------------------------------------------

    async def apply(
        self, job: Job, profile: Profile, *, greeting: str | None = None
    ) -> Application:  # noqa: C901
        """Send a greeting to the recruiter on Boss直聘.

        ``greeting`` carries a per-JD personalized message generated by the
        Agent; when omitted the configured ``$company/$title/$name`` template
        is used, and when that is empty too Boss's own default is sent.

        Returns an Application with appropriate status:
          - SUBMITTED  - greeting sent (or already chatting)
          - FAILED     - unrecoverable error (captcha, limit, etc.)
          - CAPTCHA_BLOCKED - captcha detected, manual intervention needed
        """
        t0 = time.monotonic()
        logger.info("Boss apply START: %s @ %s [%s]", job.title, job.company, job.url)

        # --- pre-flight checks ------------------------------------------------
        if self._history.is_applied(job.id):
            logger.info("Skipped (already applied): %s", job.id)
            return self._make_app(
                job, ApplicationStatus.SUBMITTED,
                extra={"reason": "already_applied"},
            )

        if self._history.today_count() >= self._settings.boss_daily_limit:
            logger.warning("Daily limit reached (%d)", self._settings.boss_daily_limit)
            return self._make_app(
                job, ApplicationStatus.FAILED,
                extra={"reason": "daily_limit"},
            )

        # Refuse immediately while rate-limit cooldown is active - a real
        # request during cooldown would only deepen the block (same guard
        # as BossCdpBackend.discover; this was missing here before).
        allowed, remaining_min, _ = get_boss_cooldown().check()
        if not allowed:
            logger.warning("Boss apply blocked by cooldown (%d min left)", remaining_min)
            return self._make_app(
                job, ApplicationStatus.FAILED,
                extra={"reason": "cooldown_active", "remaining_minutes": remaining_min},
            )

        if self._context is None:
            raise RuntimeError("BossApplier not initialised — use 'async with'.")

        # --- page ---------------------------------------------------------------
        # Tab from the bounded pool: keeper keeps the debug Chrome alive,
        # max_tabs caps how many stay open, and tabs retire after
        # max_reuses (Boss flags repeatedly re-navigated pages).
        if self._crawl_gate is not None:
            # Greeting-page navigation shares the account's crawl budget.
            await self._crawl_gate.acquire("boss-cdp")
        if self._tab_pool is None:
            self._tab_pool = CdpTabPool(self._context)
        page = await self._tab_pool.acquire()

        try:
            return await self._do_apply(page, job, profile, t0, greeting=greeting)
        except Exception as exc:
            logger.exception("Unexpected error during Boss apply: %s", exc)
            return self._make_app(
                job, ApplicationStatus.FAILED,
                extra={"reason": "unexpected_error", "error": str(exc)},
            )
        finally:
            await self._tab_pool.release(page)

    # -- internal flow ------------------------------------------------------

    async def _do_apply(  # noqa: C901
        self,
        page: Page,
        job: Job,
        profile: Profile,
        t0: float,
        *,
        greeting: str | None = None,
    ) -> Application:
        """Core apply flow inside a browser page."""

        # 1. Navigate to job detail page
        logger.info("Navigating to %s", job.url)
        await page.goto(str(job.url), wait_until="domcontentloaded", timeout=30_000)
        await self._human_delay(0.5, 1.5)

        # 2. Captcha check
        if await detect_captcha(page):
            if self._notifier:
                await notify_captcha(self._notifier, str(job.url))
            return self._make_app(
                job, ApplicationStatus.CAPTCHA_BLOCKED,
                extra={"reason": "captcha"},
            )

        # 3. Check daily limit text on page
        body = await page.inner_text("body")
        for limit_text in _DAILY_LIMIT_TEXT:
            if limit_text in body:
                logger.warning("Daily limit detected on page: %s", limit_text)
                return self._make_app(
                    job, ApplicationStatus.FAILED,
                    extra={"reason": "daily_limit"},
                )

        # 4. Check if already chatting (继续沟通)
        continue_btn = await self._find_element(page, _BTN_CONTINUE_CHAT)
        if continue_btn:
            logger.info("Already in conversation for %s", job.id)
            self._history.mark_applied(job.id, "submitted")
            return self._make_app(
                job, ApplicationStatus.SUBMITTED,
                extra={"reason": "already_applied"},
            )

        # 5. Find and click 立即沟通
        start_btn = await self._find_element(page, _BTN_START_CHAT)
        if not start_btn:
            logger.error("Cannot find '立即沟通' button for %s", job.url)
            return self._make_app(
                job, ApplicationStatus.FAILED,
                extra={"reason": "button_not_found"},
            )

        logger.info("Clicking '立即沟通'")
        await start_btn.click()
        await self._human_delay(1.5, 3.0)

        # 6. Post-click captcha check
        if await detect_captcha(page):
            if self._notifier:
                await notify_captcha(self._notifier, str(job.url))
            return self._make_app(
                job, ApplicationStatus.CAPTCHA_BLOCKED,
                extra={"reason": "captcha"},
            )

        # 7. Compose greeting: per-JD personalized text wins over the template
        greeting = greeting or self._build_greeting(job, profile)

        if greeting:
            # 8. Find chat input and type greeting
            chat_input = await self._find_element(page, _CHAT_INPUT)
            if not chat_input:
                # Boss may have auto-sent its default greeting as part of the
                # "立即沟通" action and leave no editor on the job page. Keep
                # that message and append the user's custom greeting through
                # the already-verified direct chat transport.
                logger.info(
                    "No chat input found — appending custom greeting after Boss default"
                )
                custom_sent = False
                custom_error = ""
                target = None
                attempt_started_ms = int(time.time() * 1000)
                try:
                    assert self._context is not None
                    cookies = {
                        item["name"]: item["value"]
                        for item in await self._context.cookies("https://www.zhipin.com")
                    }
                    target = await send_text_to_conversation(
                        cookies=cookies,
                        company=job.company,
                        job_title=job.title,
                        text=greeting,
                    )
                    custom_sent = True
                except BossPublishAmbiguous as exc:
                    # PUBACK lost does not mean delivery failed (field
                    # finding 2026-09-22). Verify through history with the
                    # target the exception carries before judging.
                    target = exc.target
                    confirmed = False
                    try:
                        confirmed = await verify_text_in_conversation(
                            cookies=cookies,
                            target=exc.target,
                            text=greeting,
                            not_before_ms=attempt_started_ms,
                        )
                    except Exception:
                        logger.warning(
                            "history verify after ambiguous publish failed",
                            exc_info=True,
                        )
                    if confirmed:
                        custom_sent = True
                        custom_error = "ack_lost_history_confirmed"
                    else:
                        custom_error = "ack_lost_history_unconfirmed"
                    logger.warning(
                        "Custom greeting follow-up publish ambiguous: %s -> %s",
                        type(exc.original).__name__,
                        custom_error,
                    )
                except Exception as exc:
                    custom_error = type(exc).__name__
                    logger.warning(
                        "Custom greeting follow-up failed after default greeting: %s",
                        custom_error,
                    )
                self._history.mark_applied(job.id, "submitted")
                elapsed = time.monotonic() - t0
                return self._make_app(
                    job, ApplicationStatus.SUBMITTED,
                    extra={
                        "reason": "default_greeting",
                        "greeting_sent": custom_sent,
                        "custom_greeting_status": "sent" if custom_sent else "failed",
                        "requested_greeting": greeting,
                        "custom_greeting_error": custom_error,
                        "conversation": (
                            {
                                "friend_id": target.friend_id,
                                "friend_source": target.friend_source,
                                "encrypt_boss_id": target.encrypt_boss_id,
                                "name": target.name,
                                "company": target.company,
                                "job_title": target.job_title,
                            }
                            if custom_sent
                            else None
                        ),
                        "response_time": round(elapsed, 2),
                    },
                )

            logger.info("Typing greeting: %s", greeting[:60])
            await chat_input.click()
            await self._human_delay(0.3, 0.8)

            # Type slowly to mimic human input
            await chat_input.fill("")
            await page.keyboard.type(greeting, delay=random.randint(30, 80))
            await self._human_delay(0.5, 1.0)

            # 9. Send
            send_btn = await self._find_element(page, _BTN_SEND)
            if send_btn:
                await send_btn.click()
                logger.info("Send button clicked")
            else:
                # Fallback: press Enter
                await page.keyboard.press("Enter")
                logger.info("Sent via Enter key")

            await self._human_delay(1.0, 2.0)
        else:
            # No custom greeting — boss will use the pre-configured default
            logger.info("No custom greeting; relying on Boss default greeting")

        # 10. Verify success — check for sent message or conversation state
        # After sending, Boss typically shows the message in the chat window
        # We consider it successful if no error dialog appeared
        if await detect_captcha(page):
            if self._notifier:
                await notify_captcha(self._notifier, str(job.url))
            return self._make_app(
                job, ApplicationStatus.CAPTCHA_BLOCKED,
                extra={"reason": "captcha"},
            )

        # Mark in history
        self._history.mark_applied(job.id, "submitted")
        elapsed = time.monotonic() - t0
        logger.info(
            "Boss apply SUCCESS: %s @ %s (%.1fs)",
            job.title, job.company, elapsed,
        )

        # Inter-apply delay (human-like pacing)
        delay = random.uniform(
            self._settings.boss_apply_delay_min,
            self._settings.boss_apply_delay_max,
        )
        logger.debug("Waiting %.1fs before next apply", delay)
        await asyncio.sleep(delay)

        return self._make_app(
            job, ApplicationStatus.SUBMITTED,
            extra={
                "greeting_sent": greeting or "(default)",
                "response_time": round(elapsed, 2),
            },
        )

    # -- helpers ------------------------------------------------------------

    def _build_greeting(self, job: Job, profile: Profile) -> str | None:
        """Build a greeting message from template, or return None for default."""
        template = self._settings.boss_greeting
        if not template:
            return None

        # Support $-style substitution: $company, $title, $name
        try:
            return Template(template).safe_substitute(
                company=job.company,
                title=job.title,
                name=profile.name,
            )
        except Exception as e:
            logger.warning("Greeting template error: %s — using raw template", e)
            return template

    @staticmethod
    async def _find_element(
        page: Page,
        selectors: list[str],
    ) -> ElementHandle | None:
        """Try multiple selectors and return the first visible match."""
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    visible = await el.is_visible()
                    if visible:
                        return el
            except Exception:
                continue
        return None

    @staticmethod
    async def _human_delay(lo: float = 0.5, hi: float = 1.5) -> None:
        """Sleep for a random duration to mimic human interaction."""
        await asyncio.sleep(random.uniform(lo, hi))

    @staticmethod
    def _make_app(
        job: Job,
        status: ApplicationStatus,
        *,
        extra: dict | None = None,
    ) -> Application:
        """Create an Application result."""
        return Application(
            job_id=job.id,
            source=JobSource.BOSS,
            status=status,
            message=f"{job.title} @ {job.company}",
            applied_at=datetime.now(UTC) if status == ApplicationStatus.SUBMITTED else None,
            extra=extra or {},
        )
