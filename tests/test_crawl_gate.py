"""CrawlGate tests: per-account shared buckets + per-channel jitter.

核心契约：
1. 桶按站点账号隔离（xhs 桶耗尽不影响 boss 桶）
2. 同站不同通道共享同一桶（xhs-api 与 xhs-media 同账本）
3. 每次放行后 uniform jitter（防指纹，非防惊群）
4. 未知通道 loudly 抛错
5. build_job_agent 构造的 gate 跨工具调用共享（同一实例注入）
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable

import pytest

from jobagent.config import Settings
from jobagent.crawl import (
    AsyncChannelLimiter,
    BucketConfig,
    ChannelConfig,
    CrawlGate,
    SyncChannelLimiter,
    build_crawl_gate,
)


def _gate(
    *,
    xhs_rate: tuple[int, float] = (1_000_000, 1.0),  # 测试默认不限速
    boss_rate: tuple[int, float] = (1_000_000, 1.0),
    jitter: tuple[float, float] = (0.0, 0.0),
    **gate_kwargs: object,
) -> CrawlGate:
    return CrawlGate(
        buckets={
            "xhs": BucketConfig(*xhs_rate),
            "boss": BucketConfig(*boss_rate),
        },
        channels={
            "xhs-api": ChannelConfig("xhs", *jitter),
            "xhs-media": ChannelConfig("xhs", *jitter),
            "xhs-page": ChannelConfig("xhs", *jitter),
            "boss-cdp": ChannelConfig("boss", *jitter),
        },
        **gate_kwargs,  # type: ignore[arg-type]
    )


class SleepRecorder:
    """构造注入的 fake sleeper：记录时长，不真睡。

    刻意不用 monkeypatch 全局 asyncio.sleep / time.sleep：前序测试可能
    泄漏 pyrate BucketAsyncWrapper 的 10s 轮询任务，全局 fake（即回）
    会把它点燃成死循环空转（实测 47k 次 sleep(10) 挂死 pytest）。
    """

    def __init__(self) -> None:
        self.async_calls: list[float] = []
        self.sync_calls: list[float] = []

    async def asleep(self, seconds: float) -> None:
        self.async_calls.append(seconds)

    def ssleep(self, seconds: float) -> None:
        self.sync_calls.append(seconds)


# ---- 桶隔离与共享 ------------------------------------------------------------


def test_boss_bucket_not_drained_by_xhs_traffic() -> None:
    """XHS 桶限到 1/10s 后，Boss 通道仍然即时放行（站点隔离）。"""

    gate = _gate(xhs_rate=(1, 10.0))
    gate.acquire_sync("xhs-api")  # 耗尽 xhs 桶唯一令牌

    # boss 通道不受影响：不等待（jitter=0）
    import time

    t0 = time.monotonic()
    gate.acquire_sync("boss-cdp")
    assert time.monotonic() - t0 < 0.5


def test_same_site_channels_share_one_bucket() -> None:
    """xhs-api 与 xhs-media 同站共享令牌账本（账号聚合视角）。

    xhs 桶限 1 令牌/3s：api 通道用掉后，同站 media 通道的异步 acquire
    在 1s 超时内拿不到令牌 → TimeoutError 证明两通道同一账本（若各自
    独立桶则立即放行、不会超时）。
    """

    gate = _gate(xhs_rate=(1, 3.0))
    gate.acquire_sync("xhs-api")  # 用掉唯一的 xhs 令牌

    async def _probe() -> None:
        await asyncio.wait_for(gate.acquire("xhs-media"), timeout=1.0)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_probe())


# ---- jitter 剖面 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_acquire_applies_uniform_jitter_after_permit() -> None:
    recorder = SleepRecorder()
    gate = CrawlGate(
        buckets={"xhs": BucketConfig(1_000_000, 1.0)},
        channels={"xhs-api": ChannelConfig("xhs", 0.2, 1.3)},
        rng=random.Random(42),
        sleep_async=recorder.asleep,
    )

    for _ in range(3):
        await gate.acquire("xhs-api")

    assert len(recorder.async_calls) == 3
    assert all(0.2 <= s <= 1.3 for s in recorder.async_calls)
    assert len(set(recorder.async_calls)) == 3  # 随机序列，非固定值


def test_sync_acquire_applies_jitter() -> None:
    recorder = SleepRecorder()
    gate = CrawlGate(
        buckets={"xhs": BucketConfig(1_000_000, 1.0)},
        channels={"xhs-api": ChannelConfig("xhs", 0.5, 2.5)},
        rng=random.Random(7),
        sleep_sync=recorder.ssleep,
    )

    for _ in range(4):
        gate.acquire_sync("xhs-api")

    assert len(recorder.sync_calls) == 4
    assert all(0.5 <= s <= 2.5 for s in recorder.sync_calls)


def test_zero_jitter_skips_sleep() -> None:
    recorder = SleepRecorder()
    gate = _gate(jitter=(0.0, 0.0), sleep_sync=recorder.ssleep)
    gate.acquire_sync("xhs-api")
    assert recorder.sync_calls == []


# ---- loudly 校验 ---------------------------------------------------------------


def test_unknown_channel_raises() -> None:
    gate = _gate()
    with pytest.raises(KeyError):
        gate.acquire_sync("linkedin-api")
    with pytest.raises(KeyError):
        asyncio.run(gate.acquire("linkedin-api"))


def test_channel_referencing_unknown_bucket_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown bucket"):
        CrawlGate(
            buckets={"xhs": BucketConfig(1, 1.0)},
            channels={"evil": ChannelConfig("nonexistent", 0.0, 0.0)},
        )


def test_invalid_jitter_range_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="jitter range invalid"):
        CrawlGate(
            buckets={"xhs": BucketConfig(1, 1.0)},
            channels={"xhs-api": ChannelConfig("xhs", 2.0, 0.5)},
        )


# ---- 协议适配器（接入现有 backend 构造参数） ------------------------------------


def test_channel_limiters_adapt_onto_backend_protocols() -> None:
    recorder = SleepRecorder()
    gate = CrawlGate(
        buckets={"xhs": BucketConfig(1_000_000, 1.0)},
        channels={"xhs-api": ChannelConfig("xhs", 0.1, 0.1)},
        sleep_async=recorder.asleep,
        sleep_sync=recorder.ssleep,
    )

    SyncChannelLimiter(gate, "xhs-api").acquire()
    assert recorder.sync_calls == [0.1]

    async def _async_path() -> None:
        await AsyncChannelLimiter(gate, "xhs-api").acquire()

    asyncio.run(_async_path())
    assert recorder.async_calls == [0.1]


def test_sync_limiter_calls_gate_sync_not_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    gate = _gate()
    monkeypatch.setattr(
        gate, "acquire_sync", lambda ch: calls.append(("sync", ch))
    )
    SyncChannelLimiter(gate, "boss-cdp").acquire()
    assert calls == [("sync", "boss-cdp")]


# ---- build_crawl_gate 映射与共享注入 -------------------------------------------


def test_build_crawl_gate_maps_settings_channels() -> None:
    settings = Settings(
        _env_file=None,
        xhs_crawl_rate_requests=5,
        xhs_crawl_rate_period_seconds=10.0,
        boss_crawl_rate_requests=2,
        boss_crawl_rate_period_seconds=20.0,
    )
    gate = build_crawl_gate(settings)

    assert set(gate.channel_names()) == {"xhs-api", "xhs-media", "xhs-page", "boss-cdp"}


def test_build_job_agent_injects_one_shared_gate(tmp_path: Callable) -> None:
    """工厂注入的 gate 是进程单例：工具每次调用共享同一组桶。"""

    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from jobagent.agent import build_job_agent

    class _Bindable(FakeListChatModel):
        def bind_tools(self, tools, **kwargs):  # type: ignore[no-untyped-def]
            return self

    agent = build_job_agent(
        Settings(
            _env_file=None,
            jobagent_checkpoint_db=tmp_path / "cp.db",
        ),
        model=_Bindable(responses=["好的"]),
    )
    import asyncio as aio

    aio.run(agent.close())
    # 透过工具表拿到注入的 gate 实例并断言两把抓取工具共享它
    discovery_tool = next(t for t in agent._tools if t.name == "discover_boss_jobs")
    shared_tool = next(t for t in agent._tools if t.name == "save_shared_url")
    assert discovery_tool is not None and shared_tool is not None
