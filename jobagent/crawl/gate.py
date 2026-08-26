"""Process-wide crawl gate: one shared token bucket per platform account.

风控看到的是**账号聚合请求率**，不是请求类别。因此桶的身份 = 站点账号
（bucket "xhs" / "boss"；多账号将来是 "xhs:acct2"），同一账号的 api、
media、page 请求全部走同一个桶。请求类别只决定放行后的 jitter 剖面
（页面加载像人要慢，API 调用稍快）。

为什么不用现成库：alexdelorenzo/limiter 的 jitter 只在桶耗尽等待时掺入
（防惊群语义），桶有余量时零等待；我们需要的是**每次放行后**的随机停顿
（防指纹语义，Scrapy RANDOMIZE_DOWNLOAD_DELAY 同类）。pyrate-limiter
只有桶。所以 gate = pyrate 桶 + 每次放行后 uniform 抖动，约百行。

跨工具调用共享是硬需求：工具每次调用 new 一个 backend，若桶挂在 backend
上则每次都是空桶，速率配置在调用间形同虚设。gate 在 build_job_agent
构造一次、随进程存活，所有工具的请求共享同一组桶。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pyrate_limiter import Duration, InMemoryBucket, Limiter, Rate

from jobagent.config import Settings

__all__ = [
    "AsyncChannelLimiter",
    "BucketConfig",
    "ChannelConfig",
    "CrawlGate",
    "SyncChannelLimiter",
    "build_crawl_gate",
]


@dataclass(frozen=True, slots=True)
class BucketConfig:
    """One platform account's aggregate rate: N tokens per period."""

    requests: int
    period_seconds: float


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    """One request class: which shared bucket it draws from, plus jitter."""

    bucket: str
    jitter_min_seconds: float
    jitter_max_seconds: float


class _PyrateSyncBucket:
    """Synchronous token bucket (Spider_XHS runs in worker threads)."""

    def __init__(self, config: BucketConfig) -> None:
        rate = Rate(config.requests, Duration.SECOND * config.period_seconds)
        self._limiter = Limiter(InMemoryBucket([rate]))
        self._key = "crawl"

    def acquire(self) -> None:
        acquired = self._limiter.try_acquire(self._key)
        if not acquired:  # type: ignore[unreachable]
            raise RuntimeError("crawl gate failed to grant a permit")


class _PyrateAsyncBucket:
    """Async view over ONE shared sync bucket (single ledger per site).

    pyrate 的 BucketAsyncWrapper 首次 leak 需要 running loop，且与同步
    Limiter 不能共享同一 InMemoryBucket 实例的唤醒语义。为了令牌账本
    真正单例，async 路径经 to_thread 走同步桶：等待发生在工作线程，
    事件循环不阻塞；当前负载（每秒个位数请求）下完全够用。
    """

    def __init__(self, sync_bucket: _PyrateSyncBucket) -> None:
        self._sync_bucket = sync_bucket

    async def acquire(self) -> None:
        await asyncio.to_thread(self._sync_bucket.acquire)


