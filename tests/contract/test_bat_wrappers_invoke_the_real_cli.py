"""The eleven wrappers are the entire interface of the release. Nobody had run one.

An operator who installs from the ZIP never types ``pz-agent``. They
double-click ``install.bat``, then ``doctor.bat``, then ``run-live-tests.bat``.
Those files were written, packaged, checksummed and shipped, and not one of them
had ever been executed — not here, where ``.bat`` does not run, and not on a
Windows machine, because there has not been one.

Batch scripting cannot be exercised on Linux, and this file does not pretend to.
What it exercises is the part that can be wrong without anyone noticing and that
does not depend on the shell: **the shape of the command line each wrapper hands
to the CLI**. A wrapper that puts a flag where the parser does not accept it
fails on the operator's first command, with an argparse usage message they did
not cause and cannot act on.

That risk is not hypothetical here. ``pz-agent`` puts ``--evidence-dir`` on the
``live-test`` *group* rather than on its subcommands, so ``live-test run
--evidence-dir X`` is rejected while ``live-test --evidence-dir X run`` is
accepted. The wrappers use the second form. This file is how that stays true.

Each invocation is parsed by the real parser rather than run, because running
``uninstall-mod`` to check its argument shape would be a poor trade.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.app import COMMANDS, build_parser

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BAT_DIR: Final = REPO_ROOT / "packaging" / "windows" / "bat"

#: ``"%PZ_AGENT%" live-test %EVIDENCE% run %*`` — everything after the executable.
_INVOCATION: Final = re.compile(r'^\s*"%PZ_AGENT%"\s+(.+?)\s*$', re.MULTILINE)

#: What each batch variable expands to at run time, as a real argument list.
#: ``%*`` is the operator's own arguments and expands to nothing when they pass
#: none, which is the case this checks: the wrapper must work double-clicked.
_EXPANSIONS: Final[dict[str, list[str]]] = {
    "%EVIDENCE%": ["--evidence-dir", "C:\\pz-agent\\evidence"],
    "%OUTPUT%": ["--output", "C:\\pz-agent\\release\\evidence-manifest.json"],
    '--source "%MOD_SOURCE%"': ["--source", "C:\\pz-agent\\mod"],
    "%*": [],
}


def _wrappers() -> list[Path]:
    found = sorted(BAT_DIR.glob("*.bat"))
    assert len(found) == 11, f"expected eleven wrappers, found {len(found)}"
    return found


def _expanded(raw: str) -> list[str]:
    """One wrapper line, with its batch variables replaced by real arguments."""
    line = raw
    for token in sorted(_EXPANSIONS, key=len, reverse=True):
        line = line.replace(token, "\x01".join(["", *_EXPANSIONS[token], ""]))
    argv: list[str] = []
    for piece in line.replace("\x01", " \x01 ").split():
        argv.append(piece.strip('"'))
    return [arg for arg in argv if arg and arg != "\x01"]


def _invocations() -> list[tuple[str, str, list[str]]]:
    built: list[tuple[str, str, list[str]]] = []
    for path in _wrappers():
        for raw in _INVOCATION.findall(path.read_text(encoding="utf-8")):
            built.append((path.name, raw, _expanded(raw)))
    assert built, "no invocations were extracted; the pattern has stopped matching"
    return built


def test_every_wrapper_makes_at_least_one_invocation() -> None:
    """A wrapper that calls nothing is a wrapper around nothing."""
    calling = {name for name, _, _ in _invocations()}
    silent = sorted({path.name for path in _wrappers()} - calling)
    assert silent == [], "these wrappers never invoke pz-agent"


@pytest.mark.parametrize(
    ("wrapper", "raw", "argv"),
    _invocations(),
    ids=[f"{name}:{raw[:40]}" for name, raw, _ in _invocations()],
)
def test_the_parser_accepts_the_command_line_the_wrapper_builds(
    wrapper: str, raw: str, argv: list[str]
) -> None:
    """Parsed, not run. A usage error here is an operator's first experience."""
    parser = build_parser()
    try:
        parser.parse_args(argv)
    except SystemExit as exit_code:  # argparse exits rather than raising
        pytest.fail(f"{wrapper} builds `pz-agent {raw}`, which the parser rejects ({exit_code})")


def test_every_command_a_wrapper_names_is_a_real_command() -> None:
    """Caught before the parser would, and with a better message."""
    unknown = sorted({argv[0] for _, _, argv in _invocations() if argv and argv[0] not in COMMANDS})
    assert unknown == [], "the wrappers call these, and the CLI has no such command"
