"""Command-line interface for JobAgent workflows."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import click

from jobagent.applier.boss import BossApplier
from jobagent.applier.linkedin import LinkedInApplier
from jobagent.artifacts import OpportunityStatusBoard, StatusPeriod
from jobagent.auth.browser_login import (
    PLATFORM_CONFIG,
    cookies_valid,
    get_cookie_age_hours,
    inspect_cookie_providers,
    interactive_login,
    sync_boss_cookies_from_cdp_context,
)
from jobagent.cli_status import TypewriterTranscript
from jobagent.config import Settings, get_settings
from jobagent.crawl import build_crawl_gate
from jobagent.models import Job, JobSource
from jobagent.notifier.discord import DiscordNotifier
from jobagent.notifier.telegram import TelegramNotifier
from jobagent.observability import setup_logging
from jobagent.profile.loader import load_profile
from jobagent.scraper.base import BaseScraper
from jobagent.scraper.linkedin import LinkedInScraper
from jobagent.scraper.xhs_backend import SpiderXhsBackend
from jobagent.scraper.xhs_discovery import XhsDiscoveryRequest, discover_xhs_notes

logger = logging.getLogger(__name__)


class StreamingJobAgent(Protocol):
    async def resume_session(self, session_id: str) -> object: ...

    async def list_sessions(self, *, limit: int = 50) -> tuple[object, ...]: ...

    async def opportunity_status(self, period: StatusPeriod) -> OpportunityStatusBoard: ...

    def stream_reply(
        self,
        message: str,
        *,
        session_id: str,
    ) -> AsyncIterator[object]: ...

    def resume_reply(
        self,
        approved: bool | Sequence[bool],
        *,
        session_id: str,
        reject_reason: str = "",
    ) -> AsyncIterator[object]: ...

    async def close(self) -> None: ...


@click.group(help="JobAgent: AI-powered job hunting agent.")
def main() -> None:
    """Root CLI group."""


@main.command("validate-profile", hidden=True)
@click.option(
    "--profile",
    "profile_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Path to profile YAML or JSON file.",
)
def validate_profile_command(profile_path: Path) -> None:
    """Validate a profile document against the Profile model."""

    profile = load_profile(profile_path)
    click.echo(f"Profile OK: {profile.name} ({len(profile.skills)} skills)")


@main.command("scrape")
@click.option("--platform", type=click.Choice(["boss", "linkedin", "all"]), default="all")
@click.option("--query", required=True, help="Search query, e.g. 'Python Engineer'.")
@click.option("--location", default=None, help="Optional location filter.")
@click.option("--limit", default=20, show_default=True, type=int)
def scrape_command(platform: str, query: str, location: str | None, limit: int) -> None:
    """Scrape jobs from selected platforms and print a summary."""

    asyncio.run(_scrape(platform=platform, query=query, location=location, limit=limit))


@main.command("run")
@click.option("--platform", type=click.Choice(["boss", "linkedin", "all"]), default="all")
@click.option("--query", required=True, help="Search query, e.g. 'AI Engineer'.")
@click.option("--location", default=None, help="Optional location filter.")
@click.option(
    "--profile",
    "profile_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
)
@click.option(
    "--apply",
    "apply_enabled",
    is_flag=True,
    default=False,
    help=(
        "Send applications for jobs scoring >= 0.75 after matching. "
        "Requires interactive per-job confirmation (HITL); skipped by default."
    ),
)
@click.option("--limit", default=20, show_default=True, type=int)
def run_command(
    platform: str,
    query: str,
    location: str | None,
    profile_path: Path,
    limit: int,
    apply_enabled: bool,
) -> None:
    """Run end-to-end pipeline: scrape -> match -> notify."""

    asyncio.run(
        _run_pipeline(
            platform=platform,
            query=query,
            location=location,
            profile_path=profile_path,
            limit=limit,
            apply_enabled=apply_enabled,
        )
    )


@main.command("login")
@click.option(
    "--platform",
    default="xhs",
    show_default=True,
    help="Platform to log in to. [xhs, linkedin, wechat, all]",
)
@click.option("--timeout", type=int, default=5, show_default=True, help="Login timeout in minutes.")
@click.option("--check", is_flag=True, help="Only check if existing cookies are valid.")
def login_command(platform: str, timeout: int, check: bool) -> None:
    """Interactive browser login to save cookies."""
    valid = {"boss", "xhs", "linkedin", "wechat", "all"}
    if platform == "boss":
        asyncio.run(_boss_cdp_login(check_only=check, timeout_minutes=timeout))
        return
    if platform not in valid:
        raise click.UsageError(
            f"无效平台 '{platform}'，可选: xhs, linkedin, wechat, all"
        )
    if platform == "wechat":
        asyncio.run(_wechat_login(check_only=check, timeout_minutes=timeout))
        return
    platforms = [
        p for p in PLATFORM_CONFIG.keys() if p != "boss"
    ] if platform == "all" else [platform]
    asyncio.run(_login(platforms=platforms, timeout=timeout, check_only=check))


async def _boss_cdp_login(*, check_only: bool, timeout_minutes: int) -> None:
    """Verify a manually logged-in debug Chrome session and persist its cookies."""

    from playwright.async_api import async_playwright

    settings = get_settings()
    driver = await async_playwright().start()
    browser = None
    try:
        try:
            browser = await driver.chromium.connect_over_cdp(
                settings.debug_chrome_cdp_endpoint, timeout=10_000
            )
        except Exception:
            click.echo(
                "[boss] 未连接到调试 Chrome。请先启动带 remote-debugging-port 的 Chrome 后重试。"
            )
            return
        context = next((item for item in browser.contexts if item.pages), None)
        if context is None:
            click.echo("[boss] 调试 Chrome 没有可用浏览器上下文。")
            return
        page = next(
            (
                item
                for item in context.pages
                if "zhipin.com" in str(item.url or "")
            ),
            None,
        )
        if page is None:
            page = await context.new_page()
        try:
            await page.goto(
                "https://www.zhipin.com/web/geek/job",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception:
            pass
        if not check_only:
            click.echo(
                "[boss] 请在已打开的调试 Chrome 中手动完成 Boss 登录；登录完成后保持页面打开。"
            )
        deadline = asyncio.get_event_loop().time() + (
            1 if check_only else timeout_minutes * 60
        )
        while asyncio.get_event_loop().time() < deadline:
            try:
                logged_in = await page.evaluate(
                    """() => {
                        const visible = (el) => !!el && !!(
                          el.offsetWidth || el.offsetHeight || el.getClientRects().length
                        );
                        const login = [
                          '[class*="login-dialog"]', '[class*="boss-login"]', '.header-login-btn'
                        ]
                          .some((selector) => visible(document.querySelector(selector)));
                        const user = ['.user-nav', '.nav-figure'].some(
                          (selector) => visible(document.querySelector(selector))
                        );
                        return user && !login;
                    }"""
                )
            except Exception:
                logged_in = False
            if logged_in is True:
                saved = await sync_boss_cookies_from_cdp_context(context)
                click.echo(
                    click.style(f"[boss] ✅ 已验证登录并同步 {saved} 个 Boss Cookie。", fg="green")
                )
                return
            if check_only:
                break
            await asyncio.sleep(1)
        click.echo(
            click.style(
                "[boss] ❌ 尚未检测到登录；请在调试 Chrome 中完成登录后再次执行本命令，"
                "或在 Agent 中回复“已登录”。",
                fg="red",
            )
        )
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        await driver.stop()


async def _wechat_login(*, check_only: bool, timeout_minutes: int) -> None:
    """QR-scan login for the WeChat iLink bot account (no browser needed)."""

    from jobagent.wechat import (
        QRLoginState,
        WeixinAccount,
        WeixinAccountStore,
        WeixinBotClient,
        WeixinSessionExpired,
    )

    store = WeixinAccountStore()
    if check_only:
        account = store.load()
        if account is None:
            click.echo("JobAgent · 微信 Bot 尚未登录；运行 jobagent login --platform wechat 扫码。")
            return
        try:
            async with WeixinBotClient(account=account) as client:
                _, _ = await client.get_updates("")
        except WeixinSessionExpired:
            click.echo("JobAgent · 微信 Bot 登录已过期（errcode -14），请重新扫码登录。")
        except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
            click.echo(f"JobAgent · 微信 Bot 状态检查失败：{exc}")
        else:
            click.echo(
                f"JobAgent · 微信 Bot 登录有效（bot_id={account.bot_id or 'N/A'}）。"
            )
        return

    click.echo("JobAgent · 微信 Bot 扫码登录（iLink 官方 API，无封号风险）")
    async with WeixinBotClient() as client:
        qr = await client.request_qr_code()
        _render_wechat_qr(qr.img_content)
        poll_base: str | None = None
        refresh_count = 0
        deadline = asyncio.get_event_loop().time() + timeout_minutes * 60
        while asyncio.get_event_loop().time() < deadline:
            status = await client.poll_qr_status(qr.key, base_url=poll_base)
            if status.state is QRLoginState.CONFIRMED:
                store.save(
                    WeixinAccount(
                        bot_token=status.bot_token,
                        base_url=status.base_url,
                        bot_id=status.bot_id,
                        owner_user_id=status.owner_user_id,
                        login_at=datetime.now().astimezone().isoformat(),
                    )
                )
                click.echo(
                    f"JobAgent · 微信 Bot 登录成功（bot_id={status.bot_id or 'N/A'}），"
                    f"凭证已保存到 {store.path}。"
                )
                click.echo(
                    "提示：iLink bot 无法主动发起会话；"
                    "请在微信里给 bot 发一条消息激活对话，之后运行 jobagent watch。"
                )
                return
            if status.state is QRLoginState.REDIRECT and status.redirect_host:
                poll_base = f"https://{status.redirect_host}"
                continue
            if status.state is QRLoginState.EXPIRED:
                refresh_count += 1
                if refresh_count > 3:
                    click.echo("JobAgent · 二维码多次过期，请重新运行登录命令。")
                    return
                click.echo(f"JobAgent · 二维码已过期，自动刷新 ({refresh_count}/3)…")
                qr = await client.request_qr_code()
                _render_wechat_qr(qr.img_content)
                continue
            if status.state is QRLoginState.SCANNED:
                click.echo("已扫码，请在手机微信中确认…")
            await asyncio.sleep(2)
        click.echo("JobAgent · 登录超时，未完成扫码确认。")


def _render_wechat_qr(img_content: str) -> None:
    """Render the scannable liteapp URL as a terminal ASCII QR code."""

    click.echo("请用微信扫描下方二维码（或在手机微信中长按识别）：\n")
    try:
        import qrcode

        matrix = qrcode.QRCode(border=1)
        matrix.add_data(img_content)
        matrix.print_ascii(invert=True)
    except ImportError:
        click.echo(f"    二维码链接: {img_content}")


@main.command("watch")
@click.option(
    "--channel",
    "channels",
    multiple=True,
    type=click.Choice(["wechat"]),
    default=["wechat"],
    show_default=True,
    help="Gateway channels to run (more coming: boss message monitor).",
)
def watch_command(channels: tuple[str, ...]) -> None:
    """Run the HR Gateway process: WeChat bot + job progress commands."""

    asyncio.run(_watch(channels=channels))


async def _watch(channels: tuple[str, ...]) -> None:
    """Gateway entry: run all requested channels until Ctrl+C."""

    from jobagent.gateway import WeChatChannel, build_registry_command_handler
    from jobagent.wechat import WeixinAccountStore

    settings = get_settings()
    running: list[WeChatChannel] = []
    if "wechat" in channels:
        account = WeixinAccountStore().load()
        if account is None:
            click.echo(
                "JobAgent · 微信 Bot 未登录；先运行 jobagent login --platform wechat。"
            )
            return
        running.append(
            WeChatChannel(
                account=account,
                handler=build_registry_command_handler(settings.jobagent_state_db),
            )
        )
    if not running:
        click.echo("JobAgent · 没有可运行的通道。")
        return
    click.echo(
        f"JobAgent · HR Gateway 已启动（通道: {', '.join(channels)}），Ctrl+C 退出。"
    )
    try:
        await asyncio.gather(*(channel.run() for channel in running))
    except KeyboardInterrupt:
        click.echo("\nJobAgent · HR Gateway 已停止。")
    finally:
        for channel in running:
            await channel.aclose()


@main.command("chat")
@click.option(
    "--config",
    "startup_config",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=False,
    help="Optional YAML import for search profile and resume/background.",
)
@click.option("--session-id", default=None, help="Existing session ID; omitted creates one.")
@click.option("--sessions", "list_sessions", is_flag=True, help="List saved sessions and exit.")
@click.option(
    "-c",
    "--command",
    "one_shot",
    default=None,
    help="Send one message, print the reply, and exit (non-interactive; handy for scripting).",
)
def chat_command(
    startup_config: Path | None,
    session_id: str | None,
    list_sessions: bool,
    one_shot: str | None,
) -> None:
    """Start a conversational JobAgent session."""

    if list_sessions:
        asyncio.run(_list_sessions_only())
        return
    asyncio.run(
        _chat(startup_config=startup_config, session_id=session_id, one_shot=one_shot)
    )


async def _list_sessions_only() -> None:
    """Print saved sessions without entering the interactive chat loop."""

    from jobagent.agent import build_job_agent

    settings = get_settings()
    setup_logging()
    agent = build_job_agent(settings)
    try:
        sessions = await agent.list_sessions()
    finally:
        await agent.close()
    if not sessions:
        click.echo("JobAgent · 暂无已保存的历史会话。")
        return
    click.echo("JobAgent · 历史会话（最近优先）：")
    for index, session in enumerate(sessions, start=1):
        click.echo(
            f"  {index}. {session.session_id} "
            f"({session.message_count} 条消息, {session.last_used_at})"
        )
    click.echo("\n用 `jobagent chat --session-id <会话 ID>` 恢复某个会话。")


async def _chat(
    startup_config: Path | None, session_id: str | None, one_shot: str | None = None
) -> None:
    """Run an interactive terminal conversation; Tools perform the workflows."""

    from jobagent.agent import build_job_agent
    from jobagent.profile import SQLiteCandidateContextProvider, load_candidate_context

    settings = get_settings()
    log_path = settings.jobagent_log_file.expanduser().resolve()
    setup_logging(console_level="INFO" if settings.jobagent_debug_trace else None)
    click.echo(f"JobAgent · 日志文件：{log_path}")

    context = load_candidate_context(startup_config) if startup_config else None
    agent = build_job_agent(settings, candidate_context=context, platform_hint="cli")
    active_session_id = session_id or _new_session_id()
    try:
        if one_shot is not None:
            # Non-interactive mode (pi-style -c): one message, one reply, exit.
            # External writes pause for approval - reject them safely here.
            await _render_streaming_reply(
                agent, one_shot, active_session_id, interactive=False
            )
            return
        click.echo(
            "JobAgent ready. Describe a target job or ask for help. "
            "Type /sessions to switch conversations, /status for job progress, "
            "or /exit to quit."
        )
        click.echo(f"JobAgent · 当前会话：{active_session_id}")
        await _render_restored_history(agent, active_session_id)
        stored_context = SQLiteCandidateContextProvider(settings.jobagent_state_db).load()
        _render_candidate_setup_prompt(stored_context)
        _render_cookie_health_warnings()
        while True:
            try:
                message = click.prompt("You", prompt_suffix="> ")
            except (EOFError, KeyboardInterrupt):
                click.echo()
                return
            command = message.strip().lower()
            if command in {"/exit", "/quit"}:
                return
            if command == "/sessions":
                active_session_id = await _choose_session(agent, active_session_id)
                continue
            if command == "/status" or command.startswith("/status "):
                await _render_opportunity_status(agent, command)
                continue
            try:
                await _render_streaming_reply(agent, message, active_session_id)
            except Exception:
                logger.exception("JobAgent turn failed")
                if settings.jobagent_debug_trace:
                    click.echo(click.style(traceback.format_exc(), fg="yellow"))
                click.echo(
                    click.style(
                        "JobAgent> 本轮处理失败，但会话仍然可用；可以继续输入或稍后重试。",
                        fg="red",
                    )
                )
    finally:
        click.echo()
        click.echo(
            click.style("会话已保存: ", fg="cyan") + active_session_id
        )
        click.echo(
            click.style("下次继续: ", fg="cyan")
            + f"jobagent chat --session-id {active_session_id}"
        )
        await agent.close()


def _render_cookie_health_warnings() -> None:
    """Warn about locally expired provider cookies without blocking chat startup."""

    labels = {"boss": "Boss", "xhs": "小红书", "linkedin": "LinkedIn"}
    try:
        statuses = inspect_cookie_providers()
    except Exception:
        logger.warning("Cookie provider inspection failed", exc_info=True)
        return
    for status in statuses:
        if not status.matched or status.expired_count == 0:
            continue
        key_hint = "，其中包含关键登录 Cookie" if status.key_cookie_expired else ""
        label = labels.get(status.platform, status.platform)
        click.echo(
            f"JobAgent · {label} Cookie 中有 {status.expired_count} 项已过期{key_hint}；"
            "请更新 Cookie。该提醒不会阻止继续使用其他功能。"
        )


def _render_candidate_setup_prompt(context: object | None) -> None:
    """Ask only for global candidate inputs that are still missing."""

    has_candidate_facts = bool(
        context is not None
        and (
            getattr(context, "resume_text", None)
            or getattr(context, "background", None)
        )
    )
    has_search_profile = bool(
        context is not None and getattr(context, "search_profile", None)
    )
    if has_candidate_facts and has_search_profile:
        click.echo("JobAgent · 候选人资料与求职意向已就绪，可以分析 JD 或开始找岗。")
        return
    if not has_candidate_facts and not has_search_profile:
        click.echo(
            "JobAgent> 开始前请先提供基础简历，以及期望岗位、城市、薪资、公司特征、"
            "职位特征、行业偏好和不能接受的条件；也可以直接提供一个目标 JD 作为本次求职旅程"
            "的起点。"
        )
        return
    if not has_candidate_facts:
        click.echo(
            "JobAgent> 求职意向已经保存。请再提供基础简历，之后我才能可靠计算岗位匹配度。"
        )
        return
    click.echo(
        "JobAgent> 候选人资料已经保存。请补充期望岗位、城市、薪资、公司特征、职位特征、"
        "行业偏好和不能接受的条件；也可以直接提供一个目标 JD。"
    )


def _new_session_id() -> str:
    """Create a readable unique conversation session ID."""

    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"session-{timestamp}-{uuid4().hex[:8]}"


async def _choose_session(agent: StreamingJobAgent, current_session_id: str) -> str:
    """List saved sessions and switch by displayed number or exact ID."""

    sessions = await agent.list_sessions()
    if not sessions:
        click.echo("JobAgent · 暂无已保存的历史会话。")
        return current_session_id
    click.echo("JobAgent · 历史会话（最近优先）：")
    for index, session in enumerate(sessions, start=1):
        session_id = str(getattr(session, "session_id", ""))
        msg_count = int(getattr(session, "message_count", 0))
        last_used = str(getattr(session, "last_used_at", ""))
        marker = " [当前]" if session_id == current_session_id else ""
        click.echo(f"  {index}. {session_id} ({msg_count} 条消息, {last_used}){marker}")
    try:
        selection = click.prompt(
            "选择序号或输入会话 ID（直接回车取消）",
            default="",
            show_default=False,
        ).strip()
    except (EOFError, KeyboardInterrupt):
        click.echo()
        return current_session_id
    if not selection:
        return current_session_id
    selected_id = ""
    if selection.isdigit():
        index = int(selection) - 1
        if 0 <= index < len(sessions):
            selected_id = str(getattr(sessions[index], "session_id", ""))
    else:
        selected_id = next(
            (
                str(getattr(session, "session_id", ""))
                for session in sessions
                if str(getattr(session, "session_id", "")) == selection
            ),
            "",
        )
    if not selected_id:
        click.echo("JobAgent · 无效的会话 ID，继续使用当前会话。")
        return current_session_id
    if selected_id == current_session_id:
        click.echo(f"JobAgent · 已在当前会话：{selected_id}")
        return current_session_id
    click.echo(f"JobAgent · 已切换会话：{selected_id}")
    await _render_restored_history(agent, selected_id)
    return selected_id


async def _render_opportunity_status(
    agent: StreamingJobAgent,
    command: str,
) -> None:
    """Render all-time, weekly, or monthly Opportunity status from local artifacts."""

    requested = command.removeprefix("/status").strip() or "all"
    aliases = {
        "all": StatusPeriod.ALL,
        "week": StatusPeriod.WEEK,
        "month": StatusPeriod.MONTH,
        "总体": StatusPeriod.ALL,
        "本周": StatusPeriod.WEEK,
        "本月": StatusPeriod.MONTH,
    }
    period = aliases.get(requested)
    if period is None:
        click.echo("JobAgent · 用法：/status [all|week|month]")
        return
    board = await agent.opportunity_status(period)
    title = {
        StatusPeriod.ALL: "总体状态",
        StatusPeriod.WEEK: "本周状态",
        StatusPeriod.MONTH: "本月状态",
    }[period]
    click.echo(
        f"JobAgent · {title}：已分析 {board.analyzed} · 适合 {board.suitable} · "
        f"待确认 {board.uncertain} · 不适合 {board.unsuitable} · 已投递 {board.applied}"
    )
    for item in board.items:
        click.echo(
            f"  - {item.company} · {item.role} | {item.fit.value} | "
            f"{item.application_state.value} | 分析 v{item.analysis_version}"
        )


async def _render_restored_history(agent: StreamingJobAgent, session_id: str) -> None:
    """Show enough restored context for users to recognize a durable session."""

    click.echo(f"JobAgent · 正在加载会话：{session_id}")
    history = await agent.resume_session(session_id)
    summary = getattr(history, "summary", None)
    recent = tuple(getattr(history, "recent", ()))
    if not summary and not recent:
        click.echo("JobAgent · 未找到历史记录，将创建新会话。")
        return
    click.echo(f"JobAgent · 已恢复历史会话：{session_id}")
    if summary:
        click.echo(f"Earlier summary · {summary}")
    for entry in recent:
        role = getattr(entry, "role", "")
        text = str(getattr(entry, "text", ""))
        label = "You" if role == "user" else "JobAgent"
        click.echo(f"{label} · {text}")


async def _render_streaming_reply(
    agent: StreamingJobAgent,
    message: str,
    session_id: str,
    *,
    interactive: bool = True,
) -> None:
    """Render thinking and tool progress transiently, then the final answer.

    Reasoning deltas and tool summaries stream into a transient typewriter
    region (erased once the formal answer begins, like pi/Claude Code);
    answer tokens keep printing durably so the transcript stays intact.
    HITL interrupts pause for an explicit approve/reject prompt
    (non-interactive one-shot mode rejects safely).
    """

    transcript = TypewriterTranscript(stream=click.get_text_stream("stdout"))
    answer_line_open = False
    emitted_answer = False
    reply_stream = agent.stream_reply(message, session_id=session_id)
    try:
        async for event in reply_stream:
            kind = getattr(event, "kind", "")
            text = str(getattr(event, "text", ""))
            if kind == "interrupt" and text:
                transcript.close()
                await _handle_hitl_interrupt(
                    agent,
                    text,
                    session_id,
                    interactive=interactive,
                )
                return
            if kind == "status" and text:
                if answer_line_open:
                    click.echo()
                    answer_line_open = False
                # Mid-answer tool activity re-activates the animated spinner;
                # it is erased again when the answer continues.
                transcript.update_status(text)
            elif kind == "thinking" and text:
                transcript.show_thinking(text)
            elif kind == "tool" and text:
                transcript.show_tool(text)
            elif kind == "token" and text:
                emitted_answer = True
                transcript.begin_answer()
                if not answer_line_open:
                    click.echo("JobAgent> ", nl=False)
                    answer_line_open = True
                click.echo(text, nl=False)
    finally:
        await _close_async_stream(reply_stream)
        transcript.close()
    if answer_line_open:
        click.echo()
    elif not emitted_answer:
        click.echo(
            "JobAgent> 工具已完成，但模型没有生成最终回答；"
            "本轮没有丢失资料，请重试或继续追问。"
        )


async def _handle_hitl_interrupt(
    agent: StreamingJobAgent,
    payload_json: str,
    session_id: str,
    *,
    interactive: bool,
) -> None:
    """Show the paused tool call and resume with the human's decision."""

    try:
        request = json.loads(payload_json)
    except json.JSONDecodeError:
        request = {"actions": [{"name": "unknown", "args": {}}]}
    actions = _hitl_actions(request)
    click.echo()
    if not interactive:
        click.echo(_format_hitl_review(request))
        click.echo(
            click.style(
                "非交互模式：已安全拒绝该操作。请在交互模式（jobagent chat）中执行。",
                fg="red",
            )
        )
        resume_stream = agent.resume_reply(
            False,
            session_id=session_id,
            reject_reason="非交互模式自动拒绝：请用户在交互会话中确认。",
        )
        try:
            async for event in resume_stream:
                text = str(getattr(event, "text", ""))
                if getattr(event, "kind", "") == "token" and text:
                    click.echo(text, nl=False)
        finally:
            await _close_async_stream(resume_stream)
        click.echo()
        return
    decisions: list[bool] = []
    try:
        for index, action in enumerate(actions, start=1):
            click.echo()
            click.echo(_format_hitl_action(action, index))
            decisions.append(
                click.confirm(
                    click.style(
                        "确认批准上面这项操作？",
                        fg="yellow",
                    ),
                    default=False,
                )
            )
    except (EOFError, KeyboardInterrupt):
        decisions.extend([False] * (len(actions) - len(decisions)))
    if not decisions:
        decisions = [False]
    resume_decision: bool | Sequence[bool] = (
        decisions[0] if len(decisions) == 1 else decisions
    )
    answer_line_open = False
    resume_stream = agent.resume_reply(resume_decision, session_id=session_id)
    try:
        async for event in resume_stream:
            kind = getattr(event, "kind", "")
            text = str(getattr(event, "text", ""))
            if kind == "token" and text:
                if not answer_line_open:
                    click.echo("JobAgent> ", nl=False)
                    answer_line_open = True
                click.echo(text, nl=False)
            elif kind == "interrupt" and text:
                await _handle_hitl_interrupt(
                    agent,
                    text,
                    session_id,
                    interactive=interactive,
                )
                return
    finally:
        await _close_async_stream(resume_stream)
    if answer_line_open:
        click.echo()


