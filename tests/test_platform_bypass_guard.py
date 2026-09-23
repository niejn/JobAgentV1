"""PlatformBypassGuardMiddleware: execute must not reach platform internals.

Field incident 2026-09-22 (trace 76de8f7…): the model wrote a script importing
``jobagent.applier.boss_ws.send_text_to_conversation`` + ``get_cookies`` and ran
it via ``execute`` — an outbound reply and a resume were sent with NO approval
prompt. These tests pin the physical gate that closes that bypass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from jobagent.middleware import PlatformBypassGuardMiddleware


def _request(command: str) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": "execute",
            "args": {"command": command, "timeout": 120},
            "id": "call-1",
            "type": "tool_call",
        },
        tool=None,
        state={},
        runtime=cast(Any, None),
    )


async def _ok_handler(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(content="ran", name="execute", tool_call_id="call-1")


@pytest.mark.asyncio
async def test_inline_import_of_platform_internals_is_blocked() -> None:
    guard = PlatformBypassGuardMiddleware(Path("."))
    command = (
        'python -c "from jobagent.applier.boss_ws import send_text_to_conversation; '
        "asyncio.run(send_text_to_conversation(1))\""
    )

    result = await guard.awrap_tool_call(_request(command), _ok_handler)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    payload = json.loads(str(result.content))
    assert payload["error_type"] == "platform_bypass"


@pytest.mark.asyncio
async def test_script_file_with_sensitive_import_is_blocked(tmp_path: Path) -> None:
    script = tmp_path / "workspace" / "send_reply_rora2.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "from jobagent.applier.boss_ws import send_text_to_conversation\n"
        "from jobagent.auth.cookie_manager import get_cookies\n",
        encoding="utf-8",
    )
    guard = PlatformBypassGuardMiddleware(tmp_path)
    command = f".venv/Scripts/python.exe {script.name}"

    result = await guard.awrap_tool_call(_request(command), _ok_handler)

    assert result.status == "error"
    assert "platform_bypass" in str(result.content)


@pytest.mark.asyncio
async def test_benign_script_runs_untouched(tmp_path: Path) -> None:
    script = tmp_path / "workspace" / "convert_jd.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "from PIL import Image\nImage.open('a.webp').save('a.png')\n",
        encoding="utf-8",
    )
    guard = PlatformBypassGuardMiddleware(tmp_path)

    async def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content=json.dumps({"status": "ok"}), name="execute", tool_call_id="call-1"
        )

    result = await guard.awrap_tool_call(_request(f"python {script.name}"), handler)

    assert result.status != "error"
    assert "ok" in str(result.content)


@pytest.mark.asyncio
async def test_python_dash_m_jobagent_is_blocked() -> None:
    guard = PlatformBypassGuardMiddleware(Path("."))

    result = await guard.awrap_tool_call(_request("python -m jobagent watch"), _ok_handler)

    assert result.status == "error"


@pytest.mark.asyncio
async def test_non_execute_tools_are_never_inspected() -> None:
    guard = PlatformBypassGuardMiddleware(Path("."))

    async def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="ok", name="read_file", tool_call_id="call-1")

    request = ToolCallRequest(
        tool_call={
            "name": "read_file",
            "args": {"file_path": "cookies.json"},
            "id": "call-1",
            "type": "tool_call",
        },
        tool=None,
        state={},
        runtime=cast(Any, None),
    )

    result = await guard.awrap_tool_call(request, handler)

    assert result.status != "error"


def test_relative_script_resolved_against_workspace_root(tmp_path: Path) -> None:
    """The incident script lived at <root>/workspace/… — resolution must cover it."""

    script = tmp_path / "workspace" / "verify_hr.py"
    script.parent.mkdir(parents=True)
    script.write_text("import jobagent.applier.boss_ws\n", encoding="utf-8")
    guard = PlatformBypassGuardMiddleware(tmp_path)

    assert guard.inspect_command("python workspace/verify_hr.py") is not None
    assert guard.inspect_command("python verify_hr.py") is not None


def test_benign_inline_command_is_clean() -> None:
    guard = PlatformBypassGuardMiddleware(Path("."))

    assert guard.inspect_command("python -c \"import asyncio; print('hi')\"") is None


@pytest.mark.asyncio
async def test_real_incident_command_shape_is_blocked(tmp_path: Path) -> None:
    """Regression against the literal field-bypass command.

    Layout: VFS root = <repo>/data/journeys, script physically at
    root/workspace/…, command cd's to the repo root and references the script
    via the repo-relative path with backslashes.
    """

    repo = tmp_path / "repo"
    vfs_root = repo / "data" / "journeys"
    script = vfs_root / "workspace" / "send_resume_goodnotes.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import jobagent.applier.boss_resume_delivery as brd\n"
        "from jobagent.auth.cookie_manager import get_cookies\n",
        encoding="utf-8",
    )
    guard = PlatformBypassGuardMiddleware(vfs_root)
    command = (
        f"cd /d {repo} && .venv\\Scripts\\python.exe "
        "data\\journeys\\workspace\\send_resume_goodnotes.py 2>&1"
    )

    result = await guard.awrap_tool_call(_request(command), _ok_handler)

    assert result.status == "error"
