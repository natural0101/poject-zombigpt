"""A scenario range, anywhere in the tree, ends where the catalogue does.

Three iterations found the same stale literal in three different places, and
each time the guard written for it was scoped to the files where it had just
been caught: ``packaging/windows/bat/*.bat`` first, then three named handoff
documents. Both were too narrow, and the third sweep found four more -
``scripts/check_release.py``'s own module docstring, a description in
``schemas/gameplay-knowledge.schema.json``, a task string in
``scripts/plan_epics_d.py``, and a row in ``docs/PROGRESS.md``.

None of those four was reachable: the release gate imports ``SCENARIO_IDS``
rather than enumerating, and the schema constrains ``proven_by`` only by length.
They were prose. But *"S01..S20"* in the docstring of the file that decides
whether v1.0.0 ships is read by people, and the pattern of my own guards being
drawn around the last defect rather than around the fact is the thing worth
fixing - so this one is repo-wide, and the two narrow range checks it replaces
have been removed.

**Two catalogues exist and their numbers collide**, which ``docs/RELEASE.md``
says in as many words: ``tests/game-smoke/`` holds S01-S15 plus an endurance
run, driven by ``pz-agent smoke``, and ``pz_agent_cli.livetest`` holds S01-S22,
driven by ``pz-agent live-test``. So ``S01-S15`` in that document is *correct*,
and a checker that flagged it would be a false accusation - the failure mode
this repository has already been taught to avoid once, by a pattern that could
not tell how its subject was written. Both ends are therefore derived: the live
catalogue from ``SCENARIO_IDS``, the smoke catalogue by listing its directory.
A range is wrong only when it ends at neither.

Exclusions are by name and each has a reason. ``CHANGELOG.md``,
``FINAL_IMPLEMENTATION_REPORT.md`` and ``tests/`` record defects, and a record of
this defect has to be able to quote it. ``docs/control/PLAN.md`` is the retired
hundred-step plan, kept as history. ``docs/blueprint/`` is the read-only
specification AGENTS.md forbids editing. ``.claude/worktrees/`` holds other
agents' checkouts, which are not this tree.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.livetest.scenarios import SCENARIO_IDS

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: Where a stale range would be read by a person and acted on.
SEARCHED_SUFFIXES: Final = frozenset({".py", ".md", ".json", ".yaml", ".yml", ".bat", ".lua"})

#: Directories that are not this tree, or are not ours to edit.
SKIPPED_DIRS: Final = frozenset(
    {".git", ".venv", ".claude", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"}
)

#: Files that quote the defect because they record it. Relative, POSIX.
ALLOWED: Final = frozenset(
    {
        "CHANGELOG.md",
        "FINAL_IMPLEMENTATION_REPORT.md",
        "docs/control/PLAN.md",  # the retired hundred-step plan, kept as history
    }
)

#: Prefixes excluded wholesale, each for a stated reason.
ALLOWED_PREFIXES: Final = (
    "tests/",  # a test that pins this defect has to be able to name it
    "docs/blueprint/",  # read-only specification, per AGENTS.md
)

#: ``S01..S20``, ``S01-S20``, ``S01 to S20``, en and em dashes included. Dashes
#: are spelled by codepoint: a literal en dash in a pattern is flagged as an
#: ambiguous character, and a range in prose is as likely to use one.
_RANGE: Final = re.compile(
    r"\bS(?P<first>\d{2})\b\s*(?:\.\.\.|\.\.|\u2026|to|through|[-\u2013\u2014])\s*"
    r"\bS(?P<last>\d{2})\b"
)


def _smoke_last() -> str:
    """The last numbered scenario of ``tests/game-smoke/``, listed rather than typed.

    ``S99_endurance.yaml`` is a sentinel rather than a position in the sequence,
    so it is excluded: no document writes "S01-S99".
    """
    numbers = sorted(
        path.name[1:3]
        for path in (REPO_ROOT / "tests" / "game-smoke").glob("S*.yaml")
        if path.name[1:3].isdigit() and path.name[1:3] != "99"
    )
    return numbers[-1]


def _searched() -> list[Path]:
    found: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SEARCHED_SUFFIXES:
            continue
        relative = path.relative_to(REPO_ROOT)
        if SKIPPED_DIRS & set(relative.parts):
            continue
        spelling = relative.as_posix()
        if spelling in ALLOWED or spelling.startswith(ALLOWED_PREFIXES):
            continue
        found.append(path)
    return sorted(found)


FILES: Final = _searched()


def test_the_sweep_reaches_the_tree() -> None:
    """A glob that matched almost nothing would make this file vacuous.

    The number is a floor rather than an equality: files are added routinely,
    and a test that had to be edited for every new one would be edited without
    being read.
    """
    assert len(FILES) > 200, f"only {len(FILES)} file(s) swept; the tree is larger than that"


def test_the_pattern_catches_every_spelling_that_has_appeared() -> None:
    """A control over the matcher. Each of these was a real occurrence."""
    sample = "S01..S20 and S01-S20 and S01 to S20 and S01\u2013S20"
    found = [match.group("last") for match in _RANGE.finditer(sample)]

    assert found == ["20", "20", "20", "20"]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_a_scenario_range_ends_where_a_catalogue_does(path: Path) -> None:
    """The catalogues are the facts; every range in the tree describes one of them."""
    first = SCENARIO_IDS[0][1:3]
    ends = {SCENARIO_IDS[-1][1:3], _smoke_last()}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # pragma: no cover - not a text file after all
        pytest.skip(f"{path} is not readable as UTF-8 text")

    wrong = [
        match.group(0)
        for match in _RANGE.finditer(text)
        if match.group("first") == first and match.group("last") not in ends
    ]

    assert wrong == [], (
        f"{path.relative_to(REPO_ROOT)} names scenario range(s) {wrong}, which end at "
        f"neither catalogue: live-test runs to S{SCENARIO_IDS[-1][1:3]}, "
        f"tests/game-smoke/ to S{_smoke_last()}"
    )


def test_the_two_catalogues_really_do_end_differently() -> None:
    """The premise of the rule above. If they ever converge, it loosens by one
    number without anyone noticing, so it is asserted rather than assumed."""
    assert SCENARIO_IDS[-1][1:3] != _smoke_last(), (
        "both catalogues now end at the same number; the two-ended rule has become "
        "a one-ended rule and should be simplified deliberately"
    )
