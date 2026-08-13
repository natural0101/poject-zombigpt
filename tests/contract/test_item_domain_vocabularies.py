"""The item-detail seam, checked mechanically instead of one field at a time.

``test_gates_without_producers.py`` is a ledger of dead gates found one by one.
Three of its eight rows turned out to be the *same* root: the mod and the sidecar
spelling one fact differently across a hand-written contract with nothing binding
them. ``schemas/observation.schema.json`` declares ``food`` and its siblings as
objects and constrains none of their properties, so nothing has ever compared the
two vocabularies.

This does. It reads the keys the mod's item readers emit, reads the keys the
sidecar's typed views ask for, and pins the disagreement to what is already known
and recorded. A new mismatch on either side fails here rather than becoming the
ninth thing somebody finds by accident.

**This is a ledger too, not a demand for parity.** A key the mod sends and nobody
reads is ordinary — the reader is free to ignore what it does not need. The
expensive direction is the other one: a key the sidecar *decides* on that the mod
never sends, which reads as the type's default for ever. ``FoodView.is_rotten``
asks whether ``freshness == "rotten"``, the mod sends ``rotten`` as a boolean, and
the answer has always been "no". The counts below are those measured by hand in
``docs/LIMITATIONS.md``; if this file and that section ever disagree, one of them
is out of date and this is the one that ran.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
OBSERVE_LUA: Final = (
    REPO_ROOT / "pz-mod" / "42" / "media" / "lua" / "client" / "PZAgent" / "Observe.lua"
)
POLICY: Final = REPO_ROOT / "packages" / "pz_agent_core" / "src" / "pz_agent_core" / "policy"

#: block -> (the mod reader that builds it, the sidecar module whose typed view
#: reads it, and the counts measured when this was written).
DOMAINS: Final = {
    "food": ("itemFood", "food.py", 22, 8, 6),
    "literature": ("itemLiterature", "literature.py", 11, 5, 2),
    "fluid": ("itemFluid", "drink.py", 16, 3, 1),
}

#: Keys the sidecar decides on that the mod does not send. Recorded, not
#: tolerated silently: every one of these reads as its type's default for ever.
#: See docs/LIMITATIONS.md, "The item-detail tier speaks two vocabularies".
UNSENT: Final = {
    "food": {
        "boredom_change",
        "burn_progress",
        "destroyed",
        "edible",
        "freshness",
        "frozen",
        "poison_power",
        "raw",
        "raw_unsafe",
        "remaining_portions",
        "required_tool",
        "requires_cooking",
        "rot_progress",
        "tainted",
        "total_portions",
        "unhappy_change",
    },
    "literature": {
        "already_read",
        "boredom_change",
        "destroyed",
        "kind",
        "max_level",
        "min_level",
        "pages_total",
        "unhappy_change",
        "unread_recipes",
    },
    "fluid": {
        "alcohol_units",
        "alcoholic",
        "capacity_units",
        "destroyed",
        "drinkable",
        "freshness",
        "frozen",
        "poison_power",
        "poisonous",
        "remaining_units",
        "rot_progress",
        "thirst_change",
        "type",
        "unhappy_change",
        "water",
    },
}


def _mod_keys(reader: str) -> set[str]:
    """The keys one of the mod's item readers puts in its returned table."""
    source = OBSERVE_LUA.read_text(encoding="utf-8")
    match = re.search(rf"local function {reader}\(item\)(.*?)\nend", source, re.S)
    assert match is not None, f"{reader} is no longer a local function taking one item"
    return set(re.findall(r"^\s{4}(\w+) =", match.group(1), re.M))


def _sidecar_keys(module: str) -> set[str]:
    """The keys one typed view asks its raw payload for."""
    return set(re.findall(r'payload,\s*"(\w+)"', (POLICY / module).read_text(encoding="utf-8")))


def test_both_extractors_see_something() -> None:
    """The positive control.

    Every assertion below compares two sets. An extractor that silently returned
    nothing would make the comparisons pass or fail for reasons having nothing to
    do with the seam, so each side has to demonstrably see real keys first.
    """
    for block, (reader, module, _reads, _sends, overlap) in DOMAINS.items():
        mod, side = _mod_keys(reader), _sidecar_keys(module)
        assert mod, f"{block}: read no keys out of {reader}"
        assert side, f"{block}: read no keys out of {module}"
        assert mod & side, (
            f"{block}: the two vocabularies now share nothing at all, which means "
            f"an extractor broke rather than that the seam got worse"
        )
        assert len(mod & side) == overlap, (
            f"{block}: {len(mod & side)} keys agree, {overlap} did when this was "
            f"measured — update this file and docs/LIMITATIONS.md together"
        )


def test_the_measured_counts_still_hold() -> None:
    """The numbers docs/LIMITATIONS.md quotes, re-derived rather than trusted."""
    for block, (reader, module, reads, sends, _overlap) in DOMAINS.items():
        mod, side = _mod_keys(reader), _sidecar_keys(module)
        assert len(side) == reads, f"{block}: sidecar now reads {len(side)}, not {reads}"
        assert len(mod) == sends, f"{block}: mod now sends {len(mod)}, not {sends}"


def test_no_new_key_is_decided_on_without_a_producer() -> None:
    """The one that earns this file.

    A key the sidecar reads and the mod never sends is a decision made on a
    default. The set is recorded above; anything new in it is a fresh instance of
    the defect this seam keeps producing, and anything that has *left* it is a
    producer somebody wrote — either way the ledger and LIMITATIONS.md need the
    edit before this passes again.
    """
    for block, (reader, module, _reads, _sends, _overlap) in DOMAINS.items():
        mod, side = _mod_keys(reader), _sidecar_keys(module)
        unsent = side - mod
        assert unsent == UNSENT[block], (
            f"{block}: the keys decided on without a producer have changed.\n"
            f"  new (sidecar reads, mod does not send): {sorted(unsent - UNSENT[block])}\n"
            f"  gone (a producer now exists, or the read was dropped): "
            f"{sorted(UNSENT[block] - unsent)}"
        )
