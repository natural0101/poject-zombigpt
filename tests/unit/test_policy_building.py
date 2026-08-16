"""Placement policy: what stands there, what would be sealed in, what it costs.

Every refusal is checked for the *reason* it reports, because the explanation is
the deliverable — "a wall there leaves you nowhere to walk" is what the agent
says out loud when it declines to put something permanent in the world.

The enclosure tests carry the weight of the file, and they are written around
the one thing that is easy to get wrong: what a *passing* check is allowed to
claim. It is not "the character is free". It is "within the squares this
observation described, this placement does not remove the last exit". The tests
below pin both halves — the wall that seals, and the honest silence about
everything past the edge of the window.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from pz_agent_core.actions.adapters.doors import DOOR_OBJECT_KIND as ADAPTER_DOOR_KIND
from pz_agent_core.actions.adapters.movement import (
    SEMANTIC_BLOCKED as ADAPTER_BLOCKED,
)
from pz_agent_core.actions.adapters.movement import (
    SEMANTIC_CLOSED_WINDOW as ADAPTER_CLOSED_WINDOW,
)
from pz_agent_core.actions.adapters.movement import (
    SEMANTIC_DROP as ADAPTER_DROP,
)
from pz_agent_core.actions.adapters.movement import (
    SEMANTIC_LOADED as ADAPTER_LOADED,
)
from pz_agent_core.actions.adapters.movement import (
    SQUARE_OBJECT_KIND as ADAPTER_SQUARE_KIND,
)
from pz_agent_core.policy import building as policy
from pz_agent_core.policy.building import (
    BUILDING_KEY,
    MAX_FLOOD_SQUARES,
    MAX_STRUCTURES_PER_ITEM,
    MAX_WINDOW_SQUARES,
    SEMANTIC_BLOCKED,
    SEMANTIC_DROP,
    SEMANTIC_LOADED,
    BuildingDecision,
    BuildingRefusal,
    BuildingVerdict,
    EnclosureCheck,
    StructureView,
    assess_build,
    assess_placement,
    enclosure_after,
    observed_structures,
    read_window,
    structure_named,
)
from pz_agent_core.policy.crafting import MaterialNeed
from pz_agent_core.protocol import ItemView, NearbyObject, Observation
from tests.fixtures.adapter_worlds import (
    CRATE_REF,
    HOME_X,
    HOME_Y,
    HOME_Z,
    MAIN_REF,
    a_square,
    a_world,
    a_world_object,
    an_item,
    crate_container,
    main_container,
    object_ref,
)

WALL = "WoodenWall"
DOORWAY = "WoodenDoorFrame"
PLANK = "Base.Plank"

#: The square the tests place things on: one step east of the character.
TARGET = (HOME_X + 1, HOME_Y, HOME_Z)
HOME = (HOME_X, HOME_Y, HOME_Z)


def structure_payload(
    name: str = WALL,
    *,
    materials: list[Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A structure the character knows: one plank, and it blocks when placed."""
    payload: dict[str, Any] = {
        "name": name,
        "display_name": "Wooden Wall",
        "known": True,
        "blocks_movement": True,
        "materials": materials if materials is not None else [{"full_type": PLANK, "count": 1}],
    }
    payload.update(overrides)
    return payload


