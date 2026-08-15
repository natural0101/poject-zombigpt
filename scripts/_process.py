#!/usr/bin/env python3
"""Run a child process and read its output as UTF-8, whatever the host says.

``subprocess.run(..., text=True)`` decodes the pipe with the *ambient* encoding
— ``locale.getencoding()``, which is cp1252 on a stock Windows runner and ASCII
under ``LC_ALL=C``. Every file in this repository is UTF-8 and much of it is
Russian, so on Windows ``git show <sha>:CHANGELOG.md`` came back as a
``UnicodeDecodeError`` raised inside subprocess's reader *thread*: not a failed
check with a reason, but a hundred stack traces and a control-plane gate that
could not run at all. It cost a red release build, and it is the same defect
family the evidence writers already have a fix for — bytes that are portable
only if nobody leaves the encoding to the machine.

So this repository's scripts never leave it to the machine:

* ``encoding="utf-8"`` — what git and pytest actually emit, on every platform.
* ``errors="replace"`` — a stray byte becomes U+FFFD instead of an exception. A
  gate that dies on one byte of one file is a gate that gets switched off, and
  every caller here searches the text for ASCII markers (``def name``, a path, a
  status word) that a replacement character cannot forge.

Byte output is not this function's business: a caller that wants bytes should
say ``capture_output=True`` with no ``text`` and read ``stdout`` itself.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

#: How every script in this directory decodes a child's output.
ENCODING: Final = "utf-8"
ERRORS: Final = "replace"


def run_text(
    command: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float | None = 120,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` with the decoding pinned rather than inherited."""
    return subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding=ENCODING,
        errors=ERRORS,
        timeout=timeout,
        check=check,
        env=env,
    )
