from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jobagent.applier.boss_direct_contact import BossDirectContactAdapter, BossPageTokenError
from jobagent.applier.boss_ws import BossConversationTarget
from jobagent.config import Settings
from jobagent.models import Job, JobSource


class FakeResponse:
    status_code = 200

    def json(self) -> dict:
        return {
            "code": 0,
            "zpData": {"encBossId": "enc", "bossSource": 0},
        }


class FakeClient:
    def __init__(self) -> None:
        self.call: dict = {}

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.call = {"url": url, **kwargs}
        return FakeResponse()


class MissingTokenClient(FakeClient):
    async def get(self, url: str, **kwargs: object) -> object:
        return type("Response", (), {"status_code": 200, "json": lambda self: {"zpData": {}}})()


@pytest.mark.asyncio
async def test_direct_contact_enters_and_confirms_new_conversation(tmp_path: Path) -> None:
    target = BossConversationTarget(42, 0, "enc", "叶先生", "际数科技", "标注工程师实习岗")

    async def find_target(**_: object) -> BossConversationTarget:
        return target

    client = FakeClient()
    adapter = BossDirectContactAdapter(
        Settings(_env_file=None, jobagent_state_db=tmp_path / "state.db"),
        client=client,
        cookies={"bst": "b", "wt2": "w", "__zp_stoken__": "s"},
        page_token="page-token",
        target_finder=find_target,
    )
    job = Job(
        id="boss:job-1",
        source=JobSource.BOSS,
        title="标注工程师实习岗",
        company="际数科技",
        location="上海",
        url="https://www.zhipin.com/job_detail/job-1.html",
        description="",
        metadata={"security_id": "security", "lid": "lid", "boss_name": "叶先生"},
    )

    result = await adapter.enter(job)

    assert result.status == "confirmed"
    assert result.target == target
    assert client.call["data"] == {"expectId": "0"}
    assert client.call["params"]["jobId"] == "job-1"
    assert client.call["params"]["lid"] == "lid"
    assert client.call["headers"]["token"] == "page-token"


@pytest.mark.asyncio
async def test_server_boss_id_wins_over_display_name_alias() -> None:
    target = BossConversationTarget(42, 0, "enc", "季钦城", "际数科技", "Python")

    async def finder(**kwargs: object) -> BossConversationTarget:
        assert kwargs["friend_name"] == "季先生"
        assert kwargs["expected_encrypt_boss_id"] == "enc"
        return target

    client = FakeClient()
    adapter = BossDirectContactAdapter(
        Settings(_env_file=None),
        client=client,
        cookies={"bst": "b", "wt2": "w", "__zp_stoken__": "s"},
        page_token="page-token",
        target_finder=finder,
    )
    job = Job(
        id="boss:job-2",
        source=JobSource.BOSS,
        title="Python",
        company="际数科技",
        location="上海",
        url="https://www.zhipin.com/job_detail/job-2.html",
        description="",
        metadata={"security_id": "security", "lid": "lid", "boss_name": "季先生"},
    )

    result = await adapter.enter(job)

    assert result.status == "confirmed"


@pytest.mark.asyncio
async def test_direct_contact_uses_chrome_token_when_http_token_is_missing() -> None:
    target = BossConversationTarget(42, 0, "enc", "叶先生", "际数科技", "Python")

    async def find_target(**_: object) -> BossConversationTarget:
        return target

    adapter = BossDirectContactAdapter(
        Settings(_env_file=None),
        client=MissingTokenClient(),
        cookies={"bst": "b", "wt2": "w", "__zp_stoken__": "s"},
        target_finder=find_target,
    )
    async def browser_token() -> str:
        adapter._page_token = "browser-token"
        return "browser-token"

    fallback = AsyncMock(side_effect=browser_token)
    adapter._fetch_page_token_from_cdp = fallback
    job = Job(
        id="boss:job-3",
        source=JobSource.BOSS,
        title="Python",
        company="际数科技",
        location="上海",
        url="https://www.zhipin.com/job_detail/job-3.html",
        description="",
        metadata={"security_id": "security", "lid": "lid"},
    )

    result = await adapter.enter(job)

    assert result.status == "confirmed"
    assert adapter.page_token == "browser-token"
    fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_contact_returns_page_token_missing_without_raising() -> None:
    adapter = BossDirectContactAdapter(
        Settings(_env_file=None),
        client=MissingTokenClient(),
        cookies={"bst": "b", "wt2": "w", "__zp_stoken__": "s"},
    )
    adapter._fetch_page_token_from_cdp = AsyncMock(
        side_effect=BossPageTokenError("missing")
    )
    job = Job(
        id="boss:job-4",
        source=JobSource.BOSS,
        title="Python",
        company="际数科技",
        location="上海",
        url="https://www.zhipin.com/job_detail/job-4.html",
        description="",
        metadata={"security_id": "security", "lid": "lid"},
    )

    result = await adapter.enter(job)

    assert result.status == "failed"
    assert result.error_type == "page_token_missing"
