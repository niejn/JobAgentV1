"""Fallback chain chat model: primary -> backup on provider-level failures.

为什么不用 ``with_fallbacks()``：它返回 ``RunnableWithFallbacks``，而
deepagents 的 ``resolve_model`` 只认 ``BaseChatModel``（非字符串实例会被
当 model spec 误处理）。自写薄包装保持 BaseChatModel 接口，deepagents、
``bind_tools``、``bind(max_tokens=...)`` 优雅收尾路径全部无感。

回退只对“换一个模型可能有救”的错误生效（openai 兼容栈）：
- ``APIStatusError`` 家族：429 限流 / 403 model_access_denied / 401 认证 /
  5xx 服务端错误——用户实战同时撞过 429 与 403。
- ``APIConnectionError`` 家族：网络/超时。
400 类（如上下文超长）不回退：换模型大概率同样失败，直接抛出真实错误。
"""

from __future__ import annotations

import logging
from typing import Any

import openai
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs.chat_result import ChatResult

logger = logging.getLogger(__name__)

#: 值得换模型重试的错误（openai 兼容栈；APITimeoutError 是 APIConnectionError 子类）。
FALLBACK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    openai.APIStatusError,
    openai.APIConnectionError,
)


def _string_attr(obj: Any, name: str) -> str | None:
    value = getattr(obj, name, None)
    return value.strip() if isinstance(value, str) and value.strip() else None


class FallbackChatModel(BaseChatModel):
    """Primary model with backups; delegates and falls back per-call.

    链上每个成员都是完整 ``BaseChatModel``（各自带 key/base_url/参数），
    逐个尝试直到成功；全部失败时重抛最后一个错误。``bind_tools`` 逐成员
    绑定后重建链条，保持 BaseChatModel 接口不变。
    """

    primary: Any
    fallbacks: tuple

    @property
    def _llm_type(self) -> str:
        return "jobagent-fallback-chain"

    @property
    def model_name(self) -> str:
        """展示名：主模型 -> 备份链（元数据层与诊断用）。"""

        names = [
            _string_attr(self.primary, "model_name") or type(self.primary).__name__,
            *(
                _string_attr(m, "model_name") or type(m).__name__
                for m in self.fallbacks
            ),
        ]
        return " -> ".join(names)

    def _chain(self) -> tuple:
        return (self.primary, *self.fallbacks)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_error: BaseException | None = None
        for index, model in enumerate(self._chain()):
            try:
                # 不透传 run_manager：外层 invoke 已为本模型开关回调事件，
                # 内层各自再开会重复计时。
                result = model.invoke(messages, stop=stop, **kwargs)
                if index > 0:
                    logger.warning(
                        "jobagent.llm_fallback_used",
                        extra={"failed_count": index},
                    )
                return self._as_chat_result(result)
            except FALLBACK_EXCEPTIONS as exc:
                last_error = exc
                logger.warning(
                    "jobagent.llm_fallback_candidate_failed",
                    extra={"candidate": index, "error": repr(exc)[:300]},
                )
        assert last_error is not None
        raise last_error

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_error: BaseException | None = None
        for index, model in enumerate(self._chain()):
            try:
                result = await model.ainvoke(messages, stop=stop, **kwargs)
                if index > 0:
                    logger.warning(
                        "jobagent.llm_fallback_used",
                        extra={"failed_count": index},
                    )
                return self._as_chat_result(result)
            except FALLBACK_EXCEPTIONS as exc:
                last_error = exc
                logger.warning(
                    "jobagent.llm_fallback_candidate_failed",
                    extra={"candidate": index, "error": repr(exc)[:300]},
                )
        assert last_error is not None
        raise last_error

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return FallbackChatModel(
            primary=self.primary.bind_tools(tools, **kwargs),
            fallbacks=tuple(m.bind_tools(tools, **kwargs) for m in self.fallbacks),
        )

    @staticmethod
    def _as_chat_result(result: Any) -> ChatResult:
        if isinstance(result, ChatResult):
            return result
        # 成员返回 AIMessage（宽松实现/测试 fake）时包装成 ChatResult。
        generations = result.generations if hasattr(result, "generations") else [result]
        from langchain_core.outputs import ChatGeneration

        return ChatResult(
            generations=[
                gen if isinstance(gen, ChatGeneration) else ChatGeneration(message=gen)
                for gen in generations
            ]
        )