class CrawlGate:
    """所有外发平台请求的唯一闸门：共享令牌桶（按站点账号）→ 每次放行后抖动。

    深模块：调用方只见 ``acquire(channel)``；速率、抖动、将来并进的
    冷却/风控熔断（BossCooldownManager 类语义）都在闸门内统一演进。
    """

    def __init__(
        self,
        *,
        buckets: Mapping[str, BucketConfig],
        channels: Mapping[str, ChannelConfig],
        rng: random.Random | None = None,
        sleep_async: Callable[[float], Awaitable[None]] | None = None,
        sleep_sync: Callable[[float], None] | None = None,
    ) -> None:
        for name, channel in channels.items():
            if channel.bucket not in buckets:
                raise ValueError(
                    f"channel {name!r} references unknown bucket {channel.bucket!r}"
                )
            if not (
                0 <= channel.jitter_min_seconds <= channel.jitter_max_seconds
            ):
                raise ValueError(
                    f"channel {name!r} jitter range invalid: "
                    f"[{channel.jitter_min_seconds}, {channel.jitter_max_seconds}]"
                )
        self._buckets = dict(buckets)
        self._channels = dict(channels)
        self._rng = rng or random.Random()
        # 可注入的睡眠函数（测试传 fake，避免 monkeypatch 全局 asyncio.sleep /
        # time.sleep——全局补丁会点燃其他测试泄漏的后台任务变成死循环）。
        self._sleep_async = sleep_async or asyncio.sleep
        self._sleep_sync = sleep_sync or time.sleep
        # 每站点账号一套同步桶（单一令牌账本）；async acquire 经 to_thread
        # 复用同一桶。构造无事件循环依赖（build_job_agent 是同步入口）。
        self._sync_buckets = {
            name: _PyrateSyncBucket(config) for name, config in buckets.items()
        }
        self._async_views = {
            name: _PyrateAsyncBucket(bucket) for name, bucket in self._sync_buckets.items()
        }

    def channel_names(self) -> tuple[str, ...]:
        return tuple(self._channels)

    async def acquire(self, channel: str) -> None:
        """Async path: wait for the shared bucket (worker thread), then jitter."""

        config = self._channels[channel]
        await self._async_views[config.bucket].acquire()
        jitter = self._rng.uniform(config.jitter_min_seconds, config.jitter_max_seconds)
        if jitter > 0:
            await self._sleep_async(jitter)

    def acquire_sync(self, channel: str) -> None:
        """Sync path for worker threads (Spider_XHS transport)."""

        config = self._channels[channel]
        self._sync_buckets[config.bucket].acquire()
        jitter = self._rng.uniform(config.jitter_min_seconds, config.jitter_max_seconds)
        if jitter > 0:
            self._sleep_sync(jitter)


class BlockingLimiter(Protocol):
    def acquire(self) -> None: ...


class AsyncLimiter(Protocol):
    async def acquire(self) -> None: ...


class SyncChannelLimiter:
    """Adapt one gate channel onto the BlockingRateLimiter protocol."""

    def __init__(self, gate: CrawlGate, channel: str) -> None:
        self._gate = gate
        self._channel = channel

    def acquire(self) -> None:
        self._gate.acquire_sync(self._channel)


class AsyncChannelLimiter:
    """Adapt one gate channel onto the AsyncRateLimiter protocol."""

    def __init__(self, gate: CrawlGate, channel: str) -> None:
        self._gate = gate
        self._channel = channel

    async def acquire(self) -> None:
        await self._gate.acquire(self._channel)


def build_crawl_gate(settings: Settings) -> CrawlGate:
    """Map flat settings onto the gate's channel table (single mapping point).

    桶速率是站点级的：同一小红书账号的 api/media/page 请求合计不超过
    ``xhs_crawl_rate_*``。jitter 按请求类别区分（page 像人要慢）。
    ``xhs_scrape_delay_*``（原 Playwright pacing）在此表达为 page 通道
    的 jitter 区间，消掉独立的第三种节奏机制。
    """

    buckets = {
        "xhs": BucketConfig(
            requests=settings.xhs_crawl_rate_requests,
            period_seconds=settings.xhs_crawl_rate_period_seconds,
        ),
        "boss": BucketConfig(
            requests=settings.boss_crawl_rate_requests,
            period_seconds=settings.boss_crawl_rate_period_seconds,
        ),
    }
    channels = {
        "xhs-api": ChannelConfig(
            "xhs",
            settings.xhs_api_jitter_min_seconds,
            settings.xhs_api_jitter_max_seconds,
        ),
        "xhs-media": ChannelConfig(
            "xhs",
            settings.xhs_media_jitter_min_seconds,
            settings.xhs_media_jitter_max_seconds,
        ),
        "xhs-page": ChannelConfig(
            "xhs",
            settings.xhs_scrape_delay_min,
            settings.xhs_scrape_delay_max,
        ),
        "boss-cdp": ChannelConfig(
            "boss",
            settings.boss_crawl_jitter_min_seconds,
            settings.boss_crawl_jitter_max_seconds,
        ),
    }
    return CrawlGate(buckets=buckets, channels=channels)
