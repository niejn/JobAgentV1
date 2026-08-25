"""Pytest configuration: isolate test logs from production logs."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger as loguru_logger

_TESTS_LOG_DIR = Path("data/logs/tests")
_PYTEST_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} | {message}"
)


def _add_pytest_log_sink() -> int:
    """Add the shared pytest.log sink (does not remove existing sinks)."""

    return loguru_logger.add(
        _TESTS_LOG_DIR / "pytest.log",
        level="DEBUG",
        format=_PYTEST_LOG_FORMAT,
        rotation="10 MB",
        retention="1 week",
        encoding="utf-8",
        backtrace=False,
        diagnose=False,
    )


@pytest.fixture(autouse=True, scope="session")
def _setup_test_logging() -> None:
    """Replace all loguru sinks with test-only sinks.

    Without this fixture, tests write ``data/logs/jobagent.log`` alongside
    production runs, making it impossible to tell which entries came from
    which source.
    """
    _TESTS_LOG_DIR.mkdir(parents=True, exist_ok=True)
    loguru_logger.remove()
    loguru_logger.configure(patcher=lambda record: None)
    _add_pytest_log_sink()
    loguru_logger.add(
        sys.stderr,
        level="WARNING",
        format="{time:HH:mm:ss.SSS} | {level:<7} | {message}",
        filter=lambda record: record["exception"] is not None,
        backtrace=False,
        diagnose=False,
    )


@pytest.fixture(autouse=True, scope="session")
def _redirect_cli_setup_logging_to_tests() -> Iterator[None]:
    """Keep CLI entry points from re-adding the production file sink.

    ``_chat`` and ``_list_sessions_only`` call ``setup_logging()`` which
    calls ``loguru_logger.remove()`` and defaults its file sink to
    ``data/logs/jobagent.log`` -- silently discarding the test sinks above
    and mixing test tracebacks into the production log. Any such call from
    code under test is redirected into ``data/logs/tests/cli.log``, and the
    shared pytest.log sink is restored afterwards.
    """
    import jobagent.cli as cli_module
    from jobagent.observability import setup_logging as real_setup_logging

    cli_log_file = _TESTS_LOG_DIR / "cli.log"

    def _setup_logging_for_tests(**kwargs: Any) -> list[int]:
        kwargs.setdefault("log_file", cli_log_file)
        handler_ids = real_setup_logging(**kwargs)
        handler_ids.append(_add_pytest_log_sink())
        return handler_ids

    original = cli_module.setup_logging
    cli_module.setup_logging = _setup_logging_for_tests
    try:
        yield
    finally:
        cli_module.setup_logging = original
