"""Structured logging and node tracing for the JobAgent runtime.

Logging is powered by loguru: stdlib ``logging.getLogger(...)`` calls across
the codebase (and loguru calls inside Spider_XHS) are routed through one
``InterceptHandler`` into two loguru sinks - a colored console stream and a
rotating file. Every line carries time, level, logger, function name, and
line number; decision/node events additionally append their structured
fields as JSON so machine parsing keeps working.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import re
import sys
import time
import traceback
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypeVar

from langchain.agents.middleware import AgentMiddleware
from loguru import logger as loguru_logger

_TRACE_ID: ContextVar[str | None] = ContextVar("jobagent_trace_id", default=None)

# Single source of truth for sensitive-name matching; add new platform
# credential names here (both the key regex and the query regex derive).
_SENSITIVE_NAMES = (
    "token",
    "key",
    "cookie",
    "password",
    "secret",
    "authorization",
    "web_session",
    "session",
    "a1",
    "csrf",
)
_SENSITIVE_KEY = re.compile("|".join(_SENSITIVE_NAMES), re.I)
_SENSITIVE_QUERY = re.compile(r"(?i)(" + "|".join(_SENSITIVE_NAMES) + r")=([^&\s]+)")
_NodeCallable = TypeVar("_NodeCallable", bound=Any)

_DEFAULT_LOG_FILE = Path("data/logs/jobagent.log")
_LOG_ROTATION = "10 MB"
_LOG_RETENTION = 3
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[logger_name]}</cyan>:"
    "<blue>{function}</blue>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "[{thread.name}] {extra[logger_name]}:{function}:{line} | {message}"
)
_STRUCTURED_KEYS = (
    "event_type",
    "decision_name",
    "basis",
    "outcome",
    "node_name",
    "phase",
    "duration_ms",
    "input_summary",
    "output_summary",
    "error_type",
)
# Standard-library LogRecord attributes; everything else in ``record.__dict__``
# came from ``extra={...}`` and must be forwarded to loguru.
_STDLIB_RECORD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}

# Third-party noise that must not reach the console/file at INFO.
_NOISY_LOGGERS = {
    "urllib3": logging.WARNING,
    "urllib3.connectionpool": logging.ERROR,
    "urllib3.util.retry": logging.ERROR,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "asyncio": logging.WARNING,
    "playwright": logging.WARNING,
}


class InterceptHandler(logging.Handler):
    """Route stdlib logging records into loguru with full caller context."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find the frame where the logged message originated so loguru
        # reports the real function name and line number.
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STDLIB_RECORD_FIELDS
        }
        message = record.getMessage()
        error = record.exc_info[1] if record.exc_info else None
        has_exception = error is not None
        if has_exception:
            # loguru's callable format templates do not auto-append
            # tracebacks, so embed the formatted exception in the message.
            message += "\n" + "".join(
                traceback.format_exception(error)
            ).rstrip()
        loguru_logger.opt(depth=depth).bind(
            logger_name=record.name,
            has_exception=has_exception,
            **extras,
        ).log(level, message)


def _default_logger_name(record: Any) -> None:
    """Patcher: native loguru records (e.g. Spider_XHS) use their module name."""

    record["extra"].setdefault("logger_name", record["name"])


def _file_format(record: Any) -> str:
    """Readable file lines; structured events append their fields as JSON."""

    extras = {
        key: record["extra"][key]
        for key in _STRUCTURED_KEYS
        if record["extra"].get(key) is not None
    }
    if extras.get("event_type") in {"decision", "node"} or "decision_name" in extras:
        extras.setdefault("trace_id", current_trace_id())
        suffix = json.dumps(extras, ensure_ascii=False, default=str)
        # The returned template is formatted with the record, so literal
        # braces from the JSON payload must be escaped.
        escaped = suffix.replace("{", "{{").replace("}", "}}")
        return _FILE_FORMAT + "  " + escaped + "\n"
    return _FILE_FORMAT + "\n"


