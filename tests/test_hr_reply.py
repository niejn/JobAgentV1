"""HR auto-reply subsystem tests: facts, classifier, policy, pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobagent.config import Settings
from jobagent.hr_reply.classifier import (
    Classification,
    HrQuestionClassifier,
)
from jobagent.hr_reply.facts import (
    CandidateFactStore,
    CompanyInsightStore,
    seed_default_facts,
)
from jobagent.hr_reply.orchestrator import (
    HrReplyOrchestrator,
    in_send_window,
)
from jobagent.hr_reply.policy import (
    ConversationStateStore,
    EngineConfig,
    ReplyPolicyEngine,
    question_fingerprint,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        jobagent_state_db=tmp_path / "state.db",
        jobagent_checkpoint_db=tmp_path / "checkpoint.db",
    )


class TestFactStores:
    def test_seed_salary_talking_points_once(self, tmp_path: Path) -> None:
        with CandidateFactStore(tmp_path / "db.sqlite") as store:
            assert seed_default_facts(store) == 1
            assert seed_default_facts(store) == 0
            fact = store.usable("salary_expectation")
            assert fact is not None
            assert any("3 万多" in p for p in fact.payload["talking_points"])
            assert "不暴露底线数字" in fact.constraints

    def test_upsert_versions_and_supersedes(self, tmp_path: Path) -> None:
        with CandidateFactStore(tmp_path / "db.sqlite") as store:
            first = store.upsert(
                category="availability",
                payload={"value": "一个月内到岗"},
                source="user_statement",
            )
            second = store.upsert(
                category="availability",
                payload={"value": "两周内到岗"},
                source="user_statement",
            )
            assert first.version == 1 and second.version == 2
            usable = store.usable("availability")
            assert usable is not None and usable.payload["value"] == "两周内到岗"

    def test_conflict_pauses_category(self, tmp_path: Path) -> None:
        with CandidateFactStore(tmp_path / "db.sqlite") as store:
            store.upsert(
                category="city", payload={"value": "上海"}, source="user_statement"
            )
            store.mark_conflict("city", note="历史提取矛盾")
            assert store.usable("city") is None
            assert "city" in store.gap_categories()

    def test_invalid_source_rejected(self, tmp_path: Path) -> None:
        with CandidateFactStore(tmp_path / "db.sqlite") as store:
            with pytest.raises(ValueError, match="invalid fact source"):
                store.upsert(
                    category="city", payload={"value": "x"}, source="hr_said"
                )

    def test_company_insight_outsourcing_flag(self, tmp_path: Path) -> None:
        with CompanyInsightStore(tmp_path / "db.sqlite") as insights:
            assert insights.is_outsourcing("某外包公司") is False
            insights.upsert(
                company="某外包公司",
                insight_type="outsourcing",
                payload={"is_outsourcing": True, "summary": "HR 自述外包"},
            )
            assert insights.is_outsourcing("某外包公司") is True
            assert insights.is_outsourcing("其他公司") is False


class _StubModel:
    """Structured-output stub: returns queued payloads or raises."""

    def __init__(self, outputs: list[dict[str, Any]] | None = None) -> None:
        self.outputs = outputs or []
        self.calls: list[str] = []

    def with_structured_output(self, schema: Any) -> _StubModel:
        return self

    def invoke(self, prompt: str) -> dict[str, Any]:
        self.calls.append(prompt)
        if not self.outputs:
            raise RuntimeError("stub exhausted")
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestClassifier:
    def test_classifies_intent_with_required_facts(self) -> None:
        model = _StubModel([{"intent": "salary_expectation", "confidence": 0.9}])
        classifier = HrQuestionClassifier(model=model)  # type: ignore[arg-type]
        result = classifier.classify(hr_messages=["您期望薪资多少？"])
        assert result.intent == "salary_expectation"
        assert result.required_facts == ("salary_expectation",)
        assert not result.degraded

    def test_unknown_intent_falls_back(self) -> None:
        model = _StubModel([{"intent": " Gibberish ", "confidence": 0.8}])
        classifier = HrQuestionClassifier(model=model)  # type: ignore[arg-type]
        assert classifier.classify(hr_messages=["?"]).intent == "unknown"

    def test_llm_failure_degrades_never_raises(self) -> None:
        model = _StubModel([RuntimeError("429 quota")])
        classifier = HrQuestionClassifier(model=model)  # type: ignore[arg-type]
        result = classifier.classify(hr_messages=["在吗"])
        assert result.degraded
        assert result.requires_human
        assert "llm_unavailable" in result.degraded_reason

    def test_invalid_structured_output_degrades_to_human(self) -> None:
        model = _StubModel([{"intent": "salary_expectation", "confidence": "NaN"}])
        classifier = HrQuestionClassifier(model=model)  # type: ignore[arg-type]
        result = classifier.classify(hr_messages=["您期望薪资多少？"])
        assert result.degraded
        assert result.requires_human
        assert "invalid_llm_output" in result.degraded_reason


def _facts(store: CandidateFactStore) -> Any:
    seed_default_facts(store)
    store.upsert(
        category="employment_status",
        payload={"value": "在职，看新机会"},
        source="user_statement",
    )
    return store.snapshot()


class TestPolicyEngine:
    def _engine(
        self, tmp_path: Path, *, audit_only: bool = False, insights: Any = None
    ) -> ReplyPolicyEngine:
        return ReplyPolicyEngine(
            config=EngineConfig(audit_only=audit_only),
            insights=insights,
        )

    def _base(self, tmp_path: Path, intent: str = "salary_expectation") -> dict[str, Any]:
        with CandidateFactStore(tmp_path / "db.sqlite") as store:
            return {
                "classification": Classification(
                    intent=intent,
                    confidence=0.95,
                    negotiation_followup=False,
                    question_summary="",
                    required_facts=(intent,),
                ),
                "facts": _facts(store),
                "conversation_state": {},
                "company": "测试公司",
                "fingerprint": question_fingerprint(["q"]),
            }

    def test_salary_first_round_auto_with_facts(self, tmp_path: Path) -> None:
        decision = self._engine(tmp_path).decide(**self._base(tmp_path))
        assert decision.action == "auto"
        assert "3 万多" in decision.draft_text
        assert decision.fact_ids

    def test_fact_gap_routes_human(self, tmp_path: Path) -> None:
        kwargs = self._base(tmp_path, intent="availability")
        decision = self._engine(tmp_path).decide(**kwargs)
        assert decision.action == "human"
        assert "fact_missing:availability" in decision.reasons

    def test_negotiation_followup_always_human(self, tmp_path: Path) -> None:
        kwargs = self._base(tmp_path)
        kwargs["classification"] = Classification(
            intent="salary_expectation",
            confidence=0.95,
            negotiation_followup=True,
            question_summary="HR 压价",
            required_facts=("salary_expectation",),
        )
        decision = self._engine(tmp_path).decide(**kwargs)
        assert decision.action == "human"
        assert "negotiation_followup" in decision.reasons

    def test_interview_time_always_human(self, tmp_path: Path) -> None:
        kwargs = self._base(tmp_path, intent="interview_time")
        decision = self._engine(tmp_path).decide(**kwargs)
        assert decision.action == "human"

    def test_audit_only_never_auto(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path, audit_only=True)
        decision = engine.decide(**self._base(tmp_path))
        assert decision.action == "human"
        assert "audit_only_observation" in decision.reasons
        assert decision.draft_text  # draft still assembled for review

    def test_outsourcing_insight_plus_refusal_declines(self, tmp_path: Path) -> None:
        with CandidateFactStore(tmp_path / "db.sqlite") as store:
            store.upsert(
                category="outsourcing_stance",
                payload={"stance": "refuse", "value": "不考虑外包"},
                source="user_statement",
            )
            facts = store.snapshot()
        with CompanyInsightStore(tmp_path / "db.sqlite") as insights:
            insights.upsert(
                company="外包公司",
                insight_type="outsourcing",
                payload={"is_outsourcing": True},
            )
            engine = ReplyPolicyEngine(
                config=EngineConfig(audit_only=False), insights=insights
            )
            decision = engine.decide(
                classification=Classification(
                    intent="basic_interest",
                    confidence=0.9,
                    negotiation_followup=False,
                    question_summary="",
                ),
                facts=facts,
                conversation_state={},
                company="外包公司",
                fingerprint=question_fingerprint(["感兴趣吗"]),
            )
        assert decision.action == "decline_human"
        assert "预期不太匹配" in decision.draft_text
        assert decision.reasons == ["company_outsourcing_refused"]

    def test_conversation_limits_and_duplicates(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        kwargs = self._base(tmp_path)
        import time as _time

        kwargs["conversation_state"] = {
            "auto_reply_count_24h": 2,
            "window_started_at": _time.time() - 3600,
        }
        assert engine.decide(**kwargs).reasons == ["auto_reply_limit_reached"]
        fingerprint = question_fingerprint(["q"])
        kwargs["conversation_state"] = {
            "last_question_fingerprint": fingerprint,
            "last_question_at": __import__("time").time(),
        }
        kwargs["fingerprint"] = fingerprint
        assert "duplicate_question_within_window" in engine.decide(**kwargs).reasons

    def test_manual_takeover_cooldown(self, tmp_path: Path) -> None:
        import time

        engine = self._engine(tmp_path)
        kwargs = self._base(tmp_path)
        kwargs["conversation_state"] = {"manual_cooldown_until": time.time() + 3600}
        decision = engine.decide(**kwargs)
        assert decision.action == "human"
        assert "manual_takeover_cooldown" in decision.reasons

    def test_low_confidence_human(self, tmp_path: Path) -> None:
        kwargs = self._base(tmp_path)
        kwargs["classification"] = Classification(
            intent="salary_expectation",
            confidence=0.4,
            negotiation_followup=False,
            question_summary="",
            required_facts=("salary_expectation",),
        )
        decision = self._engine(tmp_path).decide(**kwargs)
        assert decision.action == "human"
        assert any(r.startswith("low_confidence") for r in decision.reasons)


class TestConversationStateStore:
    def test_manual_outbound_sets_cooldown(self, tmp_path: Path) -> None:
        import time

        with ConversationStateStore(tmp_path / "db.sqlite") as store:
            store.note_manual_outbound(42, cooldown_hours=4)
            state = store.get(42)
            assert float(state["manual_cooldown_until"]) > time.time() + 3500

    def test_auto_reply_counter_rolls_24h(self, tmp_path: Path) -> None:
        with ConversationStateStore(tmp_path / "db.sqlite") as store:
            store.note_auto_reply(7, "回复一")
            state = store.get(7)
            assert state["auto_reply_count_24h"] == 1
            assert state["last_auto_reply_text"] == "回复一"


class TestOrchestrator:
    def _seed_inbound(self, db: Path, text: str, *, baseline: int = 0) -> None:
        import time as _time

        from jobagent.boss_daemon import BossMonitorStore  # ensures schema

        store = BossMonitorStore(db)
        conn = store._connection  # noqa: SLF001 - test seeding
        conn.execute(
            """INSERT OR REPLACE INTO boss_inbound_messages
            (message_key, conversation_id, platform_message_id, friend_id,
             friend_source, encrypt_boss_id, hr_name, company, title, text,
             sent_at, raw_json, processing_status, baseline, first_seen_at)
            VALUES ('k1', 'c1', 'p1', 42, 0, 'enc', '王HR', '测试公司', '后端',
                    ?, ?, '{}', 'unclassified', ?, datetime('now'))""",
            (text, _time.time() * 1000, baseline),
        )
        conn.commit()
        store.close()

    @pytest.mark.asyncio
    async def test_pipeline_drafts_awaiting_human_in_audit_mode(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        self._seed_inbound(settings.jobagent_state_db, "您期望薪资多少？")
        stub = _StubModel([{"intent": "salary_expectation", "confidence": 0.95}])
        orchestrator = HrReplyOrchestrator(
            settings, classifier=HrQuestionClassifier(model=stub)  # type: ignore[arg-type]
        )
        stats = await orchestrator.run_once(now=morning_timestamp())
        assert stats.processed == 1
        assert stats.awaiting_human == 1
        from jobagent.boss_reply_queue import BossReplyQueue

        queue = BossReplyQueue(settings.jobagent_state_db)
        try:
            pending = queue.pending(10)
            assert len(pending) == 1
            assert pending[0]["intent"] == "salary_expectation"
            assert "3 万多" in pending[0]["draft_text"]
        finally:
            queue.close()

    @pytest.mark.asyncio
    async def test_pipeline_skips_when_degraded(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._seed_inbound(settings.jobagent_state_db, "在吗？")
        stub = _StubModel([RuntimeError("quota")])
        orchestrator = HrReplyOrchestrator(
            settings, classifier=HrQuestionClassifier(model=stub)  # type: ignore[arg-type]
        )
        stats = await orchestrator.run_once(now=morning_timestamp())
        assert stats.degraded == 1
        assert stats.awaiting_human == 1  # degraded -> human, never auto

    def test_send_window(self) -> None:
        import datetime as _dt

        morning = _dt.datetime(2026, 9, 20, 10, 0).timestamp()
        night = _dt.datetime(2026, 9, 20, 23, 0).timestamp()
        assert in_send_window(morning) is True
        assert in_send_window(night) is False

    def test_engine_overrides_persist(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        HrReplyOrchestrator.set_engine_override(db, "auto_enabled", "0")
        assert HrReplyOrchestrator.get_engine_overrides(db)["auto_enabled"] == "0"


def morning_timestamp() -> float:
    """An explicit local 10:00 clock for orchestrator pipeline tests."""

    import datetime as _dt

    return _dt.datetime(2026, 9, 20, 10, 0).timestamp()


class TestDigestAndTools:
    def test_digest_builds_and_renders(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        from jobagent.hr_reply.digest import (
            build_digest,
            render_markdown,
            render_wechat_summary,
            write_report,
        )

        digest = build_digest(settings.jobagent_state_db)
        md = render_markdown(digest)
        assert "Boss HR 沟通日报" in md
        assert "新消息" in render_wechat_summary(digest)
        path = write_report(digest, tmp_path / "reports")
        assert path.exists()

    @pytest.mark.asyncio
    async def test_management_tools_roundtrip(self, tmp_path: Path) -> None:
        from jobagent.tools.boss_management import build_boss_management_tools

        settings = _settings(tmp_path)
        tools = {tool.name: tool for tool in build_boss_management_tools(
            settings.jobagent_state_db
        )}
        facts = await tools["boss_facts_view"].ainvoke({})
        assert facts["status"] == "ok"
        assert "salary_expectation" in facts["gaps"]
        result = await tools["boss_fact_set"].ainvoke(
            {"category": "city", "value": "上海，优先浦东"}
        )
        assert result["status"] == "ok"
        facts2 = await tools["boss_facts_view"].ainvoke({})
        assert "city" not in facts2["gaps"]
        digest = await tools["boss_daily_digest"].ainvoke({})
        assert "日报" in digest["summary"]
        config = await tools["boss_auto_config"].ainvoke(
            {"key": "auto_enabled", "value": "off"}
        )
        assert config["overrides"]["auto_enabled"] == "0"