async def _close_async_stream(stream: AsyncIterator[object]) -> None:
    """Close an interrupted async stream in the current asyncio context."""

    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # pragma: no cover - defensive cleanup boundary
        logger.debug("Failed to close Agent stream cleanly", exc_info=True)


def _hitl_actions(request: dict[str, object]) -> list[dict[str, object]]:
    """Read HITL actions from current and older LangChain payload shapes."""

    raw = request.get("action_requests") or request.get("actions") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _redact_hitl_value(value: object) -> object:
    sensitive = ("cookie", "token", "password", "secret", "authorization")
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if any(term in str(key).lower() for term in sensitive)
            else _redact_hitl_value(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_hitl_value(child) for child in value]
    return value


def _format_hitl_review(request: dict[str, object]) -> str:
    """Render an actionable, one-to-one HITL review summary."""

    actions = _hitl_actions(request)
    lines = ["⏸ 需要人工批准的操作（逐项确认）："]
    if not actions:
        lines.append("  未能解析具体操作，默认拒绝。")
        return "\n".join(lines)
    for index, action in enumerate(actions, start=1):
        lines.append(_format_hitl_action(action, index))
    lines.append("\n每一项都会单独询问，输入 y 才批准该项。")
    return "\n".join(lines)


def _format_hitl_action(action: dict[str, object], index: int) -> str:
    """Render one tool call immediately before its approval prompt."""

    lines = [f"[{index}] {action.get('name', '未知工具')}（工具）"]
    description = action.get("description")
    if description:
        lines.append(f"    说明：{description}")
    args = _redact_hitl_value(action.get("args", {}))
    details = json.dumps(args, ensure_ascii=False, indent=2, default=str)
    lines.append("    执行参数：")
    lines.extend(f"    {line}" for line in details[:5000].splitlines())
    return "\n".join(lines)


