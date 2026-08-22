"""Behavior tests for the Boss read-only HTTP adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jobagent.config import Settings
from jobagent.scraper.boss import BossDiscoveryRequest
from jobagent.scraper.boss_http import BossAccessError, BossHttpBackend


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class InvalidJsonResponse:
    def json(self) -> dict[str, object]:
        raise ValueError("not json")


@pytest.mark.asyncio
async def test_http_backend_uses_scale_codes_and_returns_matching_business_district(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    monkeypatch.setattr("jobagent.auth.browser_login.COOKIE_DIR", cookie_dir)
    monkeypatch.setattr(
        "jobagent.auth.browser_login.LEGACY_COOKIE_DIR",
        tmp_path / "legacy",
    )
    (cookie_dir / "boss.json").write_text(
        '{"cookies":[{"name":"wt2","value":"secret","domain":".zhipin.com"}]}',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def request(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(
            {
                "code": 0,
                "message": "Success",
                "zpData": {
                    "jobList": [
                        {
                            "encryptJobId": "stable-boss-id",
                            "jobName": "AI Agent 工程师",
                            "brandName": "五角场智能科技",
                            "brandScaleName": "0-20人",
                            "cityName": "上海",
                            "areaDistrict": "杨浦区",
                            "businessDistrict": "五角场",
                            "salaryDesc": "15-25K",
                            "jobLabels": ["3-5年", "本科"],
                            "skills": ["Python", "LangChain"],
                        },
                        {
                            "encryptJobId": "wrong-area",
                            "jobName": "Python 工程师",
                            "brandName": "外滩科技",
                            "brandScaleName": "20-99人",
                            "cityName": "上海",
                            "areaDistrict": "黄浦区",
                            "businessDistrict": "外滩",
                            "salaryDesc": "20-30K",
                        },
                    ]
                },
            }
        )

    backend = BossHttpBackend(Settings(_env_file=None), requester=request)
    jobs = await backend.discover(
        BossDiscoveryRequest(
            query="AI Agent",
            city="上海",
            area="五角场",
            company_sizes=["0-20人", "20-99人"],
            limit=10,
        )
    )

    assert captured["params"]["scale"] == "301,302"  # type: ignore[index]
    assert len(jobs) == 1
    assert jobs[0].id == "boss:stable-boss-id"
    assert jobs[0].metadata["business_district"] == "五角场"
    assert jobs[0].metadata["listing_summary_only"] is True


@pytest.mark.asyncio
async def test_http_backend_stops_on_boss_risk_control(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    monkeypatch.setattr("jobagent.auth.browser_login.COOKIE_DIR", cookie_dir)
    monkeypatch.setattr(
        "jobagent.auth.browser_login.LEGACY_COOKIE_DIR",
        tmp_path / "legacy",
    )
    (cookie_dir / "boss.json").write_text(
        '{"cookies":[{"name":"wt2","value":"secret","domain":".zhipin.com"}]}',
        encoding="utf-8",
    )

    backend = BossHttpBackend(
        Settings(_env_file=None),
        requester=lambda *args, **kwargs: FakeResponse(
            {"code": 37, "message": "您的环境存在异常."}
        ),
    )

    with pytest.raises(BossAccessError, match="risk control"):
        await backend.discover(BossDiscoveryRequest(query="AI", city="上海"))


@pytest.mark.asyncio
async def test_concurrent_queries_share_risk_cooldown_and_only_hit_boss_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    monkeypatch.setattr("jobagent.auth.browser_login.COOKIE_DIR", cookie_dir)
    monkeypatch.setattr(
        "jobagent.auth.browser_login.LEGACY_COOKIE_DIR",
        tmp_path / "legacy",
    )
    (cookie_dir / "boss.json").write_text(
        '{"cookies":[{"name":"wt2","value":"secret","domain":".zhipin.com"}]}',
        encoding="utf-8",
    )
    calls: list[str] = []

    def blocked_request(url: str, **kwargs: object) -> FakeResponse:
        calls.append(url)
        return FakeResponse({"code": 37, "message": "您的环境存在异常."})

    backend = BossHttpBackend(
        Settings(
            _env_file=None,
            boss_api_rate_period_seconds=0.01,
            boss_risk_cooldown_seconds=900,
        ),
        requester=blocked_request,
    )
    requests_to_run = (
        BossDiscoveryRequest(query="AI Agent", city="上海"),
        BossDiscoveryRequest(query="Python 后端", city="上海"),
    )

    results = await asyncio.gather(
        *(backend.discover(request) for request in requests_to_run),
        return_exceptions=True,
    )

    assert len(calls) == 1
    assert all(isinstance(result, BossAccessError) for result in results)


@pytest.mark.asyncio
async def test_non_json_response_becomes_safe_access_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    monkeypatch.setattr("jobagent.auth.browser_login.COOKIE_DIR", cookie_dir)
    monkeypatch.setattr(
        "jobagent.auth.browser_login.LEGACY_COOKIE_DIR",
        tmp_path / "legacy",
    )
    (cookie_dir / "boss.json").write_text(
        '{"cookies":[{"name":"wt2","value":"secret","domain":".zhipin.com"}]}',
        encoding="utf-8",
    )
    backend = BossHttpBackend(
        Settings(_env_file=None),
        requester=lambda *args, **kwargs: InvalidJsonResponse(),
    )

    with pytest.raises(BossAccessError, match="invalid response"):
        await backend.discover(BossDiscoveryRequest(query="AI", city="上海"))
