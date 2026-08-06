"""The commands the documentation promises must be the commands that exist.

This is the cheapest member of a family that has cost this branch a lot, and it
is here because one of its siblings already rotted unnoticed:
``configs/mcp/README.md`` advertised nineteen tools and seven actions long after
the surface had grown to thirty, and the test meant to catch that unioned every
document before comparing — so one file naming everything covered for another
naming half.

Both directions are checked, and both matter for different reasons.

A command a document names and the CLI does not have is a user typing something
that fails. A command the CLI has and no document names is worse in a quieter
way: it is a feature nobody will find. ``pz-agent remember`` and ``pz-agent
voice`` were both in that state within the last day — wired, tested, and
mentioned nowhere a user reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from pz_agent_cli.app import COMMANDS

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: ``pz-agent <word>``. The alternation is deliberate: a bare ``pz-agent`` in
#: prose ("the pz-agent binary") must not be read as naming a command.
_INVOCATION: Final = re.compile(r"pz-agent(?:\.exe)? ([a-z][a-z-]+)")

#: Words that follow ``pz-agent`` in prose without being commands. Listed rather
#: than guessed at, so a real command can never be silently excused as prose.
_NOT_A_COMMAND: Final = frozenset({"command", "is", "was", "did", "binary", "reads", "and"})


def _documents() -> list[Path]:
    """Every document a user is expected to read."""
    found = sorted(REPO_ROOT.joinpath("docs").glob("*.md"))
    found.append(REPO_ROOT / "README.md")
    return [path for path in found if path.is_file()]


def _named_commands() -> dict[str, list[str]]:
    """Every command named in a document, and where it was named."""
    found: dict[str, list[str]] = {}
    for path in _documents():
        text = path.read_text(encoding="utf-8")
        for word in _INVOCATION.findall(text):
            if word in _NOT_A_COMMAND:
                continue
            found.setdefault(word, []).append(path.name)
    return found


def test_every_command_the_documentation_names_actually_exists() -> None:
    """A documented command that is not there is a user typing something broken."""
    named = _named_commands()
    missing = {
        command: sorted(set(where)) for command, where in named.items() if command not in COMMANDS
    }
    assert missing == {}, "documented but absent from the CLI"


def test_every_command_the_cli_has_is_named_somewhere_a_user_reads() -> None:
    """A command nobody documents is a feature nobody finds.

    Both `remember` and `voice` were in exactly this state until they were
    written up: implemented, tested, reachable, and mentioned in no document a
    user would ever open.
    """
    undocumented = sorted(set(COMMANDS) - set(_named_commands()))
    assert undocumented == [], "in the CLI but named in no document"


def test_the_documents_this_checks_are_the_ones_a_user_reads() -> None:
    """A check over an empty file list passes and proves nothing.

    The two assertions above are both set comparisons, so a glob that stopped
    matching would make them vacuously true rather than fail. This is the guard
    against that.
    """
    documents = _documents()
    assert len(documents) >= 10, "the document glob has stopped finding the docs"
    names = {path.name for path in documents}
    for required in ("QUICKSTART.md", "LOCAL_GAME_HANDOFF.md", "TROUBLESHOOTING.md"):
        assert required in names, f"{required} is not being checked"