@main.command("xhs-download", hidden=True)
@click.argument("url")
@click.option(
    "--output",
    "output_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Download root; defaults to XHS_DOWNLOAD_DIR.",
)
def xhs_download_command(url: str, output_dir: Path | None) -> None:
    """Download one XHS note body and all images through Spider_XHS."""

    asyncio.run(_xhs_download(url=url, output_dir=output_dir))


async def _xhs_download(url: str, output_dir: Path | None) -> None:
    """Internal XHS detail and image download workflow."""

    settings = get_settings()
    async with SpiderXhsBackend(settings) as backend:
        result = await backend.download_note(url, output_dir=output_dir)

    click.echo(f"Downloaded XHS note: {result.note.note_id}")
    click.echo(f"Title: {result.note.title}")
    click.echo(f"Body: {result.body_path}")
    click.echo(f"Images: {len(result.images)}")
    click.echo(f"Directory: {result.directory}")


@main.command("xhs-search", hidden=True)
@click.option("--company", default=None, help="Target company, e.g. 字节跳动.")
@click.option("--role", default=None, help="Target role, e.g. 后端开发.")
@click.option("--city", default=None, help="Target city, e.g. 北京.")
@click.option("--keyword", default=None, help="Additional search terms.")
@click.option(
    "--user-id",
    default=None,
    help="List posts from one XHS user instead of keyword search.",
)
@click.option(
    "--days",
    type=click.IntRange(min=1),
    default=None,
    help="Only keep posts from the latest N days; defaults to settings.",
)
@click.option("--limit", type=click.IntRange(min=1, max=100), default=10, show_default=True)
@click.option(
    "--download",
    "download_limit",
    type=click.IntRange(min=0),
    default=5,
    show_default=True,
    help="Download this many accepted posts; use 0 for discovery only.",
)
@click.option(
    "--output",
    "output_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Download root; defaults to XHS_DOWNLOAD_DIR.",
)
def xhs_search_command(
    company: str | None,
    role: str | None,
    city: str | None,
    keyword: str | None,
    user_id: str | None,
    days: int | None,
    limit: int,
    download_limit: int,
    output_dir: Path | None,
) -> None:
    """Find recent XHS interview posts and filter obvious material sellers."""

    try:
        asyncio.run(
            _xhs_search(
                company=company,
                role=role,
                city=city,
                keyword=keyword,
                user_id=user_id,
                days=days,
                limit=limit,
                download_limit=download_limit,
                output_dir=output_dir,
            )
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


async def _xhs_search(
    *,
    company: str | None,
    role: str | None,
    city: str | None,
    keyword: str | None,
    user_id: str | None,
    days: int | None,
    limit: int,
    download_limit: int,
    output_dir: Path | None,
) -> None:
    """Internal XHS interview-post discovery workflow."""

    settings = get_settings()
    request = XhsDiscoveryRequest(
        company=company,
        role=role,
        city=city,
        keyword=keyword,
        user_id=user_id,
        days=days or settings.xhs_referral_stale_days,
        limit=limit,
        download_limit=download_limit,
        output_dir=output_dir,
    )
    async with SpiderXhsBackend(settings) as backend:
        result = await discover_xhs_notes(backend, request)

    source = f"user:{user_id}" if user_id else result.query
    click.echo(f"XHS source: {source}")
    click.echo(
        f"Evaluated={len(result.candidates)} Accepted={len(result.accepted)} "
        f"Rejected={len(result.rejected)} Downloaded={len(result.downloads)}"
    )
    for candidate in result.candidates:
        decision = "ACCEPT" if candidate.accepted else "REJECT"
        reasons: list[str] = []
        if not candidate.is_fresh:
            reasons.append("old-or-unknown-date")
        if candidate.seller_risk:
            reasons.append("seller:" + ",".join(candidate.risk_flags))
        suffix = f" ({'; '.join(reasons)})" if reasons else ""
        click.echo(
            f"[{decision}] {candidate.note.published_at or 'unknown-date'} "
            f"{candidate.note.author_name} | {candidate.note.title}{suffix}"
        )
        click.echo(f"  {candidate.note.url}")
    for downloaded in result.downloads:
        click.echo(f"Downloaded: {downloaded.directory}")


async def _login(platforms: list[str], timeout: int, check_only: bool) -> None:
    """Internal async login workflow."""
    for plat in platforms:
        if check_only:
            age = get_cookie_age_hours(plat)
            if age is not None:
                click.echo(f"[{plat}] Cookie file age: {age:.1f} hours")
            else:
                click.echo(f"[{plat}] No saved cookies found.")
                continue

            click.echo(f"[{plat}] Validating cookies...")
            valid = await cookies_valid(plat)
            if valid:
                click.echo(click.style(f"[{plat}] ✅ Cookies are valid!", fg="green"))
            else:
                click.echo(
                    click.style(
                    f"[{plat}] ❌ Cookies expired or invalid. "
                    f"Run: jobagent login --platform {plat}",
                        fg="red",
                    )
                )
            continue

        # Interactive login
        click.echo(f"[{plat}] Opening browser for login (timeout={timeout}m)...")
        click.echo("Please log in manually. The browser will close automatically on success.")
        try:
            key_cookies = await interactive_login(plat, timeout_minutes=timeout)
            click.echo(click.style(f"[{plat}] ✅ Login successful!", fg="green"))
            click.echo(f"  Key cookies saved: {', '.join(key_cookies.keys())}")
            age = get_cookie_age_hours(plat)
            if age is not None:
                click.echo(f"  Cookie file age: {age:.1f} hours")
        except TimeoutError as e:
            click.echo(click.style(f"[{plat}] ❌ {e}", fg="red"))
        except Exception as e:
            click.echo(click.style(f"[{plat}] ❌ Login failed: {e}", fg="red"))


@main.command("boss-circuit")
@click.argument("action", type=click.Choice(["status", "reset"]), default="status")
def boss_circuit_command(action: str) -> None:
    """Show or reset the Boss circuit breaker (anti-bot cooldown)."""

    from datetime import datetime

    from jobagent.applier.boss_circuit import BossCircuit, boss_circuit_path
    from jobagent.config import get_settings

    circuit = BossCircuit(boss_circuit_path(get_settings().jobagent_state_db))
    if action == "reset":
        circuit._save({})
        click.echo(click.style("Boss circuit reset.", fg="green"))
        return
    refusal = circuit.check()
    if refusal is None:
        state = circuit._load()
        failures = state.get("failures", 0)
        click.echo(f"Boss circuit: closed ({failures} consecutive failure(s)).")
    else:
        click.echo(
            click.style(
                f"Boss circuit: OPEN - {refusal['message']}",
                fg="yellow",
            )
        )
        until = datetime.fromisoformat(refusal["retry_after"])
        remaining = until - datetime.now().astimezone()
        mins = max(0, int(remaining.total_seconds() // 60))
        click.echo(f"  resets automatically in ~{mins} min (or: jobagent boss-circuit reset)")


async def _scrape(platform: str, query: str, location: str | None, limit: int) -> None:
    """Internal async scrape workflow."""

    settings = get_settings()
    scrapers: list[BaseScraper] = []
    if platform in {"linkedin", "all"}:
        scrapers.append(LinkedInScraper(settings))

    if not scrapers:
        click.echo("No scraper configured for requested platform.")
        return

    total_jobs = 0
    for scraper in scrapers:
        async with scraper:
            jobs = await scraper.scrape_jobs(query=query, location=location, limit=limit)
            total_jobs += len(jobs)
            click.echo(f"{scraper.source.value}: scraped {len(jobs)} job(s)")

    click.echo(f"Total scraped jobs: {total_jobs} | Headless={settings.jobagent_headless}")


async def _run_pipeline(
    platform: str,
    query: str,
    location: str | None,
    profile_path: Path,
    limit: int,
    apply_enabled: bool,
) -> None:
    """Internal async end-to-end workflow."""

    # Keep optional/provider-specific LLM imports out of unrelated commands
    # such as login and xhs-download.
    from jobagent.matcher.llm_matcher import LLMMatcher

    settings = get_settings()
    profile = load_profile(profile_path)
    matcher = LLMMatcher(settings=settings)

    scrapers: list[BaseScraper] = []
    if platform in {"linkedin", "all"}:
        scrapers.append(LinkedInScraper(settings))

    jobs: list[Job] = []
    for scraper in scrapers:
        async with scraper:
            jobs.extend(await scraper.scrape_jobs(query=query, location=location, limit=limit))

    if not jobs:
        click.echo("No jobs found.")
        return

    matches = await matcher.batch_match(jobs=jobs, profile=profile)
    eval_failed = sum(1 for m in matches if m.evaluation_failed)
    if eval_failed:
        click.echo(
            f"Warning: {eval_failed} match evaluation(s) failed; their score "
            "0.0 means unknown, not no-match. Re-run to retry them."
        )
    ranked_matches = sorted(matches, key=lambda match: match.score, reverse=True)

    top_matches = ranked_matches[: min(5, len(ranked_matches))]
    click.echo(f"Top matches for {profile.name}:")
    for match in top_matches:
        click.echo(f"- {match.job_id}: score={match.score:.2f}")

    def _score_of(job: Job) -> float:
        return next((item.score for item in top_matches if item.job_id == job.id), 0.0)

    candidates = [job for job in jobs if _score_of(job) >= 0.75]

    applications = []
    if not apply_enabled:
        # HITL: application submission is an external write and must never run
        # unsupervised from a pipeline command.
        if candidates:
            click.echo(
                f"{len(candidates)} job(s) scored >= 0.75; application step skipped. "
                "Re-run with --apply to confirm and send each one."
            )
    else:
        # One browser session for the whole batch (not one per job), and an
        # explicit interactive confirmation before every external submission.
        async with AsyncExitStack() as stack:
            boss_applier = (
                await stack.enter_async_context(
                    BossApplier(settings, crawl_gate=build_crawl_gate(settings))
                )
                if any(job.source is JobSource.BOSS for job in candidates)
                else None
            )
            linkedin_applier = (
                await stack.enter_async_context(LinkedInApplier(settings))
                if any(job.source is JobSource.LINKEDIN for job in candidates)
                else None
            )
            for job in candidates:
                confirmed = click.confirm(
                    f"Send application/greeting to '{job.title}' @ {job.company} "
                    f"(score {_score_of(job):.2f})?",
                    default=False,
                )
                if not confirmed:
                    continue
                if job.source is JobSource.BOSS and boss_applier is not None:
                    applications.append(
                        await boss_applier.apply(job=job, profile=profile)
                    )
                elif job.source is JobSource.LINKEDIN and linkedin_applier is not None:
                    applications.append(
                        await linkedin_applier.apply(job=job, profile=profile)
                    )

    click.echo(f"Applications attempted: {len(applications)}")


    await _notify_summary(
        settings=settings,
        profile_name=profile.name,
        total_jobs=len(jobs),
        total_matches=len(top_matches),
        total_applications=len(applications),
    )


async def _notify_summary(
    *,
    settings: Settings,
    profile_name: str,
    total_jobs: int,
    total_matches: int,
    total_applications: int,
) -> None:
    """Send summary notifications to configured channels."""

    summary = (
        f"JobAgent run complete for {profile_name}. "
        f"Jobs={total_jobs}, TopMatches={total_matches}, Applications={total_applications}."
    )

    telegram_token = settings.telegram_bot_token
    telegram_chat = settings.telegram_chat_id
    if telegram_token and telegram_chat:
        telegram = TelegramNotifier(
            bot_token=telegram_token,
            chat_id=telegram_chat,
        )
        await telegram.send_text(summary)

    discord_webhook = settings.discord_webhook_url
    if discord_webhook:
        discord = DiscordNotifier(webhook_url=discord_webhook)
        await discord.send_text(summary)


if __name__ == "__main__":
    main()
