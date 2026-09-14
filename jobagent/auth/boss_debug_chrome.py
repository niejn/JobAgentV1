"""Own the dedicated visible Chrome used for Boss CDP login and reads."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from jobagent.config import Settings


class BossDebugChromeError(RuntimeError):
    """The local debug Chrome endpoint could not be made available."""


_LAUNCHED_PROCESS: subprocess.Popen[bytes] | None = None


def _endpoint_ready(endpoint: str) -> bool:
    try:
        with urlopen(f"{endpoint.rstrip('/')}/json/version", timeout=1) as response:  # noqa: S310
            return bool(response.status == 200)
    except OSError:
        return False


def _chrome_executable(settings: Settings) -> str:
    configured = settings.boss_debug_chrome_executable
    if configured is not None and configured.is_file():
        return str(configured)
    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        return found
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    executable = next((candidate for candidate in candidates if candidate.is_file()), None)
    if executable is None:
        raise BossDebugChromeError("未找到 Chrome；请设置 BOSS_DEBUG_CHROME_EXECUTABLE。")
    return str(executable)


async def ensure_boss_debug_chrome(settings: Settings, *, force_restart: bool = False) -> bool:
    """Return whether this call launched Chrome; never close an existing instance."""

    endpoint = settings.debug_chrome_cdp_endpoint
    global _LAUNCHED_PROCESS
    if force_restart and _LAUNCHED_PROCESS is not None:
        if _LAUNCHED_PROCESS.poll() is None:
            _LAUNCHED_PROCESS.terminate()
            try:
                await asyncio.to_thread(_LAUNCHED_PROCESS.wait, 5)
            except Exception:
                _LAUNCHED_PROCESS.kill()
        _LAUNCHED_PROCESS = None
    if not force_restart and await asyncio.to_thread(_endpoint_ready, endpoint):
        return False
    parsed = urlsplit(endpoint)
    port = parsed.port
    if port is None:
        raise BossDebugChromeError("DEBUG_CHROME_CDP_ENDPOINT 缺少端口。")
    profile = settings.boss_debug_chrome_profile_dir.expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        _chrome_executable(settings),
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.zhipin.com/web/geek/job",
    ]
    try:
        _LAUNCHED_PROCESS = subprocess.Popen(command)  # noqa: S603 - fixed executable and argument list
    except OSError as exc:
        raise BossDebugChromeError("无法启动 Boss 调试 Chrome。") from exc
    deadline = time.monotonic() + settings.boss_debug_chrome_start_timeout_seconds
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_endpoint_ready, endpoint):
            return True
        await asyncio.sleep(0.25)
    raise BossDebugChromeError("Boss 调试 Chrome 已启动，但 CDP 端口未就绪。")
