"""Sidecar behaviour gated on values the mod cannot produce.

Three times in this stabilization pass the same shape has turned up: the sidecar
branches on something, both branches are tested, and the mod has no code path
that reaches one of them. The suite is green because each side is exercised
against its own idea of the document; the behaviour is dead in the shipped
system and nobody could tell.

The square tier was the expensive one — ``movement.move_to`` refuses every real
observation because no ``kind="square"`` entry exists to find, and the sidecar's
fixtures mint the entries the mod never sends. It was found by hand while
chasing something unrelated. So were the two below. That is three for three
found by accident, which is the reason this file exists.

This is a **ledger, not a prohibition**. A dead gate is sometimes the right
state: ``observation.full`` is always true and the partial-snapshot merge is
simply unused, which is the safe direction. What must not happen is a dead gate
nobody knows about. Each row below therefore asserts the producer really is
still absent — so the day somebody implements one, this test fails and says to
move the row, rather than the gate quietly staying dead beside its new producer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
MOD_ROOT: Final = REPO_ROOT / "pz-mod"


def _mod_sources() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(MOD_ROOT.rglob("*.lua")))


#: Gates the sidecar makes decisions on, whose triggering value the mod has no
#: path to produce. Each value is (what the sidecar does with it, why the mod
#: cannot say it, and a pattern that would appear in the mod if it could).
WITHOUT_PRODUCER: Final = {
    "container.accessible == False": (
        "Five sidecar sites refuse on it — container.py twice, inventory.py, "
        "selection.py and the container_chain rollup — so an unreachable container "
        "is meant to be refused rather than attempted. ObserveModel computes "
        "`accessible = node.accessible ~= false`, and nothing anywhere in the mod "
        "ever sets that field: it is `true` at three hardcoded roots and `nil` "
        "everywhere else, so every container in every document the mod can build "
        "is accessible. Closing it needs an engine reader for reachability, which "
        "is the same unverified-symbol problem that sank the square tier.",
        r"accessible\s*=\s*false",
    ),
    "observation.full == False": (
        "observation/store.py branches three times on a partial snapshot, merging "
        "it onto the last full one. Observe.context sets `full = true` "
        "unconditionally, so the mod has never sent a delta and those branches "
        "have never run. Benign — a full snapshot every tick is the safe "
        "direction — but the merge is untested against anything real.",
        r"full\s*=\s*false",
    ),
    'nearby object kind "square"': (
        "movement.move_to, movement.move_near, world.inspect and the navigation "
        "local map all locate a destination square by scanning nearby.objects for "
        'kind == "square". The mod emits no such entry, so movement refuses '
        "every real observation with TARGET_NOT_LOADED. See LIMITATIONS.md for "
        "the full account and the two blockers a fix must still solve.",
        r'kind\s*=\s*[\'"]square[\'"]',
    ),
}


def test_the_mod_sources_are_being_read() -> None:
    """Every assertion below is an absence check, so an empty corpus passes them all."""
    sources = _mod_sources()
    assert len(sources) > 100_000, "the mod glob has stopped finding Lua"
    assert "ObserveModel" in sources, "the glob is not reaching the files this checks"


def test_a_present_producer_is_visible_to_this_check() -> None:
    """The positive control.

    Without it, a pattern language that matched nothing would make every row
    below pass by construction — which is the exact failure mode this file was
    written about.
    """
    sources = _mod_sources()
    assert re.search(r'kind\s*=\s*[\'"]corpse[\'"]', sources), (
        "the mod demonstrably emits a corpse object, and this check cannot see it, "
        "so its pattern matching proves nothing about the rows below"
    )


def test_every_gate_without_a_producer_is_still_without_one() -> None:
    """A row that has grown a producer is a gate nobody re-examined.

    This fails in the *good* direction: somebody implemented the missing half and
    now has to say so here, rather than the sidecar's dead branch waking up
    silently beside it.
    """
    sources = _mod_sources()
    now_produced = [
        gate for gate, (_reason, pattern) in WITHOUT_PRODUCER.items() if re.search(pattern, sources)
    ]
    assert now_produced == [], (
        "the mod can now produce these, so the sidecar behaviour gated on them is "
        "live: drop the row and make sure the behaviour is tested against a "
        "document the mod actually builds, not a fixture"
    )


def test_every_row_explains_itself() -> None:
    """A ledger of dead gates is only useful if each row says why."""
    for gate, (reason, pattern) in sorted(WITHOUT_PRODUCER.items()):
        assert len(reason) > 120, f"{gate} needs a reason a reader can act on"
        assert pattern.strip(), f"{gate} has no pattern, so its row proves nothing"
