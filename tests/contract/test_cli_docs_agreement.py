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

import argparse
import re
from pathlib import Path
from typing import Final

from pz_agent_cli.app import COMMANDS, build_parser

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: ``pz-agent <word>``. The alternation is deliberate: a bare ``pz-agent`` in
#: prose ("the pz-agent binary") must not be read as naming a command.
_INVOCATION: Final = re.compile(r"pz-agent(?:\.exe)? ([a-z][a-z-]+)")

#: Words that follow ``pz-agent`` in prose without being commands. Listed rather
#: than guessed at, so a real command can never be silently excused as prose.
_NOT_A_COMMAND: Final = frozenset({"command", "is", "was", "did", "binary", "reads", "and"})


def _documents() -> list[Path]:
    """Every document a user is expected to read.

    ``docs/blueprint/`` is deliberately *not* here — it is the requirement
    baseline, read-only per AGENTS.md, and it names the commands the design
    asked for rather than the ones that were built. Those are checked
    separately, below, against a declared map.
    """
    found = sorted(REPO_ROOT.joinpath("docs").glob("*.md"))
    found.append(REPO_ROOT / "README.md")
    return [path for path in found if path.is_file()]


def _blueprint_documents() -> list[Path]:
    found = sorted(REPO_ROOT.joinpath("docs", "blueprint").glob("*.md"))
    return [path for path in found if path.is_file()]


def _blueprint_commands() -> set[str]:
    found: set[str] = set()
    for path in _blueprint_documents():
        text = path.read_text(encoding="utf-8")
        found |= {word for word in _INVOCATION.findall(text) if word not in _NOT_A_COMMAND}
    return found


#: Commands the blueprint asks for that this build delivers under another name,
#: and what delivers them. Declared rather than ignored: an entry here is a
#: deviation someone decided on, and ``docs/PROGRESS.md`` records the reasoning.
#: Anything the blueprint names that is neither a real command nor in this map
#: fails the check below, which is the state ``setup`` and ``support-bundle``
#: were in — named in the requirement baseline, absent from the CLI, and
#: invisible to every test because this file's glob stopped at ``docs/*.md``.
BLUEPRINT_ALIASES: Final = {
    "setup": "install-mod, then validate-config and doctor as separate steps",
    "support-bundle": "logs --bundle [--verify]",
}


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


def test_the_command_list_is_the_parser_and_the_parser_is_the_command_list() -> None:
    """``COMMANDS`` is what the two tests below mean by "the CLI". It must be true.

    It was not. ``smoke`` had a parser, a dispatch branch and a working
    subsystem, and was absent from this tuple — so the CLI accepted a command
    the list denied having. Both directions below were wrong because of it: a
    document naming ``pz-agent smoke`` failed the first assertion for naming
    something "absent from the CLI", and the second could never notice that a
    real command was documented nowhere.

    Derived from the parser rather than restated, because a second hand-written
    list would only move the place the two can disagree.
    """
    # argparse exposes no public reader for its subcommands, so this reaches for
    # the private one. The alternative is a second hand-written list, which is
    # the thing being tested for.
    parser = build_parser()
    accepted: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            accepted |= set(action.choices)

    assert accepted, "no subcommands were found; this check would otherwise be vacuous"
    assert accepted == set(COMMANDS), (
        "the parser and COMMANDS disagree — "
        f"accepted but unlisted: {sorted(accepted - set(COMMANDS))}, "
        f"listed but not accepted: {sorted(set(COMMANDS) - accepted)}"
    )


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


def test_every_command_the_blueprint_asks_for_exists_or_is_a_declared_deviation() -> None:
    """The requirement baseline is checked too, against a map rather than silently.

    ``docs/blueprint/`` is read-only, so a name it uses cannot simply be
    corrected. What it can be is *accounted for*: either the command exists, or
    this file says what delivers it instead. Two do — ``setup`` and
    ``support-bundle`` — and both were unaccounted for until now, because the
    glob above never descended into the blueprint at all.
    """
    named = _blueprint_commands()
    assert named, "the blueprint glob found no commands; the check has gone hollow"

    unaccounted = sorted(named - set(COMMANDS) - set(BLUEPRINT_ALIASES))
    assert unaccounted == [], (
        "the blueprint asks for these and nothing delivers them under any name; "
        "either build them or record the deviation in BLUEPRINT_ALIASES"
    )


def test_the_alias_map_does_not_excuse_a_command_that_now_exists() -> None:
    """An alias outliving the gap it described is a deviation nobody revisited."""
    stale = sorted(name for name in BLUEPRINT_ALIASES if name in COMMANDS)
    assert stale == [], "these are real commands now; drop them from BLUEPRINT_ALIASES"


def test_every_alias_names_a_command_that_exists() -> None:
    """The right-hand side has to be reachable, or the map is a shrug."""
    for spec, delivered in sorted(BLUEPRINT_ALIASES.items()):
        head = delivered.split()[0].rstrip(",")
        assert head in COMMANDS, f"{spec} is mapped to {head!r}, which is not a command"
