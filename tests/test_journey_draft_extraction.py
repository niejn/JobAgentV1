from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jobagent import web
from jobagent.journey.draft import JourneyDraft, extract_draft

TEXT = (Path(__file__).parents[1]
        / 'jobagent/ui/fixtures/job-card-single-line.txt').read_text(encoding='utf-8').strip()
SPACED_TEXT = (Path(__file__).parent
               / 'fixtures/job-card-spaced-ocr.txt').read_text(encoding='utf-8').strip()


def test_full_spaced_ocr_from_user():
    draft = extract_draft(SPACED_TEXT)
    assert draft.company == '狼腾知光'
    assert draft.role == '算法与应用开发工程师'
    assert draft.location == '上海临港'
    assert draft.department == ''
    assert draft.description == SPACED_TEXT


@pytest.mark.parametrize(
    'text',
    [TEXT, TEXT.replace(' ', '\n'),
     TEXT.replace('职位描述', '职 位 描 述').replace('企业级', '企 业 级')],
    ids=['single-line', 'multiline', 'ocr-spacing'])
def test_real_ocr(text):
    draft = extract_draft(text)
    assert draft.company == '狼腾知光'
    assert draft.role in ('算法与应用开发工程师', 'AI 算法与应用开发工程师')
    assert draft.department == ''
    assert draft.location == '上海临港'
    assert draft.description == text


def test_short_labels_do_not_match_prose():
    draft = extract_draft(
        '职位描述\n公司基本信息\n微信扫码分享\n'
        '具备企业级应用系统开发经验\n缩短跨部门协作周期')
    assert draft.company == draft.role == draft.department == ''


@pytest.mark.parametrize('title', ['后端工程师', 'Python开发'])
def test_title_at_start_and_company_in_footer(title):
    draft = extract_draft(f'{title}\n职位描述\n职责正文\n工作地点 : 临港\n示例企业 ”HRM')
    assert draft.company == '示例企业'
    assert draft.role == title
    assert draft.location == '上海临港'


def test_correction_preserves_jd_and_untouched_fields():
    current = JourneyDraft(description=TEXT, department='研发部', location='上海临港')
    result = extract_draft('企业名称狼腾知光 岗位名称 AI 算法与应用开发工程师', current)
    assert result.company == '狼腾知光'
    assert result.role == 'AI 算法与应用开发工程师'
    assert result.department == '研发部'
    assert result.description == TEXT
    assert result.location == '上海临港'


def test_extract_endpoint_and_correction():
    with TestClient(web.app) as client:
        response = client.post('/api/journey-drafts/extract', json={'text': TEXT})
        assert response.status_code == 200
        payload = response.json()
        assert payload['draft']['company'] == '狼腾知光'
        assert payload['draft']['role'] == '算法与应用开发工程师'
        assert payload['draft']['location'] == '上海临港'
        assert payload['missing_fields'] == []
        response = client.post('/api/journey-drafts/extract', json={
            'text': '公司：示例科技 岗位：后端开发 部门：研发部', 'current': payload['draft']})
        assert response.status_code == 200
        updated = response.json()['draft']
        assert updated['company'] == '示例科技'
        assert updated['role'] == '后端开发'
        assert updated['department'] == '研发部'
        assert updated['description'] == TEXT
        assert client.post('/api/journey-drafts/extract', json={'text': '  '}).status_code == 422


@pytest.mark.parametrize('ocr_text', [TEXT, SPACED_TEXT], ids=['original', 'full-spaced-ocr'])
def test_ocr_endpoint_returns_same_summary(monkeypatch, ocr_text):
    monkeypatch.setattr(
        web.TesseractOcrExtractor, 'extract',
        lambda self, path: SimpleNamespace(
            text=ocr_text, confidence=.8, engine='fixture', language='chi_sim'))
    with TestClient(web.app) as client:
        response = client.post('/api/journey-drafts/ocr', files={
            'file': ('jd.png', b'fixture-image', 'image/png')})
        assert response.status_code == 200
        ocr = response.json()
        text = client.post('/api/journey-drafts/extract', json={'text': ocr_text}).json()
        assert ocr['text'] == ocr_text
        assert ocr['draft'] == text['draft']
        assert ocr['draft']['company'] == '狼腾知光'
        assert ocr['draft']['location'] == '上海临港'
        assert ocr['method'] == 'rules'
