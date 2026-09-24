from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from jobagent import web
from jobagent.agent import build_hitl_middleware
from jobagent.journey.creation import CreateJourneyRequest, create_journey
from jobagent.journey.store import SQLiteJourneyStore
from jobagent.tools.journey_creation import build_create_journey_tool

ARGS = {'company': '上海启链云智能科技', 'role': 'AI Agent 全栈开发工程师',
        'source_job_id': 'boss:26ba163c0a1b8ba90nB93dS0EVtZ'}


def test_concurrent_retries_create_only_one_journey(tmp_path):
    path = tmp_path / 'state.db'
    with SQLiteJourneyStore(path):
        pass
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda _: create_journey(path, CreateJourneyRequest(**ARGS)), range(8)))
    assert len({journey.id for journey, _ in results}) == 1
    assert sum(created for _, created in results) == 1


def test_web_and_cli_service_share_keys(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    monkeypatch.setattr(web, 'STATE_DB', path)
    journey, _ = create_journey(path, CreateJourneyRequest(**ARGS))
    with TestClient(web.app) as client:
        response = client.post('/api/journeys', json=ARGS)
        assert response.status_code == 201
        assert response.json()['id'] == journey.id
        assert response.json()['created'] is False
        assert client.get('/api/journeys').json()[0]['id'] == journey.id


def test_missing_jd_and_distinct_ids(tmp_path):
    path = tmp_path / 'state.db'
    first, _ = create_journey(path, CreateJourneyRequest(**ARGS))
    second, _ = create_journey(
        path, CreateJourneyRequest(**{**ARGS, 'source_job_id': 'boss:other'}))
    assert first.id != second.id
    assert first.job_description == ''
    assert first.stage == 'targeted'


def test_web_fields_retry_preserves_existing_jd(tmp_path):
    path = tmp_path / 'state.db'
    request = CreateJourneyRequest(company='示例公司', role='后端开发', job_description='原始JD')
    first, _ = create_journey(path, request)
    repeated, created = create_journey(
        path, request.model_copy(update={'job_description': '不应覆盖'}))
    assert repeated.id == first.id
    assert not created
    assert repeated.job_description == '原始JD'


class CreationModel(BaseChatModel):
    @property
    def _llm_type(self):
        return 'journey-hitl-test'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = (
            AIMessage(content='完成') if any(isinstance(m, ToolMessage) for m in messages)
            else AIMessage(content='', tool_calls=[
                {'name': 'create_opportunity_journey', 'args': ARGS, 'id': 'create-1'}]))
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['approve', 'reject'])
async def test_real_hitl_prevents_write_until_approved(tmp_path, decision, monkeypatch):
    path = tmp_path / 'state.db'
    monkeypatch.setattr(web, 'STATE_DB', path)
    with SQLiteJourneyStore(path):
        pass
    agent = create_agent(CreationModel(), tools=[build_create_journey_tool(path)],
                         middleware=[build_hitl_middleware()], checkpointer=InMemorySaver())
    config = {'configurable': {'thread_id': decision}}
    paused = await agent.ainvoke({'messages': [('user', '关注此岗位并创建Journey')]}, config)
    assert paused['__interrupt__']
    with SQLiteJourneyStore(path) as store:
        assert not store.list_journeys()
    result = await agent.ainvoke(Command(resume={'decisions': [{'type': decision}]}), config)
    with TestClient(web.app) as client:
        rows = client.get('/api/journeys').json()
    assert len(rows) == (1 if decision == 'approve' else 0)
    if decision == 'approve':
        messages = [m for m in result['messages'] if isinstance(m, ToolMessage)]
        assert rows[0]['id'] in messages[-1].content


def test_model_cannot_supply_approval_boolean():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CreateJourneyRequest(**ARGS, user_confirmed=True)
