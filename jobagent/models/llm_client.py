"""OpenAI-compatible model construction for JobAgent."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.model_profile import ModelProfile
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from jobagent.config import Settings

_PROVIDER_ALIASES = {
    "openai": "openai-compatible",
    "openai_compatible": "openai-compatible",
}


class ChatClient(Protocol):
    """Small text-chat interface used by matching and planning modules."""

    async def chat(
        self,
        user_message: str,
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> str:
        """Return assistant text for one user message."""


@dataclass(frozen=True, slots=True)
class LLMRuntimeConfig:
    """Validated OpenAI-compatible runtime configuration."""

    provider: str
    model: str
    api_key: str
    base_url: str
    context_window: int
    max_tokens: int
    reasoning: bool
    supports_developer_role: bool
    thinking_format: str | None
    thinking_level: str | None


class OpenAICompatibleClient:
    """Adapt LangChain ChatOpenAI to JobAgent's text-chat interface."""

    def __init__(self, model: Any, *, max_tokens_limit: int = 128_000) -> None:
        self._model = model
        self._max_tokens_limit = max_tokens_limit

    async def chat(
        self,
        user_message: str,
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> str:
        if max_tokens > self._max_tokens_limit:
            raise ValueError(
                f"max_tokens={max_tokens} exceeds model limit "
                f"{self._max_tokens_limit}"
            )
        messages: list[BaseMessage] = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=user_message))
        response = await self._model.ainvoke(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return _message_text(response.content)


def resolve_llm_config(settings: Settings) -> LLMRuntimeConfig:
    """Validate the single provider supported by the first JobAgent release."""

    requested = settings.jobagent_llm_provider.strip().lower()
    provider = _PROVIDER_ALIASES.get(requested, requested)
    if provider != "openai-compatible":
        raise ValueError(
            "The first JobAgent release supports JOBAGENT_LLM_PROVIDER="
            "openai-compatible only; Claude support is deferred."
        )
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for openai-compatible models")
    if not settings.openai_base_url.strip():
        raise RuntimeError("OPENAI_BASE_URL is required for openai-compatible models")
    if not settings.jobagent_llm_model.strip():
        raise RuntimeError("JOBAGENT_LLM_MODEL is required")
    if settings.jobagent_llm_max_tokens > settings.jobagent_llm_context_window:
        raise ValueError(
            "JOBAGENT_LLM_MAX_TOKENS cannot exceed JOBAGENT_LLM_CONTEXT_WINDOW"
        )
    return LLMRuntimeConfig(
        provider=provider,
        model=settings.jobagent_llm_model.strip(),
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url.rstrip("/"),
        context_window=settings.jobagent_llm_context_window,
        max_tokens=settings.jobagent_llm_max_tokens,
        reasoning=settings.jobagent_llm_reasoning,
        supports_developer_role=settings.jobagent_llm_supports_developer_role,
        thinking_format=_optional_text(settings.jobagent_llm_thinking_format),
        thinking_level=_optional_text(settings.jobagent_llm_thinking_level),
    )


def build_chat_client(
    settings: Settings,
    *,
    model_override: str | None = None,
    openai_factory: Callable[..., Any] = ChatOpenAI,
) -> ChatClient:
    """Build the text client without performing network I/O."""

    runtime = resolve_llm_config(settings)
    llm = openai_factory(
        model=model_override or runtime.model,
        api_key=runtime.api_key,
        base_url=runtime.base_url,
        timeout=float(settings.jobagent_llm_timeout),
        max_retries=2,
        use_responses_api=False,
    )
    return OpenAICompatibleClient(
        _with_fallback(settings, llm, runtime),
        max_tokens_limit=runtime.max_tokens,
    )


def build_agent_model(settings: Settings) -> BaseChatModel:
    """Build the tool-capable model used by the conversational JobAgent."""

    runtime = resolve_llm_config(settings)
    # profile.max_input_tokens lets deepagents' SummarizationMiddleware pick
    # fraction-based compaction thresholds (trigger 85% / keep 10% of the
    # window) instead of its no-profile fallback (fixed 170k tokens).
    profile: ModelProfile = {"max_input_tokens": settings.jobagent_llm_context_window}
    model = ChatOpenAI(
        model=runtime.model,
        api_key=SecretStr(runtime.api_key),
        base_url=runtime.base_url,
        timeout=float(settings.jobagent_llm_timeout),
        max_retries=2,
        max_completion_tokens=runtime.max_tokens,
        use_responses_api=False,
        profile=profile,
    )
    # Keep the native ChatOpenAI object so DeepAgents can inspect and resolve it.
    # ChatOpenAI exposes this constructor field as max_completion_tokens while
    # translating the legacy max_tokens request shape for compatible providers.
    return _with_fallback(settings, model, runtime, profile=profile)


def _with_fallback(
    settings: Settings,
    primary: BaseChatModel,
    runtime: LLMRuntimeConfig,
    *,
    profile: ModelProfile | None = None,
) -> BaseChatModel:
    """Wrap the primary model with a backup when one is configured.

    429（限流）/403（model_access_denied）等供应商级错误时逐次回退到备份
    模型（如 deepseek）。包装器保持 BaseChatModel 接口，deepagents 的
    resolve_model 与 bind_tools 均无感。未配置备份时原样返回（零行为
    变化）。备份缺省复用主供应商的 key/base_url（同网关换模型的场景）。
    ``profile`` 透传给链上每个成员与包装器自身，让 deepagents 的
    SummarizationMiddleware 在包装器实例上也能读到 context window。
    """

    from jobagent.models.fallback import FallbackChatModel

    fallback_model = settings.jobagent_llm_fallback_model.strip()
    if not fallback_model:
        return primary
    backup = ChatOpenAI(
        model=fallback_model,
        api_key=SecretStr(
            settings.jobagent_llm_fallback_api_key or runtime.api_key
        ),
        base_url=(
            settings.jobagent_llm_fallback_base_url.strip() or runtime.base_url
        ).rstrip("/"),
        timeout=float(settings.jobagent_llm_timeout),
        max_retries=2,
        max_completion_tokens=settings.jobagent_llm_fallback_max_tokens,
        use_responses_api=False,
        profile=profile,
    )
    return FallbackChatModel(
        primary=primary,
        fallbacks=(backup,),
        profile=profile,
    )


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    raise TypeError(f"Unsupported LLM message content: {type(content).__name__}")
