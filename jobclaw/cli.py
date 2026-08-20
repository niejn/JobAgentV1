"""Command-line interface for JobClaw workflows."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from jobclaw.applier.boss import BossApplier
from jobclaw.applier.linkedin import LinkedInApplier
from jobclaw.auth.browser_login import (
    PLATFORM_CONFIG,
    cookies_valid,
    get_cookie_age_hours,
    interactive_login,
)
from jobclaw.config import get_settings
from jobclaw.models import JobSource
from jobclaw.notifier.discord import DiscordNotifier
from jobclaw.notifier.telegram import TelegramNotifier
from jobclaw.profile.loader import load_profile
from jobclaw.scraper.boss import BossScraper
from jobclaw.scraper.linkedin import LinkedInScraper
from jobclaw.scraper.xhs_backend import SpiderXhsBackend
from jobclaw.scraper.xhs_discovery import XhsDiscoveryRequest, discover_xhs_notes


@click.group(help="JobClaw: AI-powered job hunting agent.")
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


@main.command("xhs-download")
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


@main.command("xhs-search")
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
                click.echo(click.style(
                    f"[{plat}] ❌ Cookies expired or invalid. Run: jobclaw login --platform {plat}",
                    fg="red",
                ))
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
    scrapers = []
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

    click.echo(f"Total scraped jobs: {total_jobs} | Headless={settings.jobclaw_headless}")


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
    from jobclaw.matcher.llm_matcher import LLMMatcher

    settings = get_settings()
    profile = load_profile(profile_path)
    matcher = LLMMatcher(model_name=settings.jobclaw_llm_model)

    scrapers = []
    if platform in {"boss", "all"}:
        scrapers.append(BossScraper(settings))
    if platform in {"linkedin", "all"}:
        scrapers.append(LinkedInScraper(settings))

    jobs = []
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
    settings: object,
    profile_name: str,
    total_jobs: int,
    total_matches: int,
    total_applications: int,
) -> None:
    """Send summary notifications to configured channels."""

    summary = (
        f"JobClaw run complete for {profile_name}. "
        f"Jobs={total_jobs}, TopMatches={total_matches}, Applications={total_applications}."
    )

    if getattr(settings, "telegram_bot_token", None) and getattr(
        settings, "telegram_chat_id", None
    ):
        telegram = TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        await telegram.send_text(summary)

    if getattr(settings, "discord_webhook_url", None):
        discord = DiscordNotifier(webhook_url=settings.discord_webhook_url)
        await discord.send_text(summary)


if __name__ == "__main__":
    main()
