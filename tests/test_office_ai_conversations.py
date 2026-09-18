from fastapi.testclient import TestClient

from jobagent import web


def test_office_ai_history_is_separate_from_job_assistant(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE_DB", tmp_path / "state.db")
    with TestClient(web.app) as client:
        office = client.post("/api/office-ai/conversations", json={"title": "季度汇报"})
        assistant = client.post("/api/assistant/conversations", json={"title": "求职规划"})

        assert office.status_code == 201
        assert assistant.status_code == 201
        assert [item["title"] for item in client.get("/api/office-ai/conversations").json()] == ["季度汇报"]
        assert [item["title"] for item in client.get("/api/assistant/conversations").json()] == ["求职规划"]


def test_office_ai_conversation_can_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE_DB", tmp_path / "state.db")
    with TestClient(web.app) as client:
        conversation = client.post("/api/office-ai/conversations", json={"title": "数据分析"}).json()
        result = client.get(f"/api/office-ai/conversations/{conversation['id']}")

    assert result.status_code == 200
    assert result.json()["title"] == "数据分析"
    assert result.json()["messages"] == []
