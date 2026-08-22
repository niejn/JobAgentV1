"""Behavior tests for the interactive terminal status indicator."""

from __future__ import annotations

from io import StringIO
from typing import Any

from jobagent.cli_status import RichStatusSpinner


class InteractiveBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def test_interactive_status_uses_rich_spinner_and_clears_it() -> None:
    calls: list[tuple[str, str]] = []

    class FakeStatus:
        def start(self) -> None:
            calls.append(("start", ""))

        def update(self, message: Any) -> None:
            calls.append(("update", message.plain))

        def stop(self) -> None:
            calls.append(("stop", ""))

    class FakeConsole:
        def status(self, message: Any, *, spinner: str) -> FakeStatus:
            calls.append(("status", f"{spinner}:{message.plain}"))
            return FakeStatus()

    stream = InteractiveBuffer()
    spinner = RichStatusSpinner(
        stream=stream,
        console=FakeConsole(),  # type: ignore[arg-type]
    )

    spinner.update("正在分析你的请求…")
    spinner.update("正在读取并保存分享链接中的资料…")
    spinner.stop()

    assert calls == [
        ("status", "dots:JobAgent · 正在分析你的请求…"),
        ("start", ""),
        ("update", "JobAgent · 正在读取并保存分享链接中的资料…"),
        ("stop", ""),
    ]
