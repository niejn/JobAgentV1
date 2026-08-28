"""FallbackChatModel tests: primary -> backup on provider-level errors.

用户实战触发场景：主模型 glm-5.3 撞 429（资源用尽）与 403
（model_access_denied）。包装器在 openai 兼容栈的供应商级错误上逐次回退，
400 类不回退直接抛。
"""

from __future__ import annotations

import httpx
import openai
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from jobagent.models.fallback import FALLBACK_EXCEPTIONS, FallbackChatModel


def _api_error(cls: type[openai.APIStatusError], status: int) -> Exception:
    request = httpx.Request("POST", "https://api.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return cls("provider exploded", response=response, body=None)


class FlakyModel:
    """按脚本逐次抛错或最终成功的 fake 模型（宽松接口）。"""

    def __init__(self, name: str, script: list[BaseException | str]) -> None:
        self._name = name
        self._script = list(script)
        self.calls = 0
        self.received_kwargs: list[dict] = []

    def invoke(self, messages, stop=None, **kwargs):
        self.calls += 1
        self.received_kwargs.append(kwargs)
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return AIMessage(content=step)

    async def ainvoke(self, messages, stop=None, **kwargs):
        self.calls += 1
        self.received_kwargs.append(kwargs)
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return AIMessage(content=step)

    def bind_tools(self, tools, **kwargs):
        return FlakyModel(f"{self._name}-bound", ["bound-ok"])


MESSAGE = [HumanMessage(content="你好")]


# ---- 回退触发 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        _api_error(openai.RateLimitError, 429),
        _api_error(openai.PermissionDeniedError, 403),
        _api_error(openai.AuthenticationError, 401),
        _api_error(openai.InternalServerError, 500),
        openai.APIConnectionError(request=httpx.Request("POST", "https://api.test")),
    ],
    ids=["429", "403", "401", "500", "connection"],
)
def test_provider_level_errors_fall_back_to_backup(error: BaseException) -> None:
    primary = FlakyModel("primary", [error])
    backup = FlakyModel("backup", ["备份回复"])
    chain = FallbackChatModel(primary=primary, fallbacks=(backup,))

    result = chain.invoke(MESSAGE)

    assert result.content == "备份回复"
    assert primary.calls == 1
    assert backup.calls == 1


@pytest.mark.asyncio
async def test_async_provider_error_falls_back() -> None:
    primary = FlakyModel("primary", [_api_error(openai.PermissionDeniedError, 403)])
    backup = FlakyModel("backup", ["异步备份回复"])
    chain = FallbackChatModel(primary=primary, fallbacks=(backup,))

    result = await chain.ainvoke(MESSAGE)

    assert result.content == "异步备份回复"


def test_primary_success_never_touches_backup() -> None:
    primary = FlakyModel("primary", ["主模型正常"])
    backup = FlakyModel("backup", ["不应被调用"])
    chain = FallbackChatModel(primary=primary, fallbacks=(backup,))

    result = chain.invoke(MESSAGE)

    assert result.content == "主模型正常"
    assert backup.calls == 0


def test_all_candidates_failing_reraises_last_error() -> None:
    first = _api_error(openai.RateLimitError, 429)
    last = _api_error(openai.PermissionDeniedError, 403)
    primary = FlakyModel("primary", [first])
    backup = FlakyModel("backup", [last])
    chain = FallbackChatModel(primary=primary, fallbacks=(backup,))

    with pytest.raises(openai.PermissionDeniedError, match="provider exploded"):
        chain.invoke(MESSAGE)


def test_non_provider_errors_propagate_without_fallback() -> None:
    """400 类（上下文超长等）换模型大概率同样失败：直接抛真实错误。"""

    primary = FlakyModel("primary", [ValueError("bad request shape")])
    backup = FlakyModel("backup", ["不应被调用"])
    chain = FallbackChatModel(primary=primary, fallbacks=(backup,))

    with pytest.raises(ValueError, match="bad request shape"):
        chain.invoke(MESSAGE)
    assert backup.calls == 0


# ---- 接口保持（deepagents / 优雅收尾路径依赖） ---------------------------------


def test_bind_tools_rebuilds_the_chain_with_bound_members() -> None:
    primary = FlakyModel("primary", [_api_error(openai.RateLimitError, 429)])
    backup = FlakyModel("backup", ["ok"])
    chain = FallbackChatModel(primary=primary, fallbacks=(backup,))

    bound = chain.bind_tools([{"name": "t"}])

    assert isinstance(bound, FallbackChatModel)
    result = bound.invoke(MESSAGE)
    assert result.content == "bound-ok"


