"""Writing to the terminal. The one module in the project that is meant to print.

Every handler writes through a :class:`Printer` rather than calling ``print``,
so a test drives the real command and reads what a user would have seen. Machine
output goes to stdout and diagnostics to stderr, which is what lets
``pz-agent doctor --json`` be piped into a bug report while its warnings stay
visible in the terminal.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, TextIO

#: Longest single line the CLI emits before it wraps a value onto its own line.
MAX_LINE: Final = 100


class Printer:
    """Writes to the injected streams. Holds no state beyond them."""

    def __init__(self, stdout: TextIO, stderr: TextIO) -> None:
        self._out = stdout
        self._err = stderr

    def line(self, text: str = "") -> None:
        print(text, file=self._out)

    def lines(self, texts: Iterable[str]) -> None:
        for text in texts:
            self.line(text)

    def error(self, text: str) -> None:
        """A diagnostic for a human. Never part of the machine-readable output."""
        print(text, file=self._err)

    def json(self, document: Mapping[str, Any] | Sequence[Any]) -> None:
        """Emit one JSON document, sorted so two runs diff cleanly."""
        print(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
            file=self._out,
        )

    def heading(self, text: str) -> None:
        self.line(text)
        self.line("-" * min(len(text), MAX_LINE))

    def field(self, label: str, value: str, *, width: int = 22) -> None:
        self.line(f"  {label:<{width}} {value}")