def plank(
    runtime_id: str = "m1",
    *,
    container_ref: str = MAIN_REF,
    structures: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> ItemView:
    """One material item, carrying the building readout it participates in."""
    block: dict[str, Any] = {
        "structure_count": 1,
        "known_structure_count": 1,
        "structures": structures if structures is not None else [structure_payload()],
    }
    return an_item(
        runtime_id=runtime_id,
        container_ref=container_ref,
        full_type=PLANK,
        display_name="Plank",
        category="Item",
        extra={BUILDING_KEY: block},
        **overrides,
    )


def bare(runtime_id: str, full_type: str = PLANK, **overrides: Any) -> ItemView:
    """A material carrying no readout of its own — most of them do not."""
    return an_item(
        runtime_id=runtime_id,
        full_type=full_type,
        display_name=full_type,
        category="Item",
        **overrides,
    )


def block(
    radius: int = 1,
    *,
    blocked: tuple[tuple[int, int], ...] = (),
    missing: tuple[tuple[int, int], ...] = (),
    z: int = HOME_Z,
) -> list[NearbyObject]:
    """A square block centred on the character, minus and marked as asked.

    Offsets in *blocked* get the observer's ``blocked`` assessment; offsets in
    *missing* are simply not described, which is how "the observer said nothing
    about that square" is spelled.
    """
    squares: list[NearbyObject] = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            if (dx, dy) in missing:
                continue
            marks = [SEMANTIC_LOADED]
            if (dx, dy) in blocked:
                marks.append(SEMANTIC_BLOCKED)
            squares.append(a_square(HOME_X + dx, HOME_Y + dy, z, semantics=marks))
    return squares


def world(
    *items: ItemView,
    objects: list[NearbyObject] | None = None,
    containers: list[Any] | None = None,
    **overrides: Any,
) -> Observation:
    return a_world(
        items=list(items),
        objects=block() if objects is None else objects,
        containers=containers if containers is not None else [main_container(), crate_container()],
        **overrides,
    )


def only_structure(observation: Observation) -> StructureView:
    inventory = observation.inventory
    assert inventory is not None
    structures = observed_structures(inventory)
    assert len(structures) == 1
    return structures[0]


# --------------------------------------------------------------------------
# the observation contract, mirrored rather than imported
# --------------------------------------------------------------------------


def test_the_mirrored_observation_tokens_match_the_action_layers() -> None:
    """The policy sits below the adapters, so it copies these five strings.

    A copy is only safe while something fails when it drifts, and this is that
    something: the day the observer renames ``blocked``, the adapter constant
    moves and this assertion — not a silently permissive placement check — is
    what notices.
    """
    assert policy.SQUARE_OBJECT_KIND == ADAPTER_SQUARE_KIND
    assert policy.SEMANTIC_LOADED == ADAPTER_LOADED
    assert policy.SEMANTIC_BLOCKED == ADAPTER_BLOCKED
    assert policy.SEMANTIC_CLOSED_WINDOW == ADAPTER_CLOSED_WINDOW
    assert policy.SEMANTIC_DROP == ADAPTER_DROP
    assert policy.DOOR_OBJECT_KIND == ADAPTER_DOOR_KIND


# --------------------------------------------------------------------------
# reading the readout
# --------------------------------------------------------------------------


def test_the_same_structure_reported_by_two_materials_is_read_once() -> None:
    payload = structure_payload(
        materials=[{"full_type": PLANK, "count": 2}, {"full_type": "Base.Nails", "count": 1}]
    )
    observation = world(
        plank("m1", structures=[payload]),
        plank("m2", structures=[payload]),
    )

    inventory = observation.inventory
    assert inventory is not None
    assert [s.name for s in observed_structures(inventory)] == [WALL]


def test_structures_are_returned_in_name_order_whatever_the_inventory_order() -> None:
    observation = world(
        plank("m1", structures=[structure_payload("Zzz"), structure_payload("Aaa")])
    )

    inventory = observation.inventory
    assert inventory is not None
    assert [s.name for s in observed_structures(inventory)] == ["Aaa", "Zzz"]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"materials": [{"full_type": PLANK, "count": 1}]}, id="unnamed"),
        pytest.param(structure_payload(materials=[]), id="empty-materials"),
        pytest.param(structure_payload(materials=[{"full_type": "", "count": 1}]), id="no-type"),
        pytest.param(structure_payload(materials=["Base.Plank"]), id="not-an-object"),
    ],
)
def test_a_structure_that_cannot_be_read_whole_is_no_candidate(payload: dict[str, Any]) -> None:
    """All-or-nothing, like the crafting readout, and for the same reason.

    A requirement list read in part understates what the placement spends, and a
    structure with no name has no postcondition anyone could prove.
    """
    observation = world(plank("m1", structures=[payload]))

    inventory = observation.inventory
    assert inventory is not None
    assert observed_structures(inventory) == ()


