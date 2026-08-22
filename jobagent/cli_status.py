"""Terminal status rendering backed by Rich's tested spinner lifecycle."""

from __future__ import annotations

import sys
from typing import TextIO

from rich.console import Console
from rich.status import Status
from rich.text import Text


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
