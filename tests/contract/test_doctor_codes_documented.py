"""A code a user is handed must be a code they can look up.

``pz-agent doctor`` stamps every check with a stable identifier — ``PZD001`` to
``PZD010`` — and ``README.md`` bills ``docs/TROUBLESHOOTING.md`` as "Doctor codes
and remedies". That document listed none of them. ``grep -rn 'PZD0' docs/``
returned nothing at all, so the one instruction the tool gives a stuck user
("look it up") pointed at a page where the code did not appear.

Both directions matter, and for different reasons.

A code the tool emits and the document omits is a user with an identifier and
nowhere to take it. A code the document names and the tool never emits is
worse in the quieter way: it is a page of remedies for a failure that cannot
happen, which costs the reader's trust in the rest of the page.

The mapping is checked too, not just the presence of the string. A table that
paired ``PZD006`` with the wrong check would pass a presence test and send
someone to fix their permissions when their mod is not loading.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK
from tests.fixtures.cli_worlds import make_world

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
TROUBLESHOOTING: Final = REPO_ROOT / "docs" / "TROUBLESHOOTING.md"

#: ``| `PZD001` | `game_installation` | …`` — the code and the check beside it.
_ROW: Final = re.compile(r"^\|\s*`(PZD\d+)`\s*\|\s*`([a-z_]+)`\s*\|", re.MULTILINE)

_CODE: Final = re.compile(r"PZD\d+")


def _documented() -> dict[str, str]:
    """Every code the table names, mapped to the check it names beside it."""
    return dict(_ROW.findall(TROUBLESHOOTING.read_text(encoding="utf-8")))


def _emitted(tmp_path: Path) -> dict[str, str]:
    """Every code ``doctor`` actually stamps, mapped to its check.

    Run through the real CLI rather than grepped. A code assembled from a
    variable, or one on a branch a grep reads as live and the runtime never
    takes, would put this check out of step with what a user is handed.
    """
    world = make_world(tmp_path)
    world.reset_streams()
    assert world.run("doctor", "--json") in (EXIT_OK, EXIT_FAILURE)
    document = json.loads(world.stdout)
    return {check["code"]: check["name"] for check in document["checks"]}


def test_every_code_the_doctor_emits_is_in_the_table(tmp_path: Path) -> None:
    emitted = _emitted(tmp_path)
    assert emitted, "the doctor emitted no checks, so this test would prove nothing"

    missing = sorted(set(emitted) - set(_documented()))
    assert missing == [], "these codes are handed to users and documented nowhere"


def test_every_code_the_table_names_is_one_the_doctor_emits(tmp_path: Path) -> None:
    documented = _documented()
    assert documented, "the code table has gone missing from TROUBLESHOOTING.md"

    invented = sorted(set(documented) - set(_emitted(tmp_path)))
    assert invented == [], "these are documented remedies for failures that cannot happen"


def test_each_code_is_documented_against_the_check_it_belongs_to(tmp_path: Path) -> None:
    """Presence is not enough; a row pointing at the wrong check misdirects."""
    emitted = _emitted(tmp_path)
    documented = _documented()

    wrong = {
        code: {"documented": documented[code], "actual": emitted[code]}
        for code in sorted(set(emitted) & set(documented))
        if documented[code] != emitted[code]
    }
    assert wrong == {}, "the table pairs these codes with the wrong check"


def test_no_stray_code_appears_elsewhere_in_the_document(tmp_path: Path) -> None:
    """Prose may reference a code, but only one the tool emits.

    ``PZD011`` written into an explanation would read as authoritative and match
    nothing the user ever sees.
    """
    text = TROUBLESHOOTING.read_text(encoding="utf-8")
    mentioned = set(_CODE.findall(text))
    unknown = sorted(mentioned - set(_emitted(tmp_path)))
    assert unknown == [], "TROUBLESHOOTING.md mentions codes the doctor never emits"
