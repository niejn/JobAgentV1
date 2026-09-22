"""_Utf8TolerantShellBackend: execute() must survive non-UTF-8 child output.

Field crash 2026-09-22: upstream LocalShellBackend decodes pipes with
``text=True`` and no ``errors`` policy, so a child printing GBK bytes crashed
the subprocess reader thread (UnicodeDecodeError). These tests pin the decode
contract at the seam we own.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from jobagent.agent import _Utf8TolerantShellBackend


def _backend(tmp_path: Path) -> _Utf8TolerantShellBackend:
    env = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP")
        if name in os.environ
    }
    return _Utf8TolerantShellBackend(
        root_dir=tmp_path, virtual_mode=True, inherit_env=False, env=env, timeout=30
    )


def test_execute_decodes_pipes_lossily() -> None:
    """The subprocess call must pin utf-8 + errors=replace (the fix contract)."""

    captured: dict[str, object] = {}

    class _Result:
        returncode = 0
        stdout = "hi"
        stderr = ""

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        return _Result()

    backend = _backend(Path("."))
    with patch.object(subprocess, "run", fake_run):
        response = backend.execute("echo hi")

    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"
    assert response.exit_code == 0
    assert "hi" in response.output


def test_execute_survives_gbk_child_output(tmp_path: Path) -> None:
    """A child emitting GBK bytes must not kill the pipe reader.

    GBK bytes for 中文 are invalid UTF-8, so a strict decode raises; with the
    tolerant contract the output survives with replacement characters and the
    command result still flows back to the model.
    """

    backend = _backend(tmp_path)
    payload = "'中文'.encode('gbk')"
    command = f'{sys.executable} -c "import sys; sys.stdout.buffer.write({payload})"'

    response = backend.execute(command)

    assert response.exit_code == 0
    assert response.output  # decoded lossily, never lost
