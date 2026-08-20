"""Application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the JobClaw agent."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    jobclaw_env: str = Field(default="development")
    jobclaw_log_level: str = Field(default="INFO")
    jobclaw_headless: bool = Field(default=True)
    jobclaw_max_jobs: int = Field(default=30, ge=1, le=500)
    jobclaw_request_timeout: int = Field(default=30, ge=5, le=300)

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    jobclaw_llm_model: str = Field(default="gpt-4o-mini")

    # Claude OAuth (Claude Code CLI subscription)
    claude_credentials_path: str | None = Field(
        default=None, alias="CLAUDE_CREDENTIALS_PATH",
    )
    claude_model: str = Field(
        default="claude-sonnet-4-6", alias="CLAUDE_MODEL",
    )

    boss_cookie: str | None = None
    boss_greeting: str | None = Field(
        default=None,
        description="Greeting template for Boss直聘. Supports {company}, {title}, {name}.",
    )
    boss_apply_delay_min: float = Field(default=3.0, ge=0.5)
    boss_apply_delay_max: float = Field(default=8.0, ge=1.0)
    boss_daily_limit: int = Field(default=100, ge=1, le=150)
    boss_skip_inactive_days: int = Field(default=7, ge=1)
    linkedin_cookie: str | None = None

    # Xiaohongshu referral channel (see docs/referral-design.md)
    xhs_cookie: str | None = None
    xhs_cookie_header: str | None = Field(
        default=None,
        description="Complete XHS Cookie header containing at least a1 and web_session.",
    )
    spider_xhs_path: Path = Field(
        default=Path("../xiaohongshu_crawler/Spider_XHS"),
        description="Local Spider_XHS library source directory.",
    )
    xhs_download_dir: Path = Field(
        default=Path("data/xhs"),
        description="Root directory for downloaded XHS note bodies and images.",
    )
    xhs_referral_max_posts: int = Field(default=30, ge=1, le=100)
    xhs_referral_stale_days: int = Field(default=90, ge=1)
    xhs_scrape_delay_min: float = Field(
        default=45.0,
        ge=5.0,
        description="Min seconds between XHS page loads",
    )
    xhs_scrape_delay_max: float = Field(
        default=90.0,
        ge=10.0,
        description="Max seconds between XHS page loads",
    )

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None

    http_proxy: str | None = None
    https_proxy: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()
