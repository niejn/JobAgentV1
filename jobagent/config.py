"""Application configuration loaded from environment variables."""

import ipaddress
import os
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the JobAgent agent."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    jobagent_env: str = Field(default="development")
    jobagent_log_level: str = Field(default="INFO")
    jobagent_log_file: Path = Field(
        default=Path("data/logs/jobagent.log"),
        description="Rotating loguru file sink target (10 MB x 3 backups).",
    )
    jobagent_debug_trace: bool = Field(
        default=False,
        description="Show sanitized Agent timing, Tool, OCR, and graph diagnostics in chat.",
    )
    jobagent_headless: bool = Field(
        default=True,
        description=(
            "Headless mode for self-launched browser flows (LinkedIn applier). "
            "Boss flows attach to the user's real Chrome via CDP, where "
            "headless does not apply."
        ),
    )
    jobagent_max_jobs: int = Field(default=30, ge=1, le=500)
    jobagent_request_timeout: int = Field(default=30, ge=5, le=300)
    jobagent_workspace_root: Path = Field(
        default=Path("data/share"),
        description=(
            "Safe workspace for explicitly named user documents such as resumes and JDs."
        ),
    )

    openai_api_key: str | None = None
    openai_base_url: str = Field(default="https://api.openai.com/v1")
    jobagent_llm_provider: str = Field(
        default="openai-compatible",
        description="LLM backend; first release supports openai-compatible only.",
    )
    jobagent_llm_model: str = Field(default="gpt-4o-mini")
    jobagent_llm_fallback_model: str = Field(
        default="",
        description=(
            "Backup model name (e.g. deepseek-chat) used when the primary "
            "fails with provider-level errors (429/403/401/5xx/connection). "
            "Empty disables fallback."
        ),
    )
    jobagent_llm_fallback_base_url: str = Field(
        default="",
        description="Backup provider base URL; empty reuses the primary base_url.",
    )
    jobagent_llm_fallback_api_key: str | None = Field(
        default=None,
        description="Backup provider API key; empty reuses the primary key.",
    )
    jobagent_llm_fallback_max_tokens: int = Field(
        default=8_192,
        ge=1,
        description="Backup model max_completion_tokens (defaults fit deepseek).",
    )
    jobagent_llm_context_window: int = Field(default=128_000, ge=1)
    jobagent_llm_max_tokens: int = Field(default=4_096, ge=1)
    jobagent_llm_timeout: int = Field(default=180, ge=10, le=600)
    jobagent_llm_reasoning: bool = Field(default=False)
    jobagent_llm_supports_developer_role: bool = Field(default=False)
    jobagent_llm_thinking_format: str | None = None
    jobagent_llm_thinking_level: str | None = None

    boss_cookie: str | None = None
    boss_cookie_file: Path | None = Field(
        default=None,
        description="Browser-exported Boss Cookie JSON used by direct HTTP/WS adapters.",
    )
    boss_search_transport: Literal["cdp", "http"] = Field(
        default="cdp",
        description=(
            "Boss read-only job search transport. HTTP uses persisted session cookies; "
            "CDP remains the default fallback."
        ),
    )
    boss_contact_transport: Literal["cdp", "http"] = Field(
        default="http",
        description=(
            "Boss HR conversation creation transport. Defaults to HTTP friend/add plus "
            "WebSocket greeting; Boss job reads remain on the separate CDP transport."
        ),
    )
    boss_greeting: str | None = Field(
        default=None,
        description="Greeting template for Boss直聘. Supports {company}, {title}, {name}.",
    )
    boss_apply_delay_min: float = Field(default=3.0, ge=0.5)
    boss_apply_delay_max: float = Field(default=8.0, ge=1.0)
    boss_daily_limit: int = Field(default=100, ge=1, le=150)
    boss_skip_inactive_days: int = Field(default=7, ge=1)
    boss_risk_cooldown_seconds: int = Field(default=900, ge=60, le=86_400)
    boss_max_searches_per_session: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Max Boss searches before the session is forced to rest.",
    )
    linkedin_cookie: str | None = None

    # Email application channel (XHS referral posts with an HR mailbox).
    # QQ/Foxmail defaults: only the authorization code is required.
    jobagent_email_smtp_host: str = Field(
        default="smtp.qq.com",
        description="SMTP host for application emails (QQ/Foxmail default).",
    )
    jobagent_email_smtp_port: int = Field(default=465, ge=1, le=65535)
    jobagent_email_smtp_user: str = Field(
        default="niejn@foxmail.com",
        description="SMTP login (the sending mailbox).",
    )
    jobagent_email_smtp_password: str = Field(
        default="",
        description="16-char SMTP authorization code (NOT the account password).",
    )
    jobagent_email_sender_name: str = Field(default="聂俊能")
    jobagent_email_imap_host: str = Field(
        default="", description="Sent verification host; empty infers imap.* from smtp.*."
    )
    jobagent_email_imap_port: int = Field(default=993, ge=1, le=65535)
    jobagent_email_sent_folder: str = Field(default="Sent")
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
    xhs_api_rate_requests: int = Field(
        default=1,
        ge=1,
        description="Maximum XHS API calls in one configured period.",
    )
    xhs_api_rate_period_seconds: float = Field(default=1.0, gt=0)
    xhs_crawl_rate_requests: int = Field(
        default=1,
        ge=1,
        description=(
            "Shared per-account XHS bucket: api + media + page requests "
            "combined must not exceed this rate (risk control sees the "
            "account aggregate, not the request class)."
        ),
    )
    xhs_crawl_rate_period_seconds: float = Field(default=1.0, gt=0)
    xhs_api_jitter_min_seconds: float = Field(
        default=0.2,
        ge=0.0,
        description="Min random pause after each XHS API permit (fingerprint mitigation).",
    )
    xhs_api_jitter_max_seconds: float = Field(
        default=1.3,
        ge=0.0,
        description="Max random pause after each XHS API permit.",
    )
    xhs_media_rate_requests: int = Field(
        default=1,
        ge=1,
        description="Maximum XHS image downloads in one configured period.",
    )
    xhs_media_rate_period_seconds: float = Field(default=1.0, gt=0)
    xhs_media_jitter_min_seconds: float = Field(
        default=0.5,
        ge=0.0,
        description="Min random pause after each media download permit.",
    )
    xhs_media_jitter_max_seconds: float = Field(
        default=2.5,
        ge=0.0,
        description="Max random pause after each media download permit.",
    )
    boss_crawl_rate_requests: int = Field(
        default=1,
        ge=1,
        description="Per-account Boss bucket: page loads must not exceed this rate.",
    )
    boss_crawl_rate_period_seconds: float = Field(
        default=5.0,
        gt=0,
        description=(
            "Boss bucket period. One page load per five seconds with the "
            "configured reading jitter between permitted requests."
        ),
    )
    boss_crawl_jitter_min_seconds: float = Field(
        default=5.0,
        ge=0.0,
        description="Min random pause after each Boss page load permit (reading time).",
    )
    boss_crawl_jitter_max_seconds: float = Field(
        default=10.0,
        ge=0.0,
        description="Max random pause after each Boss page load permit (reading time).",
    )
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
    xhs_cdp_enabled: bool = Field(
        default=False,
        description=(
            "Enable the read-only Chrome CDP fallback for XHS note/author reads "
            "when Spider_XHS fails with a token/access-control error."
        ),
    )
    debug_chrome_cdp_endpoint: str = Field(
        default="http://127.0.0.1:9222",
        validation_alias=AliasChoices("debug_chrome_cdp_endpoint", "xhs_cdp_endpoint"),
        description=(
            "DevTools endpoint of the shared, already-running, logged-in debug "
            "Chrome used by both XHS and Boss CDP backends. Loopback hosts only. "
            "Legacy env name XHS_CDP_ENDPOINT is accepted for one more version."
        ),
    )
    boss_debug_chrome_executable: Path | None = Field(
        default=None,
        description="Optional Chrome executable for the dedicated visible Boss debug browser.",
    )
    boss_debug_chrome_profile_dir: Path = Field(
        default=Path("data/boss-chrome-small-profile"),
        description="Dedicated persisted Chrome profile used by Boss CDP login and reads.",
    )
    boss_debug_chrome_start_timeout_seconds: int = Field(default=20, ge=5, le=60)
    xhs_cdp_timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Bounded time budget in seconds for one CDP fallback read.",
    )
    jobagent_state_db: Path = Field(default=Path("data/jobagent.db"))
    jobagent_checkpoint_db: Path = Field(default=Path("data/jobagent-checkpoints.db"))
    jobagent_artifact_dir: Path = Field(default=Path("data/journeys"))
    jobagent_skills_dir: Path = Field(
        default=Path("data/skills"),
        description="User-installed documentation Skills directory.",
    )
    jobagent_resume_dir: Path = Field(
        default=Path("data/resumes"),
        description="Controlled directory for user-supplied PDF resume attachments.",
    )
    jobagent_model_capabilities_file: Path = Field(
        default=Path("data/model_capabilities.json"),
        description="Local model capability registry learned from provider responses.",
    )
    jobagent_opportunity_dir: Path = Field(default=Path("data/opportunities"))
    jobagent_ocr_engine: str = Field(default="tesseract")
    tesseract_cmd: Path = Field(default=Path("tesseract"))
    jobagent_ocr_language: str = Field(default="chi_sim+eng")
    jobagent_ocr_psm: int = Field(default=11, ge=3, le=13)
    jobagent_research_max_iterations: int = Field(default=3, ge=1, le=10)
    jobagent_research_queries_per_iteration: int = Field(default=4, ge=1, le=6)
    jobagent_research_results_per_query: int = Field(default=8, ge=1, le=30)
    jobagent_research_minimum_evidence: int = Field(default=3, ge=1, le=20)
    jobagent_research_required_topics: int = Field(default=3, ge=1, le=20)
    jobagent_research_timeout: int = Field(default=300, ge=30, le=1800)
    # 运行预算：deep_agent 每次调用的最大超步数（supersteps）。
    # LangGraph 的隐式默认是 25 —— 约仅 6-12 轮“模型→工具→模型”循环，复合任务
    # （连续调研+对比+写工件）会中途裸崩 GraphRecursionError。
    # 换算参考：deepagents 一轮工具循环 ≈ 2-4 超步，90 超步 ≈ 22-45 轮，
    # 已覆盖绝大多数任务（Hermes 的 90 轮模型调用 ≈ 200+ 超步，无需对齐）。
    # 超限时的收尾行为见 JobAgent.reply_stream 的 GraphRecursionError 分支
    # （无工具纯总结，对应 Hermes _budget_grace_call 思路）。
    jobagent_recursion_limit: int = Field(default=90, ge=1, le=500)

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None

    http_proxy: str | None = None
    https_proxy: str | None = None
    @field_validator("debug_chrome_cdp_endpoint")
    @classmethod
    def validate_loopback_cdp_endpoint(cls, value: str) -> str:
        """Allow only loopback Chrome DevTools endpoints.

        Chrome remote debugging grants full browser control, so the endpoint
        must never point at another host.
        """

        cleaned = value.strip()
        parsed = urlsplit(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                "DEBUG_CHROME_CDP_ENDPOINT must be an http(s) URL such as http://127.0.0.1:9222"
            )
        host = parsed.hostname.lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is None:
            if host != "localhost":
                raise ValueError(
                    "DEBUG_CHROME_CDP_ENDPOINT must be a loopback address "
                    "(127.0.0.1, ::1, or localhost)"
                ) from None
        elif not address.is_loopback:
            raise ValueError(
                "DEBUG_CHROME_CDP_ENDPOINT must be a loopback address "
                "(127.0.0.1, ::1, or localhost)"
            )
        return cleaned

    @model_validator(mode="after")
    def warn_legacy_cdp_endpoint_env(self) -> "Settings":
        """One-version deprecation notice for the old XHS_CDP_ENDPOINT name."""

        if "XHS_CDP_ENDPOINT" in os.environ:
            warnings.warn(
                "XHS_CDP_ENDPOINT is deprecated; rename it to DEBUG_CHROME_CDP_ENDPOINT. "
                "The old name keeps working for one more version.",
                DeprecationWarning,
                stacklevel=2,
            )
        return self

    @model_validator(mode="after")
    def validate_boss_apply_delay_order(self) -> "Settings":
        """Min delay must not exceed max delay."""
        if self.boss_apply_delay_min > self.boss_apply_delay_max:
            raise ValueError(
                "BOSS_APPLY_DELAY_MIN must be <= BOSS_APPLY_DELAY_MAX"
            )
        return self



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()
