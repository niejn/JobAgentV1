"""Tests for loguru-based structured logs and node tracing."""

from __future__ import annotations

import logging
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobagent.observability import (
    NodeTraceMiddleware,
    begin_trace,
    current_trace_id,
    log_decision,
    reset_trace,
    setup_logging,
    summarize_value,
    trace_node,
)


def test_setup_logging_routes_stdlib_into_loguru_sinks(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "jobagent.log"
    handler_ids = setup_logging(
        log_level="INFO",
        log_file=log_file,
        console_level="ERROR",
    )
    try:
        logger = logging.getLogger("jobagent.sink-test")
        logger.setLevel(logging.INFO)
        logger.info("普通日志行")
        log_decision(
            logger,
            "research.stop_gate",
            basis={"iteration": 3},
            outcome="no_marginal_gain",
        )
    finally:

        from loguru import logger as loguru_logger

        loguru_logger.complete()
        for handler_id in handler_ids:
            loguru_logger.remove(handler_id)

    content = log_file.read_text(encoding="utf-8")
    plain_line = next(line for line in content.splitlines() if "普通日志行" in line)
    # Every line carries time, level, logger, function name, and line number.
    assert "INFO" in plain_line
    assert "jobagent.sink-test" in plain_line
    assert "test_setup_logging_routes_stdlib_into_loguru_sinks" in plain_line
    assert ":" in plain_line  # function:line suffix
    decision_line = next(
        line for line in content.splitlines() if "research.stop_gate" in line
    )
    assert '"outcome": "no_marginal_gain"' in decision_line
    assert "iteration" in decision_line


def test_setup_logging_suppresses_noisy_third_party_loggers(tmp_path: Path) -> None:
    handler_ids = setup_logging(
        log_level="INFO",
        log_file=tmp_path / "logs" / "jobagent.log",
        console_level="ERROR",
    )
    try:
        assert logging.getLogger("urllib3").level == logging.WARNING
        assert logging.getLogger("urllib3.connectionpool").level == logging.ERROR
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("jobagent").level == logging.INFO
    finally:
        from loguru import logger as loguru_logger

        for handler_id in handler_ids:
            loguru_logger.remove(handler_id)


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


@pytest.mark.asyncio
async def test_trace_node_decorator_supports_async_nodes(caplog) -> None:
    @trace_node
    async def async_node(state: dict[str, str]) -> dict[str, str]:
        return {"result": state["value"]}

    with caplog.at_level("INFO", logger="jobagent.node"):
        result = await async_node({"value": "ok"})

    assert result == {"result": "ok"}
    assert [record.phase for record in caplog.records] == ["entry", "exit"]


def test_decision_log_contains_basis_and_outcome(caplog) -> None:
    logger = __import__("logging").getLogger("jobagent.decision-test")

    with caplog.at_level("INFO", logger="jobagent.decision-test"):
        log_decision(
            logger,
            "research.stop_gate",
            basis={"iteration": 3, "coverage": 2, "max_iterations": 3},
            outcome="no_marginal_gain",
        )

    record = caplog.records[-1]
    assert record.event_type == "decision"
    assert record.decision_name == "research.stop_gate"
    assert record.outcome == "no_marginal_gain"
    assert "iteration" in record.basis


def test_trace_id_is_present_on_structured_decision_log(caplog) -> None:
    logger = __import__("logging").getLogger("jobagent.trace-test")
    token = begin_trace()
    trace_id = current_trace_id()
    try:
        with caplog.at_level("INFO", logger="jobagent.trace-test"):
            log_decision(logger, "test.choice", basis={"step": 1}, outcome="continue")
    finally:
        reset_trace(token)

    assert trace_id
    assert caplog.records[-1].name == "jobagent.trace-test"
    assert caplog.records[-1].trace_id == trace_id


def test_reset_trace_ignores_token_from_another_context() -> None:
    """Async-generator cleanup must not leak a ContextVar traceback."""

    token = copy_context().run(begin_trace)
    reset_trace(token)


def test_trace_node_decorator_records_sync_success_and_exception(caplog) -> None:
    @trace_node
    def sync_node(state: dict[str, str]) -> dict[str, str]:
        return {"result": state["value"].upper()}

    @trace_node
    def failing_node(state: dict[str, str]) -> dict[str, str]:
        raise ValueError("expected failure")

    with caplog.at_level("INFO", logger="jobagent.node"):
        assert sync_node({"value": "ok"}) == {"result": "OK"}
        with pytest.raises(ValueError):
            failing_node({"value": "bad"})

    assert [record.phase for record in caplog.records] == ["entry", "exit", "entry", "exit"]
    assert caplog.records[-1].error_type == "ValueError"
