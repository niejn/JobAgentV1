"""Tests for structured LLM planning, assessment and preparation."""

from __future__ import annotations

import pytest

from jobagent.interview.intelligence import LLMInterviewIntelligence
from jobagent.interview.research import InterviewResearchTarget


class ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    async def chat(
        self,
        user_message: str,
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> str:
        self.prompts.append(user_message)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_llm_intelligence_parses_fenced_structured_results() -> None:
    client = ScriptedClient(
        [
            '```json\n{"iteration":1,"queries":[{"kind":"exact","text":"字节 后端 面经"}]}\n```',
            '{"note_id":"n1","grade":"A","decision":"accept",'
            '"reasons":["同公司同岗位"],"topics":["Redis"],'
            '"questions":["Redis为什么快？"],"source_answers":[]}',
            '{"jd_focus":["分布式"],"candidate_gaps":["Redis深度待确认"],'
            '"questions":[{"question":"Redis为什么快？","source_answer":null,'
            '"model_answer":"内存访问和高效数据结构。","source_note_ids":["n1"]}]}',
        ]
    )
    intelligence = LLMInterviewIntelligence(client)
    target = InterviewResearchTarget(
        company="字节跳动",
        role="后端开发",
        city="北京",
        job_description="负责 Redis 和分布式服务",
    )

    plan = await intelligence.plan(target, ("精确岗位结果不足",), 1)
    assessment = await intelligence.assess(target, "正文和OCR", "n1")
    pack = await intelligence.prepare(target, (assessment,), '{"skills":["Python"]}')

    assert plan.queries[0].text == "字节 后端 面经"
    assert assessment.accepted is True
    assert pack.questions[0].model_answer == "内存访问和高效数据结构。"
    assert pack.questions[0].source_answer is None
    assert pack.questions[0].source_note_ids == ("n1",)
    assert "精确岗位结果不足" in client.prompts[0]
