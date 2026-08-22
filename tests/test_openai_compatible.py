"""Tests for the LangChain OpenAI-compatible chat adapter."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from jobagent.models.llm_client import OpenAICompatibleClient


class FakeChatModel:
    def __init__(self, content: object) -> None:
        self.content = content
        self.messages: list[object] = []
        self.kwargs: dict[str, object] = {}

    async def ainvoke(self, messages: list[object], **kwargs: object) -> object:
        self.messages = messages
        self.kwargs = kwargs
        return SimpleNamespace(content=self.content)


@pytest.mark.asyncio
async def test_chat_sends_system_and_user_messages() -> None:
    model = FakeChatModel('{"ok": true}')
    client = OpenAICompatibleClient(model)

    result = await client.chat("hello", system="system", max_tokens=123)

    assert result == '{"ok": true}'
    assert isinstance(model.messages[0], SystemMessage)
    assert isinstance(model.messages[1], HumanMessage)
    assert model.kwargs["max_tokens"] == 123


@pytest.mark.asyncio
async def test_chat_joins_text_content_blocks() -> None:
    model = FakeChatModel([{"type": "text", "text": "hello"}, {"text": " world"}])

    result = await OpenAICompatibleClient(model).chat("question")

    assert result == "hello world"


@pytest.mark.asyncio
async def test_chat_rejects_output_budget_above_model_limit() -> None:
    model = FakeChatModel("unused")
    client = OpenAICompatibleClient(model, max_tokens_limit=100)

    with pytest.raises(ValueError, match="exceeds model limit"):
        await client.chat("question", max_tokens=101)

    assert model.messages == []