def test_call_kwargs_delegate_to_each_candidate() -> None:
    primary = FlakyModel("primary", [_api_error(openai.RateLimitError, 429)])
    backup = FlakyModel("backup", ["ok"])
    chain = FallbackChatModel(primary=primary, fallbacks=(backup,))

    chain.invoke(MESSAGE, max_tokens=4096, temperature=0.0)

    assert primary.received_kwargs[0] == {"max_tokens": 4096, "temperature": 0.0}
    assert backup.received_kwargs[0] == {"max_tokens": 4096, "temperature": 0.0}


def test_model_name_shows_the_full_chain_for_diagnostics() -> None:
    primary = FlakyModel("primary", ["x"])
    primary.model_name = "glm-5.3"
    backup = FlakyModel("backup", ["y"])
    backup.model_name = "deepseek-chat"

    chain = FallbackChatModel(primary=primary, fallbacks=(backup,))

    assert chain.model_name == "glm-5.3 -> deepseek-chat"


def test_is_base_chat_model_for_deepagents_resolution() -> None:
    from langchain_core.language_models.chat_models import BaseChatModel

    chain = FallbackChatModel(
        primary=FakeListChatModel(responses=["x"]),
        fallbacks=(FakeListChatModel(responses=["y"]),),
    )
    assert isinstance(chain, BaseChatModel)


def test_fallback_exception_surface_covers_user_incidents() -> None:
    """用户实战的 429/403 必须落在回退面上（直接或经子类关系）。"""

    def covered(exc: type[BaseException]) -> bool:
        return any(issubclass(exc, handled) for handled in FALLBACK_EXCEPTIONS)

    assert covered(openai.RateLimitError)      # 429 资源用尽
    assert covered(openai.PermissionDeniedError)  # 403 model_access_denied
    assert covered(openai.AuthenticationError)  # 401
    assert covered(openai.InternalServerError)  # 5xx
    assert covered(openai.APIConnectionError)   # 网络/超时


# ---- 配置接线 ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_fallback_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fallback-off tests must not be flipped by a developer shell that
    exports JOBAGENT_LLM_FALLBACK_* (live leak seen: shell env polluted the
    suite, 3 tests failed only inside that shell)."""
    for var in (
        "JOBAGENT_LLM_FALLBACK_MODEL",
        "JOBAGENT_LLM_FALLBACK_BASE_URL",
        "JOBAGENT_LLM_FALLBACK_API_KEY",
        "JOBAGENT_LLM_FALLBACK_MAX_TOKENS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_build_agent_model_without_fallback_stays_native(tmp_path) -> None:
    from langchain_openai import ChatOpenAI

    from jobagent.config import Settings
    from jobagent.models.llm_client import build_agent_model

    settings = Settings(
        _env_file=None,
        openai_api_key="k",
        openai_base_url="https://gw.test/v1",
        jobagent_llm_model="glm-5.3",
    )
    model = build_agent_model(settings)
    assert type(model) is ChatOpenAI  # 未配置备份：零行为变化


def test_build_agent_model_with_fallback_wraps_chain(tmp_path) -> None:
    from jobagent.config import Settings
    from jobagent.models.fallback import FallbackChatModel
    from jobagent.models.llm_client import build_agent_model

    settings = Settings(
        _env_file=None,
        openai_api_key="primary-key",
        openai_base_url="https://gw.test/v1",
        jobagent_llm_model="glm-5.3",
        jobagent_llm_fallback_model="deepseek-chat",
        jobagent_llm_fallback_base_url="https://api.deepseek.com/v1",
        jobagent_llm_fallback_api_key="deepseek-key",
    )
    model = build_agent_model(settings)

    assert isinstance(model, FallbackChatModel)
    backup = model.fallbacks[0]
    assert backup.model_name == "deepseek-chat"
    assert backup.openai_api_base == "https://api.deepseek.com/v1"
    assert model.model_name == "glm-5.3 -> deepseek-chat"


def test_build_agent_model_fallback_defaults_reuse_primary_endpoint() -> None:
    from jobagent.config import Settings
    from jobagent.models.fallback import FallbackChatModel
    from jobagent.models.llm_client import build_agent_model

    settings = Settings(
        _env_file=None,
        openai_api_key="primary-key",
        openai_base_url="https://gw.test/v1",
        jobagent_llm_model="glm-5.3",
        jobagent_llm_fallback_model="glm-5.3-air",  # 同网关换模型
    )
    model = build_agent_model(settings)

    assert isinstance(model, FallbackChatModel)
    backup = model.fallbacks[0]
    assert backup.openai_api_base == "https://gw.test/v1"
