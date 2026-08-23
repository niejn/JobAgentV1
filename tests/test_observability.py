"""Tests for structured rotating logs and node tracing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from jobagent.observability import NodeTraceMiddleware, summarize_value


def test_logging_yaml_uses_one_mib_seven_file_rotation() -> None:
    config = yaml.safe_load(Path("logging.yml").read_text(encoding="utf-8"))
    handler = config["handlers"]["rotating_file"]

    assert handler["class"] == "logging.handlers.RotatingFileHandler"
    assert handler["maxBytes"] == 1_048_576
    assert handler["backupCount"] == 7


@pytest.mark.asyncio
async def test_node_trace_logs_model_and_tool_entry_exit(caplog) -> None:
    middleware = NodeTraceMiddleware()
    model_request = SimpleNamespace(
        state={"messages": [SimpleNamespace(type="human", content="hello")]}
    )
    model_response = SimpleNamespace(
        result=[SimpleNamespace(content="answer", tool_calls=[])]
    )
    tool_request = SimpleNamespace(
        state={},
        tool_call={
            "name": "save_shared_url",
            "args": {"url": "https://xhs.test/item?xsec_token=secret"},
        },
    )
    tool_response = SimpleNamespace(content='{"status":"completed"}')

    async def model_handler(request):
        return model_response

    async def tool_handler(request):
        return tool_response

    with caplog.at_level("INFO", logger="jobagent.node"):
        await middleware.awrap_model_call(model_request, model_handler)
        await middleware.awrap_tool_call(tool_request, tool_handler)

    entries = [record for record in caplog.records if record.phase == "entry"]
    exits = [record for record in caplog.records if record.phase == "exit"]
    assert {record.node_name for record in entries} == {"model", "tools.save_shared_url"}
    assert {record.node_name for record in exits} == {"model", "tools.save_shared_url"}
    assert all(record.duration_ms >= 0 for record in exits)
    assert "secret" not in summarize_value(tool_request.tool_call["args"])
