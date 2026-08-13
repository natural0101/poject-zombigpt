"""Sidecar behaviour gated on values the mod cannot produce.

Three times in this stabilization pass the same shape has turned up: the sidecar
branches on something, both branches are tested, and the mod has no code path
that reaches one of them. The suite is green because each side is exercised
against its own idea of the document; the behaviour is dead in the shipped
system and nobody could tell.

The square tier was the expensive one — ``movement.move_to`` refuses every real
observation because no ``kind="square"`` entry exists to find, and the sidecar's
fixtures mint the entries the mod never sends. It was found by hand while
chasing something unrelated. So were the two after it. That is three for three
found by accident, which is the reason this file exists.

The last two rows were not accidents. They came out of an audit that asked, of
every comment claiming a guarantee, whether the code it named actually gives it
— and both times the answer was a gate whose producer had never been written,
sitting behind a comment that said it had. ``ActionState.type`` is the one that
matters: a safety rung the spec asks for, dead since it was written, invisible
because the comment beside it asserted the mod filled the field. Five rows now,
two of them safety-relevant, and the count is a reason to keep looking rather
than a reason to feel finished.

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
    "ActionState.type": (
        "safety/reflex.py matches DEFAULT_VULNERABLE_ACTIONS against it to serve "
        "§17.2's 'visible zombie near during read/eat -> interrupt'. The mod never "
        "fills it: the observation's action block is Ownership.describe's table "
        "(Runtime.lua -> Observe.context), which carries ownership, busy, readable, "
        "total, mod_owned, foreign, truncated and classes and no action_type — the "
        "one field ObserveModel.action reads into `type`. So running_type is always "
        "the empty string and that rung has never fired. Bounded rather than absent: "
        "the flee rung above it ignores the action type, so the loss is the earlier "
        "reaction at interrupt_at, not the emergency at flee_at. The window below is "
        "measured, not guessed: describe's return table ends 784 characters into the "
        "function and the nearest unrelated action_type (the panic plan's, a "
        "different function) is 2296 in, so 1200 sees a real producer and not that.",
        r"function Ownership\.describe[\s\S]{0,1200}?action_type",
    ),
    "a world container in observation.inventory.containers": (
        "resolve_container refuses any ref not in inventory.containers, and "
        "InventoryView.container searches that list alone, so container.inspect's "
        "precondition (container.py:161) and its postcondition (:170) both need the "
        "crate to be in the tree. The mod's inventory has exactly two roots -- the "
        "main inventory and each worn container, plus CARRIED containers nested "
        "inside items -- and no third one. A nearby crate is *referenced*: "
        "buildObject mints it a container ref when the descriptor carries both an "
        "object_index and a container_index. It is never listed. So a world "
        "container can be named and never resolved, and every container action "
        "against a crate refuses INVALID_REF. loot_area cannot take anything out of "
        "anything. Recorded, not repaired: the missing half is a mod-side inventory "
        "tier for an open world container, which is a contract addition no test "
        "here can confirm. Pattern checked both ways -- no match today, matches when "
        "a WORLD root is spliced in beside the WORN one.",
        r"walkItemContainer\([\s\S]{0,200}?CONTAINER_KIND\.WORLD",
    ),
    "a nearby zombie's position sub-table": (
        "ObserveModel.dangerFloor decides whether a zombie is on the player's floor "
        'with `type(zombie.position) ~= "table"`, and its only production caller '
        "hands it the raw reader table from Observe.nearbyFields, whose zombies carry "
        "flat x, y, z — the position sub-table is added later, by buildZombie. So the "
        "guard's escape branch fires for every zombie and a horde one storey up counts "
        "as closing. The error runs toward caution, which is why the docstring was "
        "corrected and the code was not: reading the flat z would make a safety guard "
        "less conservative on static reasoning alone, and that wants a live game.",
        r"chasing\s*=\s*chasing,[\s\S]{0,400}?position\s*=\s*\{",
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
