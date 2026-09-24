import sqlite3

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
from jobagent.journey.management import (
    JourneyTarget,
    JourneyUpdate,
    change_journey,
    get_journey,
    list_journeys,
)
from jobagent.journey.store import SQLiteJourneyStore
from jobagent.tools.journey_management import build_journey_management_tools


def seed(path):
    return create_journey(
        path, CreateJourneyRequest(company='测试公司', role='工程师', job_description='初始JD'))[0]


def target(journey):
    return dict(journey_id=journey['id'], expected_company=journey['company'],
                expected_role=journey['role'], expected_version=journey['version'],
                reason='用户确认')


def test_update_delete_restore_and_creation_key(tmp_path):
    path = tmp_path / 'state.db'
    original = seed(path)
    args = target(get_journey(path, original.id))
    updated = change_journey(
        path, 'update',
        JourneyUpdate(**args, changes={'job_description': '新版JD', 'department': '研发'}))
    assert updated['version'] == 2
    assert get_journey(path, original.id)['jd_version_count'] == 2
    with pytest.raises(ValueError, match='已变化'):
        change_journey(path, 'delete', JourneyTarget(**args))
    deleted = change_journey(path, 'delete', JourneyTarget(**target(updated)))
    assert deleted['deleted_at']
    with SQLiteJourneyStore(path) as store:
        with pytest.raises(ValueError, match='已删除'):
            store.start_task(original.id, 'research')
    assert list_journeys(path) == []
    assert list_journeys(path, include_deleted=True)[0]['id'] == original.id
    assert get_journey(path, original.id)['jd_version_count'] == 2
    repeated = seed(path)
    assert repeated.id == original.id
    assert repeated.deleted_at
    restored = change_journey(path, 'restore', JourneyTarget(**target(deleted)))
    assert restored['deleted_at'] is None
    assert list_journeys(path)[0]['job_description'] == '新版JD'
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            'SELECT action FROM journey_change_events ORDER BY id'
        ).fetchall() == [('update',), ('delete',), ('restore',)]


def test_running_tasks_block_delete_and_are_preserved(tmp_path):
    path = tmp_path / 'state.db'
    journey = seed(path)
    with SQLiteJourneyStore(path) as store:
        task = store.start_task(journey.id, 'research')
    args = JourneyTarget(**target(get_journey(path, journey.id)))
    with pytest.raises(ValueError, match='运行'):
        change_journey(path, 'delete', args)
    with SQLiteJourneyStore(path) as store:
        store.fail_task(task.id, 'finished unsuccessfully')
    change_journey(path, 'delete', args)
    assert get_journey(path, journey.id)['task_count'] == 1


def test_http_mutations_are_version_checked_and_shared(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    monkeypatch.setattr(web, 'STATE_DB', path)
    journey = seed(path)
    with TestClient(web.app) as client:
        url = '/api/journeys/' + journey.id
        args = target(client.get(url).json())
        response = client.patch(url, json={**args, 'changes': {'role': '高级工程师'}})
        assert response.status_code == 200
        assert client.request('DELETE', url, json=args).status_code == 409
        args = target(response.json())
        deleted = client.request('DELETE', url, json=args)
        assert deleted.status_code == 200
        assert client.get('/api/journeys').json() == []
        assert len(client.get('/api/journeys?include_deleted=true').json()) == 1
        assert client.post(url + '/restore', json=target(deleted.json())).status_code == 200
        assert list_journeys(path)[0]['role'] == '高级工程师'


class ManagementModel(BaseChatModel):
    action: str
    arguments: dict

    @property
    def _llm_type(self):
        return 'management-hitl-test'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        reply = (
            AIMessage(content='完成') if any(isinstance(m, ToolMessage) for m in messages)
            else AIMessage(content='', tool_calls=[
                {'name': self.action + '_opportunity_journey',
                 'args': self.arguments, 'id': 'op-1'}]))
        return ChatResult(generations=[ChatGeneration(message=reply)])


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['update', 'delete', 'restore'])
@pytest.mark.parametrize('decision', ['approve', 'reject'])
async def test_actual_hitl_gates_every_mutation(tmp_path, action, decision):
    path = tmp_path / 'state.db'
    journey = seed(path)
    before = get_journey(path, journey.id)
    if action == 'restore':
        change_journey(path, 'delete', JourneyTarget(**target(before)))
        before = get_journey(path, journey.id)
    args = target(before)
    if action == 'update':
        args['changes'] = {'department': '研发部'}
    graph = create_agent(ManagementModel(action=action, arguments=args),
                         tools=build_journey_management_tools(path),
                         middleware=[build_hitl_middleware()], checkpointer=InMemorySaver())
    config = {'configurable': {'thread_id': action + decision}}
    paused = await graph.ainvoke({'messages': [('user', '管理此Journey')]}, config)
    assert paused['__interrupt__']
    assert get_journey(path, journey.id) == before
    await graph.ainvoke(Command(resume={'decisions': [{'type': decision}]}), config)
    after = get_journey(path, journey.id)
    assert after['version'] == before['version'] + (1 if decision == 'approve' else 0)
    if decision == 'reject':
        assert after == before


def test_unknown_patch_fields_and_identity_mismatch(tmp_path):
    from pydantic import ValidationError
    path = tmp_path / 'state.db'
    journey = seed(path)
    args = target(get_journey(path, journey.id))
    for changes in ({'stage': 'applied'}, {'role': ''}, {'role': None}, {}):
        with pytest.raises(ValidationError):
            JourneyUpdate(**args, changes=changes)
    args['expected_company'] = '其他公司'
    with pytest.raises(ValueError):
        change_journey(path, 'delete', JourneyTarget(**args))


@pytest.mark.asyncio
async def test_query_tools_filter_paginate_and_show_deleted(tmp_path):
    path = tmp_path / 'state.db'
    journey = seed(path)
    tools = {tool.name: tool for tool in build_journey_management_tools(path)}
    rows = await tools['list_opportunity_journeys'].ainvoke({'company': '测试', 'limit': 1})
    assert rows['journeys'][0]['id'] == journey.id
    page = await tools['list_opportunity_journeys'].ainvoke({'offset': 1})
    assert page['journeys'] == []
    details = await tools['get_opportunity_journey'].ainvoke({'journey_id': journey.id})
    change_journey(path, 'delete', JourneyTarget(**target(details['journey'])))
    hidden = await tools['list_opportunity_journeys'].ainvoke({})
    assert hidden['journeys'] == []
    deleted = await tools['list_opportunity_journeys'].ainvoke({'include_deleted': True})
    assert deleted['journeys'][0]['deleted_at']
    missing = await tools['get_opportunity_journey'].ainvoke({'journey_id': 'unknown'})
    assert missing['status'] == 'not_found'
