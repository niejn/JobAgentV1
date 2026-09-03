from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.config import Settings
from jobagent.scraper.boss import BossDiscoveryRequest
from jobagent.scraper.boss_http import BossHttpBackend


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.kwargs: dict = {}

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.kwargs = {"url": url, **kwargs}
        return self.response


@pytest.mark.asyncio
async def test_http_search_normalizes_jobs_and_preserves_security_id(tmp_path: Path) -> None:
    client = FakeClient(
        FakeResponse(
            {
                "code": 0,
                "zpData": {
                    "jobList": [
                        {
                            "encryptJobId": "job-1",
                            "brandName": "际数科技",
                            "jobName": "AI 后端开发工程师",
                            "cityName": "上海",
                            "areaDistrict": "闵行区",
                            "salaryDesc": "20-30K·15薪",
                            "securityId": "opaque-security-id",
                            "encryptBossId": "opaque-boss-id",
                            "bossName": "王媛",
                            "bossTitle": "招聘者",
                            "lid": "opaque-lid",
                        }
                    ]
                },
            }
        )
    )
    settings = Settings(_env_file=None, jobagent_state_db=tmp_path / "state.db")
    backend = BossHttpBackend(
        settings,
        client=client,
        cookies={"bst": "bst-value", "wt2": "wt-value"},
    )

    jobs = await backend.discover(
        BossDiscoveryRequest(query="AI 后端", city="上海", area="闵行区")
    )

    assert len(jobs) == 1
    assert jobs[0].id == "boss:job-1"
    assert jobs[0].metadata["security_id"] == "opaque-security-id"
    assert jobs[0].metadata["boss_name"] == "王媛"
    assert jobs[0].metadata["encrypt_boss_id"] == "opaque-boss-id"
    assert client.kwargs["params"]["city"] == "101020100"
    assert client.kwargs["headers"]["zp_token"] == "bst-value"


@pytest.mark.asyncio
async def test_http_search_stops_on_risk_control(tmp_path: Path) -> None:
    client = FakeClient(FakeResponse({"code": 37, "message": "blocked"}))
    settings = Settings(_env_file=None, jobagent_state_db=tmp_path / "state.db")
    backend = BossHttpBackend(settings, client=client, cookies={"bst": "x"})

    with pytest.raises(Exception, match="blocked"):
        await backend.discover(BossDiscoveryRequest(query="AI", city="上海"))