def setup_logging(
    *,
    log_level: str | None = None,
    log_file: Path | None = None,
    console_level: str | None = None,
) -> list[int]:
    """Configure loguru sinks and route all stdlib logging into them.

    Args:
        log_level: File/console verbosity for ``jobagent`` loggers; defaults to
            ``JOBAGENT_LOG_LEVEL`` (INFO).
        log_file: Rotating log target; defaults to ``JOBAGENT_LOG_FILE``.
        console_level: Console sink level; defaults to WARNING so the chat
            interface stays clean while the file keeps the full trace.

    Returns:
        The list of loguru handler ids (useful for tests to remove sinks).
    """

    from jobagent.config import get_settings

    settings = get_settings()
    level = (log_level or settings.jobagent_log_level).upper()
    log_path = (log_file or settings.jobagent_log_file).expanduser()
    console = (console_level or "WARNING").upper()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    loguru_logger.remove()
    loguru_logger.configure(patcher=_default_logger_name)
    handler_ids = [
        loguru_logger.add(
            sys.stdout,
            level=console,
            format=_CONSOLE_FORMAT,
            # Structured node/decision events stay file-only; records carrying
            # an exception keep their traceback out of the chat console too
            # (the CLI prints its own friendly error message instead).
            filter=lambda record: (
                not record["extra"].get("event_type")
                and record["exception"] is None
                and not record["extra"].get("has_exception")
            ),
            backtrace=False,
            diagnose=False,
        ),
        loguru_logger.add(
            log_path,
            level=level,
            format=_file_format,
            rotation=_LOG_ROTATION,
            retention=_LOG_RETENTION,
            encoding="utf-8",
            backtrace=False,
            diagnose=False,
        ),
    ]

    # All stdlib logging across the codebase flows through the intercept
    # handler into the loguru sinks above.
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.WARNING, force=True)
    logging.getLogger("jobagent").setLevel(level)
    for name, noisy_level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(noisy_level)
    return handler_ids


def begin_trace() -> tuple[ContextVar[str | None], Any]:
    """Start a request trace and return its reset token."""

    return _TRACE_ID, _TRACE_ID.set(uuid.uuid4().hex)


def reset_trace(token: tuple[ContextVar[str | None], Any]) -> None:
    """Restore the previous trace context after a request completes."""

    variable, context_token = token
    variable.reset(context_token)


def current_trace_id() -> str | None:
    return _TRACE_ID.get()


def log_decision(
    logger: logging.Logger,
    decision_name: str,
    *,
    basis: dict[str, Any],
    outcome: str,
) -> None:
    """Write a structured decision event with its explicit decision basis."""

    logger.info(
        "agent.decision",
        extra={
            "event_type": "decision",
            "decision_name": decision_name,
            "basis": summarize_value(basis),
            "outcome": outcome,
            "trace_id": current_trace_id(),
        },
    )


