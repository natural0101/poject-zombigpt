"""No call that changes the game's state may enter the mod unnamed.

AGENTS.md's capability-honesty rule ends with a sentence that had no check
behind it: *"Never simulate the effect by writing stats."* Nothing enforced it.
``scripts/check_forbidden.py`` reads shipped Lua, but only for stub markers and
dynamic loading; ``docs/GAME_API_VERIFICATION.md`` records what the mod calls,
but nothing compared it with the code.

The hole that leaves is the one the whole architecture rests on. Every engine
access goes through ``Toolkit.call(owner, name, ...)`` — a generic dispatcher
with varargs, so it writes as readily as it reads — and
:func:`ActionRuntime.verify` cannot tell a world that moved from a world that
was written to: measured, ``ActionRuntime.observedPairs`` asks only that some
``x_before`` differ from its ``x_after``, and both readings are the adapter's
own. An adapter that set the player's endurance would therefore produce a
``succeeded`` ack carrying evidence of a change it caused itself, pass every
check in this repository, and mutate the save. "Success only by observation"
fails silently at exactly that one point.

So the rule here is narrow and mechanical: a string in shipped Lua whose shape
names a state-changing engine method must appear in the inventory, where a row
has to say what the symbol is, where it is used, what happens when it is
missing, what tests it, and what its verification status is. Measured over the
tree, five such strings exist — the two input-press tables in ``Combat.lua`` —
and all five are documented, so nothing correct is accused today.

What this does **not** do is decide whether a mutating call fabricates an
effect. No scanner can, and claiming otherwise would be the false precision this
project keeps removing; that is a review question, like the swallowing handler
AGENTS.md hands to reviewers for the same reason. What it does is make the act
impossible to perform silently: a new ``setEndurance`` fails here until someone
writes the row, and the row has a column for what proves it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, NamedTuple

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INVENTORY: Final = REPO_ROOT / "docs" / "GAME_API_VERIFICATION.md"
MOD_ROOT: Final = REPO_ROOT / "pz-mod"

#: Verb stems that name a call which changes something rather than reading it.
#: A shape rule, not a list of Build 42 symbols — this file must not pretend to
#: know an engine it has never run against. ``do``/``press`` are here because
#: the two calls the mod already makes through the game's own input are spelled
#: that way, and a stat write would arrive spelled ``set``.
MUTATING_STEMS: Final = (
    "set",
    "add",
    "remove",
    "clear",
    "reset",
    "apply",
    "force",
    "give",
    "take",
    "do",
    "press",
)

#: ``"setEndurance"`` but not ``"settings"``: the stem is followed by a
#: *capital*, which is how Java-derived method names in this engine are spelled.
#:
#: The stems are spelled in both cases rather than the pattern being compiled
#: with ``re.IGNORECASE``, which was the first version and is wrong in a way
#: worth recording: the flag relaxes ``[A-Z]`` too, so ``settings`` (``set`` +
#: ``t``) and ``address`` (``add`` + ``r``) matched. A guard that accuses
#: ordinary reads gets argued with once and switched off, and
#: :func:`test_a_reading_call_is_not_mistaken_for_a_writing_one` is what caught
#: it here rather than in review.
_STEM_SPELLINGS: Final = "|".join(
    spelling for stem in MUTATING_STEMS for spelling in (stem, stem.capitalize())
)
MUTATING_NAME: Final = re.compile('"((?:' + _STEM_SPELLINGS + r')[A-Z][A-Za-z0-9_]*)"')


class Mention(NamedTuple):
    name: str
    where: str


def _shipped_lua() -> list[Path]:
    return sorted(p for p in MOD_ROOT.rglob("*.lua") if p.is_file())


def _mutating_mentions() -> list[Mention]:
    """Every state-changing name spelled in shipped Lua, with where it is.

    Read from the files directly. An earlier check of this shape shelled out to
    grep and split its output on ``":"``, which recovers ``"D"`` from a
    ``D:\\a\\…`` path and took the Windows build red one commit after landing.
    """
    mentions: list[Mention] = []
    for path in _shipped_lua():
        relative = path.relative_to(REPO_ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("--"):
                # Comments name symbols to explain them; the ban is on calling
                # one, and the inventory quotes several in its own prose.
                continue
            for match in MUTATING_NAME.finditer(line):
                mentions.append(Mention(match.group(1), f"{relative}:{number}"))
    return mentions


def _documented_names() -> set[str]:
    """Every method name the inventory's symbol rows carry, however spelled.

    A row's first cell is prose around backticked symbols, and the symbols come
    in every combination the engine offers: ``IsoPlayer.setForceShove /
    setDoShove / DoShove``, ``ingredient `getCount` / `getAmount```, ``Java list
    shape: `size()` / `get(i)```. Each backticked token is split on the
    separators the document actually uses and reduced to the bare method name,
    because that is the form the Lua carries.

    Built by measurement: three earlier versions of this parser reported 39, 11
    and 2 undocumented names, and every one of them was this function reading
    the table too narrowly rather than a real gap.
    """
    names: set[str] = set()
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 7:
            # The legend above the table uses the same words in two columns.
            continue
        for quoted in re.findall(r"`([^`]+)`", cells[0]):
            for part in re.split(r"[/,+]", quoted):
                bare = part.strip().split(".")[-1].split("(")[0].strip()
                if bare:
                    names.add(bare)
    return names


def test_the_mod_changes_the_world_only_through_declared_calls() -> None:
    documented = _documented_names()
    undeclared = sorted(
        {m for m in _mutating_mentions() if m.name not in documented},
        key=lambda m: (m.where, m.name),
    )

    assert not undeclared, (
        "shipped Lua names a state-changing engine call that "
        f"{INVENTORY.relative_to(REPO_ROOT)} does not record:\n"
        + "\n".join(f"  {m.name} at {m.where}" for m in undeclared)
        + "\nAdd its row — what it is, where it is used, the fallback when the "
        "build lacks it, the test, and its verification status. If the call "
        "writes a player stat, there is no row to write: the postcondition "
        "would then be reading back what the adapter itself set."
    )


def test_the_state_changing_surface_is_the_two_input_presses() -> None:
    """The surface, pinned by name rather than counted.

    A count would pass while one call was swapped for another. These five are
    the game's own input for a shove and a swing, each probed through several
    spellings because none of them is confirmed against a running build — the
    least certain rows in the inventory, and the reason the postcondition is
    re-observed rather than assumed.
    """
    assert {m.name for m in _mutating_mentions()} == {
        "setForceShove",
        "setDoShove",
        "DoShove",
        "pressAttack",
        "DoAttack",
    }


def test_the_reader_would_see_a_stat_write() -> None:
    """The scan's own producer, run against the shape it exists to catch.

    Without this the file could pass over nothing: a pattern that matched no
    real name would leave both tests above green and the guard would be an
    assertion about an empty set. So the pattern is handed the line an adapter
    faking a rest would carry, and has to find it.
    """
    faked = '  local ok = Toolkit.call(stats, "setEndurance", 1.0)'

    assert [m.group(1) for m in MUTATING_NAME.finditer(faked)] == ["setEndurance"]
    assert "setEndurance" not in _documented_names()


def test_a_reading_call_is_not_mistaken_for_a_writing_one() -> None:
    """The other half: the pattern must leave the 100-odd read calls alone.

    ``settings`` and ``address`` begin with a mutating stem and are not mutating
    calls; the capital after the stem is what separates them, and a rule without
    it would accuse most of the mod.
    """
    reading = '  local n = Toolkit.readNumberOf(stats, { "getEndurance", "settings", "address" })'

    assert list(MUTATING_NAME.finditer(reading)) == []