def test_the_readout_is_bounded_per_item() -> None:
    many = [structure_payload(f"Wall{index:02d}") for index in range(MAX_STRUCTURES_PER_ITEM + 4)]
    observation = world(plank("m1", structures=many))

    inventory = observation.inventory
    assert inventory is not None
    assert len(observed_structures(inventory)) == MAX_STRUCTURES_PER_ITEM


def test_an_item_with_a_junk_readout_contributes_nothing_and_raises_nothing() -> None:
    observation = world(bare("m1"), an_item("m2", extra={BUILDING_KEY: "not a table"}))

    inventory = observation.inventory
    assert inventory is not None
    assert observed_structures(inventory) == ()
    assert structure_named(inventory, WALL) is None


def test_an_unreadable_known_flag_is_not_a_learned_structure() -> None:
    payload = structure_payload()
    del payload["known"]
    observation = world(plank("m1", structures=[payload]))

    decision = assess_build(observation, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.STRUCTURE_UNKNOWN
    assert "unreadable is never learned" in decision.detail


def test_an_unreadable_blocking_flag_is_read_as_a_wall() -> None:
    """The doorway question, answered the safe way round.

    A build that does not say whether what it places can be walked through gets
    no credit for maybe being a door frame: the cost of guessing "door" is a
    character sealed into a room, and the cost of guessing "wall" is one
    refused command.
    """
    payload = structure_payload()
    del payload["blocks_movement"]
    observation = world(plank("m1", structures=[payload]))

    structure = only_structure(observation)

    assert structure.blocks_movement is None
    assert structure.blocks_when_placed


def test_a_structure_the_mod_calls_walk_through_does_not_block() -> None:
    observation = world(plank("m1", structures=[structure_payload(DOORWAY, blocks_movement=False)]))

    structure = only_structure(observation)

    assert not structure.blocks_when_placed


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------


def test_no_nearby_tier_is_no_window() -> None:
    assert read_window(a_world(no_nearby=True), HOME_Z) is None


def test_a_tier_that_describes_no_square_is_no_window() -> None:
    only_a_crate = a_world(objects=[a_world_object(object_ref("c1"))])

    assert read_window(only_a_crate, HOME_Z) is None


def test_a_window_larger_than_the_bound_is_refused_rather_than_trimmed() -> None:
    """Trimming a window can only ever remove walls from it.

    A window read in part is a window that has lost obstacles, never gained
    them, so a truncated read would turn a sealed room into an open one and a
    refusal into a wall. Refusing the whole read is the only direction that
    fails safe.
    """
    side = 40  # 1600 squares, comfortably past MAX_WINDOW_SQUARES
    huge = [a_square(HOME_X + dx, HOME_Y + dy) for dx in range(side) for dy in range(side)]
    assert len(huge) > MAX_WINDOW_SQUARES

    assert read_window(a_world(objects=huge), HOME_Z) is None


def test_the_window_holds_only_the_floor_it_was_asked_about() -> None:
    upstairs = a_square(HOME_X, HOME_Y, HOME_Z + 1)
    observation = a_world(objects=[*block(), upstairs])

    window = read_window(observation, HOME_Z)

    assert window is not None
    assert not window.describes((HOME_X, HOME_Y, HOME_Z + 1))
    assert window.describes(HOME)


def test_the_window_names_what_stands_on_a_square() -> None:
    crate = a_world_object(object_ref("c1"), x=TARGET[0], y=TARGET[1])
    observation = a_world(objects=[*block(), crate])

    window = read_window(observation, HOME_Z)

    assert window is not None
    assert window.is_occupied(TARGET)
    assert window.blockers_on(TARGET) == (f"container:{crate.ref}",)
    assert not window.is_passable(TARGET)


# --------------------------------------------------------------------------
# the gates, in order
# --------------------------------------------------------------------------


def test_an_unreadable_map_refuses_rather_than_passing() -> None:
    """The first gate, and the one whose direction matters most.

    "The mod sent no surroundings" is not "the coast is clear". A placement
    whose consequences cannot be computed has not been shown to be safe, and
    this is exactly the picture in which a trapping wall is most likely.
    """
    blind = a_world(items=[plank("m1")], no_nearby=True)

    decision = assess_build(blind, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.WOULD_TRAP_PLAYER
    assert "could not be computed" in decision.detail


def test_a_structure_nothing_observed_mentions_is_unknown() -> None:
    decision = assess_build(world(bare("m1")), WALL, TARGET)

    assert decision.refusal is BuildingRefusal.STRUCTURE_UNKNOWN
    assert decision.structure is None


def test_a_structure_the_character_has_not_learned_is_refused() -> None:
    observation = world(plank("m1", structures=[structure_payload(known=False)]))

    assert assess_build(observation, WALL, TARGET).refusal is BuildingRefusal.STRUCTURE_UNKNOWN


def test_a_square_the_observation_never_described_is_unreadable() -> None:
    observation = world(plank("m1"), objects=block(missing=((1, 0),)))

    decision = assess_build(observation, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.SQUARE_UNREADABLE
    assert "describes no square" in decision.detail


def test_a_square_described_but_not_loaded_is_unreadable() -> None:
    described_but_dark = [
        *block(missing=((1, 0),)),
        a_square(TARGET[0], TARGET[1], semantics=[]),
    ]
    observation = world(plank("m1"), objects=described_but_dark)

    decision = assess_build(observation, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.SQUARE_UNREADABLE
    assert "not loaded" in decision.detail


def test_a_square_something_stands_on_is_refused_naming_it() -> None:
    """The agent never clears a square, so it says what is in the way."""
    crate = a_world_object(object_ref("c1"), x=TARGET[0], y=TARGET[1])
    observation = world(plank("m1"), objects=[*block(), crate])

    decision = assess_build(observation, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.SQUARE_OCCUPIED
    assert decision.occupied_by == (f"container:{crate.ref}",)
    assert crate.ref in decision.detail
    assert "Clearing a square is not something this agent does" in decision.detail


def test_a_square_the_observer_calls_blocked_is_occupied() -> None:
    observation = world(plank("m1"), objects=block(blocked=((1, 0),)))

    decision = assess_build(observation, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.SQUARE_OCCUPIED
    assert decision.occupied_by == ()


def test_the_materials_are_the_last_question_not_the_first() -> None:
    """ "You cannot build here at all" outranks "you are one plank short"."""
    observation = world(
        plank("m1", structures=[structure_payload(materials=[{"full_type": PLANK, "count": 9}])]),
        objects=block(blocked=((1, 0),)),
    )

    assert assess_build(observation, WALL, TARGET).refusal is BuildingRefusal.SQUARE_OCCUPIED


# --------------------------------------------------------------------------
# the check this module exists for
# --------------------------------------------------------------------------


def test_a_wall_that_takes_the_last_exit_is_refused() -> None:
    """Three sides walled already, and the placement closes the fourth."""
    observation = world(plank("m1"), objects=block(blocked=((-1, 0), (0, 1), (0, -1))))

    decision = assess_build(observation, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.WOULD_TRAP_PLAYER
    assert decision.enclosure is not None
    assert not decision.enclosure.passed
    assert "nothing in this build can take a structure back down" in decision.detail.lower()


def test_the_same_placement_is_allowed_when_one_exit_remains() -> None:
    observation = world(plank("m1"), objects=block(blocked=((-1, 0), (0, 1))))

    decision = assess_build(observation, WALL, TARGET)

    assert decision.verdict is BuildingVerdict.BUILD
    assert decision.enclosure is not None
    assert decision.enclosure.passed
    assert decision.enclosure.exit_square is not None


def test_a_doorway_does_not_seal_the_square_it_stands_on() -> None:
    """The one placement that is not a wall, and the only reason it is not.

    Identical geometry to the refusal above: the difference is that the mod
    positively said this structure can be walked through.
    """
    payload = structure_payload(DOORWAY, blocks_movement=False)
    observation = world(
        plank("m1", structures=[payload]), objects=block(blocked=((-1, 0), (0, 1), (0, -1)))
    )

    decision = assess_build(observation, DOORWAY, TARGET)

    assert decision.verdict is BuildingVerdict.BUILD
    assert decision.enclosure is not None
    assert decision.enclosure.exit_square == TARGET


def test_walling_the_square_the_character_stands_on_is_a_trap() -> None:
    observation = world(plank("m1"))

    decision = assess_build(observation, WALL, HOME)

    assert decision.refusal is BuildingRefusal.WOULD_TRAP_PLAYER
    assert "standing on the square" in decision.detail


def test_a_character_whose_own_square_is_not_described_refuses() -> None:
    """No start square is no check, and no check is no permission."""
    observation = world(plank("m1"), objects=block(missing=((0, 0),)))

    decision = assess_build(observation, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.WOULD_TRAP_PLAYER
    assert "nowhere for the check to start" in decision.detail


def test_a_door_that_is_not_locked_counts_as_a_way_out() -> None:
    door = a_world_object(
        object_ref("d1"),
        x=HOME_X - 1,
        y=HOME_Y,
        kind=ADAPTER_DOOR_KIND,
        semantics=["door", "obstacle"],
        open=False,
        locked=False,
        barricaded=False,
    )
    observation = world(plank("m1"), objects=[*block(blocked=((0, 1), (0, -1))), door])

    decision = assess_build(observation, WALL, TARGET)

    assert decision.verdict is BuildingVerdict.BUILD


@pytest.mark.parametrize(
    ("locked", "barricaded"),
    [
        pytest.param(True, False, id="locked"),
        pytest.param(False, True, id="barricaded"),
        pytest.param(None, None, id="unread"),
    ],
)
def test_a_door_the_walk_cannot_resolve_is_not_a_way_out(
    locked: bool | None, barricaded: bool | None
) -> None:
    """An unread lock is not an unlocked lock, and neither is an exit.

    Stricter than the navigation map's ``passable_hint``, on purpose: that one
    decides where to walk and a walk that meets a locked door replans, while
    this one decides whether to raise something nothing can take back down.
    """
    door = a_world_object(
        object_ref("d1"),
        x=HOME_X - 1,
        y=HOME_Y,
        kind=ADAPTER_DOOR_KIND,
        semantics=["door", "obstacle"],
        locked=locked,
        barricaded=barricaded,
    )
    observation = world(plank("m1"), objects=[*block(blocked=((0, 1), (0, -1))), door])

    decision = assess_build(observation, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.WOULD_TRAP_PLAYER


@pytest.mark.parametrize("semantic", [SEMANTIC_DROP, policy.SEMANTIC_CLOSED_WINDOW])
def test_a_fall_or_a_climb_is_not_an_exit_the_check_may_count(semantic: str) -> None:
    """``movement.move_to`` refuses to walk onto either, so neither is a route.

    North and south are walled and the placement takes the east, which leaves
    the disputed square as the only candidate exit. It is described and loaded;
    it is still not a way out.
    """
    observation = world(
        plank("m1"),
        objects=[
            *block(blocked=((0, 1), (0, -1)), missing=((-1, 0),)),
            a_square(HOME_X - 1, HOME_Y, semantics=[SEMANTIC_LOADED, semantic]),
        ],
    )

    assert assess_build(observation, WALL, TARGET).refusal is BuildingRefusal.WOULD_TRAP_PLAYER


def test_the_fill_stops_at_the_edge_of_the_window_and_says_so() -> None:
    """The bound, as a property of the answer rather than a footnote."""
    observation = world(plank("m1"))
    structure = only_structure(observation)
    window = read_window(observation, HOME_Z)
    assert window is not None

    check = enclosure_after(observation, window, TARGET, blocks=structure.blocks_when_placed)

    assert check.passed
    assert "nothing is claimed about ground beyond that edge" in check.claim
    assert check.as_dict()["claim"] == check.claim


def test_a_search_that_runs_out_of_budget_has_proven_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap cannot fire while the window bound holds, so the test lowers it.

    What is being pinned is the direction of the branch, not the number: a
    fill that stops early refuses, because "did not find an exit" is never
    "there is one".
    """
    monkeypatch.setattr(policy, "MAX_FLOOD_SQUARES", 2)
    observation = world(plank("m1"), objects=block(radius=3))
    window = read_window(observation, HOME_Z)
    assert window is not None

    check = enclosure_after(observation, window, TARGET, blocks=True)

    assert not check.passed
    assert check.hit_bound
    assert "proven nothing" in check.detail
    assert MAX_FLOOD_SQUARES == 1024  # the shipped bound, unchanged by the patch


def test_the_fill_never_walks_a_square_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flood fill terminates because it remembers where it has been.

    ``visited`` is a set, so it stops growing once every reachable square is in
    it — which means ``MAX_FLOOD_SQUARES`` is checked against a number that has
    stopped moving, and the queue is what grows instead. Delete the
    ``if step in visited: continue`` guard and ``enclosure_after`` does not
    return at all for a window with a cycle in it. Nothing in the suite noticed.

    Two things about the shape of this test, both learned by planting against it.
    The window's outer ring is sealed, because an *open* window lets the fill
    leave through the first edge square it reaches — the first version of this
    test used one and passed with the guard deleted. And the bound is asserted
    as work done rather than by waiting: a non-terminating loop caught by a
    three-hundred-second timeout is a worse failure report than one caught at a
    stated ceiling.
    """
    # A sealed pocket: the outer ring of the window is blocked, so the fill can
    # never reach a passable edge square and must exhaust the inside instead.
    ring = tuple(
        (dx, dy) for dx in range(-2, 3) for dy in range(-2, 3) if max(abs(dx), abs(dy)) == 2
    )
    observation = world(plank("m1"), objects=block(radius=2, blocked=ring))
    window = read_window(observation, HOME_Z)
    assert window is not None
    ceiling = MAX_FLOOD_SQUARES * 8
    examined = 0
    real_is_passable = type(window).is_passable

    def counting_is_passable(self: Any, square: tuple[int, int, int]) -> bool:
        nonlocal examined
        examined += 1
        if examined > ceiling:
            raise AssertionError(
                f"the fill examined more than {ceiling} squares, so it is walking "
                "ground it has already walked and will not terminate"
            )
        result: bool = real_is_passable(self, square)
        return result

    monkeypatch.setattr(type(window), "is_passable", counting_is_passable)

    check = enclosure_after(observation, window, TARGET, blocks=True)

    # Returning at all is half the assertion; the other half is the wrapper
    # above, which fails the moment the fill exceeds its ceiling.
    assert check.visited <= check.window_squares
    assert examined <= ceiling


def test_a_diagonal_gap_is_not_an_exit() -> None:
    """Whether a character may cut a wall's corner is not this side's to guess.

    The south-west square is described, loaded, free and touching the edge of
    the window — an exit by every test except the one that matters, which is
    that no four-connected route reaches it once the placement closes the east.
    """
    observation = world(plank("m1"), objects=block(blocked=((-1, 0), (0, 1), (0, -1))))
    window = read_window(observation, HOME_Z)
    assert window is not None
    diagonal = (HOME_X - 1, HOME_Y - 1, HOME_Z)
    assert window.is_passable(diagonal) and window.is_frontier(diagonal)

    assert assess_build(observation, WALL, TARGET).refusal is BuildingRefusal.WOULD_TRAP_PLAYER


# --------------------------------------------------------------------------
# materials, on the crafting policy's shapes
# --------------------------------------------------------------------------


def test_a_placement_short_of_materials_says_what_is_short() -> None:
    observation = world(
        plank("m1", structures=[structure_payload(materials=[{"full_type": PLANK, "count": 3}])])
    )

    decision = assess_build(observation, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.MATERIALS_MISSING
    assert decision.shortfalls[0].needed == 3
    assert decision.shortfalls[0].free == 1


def test_a_reserve_is_its_own_refusal_so_the_user_can_answer_it() -> None:
    observation = world(
        plank("m1", favorite=True),
        plank("m2", favorite=True),
    )

    decision = assess_build(observation, WALL, TARGET)

    assert decision.refusal is BuildingRefusal.MATERIALS_RESERVED
    assert decision.shortfalls[0].reserved == 2
    assert "you reserved what it needs" in decision.detail


def test_a_material_in_a_world_container_is_counted_and_flagged_as_travel() -> None:
    observation = world(plank("m1", container_ref=CRATE_REF))

    decision = assess_build(observation, WALL, TARGET)

    assert decision.verdict is BuildingVerdict.BUILD
    assert decision.needs_travel


def test_everything_held_and_nothing_in_the_way_is_a_build() -> None:
    observation = world(plank("m1"))

    decision = assess_build(observation, WALL, TARGET)

    assert decision.verdict is BuildingVerdict.BUILD
    assert decision.refusal is None
    assert "one structure" in decision.detail


# --------------------------------------------------------------------------
# totality and the value objects
# --------------------------------------------------------------------------


def test_an_observation_without_an_inventory_refuses_rather_than_raising() -> None:
    structure = StructureView(
        name=WALL,
        display_name="Wooden Wall",
        known=True,
        blocks_movement=True,
        materials=(MaterialNeed(full_type=PLANK, count=1),),
    )
    blind = a_world(no_inventory=True, objects=block())

    assert assess_placement(blind, structure, TARGET).refusal is BuildingRefusal.MATERIALS_MISSING
    assert assess_build(blind, WALL, TARGET).refusal is BuildingRefusal.STRUCTURE_UNKNOWN


def test_the_decision_is_identical_under_shuffled_input() -> None:
    payloads = [structure_payload("Bbb"), structure_payload("Aaa")]
    items = [plank("m1", structures=payloads), bare("m2"), plank("m3", structures=payloads)]
    baseline = assess_build(world(*items), "Aaa", TARGET)

    for seed in range(8):
        shuffled = list(items)
        random.Random(seed).shuffle(shuffled)
        result = assess_build(world(*shuffled), "Aaa", TARGET)

        assert result.detail == baseline.detail
        assert result.verdict is baseline.verdict


def test_a_decision_carries_its_refusal_token_and_only_a_refusal_does() -> None:
    with pytest.raises(ValueError, match="a refusal carries its token"):
        BuildingDecision(
            verdict=BuildingVerdict.BUILD,
            refusal=BuildingRefusal.SQUARE_OCCUPIED,
            detail="contradiction",
            structure=None,
            square=TARGET,
        )
    with pytest.raises(ValueError, match="must say what it saw"):
        BuildingDecision(
            verdict=BuildingVerdict.BUILD,
            refusal=None,
            detail="   ",
            structure=None,
            square=TARGET,
        )


def test_an_enclosure_answer_must_say_what_it_saw_and_name_its_exit() -> None:
    with pytest.raises(ValueError, match="must say what it saw"):
        EnclosureCheck(
            passed=False, detail=" ", start=None, exit_square=None, window_squares=0, visited=0
        )
    with pytest.raises(ValueError, match="names the exit it found"):
        EnclosureCheck(
            passed=True,
            detail="a route remains",
            start=HOME,
            exit_square=None,
            window_squares=9,
            visited=1,
        )


def test_the_payload_says_what_it_saw_without_recomputation() -> None:
    observation = world(plank("m1"), objects=block(blocked=((-1, 0), (0, 1), (0, -1))))

    document = assess_build(observation, WALL, TARGET).as_dict()

    assert document["verdict"] == BuildingVerdict.REFUSE.value
    assert document["refusal"] == BuildingRefusal.WOULD_TRAP_PLAYER.value
    assert document["square"] == {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]}
    assert document["structure"]["blocks_when_placed"] is True
    assert document["enclosure"]["passed"] is False
    assert "no route remains" in document["enclosure"]["claim"]
