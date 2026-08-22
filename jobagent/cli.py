"""Command-line interface for JobAgent workflows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
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
)
from jobagent.config import Settings, get_settings
from jobagent.models import Job, JobSource
from jobagent.notifier.discord import DiscordNotifier
from jobagent.notifier.telegram import TelegramNotifier
from jobagent.profile.loader import load_profile
from jobagent.scraper.base import BaseScraper
from jobagent.scraper.boss import BossScraper
from jobagent.scraper.linkedin import LinkedInScraper
from jobagent.scraper.xhs_backend import SpiderXhsBackend
from jobagent.scraper.xhs_discovery import XhsDiscoveryRequest, discover_xhs_notes

logger = logging.getLogger(__name__)


class StreamingJobAgent(Protocol):
    async def resume_thread(self, thread_id: str) -> object: ...

    async def list_sessions(self, *, limit: int = 50) -> tuple[object, ...]: ...

    async def opportunity_status(self, period: StatusPeriod) -> OpportunityStatusBoard: ...

    def stream_reply(
        self,
        message: str,
        *,
        thread_id: str,
    ) -> AsyncIterator[object]: ...

    async def close(self) -> None: ...


@click.group(help="JobAgent: AI-powered job hunting agent.")
def main() -> None:
    """Root CLI group."""


@main.command("validate-profile")
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
@click.option("--limit", default=20, show_default=True, type=int)
def run_command(
    platform: str,
    query: str,
    location: str | None,
    profile_path: Path,
    limit: int,
) -> None:
    """Run end-to-end pipeline: scrape -> match -> notify."""

    asyncio.run(
        _run_pipeline(
            platform=platform,
            query=query,
            location=location,
            profile_path=profile_path,
            limit=limit,
        )
    )


@main.command("login")
@click.option(
    "--platform",
    type=click.Choice(["boss", "linkedin", "xhs", "all"]),
    default="boss",
    show_default=True,
    help="Platform to log in to.",
)
@click.option("--timeout", type=int, default=5, show_default=True, help="Login timeout in minutes.")
@click.option("--check", is_flag=True, help="Only check if existing cookies are valid.")
def login_command(platform: str, timeout: int, check: bool) -> None:
    """Interactive browser login to save cookies."""
    platforms = list(PLATFORM_CONFIG.keys()) if platform == "all" else [platform]
    asyncio.run(_login(platforms=platforms, timeout=timeout, check_only=check))


@main.command("chat")
@click.option(
    "--config",
    "startup_config",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=False,
    help="Optional YAML import for search profile and resume/background.",
)
@click.option("--thread-id", default=None, help="Existing session ID; omitted creates one.")
def chat_command(startup_config: Path | None, thread_id: str | None) -> None:
    """Start a conversational JobAgent session."""

    asyncio.run(_chat(startup_config=startup_config, thread_id=thread_id))


async def _chat(startup_config: Path | None, thread_id: str | None) -> None:
    """Run an interactive terminal conversation; Tools perform the workflows."""

    from jobagent.agent import build_job_agent
    from jobagent.profile import SQLiteCandidateContextProvider, load_candidate_context

    settings = get_settings()
    context = load_candidate_context(startup_config) if startup_config else None
    agent = build_job_agent(settings, candidate_context=context)
    active_thread_id = thread_id or _new_session_id()
    try:
        click.echo(
            "JobAgent ready. Describe a target job or ask for help. "
            "Type /sessions to switch conversations, /status for job progress, "
            "or /exit to quit."
        )
        click.echo(f"JobAgent · 当前会话：{active_thread_id}")
        await _render_restored_history(agent, active_thread_id)
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
                active_thread_id = await _choose_session(agent, active_thread_id)
                continue
            if command == "/status" or command.startswith("/status "):
                await _render_opportunity_status(agent, command)
                continue
            try:
                await _render_streaming_reply(agent, message, active_thread_id)
            except Exception:
                logger.exception("JobAgent turn failed")
                click.echo(
                    click.style(
                        "JobAgent> 本轮处理失败，但会话仍然可用；可以继续输入或稍后重试。",
                        fg="red",
                    )
                )
    finally:
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


async def _choose_session(agent: StreamingJobAgent, current_thread_id: str) -> str:
    """List saved sessions and switch by displayed number or exact ID."""

    sessions = await agent.list_sessions()
    if not sessions:
        click.echo("JobAgent · 暂无已保存的历史会话。")
        return current_thread_id
    click.echo("JobAgent · 历史会话（最近优先）：")
    for index, session in enumerate(sessions, start=1):
        thread_id = str(getattr(session, "thread_id", ""))
        checkpoint_count = int(getattr(session, "checkpoint_count", 0))
        marker = " [当前]" if thread_id == current_thread_id else ""
        click.echo(f"  {index}. {thread_id} ({checkpoint_count} checkpoints){marker}")
    try:
        selection = click.prompt(
            "选择序号或输入 session ID（直接回车取消）",
            default="",
            show_default=False,
        ).strip()
    except (EOFError, KeyboardInterrupt):
        click.echo()
        return current_thread_id
    if not selection:
        return current_thread_id
    selected_id = ""
    if selection.isdigit():
        index = int(selection) - 1
        if 0 <= index < len(sessions):
            selected_id = str(getattr(sessions[index], "thread_id", ""))
    else:
        selected_id = next(
            (
                str(getattr(session, "thread_id", ""))
                for session in sessions
                if str(getattr(session, "thread_id", "")) == selection
            ),
            "",
        )
    if not selected_id:
        click.echo("JobAgent · 无效的 session，继续使用当前会话。")
        return current_thread_id
    if selected_id == current_thread_id:
        click.echo(f"JobAgent · 已在当前会话：{selected_id}")
        return current_thread_id
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


async def _render_restored_history(agent: StreamingJobAgent, thread_id: str) -> None:
    """Show enough restored context for users to recognize a durable thread."""

    click.echo(f"JobAgent · 正在加载会话：{thread_id}")
    history = await agent.resume_thread(thread_id)
    summary = getattr(history, "summary", None)
    recent = tuple(getattr(history, "recent", ()))
    if not summary and not recent:
        click.echo("JobAgent · 未找到历史记录，将创建新会话。")
        return
    click.echo(f"JobAgent · 已恢复历史会话：{thread_id}")
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
    thread_id: str,
) -> None:
    """Render safe Agent events while preserving token-level output."""

    answer_line_open = False
    emitted_answer = False
    async for event in agent.stream_reply(message, thread_id=thread_id):
        kind = getattr(event, "kind", "")
        text = str(getattr(event, "text", ""))
        if kind == "status" and text:
            if answer_line_open:
                click.echo()
                answer_line_open = False
            click.echo(f"JobAgent · {text}")
        elif kind == "token" and text:
            emitted_answer = True
            if not answer_line_open:
                click.echo("JobAgent> ", nl=False)
                answer_line_open = True
            click.echo(text, nl=False)
    if answer_line_open:
        click.echo()
    elif not emitted_answer:
        click.echo(
            "JobAgent> 工具已完成，但模型没有生成最终回答；"
            "本轮没有丢失资料，请重试或继续追问。"
        )


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


async def _scrape(platform: str, query: str, location: str | None, limit: int) -> None:
    """Internal async scrape workflow."""

    settings = get_settings()
    scrapers: list[BaseScraper] = []
    if platform in {"boss", "all"}:
        scrapers.append(BossScraper(settings))
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
) -> None:
    """Internal async end-to-end workflow."""

    # Keep optional/provider-specific LLM imports out of unrelated commands
    # such as login and xhs-download.
    from jobagent.matcher.llm_matcher import LLMMatcher

    settings = get_settings()
    profile = load_profile(profile_path)
    matcher = LLMMatcher(settings=settings)

    scrapers: list[BaseScraper] = []
    if platform in {"boss", "all"}:
        scrapers.append(BossScraper(settings))
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
    ranked_matches = sorted(matches, key=lambda match: match.score, reverse=True)

    top_matches = ranked_matches[: min(5, len(ranked_matches))]
    click.echo(f"Top matches for {profile.name}:")
    for match in top_matches:
        click.echo(f"- {match.job_id}: score={match.score:.2f}")

    boss_applier = BossApplier(settings)
    linkedin_applier = LinkedInApplier(settings)

    applications = []
    for job in jobs:
        score = next((item.score for item in top_matches if item.job_id == job.id), 0.0)
        if score < 0.75:
            continue

        if job.source == JobSource.BOSS:
            async with boss_applier:
                applications.append(await boss_applier.apply(job=job, profile=profile))
        elif job.source == JobSource.LINKEDIN:
            async with linkedin_applier:
                applications.append(await linkedin_applier.apply(job=job, profile=profile))

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
