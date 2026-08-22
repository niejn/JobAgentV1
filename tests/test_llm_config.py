"""Tests for explicit and automatic LLM provider selection."""

import pytest

from jobagent.config import Settings
from jobagent.models.llm_client import (
    build_agent_model,
    build_chat_client,
    resolve_llm_config,
)


def test_openai_compatible_configuration_is_resolved() -> None:
    settings = Settings(
        _env_file=None,
        jobagent_llm_provider="openai-compatible",
        jobagent_llm_model="deepseek-chat",
        openai_api_key="test-key",
        openai_base_url="https://api.deepseek.com/v1/",
        jobagent_llm_context_window=1_024_000,
        jobagent_llm_max_tokens=128_000,
        jobagent_llm_reasoning=True,
        jobagent_llm_input_modalities="text",
        jobagent_llm_supports_developer_role=False,
        jobagent_llm_thinking_format="deepseek",
    )

    runtime = resolve_llm_config(settings)

    assert runtime.provider == "openai-compatible"
    assert runtime.model == "deepseek-chat"
    assert runtime.base_url == "https://api.deepseek.com/v1"
    assert runtime.context_window == 1_024_000
    assert runtime.max_tokens == 128_000
    assert runtime.reasoning is True
    assert runtime.input_modalities == ("text",)
    assert runtime.supports_developer_role is False
    assert runtime.thinking_format == "deepseek"


def test_claude_provider_is_deferred_in_first_release() -> None:
    settings = Settings(
        _env_file=None,
        jobagent_llm_provider="claude-oauth",
        openai_api_key="test-key",
    )

    with pytest.raises(ValueError, match="Claude support is deferred"):
        resolve_llm_config(settings)


def test_explicit_provider_requires_its_own_key() -> None:
    settings = Settings(
        _env_file=None,
        jobagent_llm_provider="openai-compatible",
        openai_api_key=None,
    )

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        resolve_llm_config(settings)


def test_builder_passes_base_url_key_and_model_to_chat_openai() -> None:
    captured: dict[str, object] = {}

    class FakeModel:
        pass

    def fake_factory(**kwargs: object) -> FakeModel:
        captured.update(kwargs)
        return FakeModel()

    settings = Settings(
        _env_file=None,
        jobagent_llm_provider="openai-compatible",
        jobagent_llm_model="doubao-model-id",
        openai_api_key="test-key",
        openai_base_url="https://ark.example/api/v3",
        jobagent_llm_max_tokens=128_000,
        jobagent_llm_timeout=91,
    )

    build_chat_client(settings, openai_factory=fake_factory)

    assert captured["model"] == "doubao-model-id"
    assert captured["api_key"] == "test-key"
    assert captured["base_url"] == "https://ark.example/api/v3"
    assert captured["use_responses_api"] is False
    assert captured["timeout"] == 91.0


def test_max_tokens_cannot_exceed_context_window() -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="test-key",
        jobagent_llm_context_window=1_000,
        jobagent_llm_max_tokens=1_001,
    )

    with pytest.raises(ValueError, match="cannot exceed"):
        resolve_llm_config(settings)


def test_agent_model_binds_ark_compatible_max_tokens_field() -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="test-key",
        openai_base_url="https://ark.example/api/plan/v3",
        jobagent_llm_model="glm-5.3",
        jobagent_llm_context_window=1_024_000,
        jobagent_llm_max_tokens=128_000,
    )

    model = build_agent_model(settings)

    assert model.kwargs == {"max_tokens": 128_000}  # type: ignore[attr-defined]