def trace_node(node_func: _NodeCallable) -> _NodeCallable:
    """Decorate a sync or async LangGraph node with structured entry/exit tracing."""

    node_name = getattr(node_func, "__name__", "anonymous_node")
    if inspect.iscoroutinefunction(node_func):

        @functools.wraps(node_func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            input_value = args[0] if args else kwargs
            _log_node_event(node_name, "entry", input_value=input_value)
            try:
                output = await node_func(*args, **kwargs)
            except Exception as exc:
                _log_node_event(
                    node_name,
                    "exit",
                    duration_ms=_duration_ms(started),
                    output_summary="error",
                    error_type=type(exc).__name__,
                )
                raise
            _log_node_event(
                node_name,
                "exit",
                duration_ms=_duration_ms(started),
                output_summary=summarize_value(output),
            )
            return output

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(node_func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        input_value = args[0] if args else kwargs
        _log_node_event(node_name, "entry", input_value=input_value)
        try:
            output = node_func(*args, **kwargs)
        except Exception as exc:
            _log_node_event(
                node_name,
                "exit",
                duration_ms=_duration_ms(started),
                output_summary="error",
                error_type=type(exc).__name__,
            )
            raise
        _log_node_event(
            node_name,
            "exit",
            duration_ms=_duration_ms(started),
            output_summary=summarize_value(output),
        )
        return output

    return sync_wrapper  # type: ignore[return-value]


class NodeTraceMiddleware(AgentMiddleware):
    """Trace model and Tool nodes with bounded, redacted input/output summaries."""

    name = "NodeTraceMiddleware"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("jobagent.node")
        self._model_started: ContextVar[float | None] = ContextVar(
            "jobagent_model_started", default=None
        )

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        started = time.perf_counter()
        token = self._model_started.set(started)
        input_summary = summarize_state(getattr(request, "state", {}))
        self._emit("model", "entry", input_summary=input_summary)
        try:
            response = await handler(request)
        except Exception as exc:
            self._emit(
                "model",
                "exit",
                duration_ms=_duration_ms(started),
                output_summary="error",
                error_type=type(exc).__name__,
            )
            raise
        finally:
            self._model_started.reset(token)
        self._emit(
            "model",
            "exit",
            duration_ms=_duration_ms(started),
            output_summary=summarize_model_response(response),
        )
        return response

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        started = time.perf_counter()
        tool_call = getattr(request, "tool_call", {}) or {}
        node_name = f"tools.{tool_call.get('name', 'unknown')}"
        self._emit(
            node_name,
            "entry",
            input_summary=summarize_value(tool_call.get("args", {})),
        )
        try:
            response = await handler(request)
        except Exception as exc:
            self._emit(
                node_name,
                "exit",
                duration_ms=_duration_ms(started),
                output_summary="error",
                error_type=type(exc).__name__,
            )
            raise
        self._emit(
            node_name,
            "exit",
            duration_ms=_duration_ms(started),
            output_summary=summarize_value(getattr(response, "content", response)),
        )
        return response

    def _emit(self, node_name: str, phase: str, **fields: Any) -> None:
        self._logger.info(
            "agent.node.%s node=%s",
            phase,
            node_name,
            extra={
                "node_name": node_name,
                "phase": phase,
                "trace_id": current_trace_id(),
                **fields,
            },
        )


def summarize_state(state: Any) -> dict[str, Any]:
    messages = state.get("messages", []) if isinstance(state, dict) else []
    last = messages[-1] if messages else None
    return {
        "message_count": len(messages),
        "last_type": getattr(last, "type", None),
        "last_chars": len(_message_text(last)) if last is not None else 0,
    }


def summarize_model_response(response: Any) -> dict[str, Any]:
    results = getattr(response, "result", []) or []
    return {
        "message_count": len(results),
        "tool_calls": sum(len(getattr(item, "tool_calls", []) or []) for item in results),
        "visible_chars": sum(len(_message_text(item)) for item in results),
    }


def summarize_value(value: Any) -> str:
    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else scrub(child)
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [scrub(child) for child in item[:10]]
        if isinstance(item, str):
            return _SENSITIVE_QUERY.sub(r"\1=[REDACTED]", item[:500])
        return item

    try:
        return json.dumps(scrub(value), ensure_ascii=False, default=str)[:1_000]
    except (TypeError, ValueError):
        return "[unserializable]"


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return str(content) if content is not None else ""


def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _log_node_event(node_name: str, phase: str, **fields: Any) -> None:
    logging.getLogger("jobagent.node").info(
        "agent.node.%s",
        phase,
        extra={
            "node_name": node_name,
            "phase": phase,
            "trace_id": current_trace_id(),
            "input_summary": summarize_value(fields.pop("input_value"))
            if "input_value" in fields
            else None,
            **fields,
        },
    )
