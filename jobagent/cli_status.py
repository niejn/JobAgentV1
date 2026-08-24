"""Terminal status rendering backed by Rich's tested spinner lifecycle."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.status import Status
from rich.text import Text

_MAX_THINKING_CHARS = 800
_MAX_TOOL_LINES = 6
_SPINNER_REFRESH_PER_SECOND = 12


class RichStatusSpinner:
    """Show animated statuses on a TTY and plain durable lines elsewhere."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        console: Console | None = None,
    ) -> None:
        self._stream = stream or sys.stdout
        self._interactive = bool(self._stream.isatty())
        self._console = console or Console(
            file=self._stream,
            color_system="auto" if self._interactive else None,
        )
        self._status: Status | None = None

    def update(self, message: str) -> None:
        """Start the spinner or replace its current status message."""

        rendered = Text(f"JobAgent · {message}")
        if not self._interactive:
            self._console.print(rendered)
            return
        if self._status is None:
            self._status = self._console.status(rendered, spinner="dots")
            self._status.start()
            return
        self._status.update(rendered)

    def stop(self) -> None:
        """Stop and clear the active spinner; safe to call repeatedly."""

        status = self._status
        self._status = None
        if status is not None:
            status.stop()


class TypewriterTranscript:
    """Transient typewriter display for thinking and tool progress.

    Interactive terminals get a pi/Claude-Code-style live region: an
    animated spinner plus the current status line, streamed thinking (dim
    italic), and bounded tool summaries render while the model works. The
    region is erased whenever answer tokens print, and can be re-activated
    for mid-answer tool activity (the spinner keeps animating during long
    tool runs). Non-interactive streams receive the same events as plain
    durable lines so logs keep the information.
    """

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        console: Console | None = None,
        live_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._stream = stream or sys.stdout
        self._interactive = bool(self._stream.isatty())
        self._console = console or Console(
            file=self._stream,
            color_system="auto" if self._interactive else None,
        )
        self._live_factory = live_factory or Live
        self._live: Any | None = None
        self._status = ""
        self._thinking = ""
        self._tool_lines: list[str] = []
        self._thinking_line_open = False

    @property
    def interactive(self) -> bool:
        """Whether the target stream supports the transient live display."""

        return self._interactive

    def update_status(self, message: str) -> None:
        """Show (or replace) the current activity line."""

        if not message:
            return
        if not self._interactive:
            self._close_thinking_line()
            self._write_line(f"JobAgent · {message}")
            return
        self._status = message
        self._refresh()

    def show_thinking(self, delta: str) -> None:
        """Append one reasoning delta to the typewriter stream."""

        if not delta:
            return
        if not self._interactive:
            self._stream.write(delta)
            self._stream.flush()
            self._thinking_line_open = True
            return
        self._thinking = (self._thinking + delta)[-_MAX_THINKING_CHARS:]
        self._refresh()

    def show_tool(self, summary: str) -> None:
        """Record one bounded, redacted tool-result summary."""

        if not summary:
            return
        if not self._interactive:
            self._close_thinking_line()
            self._write_line(f"JobAgent · 工具结果 {summary}")
            return
        self._tool_lines.append(summary)
        del self._tool_lines[:-_MAX_TOOL_LINES]
        self._refresh()

    def begin_answer(self) -> None:
        """Erase the transient region; the answer (or its continuation) takes over.

        Mid-answer tool activity can re-activate the region afterwards, so
        this is intentionally not a permanent finish.
        """

        self._stop_live()

    def close(self) -> None:
        """Erase any remaining transient output (turn finished or failed)."""

        self._stop_live()

    def _stop_live(self) -> None:
        live = self._live
        self._live = None
        if live is not None:
            live.stop()
        self._status = ""
        self._thinking = ""
        self._tool_lines.clear()
        if not self._interactive:
            self._close_thinking_line()

    def _close_thinking_line(self) -> None:
        if self._thinking_line_open:
            self._thinking_line_open = False
            self._stream.write("\n")
            self._stream.flush()

    def _write_line(self, line: str) -> None:
        self._stream.write(f"{line}\n")
        self._stream.flush()

    def _refresh(self) -> None:
        if self._live is None:
            # auto-refresh keeps the spinner animating between events,
            # exactly like the Status spinner this replaces.
            self._live = self._live_factory(
                console=self._console,
                transient=True,
                refresh_per_second=_SPINNER_REFRESH_PER_SECOND,
            )
            self._live.start()
        self._live.update(self._render())
        self._live.refresh()

    def _render(self) -> Any:
        blocks: list[Any] = []
        for line in self._tool_lines:
            blocks.append(Text(f"JobAgent · 工具 {line}", style="cyan"))
        if self._thinking:
            blocks.append(Text(self._thinking, style="dim italic"))
        if self._status:
            blocks.append(
                Spinner(
                    "dots",
                    text=Text(f"JobAgent · {self._status}", style="dim"),
                    style="dim",
                )
            )
        if not blocks:
            return Text("")
        if len(blocks) == 1:
            return blocks[0]
        return Group(*blocks)
