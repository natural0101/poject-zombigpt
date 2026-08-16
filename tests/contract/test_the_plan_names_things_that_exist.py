"""Every command the plan tells an operator to run must exist.

``docs/control/MASTER_PLAN.yaml`` carries a ``verify_command`` on all 484 tasks
— 150 distinct ones — and 84 of those tasks are the live validation someone runs
on a Windows machine with the game open. That is the worst possible place to
discover that a command was renamed: after a two-hour endurance run, on a
machine this repository cannot reach, with the session spent.

It is the same defect as a gate whose producer was never written
(``tests/contract/test_gates_without_producers.py``), one layer out: there the
sidecar branches on a value the mod never sends, here the plan names a command
the CLI no longer has. Nothing checked it, and the surface moves constantly —
test files get renamed in almost every commit that adds one.

Measured over the tree: all 150 resolve. Every pytest target, script, document
and grep path is on disk; all 33 ``pz-agent`` lines parse against the real
parser, flags included; and the 22 scenario ids the plan names are exactly the
22 the catalogue defines. So this is a guard over a surface that is correct
today, not a fix.

Two decisions worth stating. The CLI lines are handed to ``build_parser()``
rather than compared against a list of command names: that is the operator's
actual question — *would this line run* — and it catches a removed flag, which a
name comparison cannot. And every command must be *classified*: a shape this
file does not recognise fails rather than being skipped, because a classifier
that silently ignores what it cannot parse reports a clean plan for a broken
one.
"""

from __future__ import annotations

import contextlib
import io
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest
import yaml

from pz_agent_cli.app import build_parser
from pz_agent_cli.livetest.scenarios import SCENARIOS

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PLAN: Final = REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml"

CLI_PREFIX: Final = ".venv/bin/pz-agent"

#: Verb-first shapes that name no artefact this repository can check — "read the
#: pull request", "follow the playbook". Listed rather than pattern-matched so
#: that adding one is a deliberate act: each is a command whose subject is a
#: person, a workflow run or a document already checked elsewhere.
PROSE_PREFIXES: Final = (
    "read ",
    "follow ",
    "run each recorded command",
    "install.bat",
    "git ",
    "cat ",
    "ls ",
    "bash scripts/check.sh",
)


def plan_commands() -> list[str]:
    """Every distinct ``verify_command``, in a stable order."""
    document = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    found: set[str] = set()
    for epic in document["epics"]:
        for milestone in epic.get("milestones", []):
            for task in milestone.get("tasks", []):
                command = (task.get("verify_command") or "").strip()
                if command:
                    found.add(command)
    return sorted(found)


#: Directories a command may name on their own — ``grep -rn 'SIGKILL' tests
#: packages`` names two of them with no separator to give them away.
ROOT_DIRECTORIES: Final = ("tests", "scripts", "docs", "packages", "installer", "pz-mod")


def _named_paths(command: str) -> list[str]:
    """Repository paths a command names, whatever verb introduces them.

    Globs come back as written. Resolving them is :func:`missing_paths`'s job,
    and it requires a match rather than skipping: ``packages/*/src`` matching
    nothing is exactly the rot this file looks for, and an earlier version of
    this function dropped every glob on the floor.
    """
    paths: list[str] = []
    for token in shlex.split(command):
        cleaned = token.strip("'\"")
        if "<" in cleaned:
            # A placeholder for the reader to fill in, not a path.
            continue
        if cleaned in ROOT_DIRECTORIES or cleaned.startswith(
            tuple(f"{name}/" for name in ROOT_DIRECTORIES)
        ):
            paths.append(cleaned)
    return paths


def unclassified(commands: Sequence[str]) -> list[str]:
    """Commands this file does not know how to check at all."""
    unknown: list[str] = []
    for command in commands:
        if command.startswith(CLI_PREFIX):
            continue
        if command.startswith(PROSE_PREFIXES):
            continue
        if _named_paths(command):
            continue
        unknown.append(command)
    return unknown


def _resolves(path: str) -> bool:
    if "*" in path:
        return any(REPO_ROOT.glob(path))
    return (REPO_ROOT / path).exists()


