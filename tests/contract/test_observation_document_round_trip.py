"""One document, built by the mod and read by the sidecar, in one process.

The observation seam is the one place in this project where a defect has been
found eight times and never by a test. The reason is structural and is stated in
``test_gates_without_producers.py``: the sidecar's fixtures build the document
the sidecar expects, the mod's suites build the document the mod emits, and
until now nothing put one side's output into the other side's reader. Both sides
are green against their own idea of the contract.

The command direction has had ``test_adapter_args_agreement.py`` for a long
time, which runs the mod's own loader and compares its declarations to what the
sidecar sends. This is that check, pointed the other way.

``tests/lua/support/dump_observation.lua`` builds one observation through
``Observe.playerFields``, ``Observe.inventoryRoots`` and ``ObserveModel.build``
— the shipped code, not a fixture — against fakes that stand in for the *engine*
and answer exactly the accessor names the mod's own readers ask for. Nothing
here writes down what the document should look like. Python then decodes it with
``Observation.from_dict`` and hands the items to the typed views the policies
use.

**What this catches that a key-set comparison cannot.** The item-detail blocks
are raw ``JsonDict`` on both sides, so a disagreement is not a type error, not a
missing field and not a crash — it is a *decision* coming out wrong. The mod
reports a sandwich as rotten, burnt and poisonous; ``FoodView.is_rotten`` asks
whether ``freshness == "rotten"`` and answers no. That is the whole defect
class, demonstrated end to end rather than argued from two files. A regex over
the sources can be fooled by how the producer is written — one row in the gate
ledger was, for four commits. A test that runs the producer cannot be.

The assertions below are therefore written as *the reading the sidecar actually
gets*, with the raw value beside it. When a repair lands, they fail and say so.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_core.actions.adapters.movement import (
    SEMANTIC_BLOCKED,
    SEMANTIC_CLOSED_WINDOW,
    SEMANTIC_LOADED,
    SEMANTIC_STAIRS,
    SQUARE_OBJECT_KIND,
)
from pz_agent_core.policy.building import read_window
from pz_agent_core.policy.food import FoodView
from pz_agent_core.policy.literature import LiteratureView
from pz_agent_core.protocol.messages import ItemView, Observation

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DUMPER: Final = REPO_ROOT / "tests" / "lua" / "support" / "dump_observation.lua"
_INTERPRETERS: Final = ("lua5.4", "lua")


def _interpreter() -> str:
    for name in _INTERPRETERS:
        found = shutil.which(name)
        if found is not None:
            return found
    pytest.skip(f"no Lua interpreter on PATH (tried {', '.join(_INTERPRETERS)})")


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    """The observation the shipped mod builds, decoded from its own JSON."""
    completed = subprocess.run(
        [_interpreter(), str(DUMPER)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"the dumper failed, so nothing below is testing the seam:\n{completed.stderr}"
    )
    decoded = json.loads(completed.stdout)
    assert isinstance(decoded, dict)
    return decoded


def _item_named(observation: Observation, display_name: str) -> ItemView:
    inventory = observation.inventory
    assert inventory is not None, "the mod produced no inventory block"
    for item in inventory.items:
        if item.display_name == display_name:
            return item
    raise AssertionError(
        f"no item called {display_name!r} in the document the mod built; "
        f"saw {[i.display_name for i in inventory.items]}"
    )


def test_the_sidecar_can_decode_what_the_mod_builds(document: dict[str, Any]) -> None:
    """The positive control, and it is not a formality.

    If ``Observation.from_dict`` refuses the mod's own document, every other
    assertion in this file is vacuous — and a refusal here would itself be the
    most serious finding the seam could produce.
    """
    observation = Observation.from_dict(document)
    assert observation.inventory is not None
    assert len(observation.inventory.items) == 3, (
        "the dumper puts three items in the character's hands; if this changes, "
        "the assertions below are looking at something else"
    )


def test_a_rotten_meal_reads_as_fresh_to_the_food_policy(document: dict[str, Any]) -> None:
    """The defect, demonstrated rather than argued.

    The mod says ``rotten: true``. ``FoodView`` asks whether ``freshness`` is the
    string ``"rotten"``, the key is absent, ``read_str`` substitutes ``""``, and
    the answer is no. Nothing errors: the two sides simply spell the fact
    differently and the reading comes out confident and wrong.

    ``poisonous`` is asserted beside it because it is the one hazard key that
    *does* cross in this block — the seam is not uniformly broken, and a repair
    that fixed rot by breaking poison would pass a looser test than this one.
    """
    observation = Observation.from_dict(document)
    sandwich = _item_named(observation, "Sandwich")

    raw = sandwich.food
    assert raw is not None, "the mod built no food block for a sandwich it fed hunger from"
    assert raw.get("rotten") is True, "the mod stopped reporting rot; re-derive this file"
    assert raw.get("poisonous") is True

    view = FoodView.from_item(sandwich)
    assert view is not None
    assert view.is_rotten is False, (
        "FoodView.is_rotten is True, which means the vocabulary gap this file "
        "pins has been repaired — update docs/LIMITATIONS.md and the UNSENT set "
        "in test_item_domain_vocabularies.py, then change this assertion"
    )
    assert view.poisonous is True, (
        "poisonous crosses the seam and must keep crossing it: it is the reason "
        "poisoned food is refused at all"
    )


def test_a_books_pages_survive_the_seam(document: dict[str, Any]) -> None:
    """The half of the seam that was repaired, held in place.

    ``pages_total``, ``min_level`` and ``max_level`` used to be sent as
    ``pages``, ``skill_level_min`` and ``skill_level_max`` and read as nothing.
    The crafting wave renamed them on the mod's side. This is what that repair
    looks like from the sidecar's chair, and it fails if the rename is undone.
    """
    observation = Observation.from_dict(document)
    book = _item_named(observation, "Carpentry for Beginners")

    view = LiteratureView.from_item(book)
    assert view is not None
    assert view.pages_total == 220
    assert view.pages_read == 40
    assert view.min_level == 0
    assert view.max_level == 2
    assert view.unread_recipes is None, (
        "absent must stay unknown rather than zero; a real count here means the "
        "mod gained a recipe reader and the tri-state can be revisited"
    )


def test_the_mod_really_does_publish_squares(document: dict[str, Any]) -> None:
    """The assertion that would have caught a retracted claim, and did not exist.

    A row in ``test_gates_without_producers.py`` said for four commits that the
    mod emitted no ``kind = "square"`` entry, so movement refused every real
    observation and ``build_structure`` refused every placement. The producer had
    been there the whole time; the row's pattern searched for a literal and the
    mod writes the token through a constant. Nothing ran the producer, so nothing
    contradicted it.

    This runs it. ``ObserveModel.buildSquare`` mints the entries and
    ``mergeNearby`` folds them into ``nearby.objects``, and both are exercised
    above by the dumper walking a real square window.
    """
    observation = Observation.from_dict(document)
    nearby = observation.nearby
    assert nearby is not None, "the mod produced no nearby block"

    squares = [o for o in nearby.objects if o.kind == SQUARE_OBJECT_KIND]
    assert squares, (
        "no square entry in nearby.objects. If this is a deliberate change, the "
        "retraction in test_gates_without_producers.py has to be un-retracted "
        "and LIMITATIONS.md rewritten — movement and the enclosure check both "
        "scan for exactly this"
    )
    assert all(o.position is not None for o in squares), (
        "movement matches a square by position; an entry without one is invisible "
        "to the reader that needs it"
    )


def test_the_two_square_semantics_the_sidecar_reads_off_the_square(
    document: dict[str, Any],
) -> None:
    """What survived the retraction, pinned as behaviour rather than as prose.

    Three of the five tokens ``movement._check_square`` reads do cross the seam.
    ``closed_window`` and ``stairs`` do not, and the mod says why: both are facts
    about an object standing on a square rather than about the square, so it
    emits them on the object entry instead. The consequences are asymmetric —
    a floor-changing move always refuses, which is the cautious direction, while
    a closed-window square is refused as ``blocked`` under the wrong name.

    Asserted on the document the mod actually built, so it fails the day either
    token starts crossing.
    """
    observation = Observation.from_dict(document)
    nearby = observation.nearby
    assert nearby is not None

    seen: set[str] = set()
    for entry in nearby.objects:
        if entry.kind == SQUARE_OBJECT_KIND:
            seen.update(entry.semantics)

    assert SEMANTIC_LOADED in seen
    assert SEMANTIC_BLOCKED in seen, (
        "the window includes one solid square on purpose; without a blocked "
        "reading this test cannot tell a refusal from an unread square"
    )
    assert SEMANTIC_CLOSED_WINDOW not in seen, (
        "closed_window now crosses the seam — retire its row in "
        "test_gates_without_producers.py and correct docs/LIMITATIONS.md"
    )
    assert SEMANTIC_STAIRS not in seen, (
        "stairs now reaches the square entry, so a floor-changing move can "
        "finally be permitted — update LIMITATIONS.md and §9 of the report"
    )


def test_the_enclosure_check_reads_the_squares_the_mod_sends(
    document: dict[str, Any],
) -> None:
    """``build_structure``'s window, built from the mod's own document.

    This is the consumer a retracted claim said was dead: ``read_window`` was
    supposed to collect nothing and refuse every placement ``WOULD_TRAP_PLAYER``.
    It collects the squares the dumper's window produced. Running the producer
    settles it; reading the sources twice did not.
    """
    observation = Observation.from_dict(document)
    window = read_window(observation, 0)
    assert window is not None, (
        "read_window returned None on a document the mod built, which is the "
        "state the retracted row described. If this is real, build_structure "
        "refuses every placement and LIMITATIONS.md needs the entry back"
    )
    assert len(window) > 1


def test_an_unreadable_chase_arrives_as_unknown_rather_than_calm(
    document: dict[str, Any],
) -> None:
    """The tri-state that matters most, carried by a document the mod built.

    ``NearbyZombie.chasing`` is ``bool | None`` on the sidecar and omitted on the
    mod when the build exposes no target reader, and the mod says why in its own
    comment: "we could not tell" must not look like "it is not chasing". A
    ``False`` arriving where nothing was read would understate the threat, and
    the reflex guard is downstream of it.

    Both cases are in one document here. One zombie has a readable target and
    arrives ``chasing=True``; the other's reader is absent and arrives with the
    key *missing*, which is what keeps the sidecar's ``None`` reachable at all.
    Asserted on the decoded view rather than on the raw payload, because the
    defect this guards against would live in the decoding.
    """
    observation = Observation.from_dict(document)
    nearby = observation.nearby
    assert nearby is not None
    assert len(nearby.zombies) == 2, "the dumper puts two zombies in the cell"

    by_chase = {z.chasing for z in nearby.zombies}
    assert True in by_chase, "the zombie with a readable target must arrive chasing"
    assert None in by_chase, (
        "the zombie whose target reader is absent arrived with a decided value. "
        "If that value is False the seam has started reporting an unread chase "
        "as a calm one, which is the understatement the tri-state exists to "
        "prevent — check ObserveModel.buildZombie and NearbyZombie together"
    )
    assert False not in by_chase


def test_a_crate_the_planner_can_name_and_nothing_can_open(
    document: dict[str, Any],
) -> None:
    """The last live gap in the build, shown as a refusal instead of a row.

    The mod puts a crate in ``nearby.objects`` and ``buildObject`` mints it a
    proper ``container:`` reference, so a planner sees it and can name it in a
    goal. ``InventoryView.container`` searches ``inventory.containers`` and
    nothing else, and the crate is never added there — the mod's inventory has
    exactly two roots, the main one and each worn container, with carried
    containers nested inside items.

    So the reference resolves to nothing and every container action against it
    refuses ``INVALID_REF``. This is why ``loot_area`` cannot take anything out
    of anything, and it is the only one of this build's three known gaps that
    still costs a whole goal kind.

    Both halves are asserted, because the gap is precisely the gap *between*
    them: the crate is nameable, and it is unresolvable. A repair closes the
    second without touching the first.
    """
    observation = Observation.from_dict(document)
    nearby = observation.nearby
    inventory = observation.inventory
    assert nearby is not None
    assert inventory is not None

    crates = [
        entry
        for entry in nearby.objects
        if entry.kind != SQUARE_OBJECT_KIND and entry.ref.startswith("container:")
    ]
    assert len(crates) == 1, (
        "the dumper stands one crate on a nearby square; without it this test "
        "is asserting the absence of something that was never there"
    )
    crate = crates[0]
    assert "world" in crate.ref, "the crate's reference is a world-container reference"

    assert inventory.container(crate.ref) is None, (
        "the world container now resolves out of inventory.containers, which "
        "means the last of this build's three gaps has been closed — retire its "
        "row in test_gates_without_producers.py, rewrite the entry in "
        "docs/LIMITATIONS.md and §9 6c of the final report, and change this "
        "assertion to the positive one"
    )
    assert inventory.container("container:x:player-main") is None
