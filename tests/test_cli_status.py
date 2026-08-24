"""Behavior tests for the interactive terminal status indicator."""

from __future__ import annotations

from io import StringIO
from typing import Any

from jobagent.cli_status import RichStatusSpinner, TypewriterTranscript


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


class FakeLive:
    """Record live-region updates without touching a real terminal."""

    instances: list[FakeLive] = []

    def __init__(
        self,
        *,
        console: Any,
        transient: bool,
        refresh_per_second: float,
    ) -> None:
        self.console = console
        self.transient = transient
        self.refresh_per_second = refresh_per_second
        self.updates: list[Any] = []
        self.refresh_count = 0
        self.stop_count = 0
        self.started = False
        FakeLive.instances.append(self)

    def start(self) -> None:
        self.started = True

    def update(self, renderable: Any) -> None:
        self.updates.append(renderable)

    def refresh(self) -> None:
        self.refresh_count += 1

    def stop(self) -> None:
        self.stop_count += 1


def _render_plain(renderable: Any) -> str:
    """Render one transcript renderable to plain text for assertions."""

    from io import StringIO

    from rich.console import Console

    buffer = StringIO()
    console = Console(file=buffer, width=250, legacy_windows=False, color_system=None)
    console.print(renderable)
    return buffer.getvalue()


def test_typewriter_transcript_streams_thinking_then_clears_on_answer() -> None:
    FakeLive.instances = []
    stream = InteractiveBuffer()
    transcript = TypewriterTranscript(
        stream=stream,
        console=None,
        live_factory=FakeLive,  # type: ignore[arg-type]
    )
    assert transcript.interactive is True

    transcript.update_status("正在分析你的请求…")
    transcript.show_thinking("模型思考：先理解")
    transcript.show_thinking("用户意图")
    transcript.show_tool("save_shared_url {'status': 'completed'}")
    transcript.begin_answer()
    # A new model turn's thinking re-activates the transient region.
    transcript.show_thinking("工具执行后的新一轮思考")
    transcript.close()

    live = FakeLive.instances[0]
    assert live.started is True
    assert live.transient is True
    assert live.refresh_per_second == 12
    assert live.stop_count == 1
    rendered = _render_plain(live.updates[-1])
    assert "JobAgent · 工具 save_shared_url {'status': 'completed'}" in rendered
    assert "模型思考：先理解用户意图" in rendered
    # The status line carries the animated dots spinner.
    assert "JobAgent · 正在分析你的请求…" in rendered
    from rich.spinner import Spinner

    last = live.updates[-1]
    assert any(isinstance(item, Spinner) for item in getattr(last, "renderables", []))
    assert live.refresh_count >= 4
    # The re-activated region shows only the new turn's thinking.
    second = FakeLive.instances[1]
    second_rendered = _render_plain(second.updates[-1])
    assert "工具执行后的新一轮思考" in second_rendered
    assert "正在分析你的请求" not in second_rendered
    assert second.stop_count == 1
    # Nothing transient ever reaches the terminal itself.
    assert stream.getvalue() == ""


def test_typewriter_transcript_reactivates_for_mid_answer_tool_activity() -> None:
    FakeLive.instances = []
    stream = InteractiveBuffer()
    transcript = TypewriterTranscript(
        stream=stream,
        console=None,
        live_factory=FakeLive,  # type: ignore[arg-type]
    )

    transcript.update_status("正在分析你的请求…")
    transcript.begin_answer()
    # Mid-answer tool activity re-activates the transient spinner region.
    transcript.update_status("正在读取并保存分享链接中的资料…")
    assert len(FakeLive.instances) == 2
    assert FakeLive.instances[0].stop_count == 1
    # Earlier thinking/status content must not reappear after re-activation.
    rendered = _render_plain(FakeLive.instances[1].updates[-1])
    assert "正在分析你的请求" not in rendered
    assert "正在读取并保存分享链接中的资料…" in rendered
    # The answer continues: the region is erased again.
    transcript.begin_answer()
    assert FakeLive.instances[1].stop_count == 1
    assert len(FakeLive.instances) == 2
    transcript.close()
    assert stream.getvalue() == ""


def test_typewriter_transcript_non_interactive_prints_durable_lines() -> None:
    stream = StringIO()
    transcript = TypewriterTranscript(stream=stream)
    assert transcript.interactive is False

    transcript.update_status("正在分析你的请求…")
    transcript.show_thinking("模型思考：")
    transcript.show_thinking("先理解意图")
    transcript.show_tool("save_shared_url (20 chars)")
    transcript.begin_answer()

    assert stream.getvalue() == (
        "JobAgent · 正在分析你的请求…\n"
        "模型思考：先理解意图"
        "\nJobAgent · 工具结果 save_shared_url (20 chars)\n"
    )


def test_typewriter_transcript_close_without_answer_is_safe() -> None:
    FakeLive.instances = []
    stream = InteractiveBuffer()
    transcript = TypewriterTranscript(
        stream=stream,
        console=None,
        live_factory=FakeLive,  # type: ignore[arg-type]
    )
    transcript.update_status("正在分析你的请求…")
    transcript.close()
    transcript.close()

    assert FakeLive.instances[0].stop_count == 1
    assert stream.getvalue() == ""
