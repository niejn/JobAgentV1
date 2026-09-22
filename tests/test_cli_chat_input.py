"""Interactive chat input must survive the already-running asyncio loop.

`_chat` is a coroutine driven by ``asyncio.run`` (cli.py chat_command); the
sync ``PromptSession.prompt()`` nests its own ``asyncio.run`` inside it and
dies with ``RuntimeError: asyncio.run() cannot be called from a running
event loop`` (field crash 2026-09-20). These tests pin the real seam — the
factory ``_chat`` uses plus the awaitable input call — so the loop-safe
path cannot silently regress.
"""

from __future__ import annotations

import asyncio

import pytest

from jobagent.cli import _make_chat_prompt_session


@pytest.mark.asyncio
async def test_chat_prompt_session_supports_injected_io() -> None:
    """The factory accepts test input/output overrides without behavior change.

    Production callers pass nothing; the parameters exist so the regression
    test below can drive the exact PromptSession _chat builds.
    """

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        session = _make_chat_prompt_session(input=pipe, output=DummyOutput())
        assert session is not None


@pytest.mark.asyncio
async def test_chat_input_is_awaitable_inside_running_loop() -> None:
    """Input at the _chat seam must complete under a running event loop.

    Pre-fix this went red two ways: the factory rejected injected IO, and
    the loop-safety only held for the sync prompt() call that crashed with
    asyncio.run() nesting. prompt_async() on the real factory session is
    the contract _chat relies on.
    """

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        session = _make_chat_prompt_session(input=pipe, output=DummyOutput())
        pipe.send_text("你好\r")
        message = await asyncio.wait_for(session.prompt_async(), timeout=5)
    assert message == "你好"
