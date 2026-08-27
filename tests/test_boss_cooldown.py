"""Tests for Boss risk-control cooldown manager and error-code passthrough."""

from __future__ import annotations

import pytest

from jobagent.config import Settings
from jobagent.scraper.boss import BossAccessError, BossCooldownManager, get_boss_cooldown
from jobagent.scraper.boss_cdp import BossCdpBackend


@pytest.fixture(autouse=True)
def _reset_cooldown():
    get_boss_cooldown().reset()
    yield
    get_boss_cooldown().reset()


class TestBossCooldownManager:
    def test_allows_requests_by_default(self) -> None:
        manager = BossCooldownManager()
        allowed, remaining, reason = manager.check()
        assert allowed is True
        assert remaining == 0
        assert reason == ""

    def test_trigger_blocks_requests(self) -> None:
        manager = BossCooldownManager(cooldown_seconds=3600)
        manager.trigger("risk_control")
        allowed, remaining, reason = manager.check()
        assert allowed is False
        assert remaining == 60
        assert reason == "risk_control"

    def test_cooldown_expires(self) -> None:
        manager = BossCooldownManager(cooldown_seconds=0)
        manager.trigger("risk_control")
        allowed, _, _ = manager.check()
        assert allowed is True

    def test_reset_clears_state(self) -> None:
        manager = BossCooldownManager()
        manager.trigger("risk_control")
        manager.reset()
        allowed, _, _ = manager.check()
        assert allowed is True

    def test_singleton_shared(self) -> None:
        first = get_boss_cooldown()
        second = get_boss_cooldown()
        assert first is second


class TestBossAccessErrorCode:
    def test_default_code(self) -> None:
        err = BossAccessError("boom")
        assert err.code == "boss_access_denied"
        assert str(err) == "boom"

    def test_custom_code(self) -> None:
        err = BossAccessError("cooldown", code="cooldown_active")
        assert err.code == "cooldown_active"


class TestDiscoverRespectsCooldown:
    @pytest.mark.asyncio
    async def test_discover_refused_immediately_during_cooldown(self) -> None:
        """During cooldown discover() must fail fast WITHOUT any network use."""

        backend = BossCdpBackend(Settings(_env_file=None))
        get_boss_cooldown().trigger("risk_control")

        from jobagent.scraper.boss import BossDiscoveryRequest

        request = BossDiscoveryRequest(query="python", city="上海", limit=5)
        with pytest.raises(BossAccessError) as exc_info:
            await backend.discover(request)

        assert exc_info.value.code == "cooldown_active"
        assert "冷却" in str(exc_info.value)
        # Connection must never have been attempted.
        assert backend._connection is None

    @pytest.mark.asyncio
    async def test_risk_control_response_triggers_cooldown(self, monkeypatch) -> None:
        """A non-zero API code must arm the shared cooldown."""

        backend = BossCdpBackend(Settings(_env_file=None))

        class FakePage:
            url = "https://www.zhipin.com/web/geek/job"

            def is_closed(self) -> bool:
                return False

            async def close(self) -> None:
                pass

            def on(self, event, handler) -> None:
                backend._captured_handler = handler

            async def goto(self, *args, **kwargs) -> None:
                # Simulate Boss returning risk-control payload.
                class FakeResponse:
                    url = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"

                    async def json(self) -> dict:
                        return {"code": 1001, "message": "rate limited"}

                await backend._captured_handler(FakeResponse())

        class FakeContext:
            def __init__(self) -> None:
                class _ExistingTab:
                    url = "https://www.zhipin.com/"

                    def is_closed(self) -> bool:
                        return False

                self.pages = [_ExistingTab()]

            async def new_page(self) -> FakePage:
                return FakePage()

        async def fake_ensure():
            return (object(), FakeContext(), object())

        monkeypatch.setattr(backend, "_ensure_connection", fake_ensure)

        from jobagent.scraper.boss import BossDiscoveryRequest

        request = BossDiscoveryRequest(query="python", city="上海", limit=5)
        with pytest.raises(BossAccessError) as exc_info:
            await backend.discover(request)

        assert exc_info.value.code == "boss_risk_control"
        allowed, remaining_min, _ = get_boss_cooldown().check()
        assert allowed is False
        assert remaining_min == 60


class TestToolPassthrough:
    def test_discover_tool_returns_real_error_code(self) -> None:
        """The tool layer must keep the backend's error code and message."""

        from jobagent.tools.job_discovery import build_boss_job_discovery_tool

        class FailingDiscovery:
            async def discover(self, request):
                raise BossAccessError(
                    "请读取 skills/ChromeCDP-setup/SKILL.md",
                    code="cdp_not_ready",
                )

        tool = build_boss_job_discovery_tool(FailingDiscovery())

        import asyncio

        result = asyncio.run(tool.ainvoke({"query": "python", "city": "上海"}))
        assert result["status"] == "blocked"
        assert result["error_type"] == "cdp_not_ready"
        assert "ChromeCDP-setup" in result["message"]
