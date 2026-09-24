from fastapi.testclient import TestClient

from jobagent import web


class _FakeAgent:
    def __init__(self, *, response: str = "完成", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.closed = False

    async def reply(self, content: str, *, session_id: str) -> str:
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self) -> None:
        self.closed = True


def test_office_ai_history_is_separate_from_job_assistant(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE_DB", tmp_path / "state.db")
    with TestClient(web.app) as client:
        office = client.post("/api/office-ai/conversations", json={"title": "季度汇报"})
        assistant = client.post("/api/assistant/conversations", json={"title": "求职规划"})

        assert office.status_code == 201
        assert assistant.status_code == 201
        assert (
            [item["title"] for item in client.get("/api/office-ai/conversations").json()]
            == ["季度汇报"])
        assert (
            [item["title"] for item in client.get("/api/assistant/conversations").json()]
            == ["求职规划"])


def test_office_ai_conversation_can_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE_DB", tmp_path / "state.db")
    with TestClient(web.app) as client:
        conversation = client.post(
            "/api/office-ai/conversations", json={"title": "数据分析"}).json()
        result = client.get(f"/api/office-ai/conversations/{conversation['id']}")

    assert result.status_code == 200
    assert result.json()["title"] == "数据分析"
    assert result.json()["messages"] == []


def test_office_ai_closes_agent_and_hides_backend_error(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE_DB", tmp_path / "state.db")
    agent = _FakeAgent(error=RuntimeError("quota token=secret-value"))
    monkeypatch.setattr(web, "build_job_agent", lambda *_args, **_kwargs: agent)
    with TestClient(web.app) as client:
        conversation = client.post("/api/office-ai/conversations", json={}).json()
        result = client.post(
            f"/api/office-ai/conversations/{conversation['id']}/messages",
            json={"content": "帮我做汇报"},
        )

    assert result.status_code == 200
    assert result.json()["content"] == "Agent 暂时无法回答，请稍后再试。"
    assert "secret-value" not in result.text
    assert agent.closed is True
