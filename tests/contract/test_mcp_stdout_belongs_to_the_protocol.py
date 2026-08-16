"""The MCP process's stdout belongs to the protocol, and the CLI lives in it.

An MCP client launches ``pz-agent-mcp`` and parses its stdout as JSON-RPC. The
repository map's answer to that is one line — ``pz_agent_cli`` is the only
package allowed to print — and it reads as though package boundaries enforce it.
They do not. ``pz_agent_mcp/__main__.py`` imports ``pz_agent_cli.context`` on
purpose, so that the state directory is derived once rather than by two copies
that drift, and that import pulls the CLI in with it — measured below, including
the two modules that do print.

So the printing half of the CLI is loaded inside the process whose stdout is the
protocol, and what keeps the stream clean is that nothing on that path calls
``print``. ``scripts/check_forbidden.py`` now refuses a terminal write outside
the CLI, which is the static half. This file is the behavioural half: it crosses
the boundary the way the entry point does, in a real child process, and reads
the pipe a client would have been parsing.

Both halves are needed. The static rule cannot see a print reached through a
library the CLI imports, and this test cannot see a print on a branch it does
not take.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ENTRY: Final = REPO_ROOT / "packages" / "pz_agent_mcp" / "src" / "pz_agent_mcp" / "__main__.py"

#: What the entry point imports when it needs the state directory. Named here so
#: that moving it is a deliberate act rather than a silent widening of what runs
#: inside the serving process.
BOUNDARY_IMPORT: Final = "from pz_agent_cli.context import CliContext, resolve_workspace"

#: The child does exactly what ``_state_dir_from_game`` does, and nothing else:
#: the import, then the two calls. Written as a program rather than driven
#: in-process because a ``StringIO`` is not what a client parses — the thing
#: being measured is a real process's real file descriptor 1.
CROSSING: Final = """
import sys
{paths}
from pz_agent_cli.context import CliContext, resolve_workspace

context = CliContext.from_process()
try:
    resolve_workspace(context)
except OSError:
    # Whether a workspace resolves depends on the machine; whether the
    # attempt was silent does not, and silence is the whole subject here.
    pass
"""


def _source_paths() -> str:
    lines = []
    for package in ("pz_agent_core", "pz_agent_mcp", "pz_agent_cli", "pz_agent_voice"):
        source = REPO_ROOT / "packages" / package / "src"
        lines.append(f"sys.path.insert(0, {str(source)!r})")
    return "\n".join(lines)


def test_the_entry_point_still_crosses_into_the_cli() -> None:
    """The premise, read from the entry point rather than assumed.

    If this import ever goes away the process stops carrying the CLI, and the
    test below would then be measuring a boundary that no longer exists — still
    passing, and proving nothing. Better to fail here and have someone decide
    what the file is for.
    """
    assert BOUNDARY_IMPORT in ENTRY.read_text(encoding="utf-8")


def test_the_printing_modules_are_loaded_in_the_serving_process() -> None:
    """The uncomfortable half of the premise, measured.

    ``pz_agent_cli.output`` and ``pz_agent_cli.status`` hold every ``print`` in
    the repository. They are not imported by mistake — importing ``context``
    reaches them through the CLI package — and this asserts the fact rather than
    leaving the docstring above to claim it.
    """
    program = CROSSING.format(paths=_source_paths()) + (
        "\nloaded = sorted(m for m in sys.modules if m.startswith('pz_agent_cli'))"
        "\nsys.stderr.write(repr(loaded))\n"
    )
    finished = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
        timeout=120,
    )

    loaded = finished.stderr
    assert "'pz_agent_cli.output'" in loaded
    assert "'pz_agent_cli.status'" in loaded


def test_crossing_into_the_cli_puts_nothing_on_stdout() -> None:
    """The property itself: a client parsing this pipe would see nothing yet.

    A single byte here is not cosmetic. It arrives in the middle of a JSON-RPC
    stream, the client reports a parse error, and the report names this server.
    """
    finished = subprocess.run(
        [sys.executable, "-c", CROSSING.format(paths=_source_paths())],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
        timeout=120,
    )

    assert finished.stdout == "", (
        "importing the CLI into the MCP process wrote to stdout, which an MCP "
        f"client parses as JSON-RPC: {finished.stdout[:400]!r}"
    )
