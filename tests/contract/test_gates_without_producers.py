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

Everything after the third row was found on purpose. Two came out of an audit
asking, of every comment claiming a guarantee, whether the code it named gives
it — and both times the answer was a gate whose producer had never been written,
behind a comment saying it had. ``ActionState.type`` is that one: a safety rung
the spec asks for, dead since it was written.

The last three came from sweeping for this class deliberately, which nobody had
ever done. The world-container row is the expensive one: it takes ``loot_area``
with it, the same way the square tier takes movement. Eight rows now, and the
lesson has changed shape — the first three were accidents, so the count read as
bad luck; five deliberate finds in two sweeps says this class is *systematic* at
this seam, and the seam is a hand-written contract between two languages with no
schema binding them. Expect more, and note that three of the eight are one root:
the mod and the sidecar spelling the same fact differently.

**The command direction is already guarded, and better than this file is.**
These rows are about the observation seam. The seam running the other way — the
args the sidecar sends against the args each mod adapter declares — has a
standing mechanical check in ``test_adapter_args_agreement.py``, which dumps
every declaration through the mod's own loader
(``tests/lua/support/dump_adapter_args.lua``) and compares it to what
``build_args`` emits. It covers *both* adapter families, the game-facing ones and
ActionRuntime's control adapters, and its docstring records two real defects
caught that way: an ``origin`` object no adapter declared, which would have
refused every transfer, and ``action.wait``/``plan.cancel`` disagreeing about
``game_seconds`` versus ``duration_ms`` and about ``command_id``.

So do not re-audit that direction by hand, and do not write a second checker for
it — this note exists because someone did, found nothing new, and clobbered the
dumper on the way past. Read that test first.

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
    """Every line of the mod's Lua that the engine would actually run.

    Comments are stripped, and that is load-bearing rather than tidy. A row here
    is retired when the mod gains a *producer*, so a pattern matching prose
    retires it on a sentence. It happened: the crafting wave's ``squares``
    section opens by explaining that ``movement`` has been reading
    ``kind = "square"`` entries nobody published, and that explanation matched
    the very pattern standing guard over the gap — reporting the largest gap in
    the build as closed by the comment describing it. The squares that wave
    publishes are a separate ``nearby.squares`` tier; ``movement.py`` still
    scans ``nearby.objects`` for ``kind == "square"`` and still finds nothing.

    Line comments only. Lua's ``--[[ ]]`` blocks are left alone deliberately:
    stripping them needs a real parser to avoid eating a long string, and every
    false positive seen so far has been a ``--`` line.
    """
    lines: list[str] = []
    for path in sorted(MOD_ROOT.rglob("*.lua")):
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split("--", 1)[0] if line.lstrip().startswith("--") else line
            lines.append(code)
    return "\n".join(lines)


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
    'square semantic "closed_window"': (
        "movement._check_square reads the *square entry's* semantics and refuses "
        "a destination reachable only through a closed window, with "
        "POLICY_DENIED. No token of that name exists anywhere in the mod. Its "
        "sibling `stairs` is the same disagreement without being the same kind "
        "of absence, so it gets no row of its own: the mod does emit `stairs`, "
        "in ObserveModel.BASE_SEMANTICS, on the *staircase object* standing on "
        "the square -- deliberately, saying both are facts about an object "
        "rather than about the square and that emitting them twice would let "
        "the two copies disagree. _check_square looks on the square. So the "
        "cross-floor branch (`changes_floor and not (allow_stairs and STAIRS in "
        "semantics)`) can never see the token it needs and every floor-changing "
        "move refuses PATH_NOT_FOUND. That runs toward caution; this one is "
        "narrow too, because a square behind a closed window reads as not "
        "passable and is refused as `blocked` anyway, so what is lost is which "
        "refusal is reported. Both are recorded in LIMITATIONS.md as a "
        "square-versus-object placement disagreement, not repaired from this "
        "side.",
        r'[\'"]closed_window[\'"]',
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
    'ItemView.extra["weapon"]': (
        "combat/policy.py's weapon_condition_fraction reads the weapon's condition "
        'out of item.extra["weapon"], and the mod never builds an extra block at '
        "all. It does read the condition, and deliberately: Observe.playerStats "
        "puts the equipped weapon's wear in the open stats map as "
        "weapon_condition and weapon_condition_max, saying why in a comment -- "
        "'because the item tier has no condition field in the schema' -- and "
        "refusing to fabricate one when the reader is absent. So this is one "
        "bridge that was never built rather than two vocabularies drifting: the "
        "mod chose a place and documented it, the sidecar looks somewhere else, "
        "and nothing reads those stat names. The fraction is therefore always "
        "None. The direction is safe and the function says so: None means "
        "unreadable and the policy refuses on it rather than guessing, so an "
        "engagement stops at weapon_unusable instead of swinging a weapon nobody "
        "measured. It also sits behind combat_assist, which is experimental and "
        "cannot be live-confirmed at all, so nothing reaches it in the shipped "
        "configuration.",
        r"extra\s*=\s*\{[\s\S]{0,200}?weapon",
    ),
    "player.present == False": (
        "Five sidecar sites refuse on it -- the action engine, the planner "
        "executor, the autonomy gate, the reflex guard and the observation's own "
        "usable() -- and the mod cannot say it: Observe.playerFields sets "
        "present = true as a literal and is only reached with a player in hand. "
        "This one is benign, and understanding why is the point of the row. The "
        "condition it guards does arrive, by another route entirely: with no "
        "character, Observe.context returns nil and the tick publishes no "
        "observation, which the engine already treats as GAME_DISCONNECTED. The "
        "unusable-character case is carried by `alive`, which defaults the safe "
        "way -- unknown liveness reads as dead. So these are belt-and-braces "
        "checks on a fact that reaches the sidecar as silence, not as a false "
        "boolean. Recorded so the next reader does not mistake the gate for the "
        "mechanism. (The pattern is anchored on a word boundary: PZAgent_Main "
        "carries an unrelated player_present = false in the agent's own state.)",
        r"\bpresent\s*=\s*false",
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


def test_a_producer_written_through_a_constant_is_visible_too() -> None:
    """The second positive control, and the one this file learned the hard way.

    The corpse control above proves only that a *literal* is visible, and a
    literal is not how this mod usually writes a token. A row here once claimed
    the mod emitted no ``kind = "square"`` entry, which was true of that exact
    spelling and false of the mod: ``ObserveModel.buildSquare`` writes
    ``kind = ObserveModel.SQUARE_KIND`` with ``SQUARE_KIND = "square"`` declared
    at the top of the file, and ``mergeNearby`` folds those entries into
    ``nearby.objects``. The producer had been there since the building wave. The
    row survived because the pattern was checked against a simulated producer
    written the same literal way the pattern was — a both-directions check that
    tested the pattern against itself.

    So the control is: the square semantics the mod demonstrably *does* send
    must be findable here. If they are not, this file cannot be trusted to say
    any token is missing, whatever its rows claim.
    """
    sources = _mod_sources()
    for token in ("loaded", "blocked", "drop", "occupied"):
        assert re.search(rf'[\'"]{token}[\'"]', sources), (
            f"the mod names the square semantic {token!r} in "
            f"ObserveModel.SQUARE_SEMANTIC and this check cannot see it, so its "
            f"absence claims about closed_window and stairs prove nothing"
        )
    assert re.search(r'SQUARE_KIND\s*=\s*[\'"]square[\'"]', sources), (
        "the square entry's kind is declared through a constant; a checker that "
        "cannot see that declaration will report the square tier as unproduced, "
        "which is exactly the false negative this control exists to prevent"
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
