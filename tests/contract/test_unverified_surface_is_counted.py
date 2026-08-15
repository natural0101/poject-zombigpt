"""How much of the engine surface is unverified, stated once and measured.

``docs/GAME_API_VERIFICATION.md`` is the inventory of every Project Zomboid
symbol this mod touches without ever having called it. Its size is the single
number that tells the agent about to run the first live session how much risk
is in front of them — and four documents carried four different versions of it,
none matching the tree:

===========================  ==========================  ====================
document                     ``grep "Build 42:"`` lines   ``requires_live`` rows
===========================  ==========================  ====================
``GAME_API_VERIFICATION``    "nine lines, in two files"   "159 symbol rows"
``LOCAL_DEBUG_MAP``          "six comments"               "52 symbols"
``LIVE_TEST_PLAYBOOK``       "finds six of them"          "52 symbols"
``LOCAL_AGENT_PROMPT``       "шесть строк в двух файлах"  "сто двадцать четыре"
measured                     **10 lines, 3 files**        **167 rows**
===========================  ==========================  ====================

Two of them said fifty-two against a real one hundred and sixty-seven, in the
documents whose whole job is to size that risk before anyone starts. The
inventory itself — the one that says *"This document is the list"* — was wrong
about its own table.

So the number now lives in exactly one document, and the other three point at
it. That is the same shape as the wrapper counts next door, with one difference
worth stating: a wrapper can drop its count because nobody needs it, while this
figure is the point, so it is stated and **checked** instead. Both figures are
measured here from the sources rather than compared between prose: the grep is
the one the documents tell the operator to run, and the table is parsed as the
table.

That means adding a symbol row, or a ``-- Build 42:`` comment, fails this file
until the inventory's own sentence is updated. Deliberate. The alternative is
the state this replaces, where the number drifted for months in four places at
once and every reader was told the surface was a third of its real size.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INVENTORY: Final = REPO_ROOT / "docs" / "GAME_API_VERIFICATION.md"
MOD_ROOT: Final = REPO_ROOT / "pz-mod"

#: The satellite documents. Each used to state its own figure; each now points
#: at the inventory. Named rather than globbed so widening the rule is a
#: deliberate act, and so the historical records stay out of it.
SATELLITES: Final = (
    REPO_ROOT / "docs" / "LOCAL_DEBUG_MAP.md",
    REPO_ROOT / "docs" / "LIVE_TEST_PLAYBOOK.md",
    REPO_ROOT / "docs" / "LOCAL_AGENT_PROMPT.md",
)

#: The comment the documents tell the operator to grep for.
MARKER: Final = "Build 42:"

_WORDS: Final = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


def _grep_lines() -> list[str]:
    """The real grep, run rather than described."""
    result = subprocess.run(
        ["grep", "-rn", MARKER, str(MOD_ROOT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _symbol_rows() -> list[list[str]]:
    """Every symbol row of the inventory table, as its cells.

    A symbol row is one with the table's full column count. The legend above it
    is a two-column table using the same words, and counting it was how the
    first measurement of this came out one too high.
    """
    rows: list[list[str]] = []
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 7:
            continue
        status = cells[6].strip("`").strip()
        if not status or status.lower() == "status" or set(status) <= set("-: "):
            continue
        rows.append(cells)
    return rows


def _stated(pattern: str) -> int | None:
    """The number the inventory states for *pattern*, digits or words."""
    match = re.search(pattern, INVENTORY.read_text(encoding="utf-8"), re.IGNORECASE)
    if match is None:
        return None
    raw = match.group("count").lower()
    return int(raw) if raw.isdigit() else _WORDS.get(raw)


def test_the_grep_the_documents_name_still_finds_something() -> None:
    """A marker nobody uses any more would make the figure below vacuous."""
    assert _grep_lines(), (
        f"no {MARKER!r} comments in {MOD_ROOT}; the documents send the operator to "
        f"a grep that finds nothing"
    )


def test_the_table_still_has_symbol_rows() -> None:
    """Likewise: a parse that matched nothing would pass every count."""
    assert len(_symbol_rows()) > 50, "the inventory table parsed to almost nothing"


def test_the_inventory_states_the_grep_it_really_returns() -> None:
    """Ten lines in three files, measured — it said nine in two."""
    lines = _grep_lines()
    files = {line.split(":", 1)[0] for line in lines}

    stated_lines = _stated(r"returns\s+(?P<count>\d+|\w+)\s+lines")
    stated_files = _stated(r"lines,\s+in\s+(?P<count>\d+|\w+)\s+files")

    assert stated_lines == len(lines), (
        f"{INVENTORY.name} says the grep returns {stated_lines} lines; it returns {len(lines)}"
    )
    assert stated_files == len(files), (
        f"{INVENTORY.name} says {stated_files} files; the grep spans {len(files)}"
    )


def test_the_inventory_states_the_number_of_rows_it_carries() -> None:
    """The load-bearing figure: how much of the surface is unconfirmed."""
    rows = _symbol_rows()
    stated = _stated(r"carries\s+(?P<count>\d+)\s+symbol rows")

    assert stated == len(rows), (
        f"{INVENTORY.name} says it carries {stated} symbol rows; the table has {len(rows)}"
    )


def test_every_row_is_still_unconfirmed() -> None:
    """The sentence says "every one ``requires_live``". If a live session ever
    confirms one, that wording has to change with it rather than quietly become
    an overstatement — the opposite direction from the defect this file records,
    and no more acceptable."""
    statuses = {row[6].strip("`").strip() for row in _symbol_rows()}

    assert statuses == {"requires_live"}, (
        f"the table now carries {sorted(statuses)}, and the inventory still says "
        f"every row is requires_live"
    )


@pytest.mark.parametrize("document", SATELLITES, ids=lambda p: p.name)
def test_no_satellite_states_a_size_of_its_own(document: Path) -> None:
    """One place states it, everything else points there.

    Narrow on purpose: what is forbidden is a count attached to the words this
    inventory is measured in, not any number appearing in these documents.
    """
    text = document.read_text(encoding="utf-8")
    claims = re.findall(
        r"\b(?:\d+|" + "|".join(_WORDS) + r"|сто\s+\w+|двадцать\s+\w+)\s+"
        r"(?:symbols|символов|symbol rows|`requires_live`)",
        text,
        re.IGNORECASE,
    )

    assert claims == [], (
        f"{document.name} states its own size of the unverified surface ({claims}); "
        f"{INVENTORY.name} is the one place that does, and every copy of this "
        f"number that has existed has rotted"
    )


@pytest.mark.parametrize("document", SATELLITES, ids=lambda p: p.name)
def test_every_satellite_sends_the_reader_to_the_inventory(document: Path) -> None:
    """Removing the number without leaving an address would be worse than the defect."""
    assert "GAME_API_VERIFICATION.md" in document.read_text(encoding="utf-8"), (
        f"{document.name} no longer states a size and does not say where to find one"
    )
