"""The section that answers "what still needs the game" must name the real bar.

``docs/PROGRESS.md`` has a section headed *Requires a live game session*, and it
is the section a reader — including whoever assembles
``FINAL_IMPLEMENTATION_REPORT.md``, which ``docs/RELEASE.md`` requires to list
exactly the steps that physically need the game — goes to for that answer.

It listed the 16 definitions under ``tests/game-smoke/`` and nothing else. Those
are real and are driven by ``pz-agent smoke``, so nothing in it was false. But
``scripts/check_release.py --release`` does not read them: it requires a ``PASS``
and hashed artefacts for every id in :data:`SCENARIO_IDS`, the 22-scenario
catalogue behind ``pz-agent live-test``. The two number their scenarios
independently and the numbers collide — ``S14`` is ``backup / restore`` in one
and ``SLEEP_REST`` in the other — so a reader who took that table for the whole
of it was reading the wrong list *and* a release bar three times smaller than
the real one.

What this pins, and what it does not. It requires the section to say the live
catalogue's size, taken from ``SCENARIO_IDS`` rather than typed here, and to
name the gate that reads it. It does not check the prose around those, which a
reviewer has to read. Anchoring on a sentence would mean inventing a phrase only
this file looks for, and this repository has already learned that a guard tied
to one wording is a guard that dies at the next rewrite.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from pz_agent_cli.livetest.scenarios import SCENARIO_IDS

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PROGRESS: Final = REPO_ROOT / "docs" / "PROGRESS.md"

#: The heading is the contract: this is where the question is answered.
HEADING: Final = "## Requires a live game session"

#: The gate that decides whether v1.0.0 may ship, named so the section points at
#: the thing a reader can run rather than at a claim about it.
RELEASE_GATE: Final = "check_release.py --release"


def _section() -> str:
    """The *Requires a live game session* section, up to the next heading."""
    text = PROGRESS.read_text(encoding="utf-8")
    start = text.find(HEADING)
    assert start != -1, f"{PROGRESS.name} no longer has a section headed {HEADING!r}"
    body = text[start + len(HEADING) :]
    end = body.find("\n## ")
    return body if end == -1 else body[:end]


def test_the_section_says_how_many_scenarios_the_release_bar_holds() -> None:
    """The count is derived on both sides, so it cannot drift on either."""
    section = _section()
    expected = str(len(SCENARIO_IDS))

    assert re.search(rf"\b{expected}\b", section), (
        f"the section never says {expected} — the size of the live-test catalogue "
        f"the release gate requires. It named only the {len(_smoke_definitions())} "
        "smoke definitions, which is a different catalogue with colliding numbers "
        "and no bearing on whether v1.0.0 may ship"
    )


def test_the_section_names_the_gate_that_reads_that_catalogue() -> None:
    """A number with no subject is a number a reader cannot check."""
    assert RELEASE_GATE in _section(), (
        f"the section does not name {RELEASE_GATE}, so nothing tells a reader which "
        "tool turns those scenarios into permission to release"
    )


def test_the_section_still_accounts_for_the_smoke_catalogue() -> None:
    """The control: the fix must not replace one omission with the other.

    Both catalogues need the game. A section that dropped ``tests/game-smoke/``
    to make room for the live one would satisfy the two checks above while
    losing exactly as much as it gained.
    """
    assert "game-smoke" in _section(), (
        "the section no longer mentions tests/game-smoke/, whose scenarios also "
        "need a live game and which nothing else in this document lists"
    )


def _smoke_definitions() -> list[Path]:
    return sorted((REPO_ROOT / "tests" / "game-smoke").glob("S*.yaml"))


def test_the_two_catalogues_really_are_different_sizes() -> None:
    """Without this the tests above could be pinning a coincidence.

    If the two catalogues ever held the same number of scenarios, the count
    assertion would pass on the wrong one and the distinction this file exists
    to keep would be invisible.
    """
    assert len(_smoke_definitions()) != len(SCENARIO_IDS), (
        "the smoke and live catalogues now hold the same number of scenarios; "
        "the count check above can no longer tell which one the section named"
    )
