"""Tests for cross-platform job identity normalization (F1-R4 JI-1)."""

from __future__ import annotations

from jobagent.journey.identity import (
    canonical_company,
    derive_business_line,
    identity_key,
    normalize_company,
    normalize_title,
)


class TestNormalizeCompany:
    def test_strips_legal_suffix_chain(self) -> None:
        assert normalize_company("上海沁诚信息科技有限公司") == "上海沁诚"

    def test_strips_english_suffixes(self) -> None:
        assert normalize_company("Acme Technology Inc.") == "acme"
        assert normalize_company("Foo Bar Ltd") == "foobar"

    def test_drops_parenthesized_qualifiers(self) -> None:
        assert normalize_company("某某公司（Qinchengsoft / NextAI）") != ""

    def test_alias_group_maps_to_representative(self) -> None:
        assert normalize_company("字节跳动") == "bytedance"
        assert normalize_company("ByteDance") == "bytedance"
        assert normalize_company("阿里巴巴集团") == "alibaba"
        assert normalize_company("Dell EMC") == "dell"

    def test_fallback_when_everything_would_strip(self) -> None:
        # 「集团」 alone must not collapse to an empty token.
        assert normalize_company("集团") == "集团"


class TestCanonicalCompany:
    def test_representative_matches_group_key(self) -> None:
        assert canonical_company("抖音集团") == "bytedance"
        assert canonical_company("蚂蚁金服") == "ant"


class TestNormalizeTitle:
    def test_drops_parenthesized_direction(self) -> None:
        assert (
            normalize_title("全栈开发工程师（RAG & Agent 方向）")
            == normalize_title("全栈开发工程师")
        )

    def test_drops_seniority(self) -> None:
        assert normalize_title("资深 AI Agent 开发工程师") == normalize_title(
            "AI Agent 开发工程师"
        )
        assert normalize_title("Senior Backend Engineer") == normalize_title(
            "Backend Engineer"
        )

    def test_folds_engineer_spellings(self) -> None:
        assert normalize_title("后端研发工程师") == normalize_title("后端开发工程师")
        assert normalize_title("Python 开发工程师").endswith("eng")

    def test_keeps_intern_distinct(self) -> None:
        assert normalize_title("AI Agent 开发实习生") != normalize_title(
            "AI Agent 开发工程师"
        )

    def test_keeps_pm_distinct_from_eng(self) -> None:
        assert normalize_title("AI 产品经理") != normalize_title("AI 开发工程师")


class TestBusinessLine:
    def test_agent_from_title(self) -> None:
        assert derive_business_line("AI Agent 开发工程师") == "agent"

    def test_rag_from_jd_text(self) -> None:
        assert derive_business_line("全栈工程师", "负责企业 RAG 知识库") == "rag"

    def test_defaults_to_general(self) -> None:
        assert derive_business_line("运营专员") == "general"


class TestIdentityKey:
    def test_same_job_different_writings_share_key(self) -> None:
        a = identity_key("上海沁诚信息科技有限公司", "资深 AI Agent 开发工程师")
        b = identity_key("上海沁诚", "AI Agent 开发工程师")
        assert a == b

    def test_missing_direction_falls_to_l3_not_force_merged(self) -> None:
        # One source carries the direction qualifier, the other does not:
        # L2 intentionally refuses to merge (roadmap F1-R4).
        with_dir = identity_key("某公司", "全栈工程师（Agent 方向）", business_line="agent")
        without_dir = identity_key("某公司", "全栈工程师")
        assert with_dir != without_dir

    def test_explicit_line_overrides_inference(self) -> None:
        inferred = identity_key("某公司", "全栈开发工程师")
        explicit = identity_key("某公司", "全栈开发工程师", business_line="rag")
        assert inferred != explicit
