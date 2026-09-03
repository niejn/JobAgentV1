from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.applier.boss_direct_contact import BossDirectContactAdapter
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