def missing_paths(commands: Sequence[str]) -> list[tuple[str, str]]:
    return [
        (command, path)
        for command in commands
        for path in _named_paths(command)
        if not _resolves(path)
    ]


def unparsable_cli_lines(commands: Sequence[str]) -> list[tuple[str, str]]:
    """The CLI lines the real parser refuses, with what it said."""
    parser = build_parser()
    refused: list[tuple[str, str]] = []
    for command in commands:
        if not command.startswith(CLI_PREFIX):
            continue
        complaint = io.StringIO()
        try:
            with contextlib.redirect_stderr(complaint), contextlib.redirect_stdout(io.StringIO()):
                parser.parse_args(shlex.split(command)[1:])
        except SystemExit:
            said = complaint.getvalue().strip().splitlines()
            refused.append((command, said[-1] if said else "the parser exited"))
    return refused


def named_scenarios(commands: Sequence[str]) -> set[str]:
    named: set[str] = set()
    for command in commands:
        tokens = shlex.split(command)
        for index, token in enumerate(tokens[:-1]):
            if token == "--scenario":
                named.add(tokens[index + 1])
    return named


def test_every_verify_command_is_classified() -> None:
    """No command may be skipped, or the checks below prove nothing."""
    commands = plan_commands()

    assert len(commands) > 100, f"only {len(commands)} commands read; the plan is not being parsed"
    unknown = unclassified(commands)
    assert not unknown, (
        "these verify commands match no shape this file can check, so they are "
        "going unverified:\n" + "\n".join(f"  {row}" for row in unknown)
    )


def test_every_path_the_plan_names_is_on_disk() -> None:
    missing = missing_paths(plan_commands())

    assert not missing, (
        "the plan tells an operator to run a command naming something that does "
        "not exist:\n" + "\n".join(f"  {path}  ← {command}" for command, path in missing)
    )


def test_every_cli_line_the_plan_names_parses() -> None:
    """Asked of the real parser, so a removed flag fails too."""
    commands = plan_commands()
    cli = [c for c in commands if c.startswith(CLI_PREFIX)]

    assert cli, "no pz-agent lines found in the plan; the prefix has changed"
    refused = unparsable_cli_lines(commands)
    assert not refused, (
        "the plan tells an operator to run a pz-agent line the CLI refuses:\n"
        + "\n".join(f"  {command}\n      {why}" for command, why in refused)
    )


def test_the_plan_and_the_catalogue_name_the_same_scenarios() -> None:
    """Both directions: an invented id, and a scenario nobody is told to run."""
    known = {scenario.id for scenario in SCENARIOS}
    named = named_scenarios(plan_commands())

    assert not named - known, f"the plan names scenarios that do not exist: {sorted(named - known)}"
    assert not known - named, (
        "the catalogue defines scenarios the plan never tells anyone to run: "
        f"{sorted(known - named)}"
    )


@pytest.mark.parametrize(
    ("planted", "checker", "expected"),
    [
        (".venv/bin/pytest tests/unit/test_that_was_renamed.py -q", missing_paths, 1),
        (".venv/bin/pz-agent replay --no-such-flag trace.jsonl", unparsable_cli_lines, 1),
        (".venv/bin/python scripts/check_gone.py", missing_paths, 1),
        ("grep -rn 'x' packages/*/nowhere", missing_paths, 1),
        ("grep -rn 'x' packages/*/src", missing_paths, 0),
    ],
    ids=["renamed test", "removed flag", "deleted script", "glob matching nothing", "live glob"],
)
def test_each_checker_catches_its_own_kind_of_rot(
    planted: str,
    checker: object,
    expected: int,
) -> None:
    """The checkers, run against the rot they exist to find.

    Planted as a command list rather than by editing the plan on disk: the plan
    is generated, and a test that rewrote it would be measuring its own edit
    rather than the checker.
    """
    assert len(checker([planted])) == expected  # type: ignore[operator]


def test_an_unknown_shape_is_refused_rather_than_skipped() -> None:
    assert unclassified(["ssh operator@machine 'do the thing'"]) == [
        "ssh operator@machine 'do the thing'"
    ]
    assert unclassified([".venv/bin/pz-agent doctor --json"]) == []
