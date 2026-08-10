"""The local navigation map: bounded, honest, and never inventing.

Every test here builds real protocol observations — the map's only input is
what the mod's observer actually emits — and checks one memory rule at a time:
what lands, what refuses to land, what newer knowledge overwrites, what older
knowledge must not, and what eviction forgets when the budget is hit.
"""

from __future__ import annotations

import pytest

from pz_agent_core.navigation import LocalMap
from pz_agent_core.navigation.local_map import NEVER_SEEN
from pz_agent_core.protocol import NearbyObject, NearbyView, Observation, Position
from tests.fixtures import DEFAULT_SESSION, make_observation, make_player

# ---------------------------------------------------------------------------
# builders — real protocol dataclasses, one knob at a time
# ---------------------------------------------------------------------------


def observed(
    seq: int,
    *,
    player_at: tuple[int, int, int] = (0, 0, 0),
    objects: list[NearbyObject] | None = None,
) -> Observation:
    x, y, z = player_at
    return make_observation(
        seq=seq,
        player=make_player(position=Position(x=float(x), y=float(y), z=z, direction="S")),
        nearby=NearbyView(objects=list(objects or [])),
    )


def a_door(
    x: int,
    y: int,
    z: int = 0,
    *,
    ref_id: str = "9001",
    open: bool | None = False,
    locked: bool | None = None,
    barricaded: bool | None = None,
    orientation: str | None = "north",
) -> NearbyObject:
    return NearbyObject(
        ref=f"object:{DEFAULT_SESSION}:{ref_id}:0",
        kind="door",
        distance=float(max(abs(x), abs(y))),
        position=Position(x=float(x), y=float(y), z=z),
        # The observer's own vocabulary: BASE_SEMANTICS gives doors "obstacle"
        # too, and the map must still file them as doors, not walls.
        semantics=["door", "obstacle"],
        open=open,
        locked=locked,
        barricaded=barricaded,
        orientation=orientation,
    )


def an_obstacle(x: int, y: int, z: int = 0, *, ref_id: str | None = None) -> NearbyObject:
    return NearbyObject(
        ref=f"object:{DEFAULT_SESSION}:{ref_id or f'tree{x}x{y}'}:0",
        kind="tree",
        distance=float(max(abs(x), abs(y))),
        position=Position(x=float(x), y=float(y), z=z),
        semantics=["tree", "obstacle"],
    )


def some_stairs(x: int, y: int, z: int = 0) -> NearbyObject:
    return NearbyObject(
        ref=f"object:{DEFAULT_SESSION}:stairs{x}x{y}:0",
        kind="stairs",
        distance=float(max(abs(x), abs(y))),
        position=Position(x=float(x), y=float(y), z=z),
        semantics=["stairs", "traversal"],
    )


def a_square(x: int, y: int, z: int = 0, *, semantics: list[str]) -> NearbyObject:
    return NearbyObject(
        ref=f"square:{DEFAULT_SESSION}:{x}:{y}:{z}",
        kind="square",
        distance=float(max(abs(x), abs(y))),
        position=Position(x=float(x), y=float(y), z=z),
        semantics=list(semantics),
    )


DOOR_REF = f"object:{DEFAULT_SESSION}:9001:0"


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


class TestIngest:
    def test_player_square_is_walkable_and_visited(self) -> None:
        world = LocalMap()
        world.observe(observed(1, player_at=(10, 20, 0)))
        cell = world.cell(10, 20, 0)
        assert cell.known
        assert cell.visited
        assert cell.walkable_hint
        assert not cell.obstructed
        assert cell.last_seen_seq == 1

    def test_unknown_square_is_not_proven_walkable(self) -> None:
        world = LocalMap()
        world.observe(observed(1))
        cell = world.cell(99, 99, 0)
        assert not cell.known
        assert not cell.walkable_hint
        assert not cell.visited
        assert cell.door is None
        assert cell.last_seen_seq == NEVER_SEEN

    def test_unknown_is_distinct_from_known_blocked(self) -> None:
        world = LocalMap()
        world.observe(observed(1, objects=[an_obstacle(5, 5)]))
        blocked = world.cell(5, 5, 0)
        unknown = world.cell(6, 6, 0)
        assert blocked.known and blocked.obstructed
        assert not unknown.known and not unknown.obstructed

    def test_object_without_position_lands_nowhere(self) -> None:
        world = LocalMap()
        ghost = NearbyObject(ref=DOOR_REF, kind="door", distance=2.0, position=None, open=False)
        world.observe(observed(1, objects=[ghost]))
        # Only the player's own square landed; the unplaceable door did not.
        assert len(world) == 1
        assert list(world.doors()) == []

    def test_door_is_remembered_as_a_door_with_state_and_ref(self) -> None:
        world = LocalMap()
        world.observe(
            observed(1, objects=[a_door(3, 0, open=False, locked=False, barricaded=False)])
        )
        cell = world.cell(3, 0, 0)
        assert cell.known
        assert cell.door is not None
        assert cell.door.ref == DOOR_REF
        assert cell.door.open is False
        assert cell.door.locked is False
        assert cell.door.barricaded is False
        assert cell.door.orientation == "north"
        assert cell.door.last_seen_seq == 1
        # A door carries the "obstacle" semantic, and still must not be filed
        # as a plain obstruction — its passability is its state's to decide.
        assert not cell.obstructed

    def test_obstacle_marks_square_obstructed(self) -> None:
        world = LocalMap()
        world.observe(observed(1, objects=[an_obstacle(4, 4)]))
        cell = world.cell(4, 4, 0)
        assert cell.known
        assert cell.obstructed
        assert not cell.walkable_hint

    def test_square_reported_blocked_is_obstructed(self) -> None:
        world = LocalMap()
        world.observe(
            observed(
                1,
                objects=[
                    a_square(2, 2, semantics=["loaded", "blocked"]),
                    a_square(2, 3, semantics=["loaded"]),
                ],
            )
        )
        assert world.cell(2, 2, 0).obstructed
        loaded = world.cell(2, 3, 0)
        assert loaded.known
        assert not loaded.obstructed
        # Known and unobstructed is still not *proven* walkable.
        assert not loaded.walkable_hint

    def test_stairs_square_is_remembered_as_floor_transition(self) -> None:
        world = LocalMap()
        world.observe(observed(1, objects=[some_stairs(7, 1)]))
        assert world.cell(7, 1, 0).stairs
        assert not world.cell(7, 2, 0).stairs

    def test_doors_iterates_every_remembered_door(self) -> None:
        world = LocalMap()
        world.observe(
            observed(
                1,
                objects=[
                    a_door(3, 0, ref_id="9001"),
                    a_door(8, 2, ref_id="9002"),
                    an_obstacle(5, 5),
                ],
            )
        )
        found = {cell.square: cell.door.ref for cell in world.doors() if cell.door is not None}
        assert found == {
            (3, 0, 0): f"object:{DEFAULT_SESSION}:9001:0",
            (8, 2, 0): f"object:{DEFAULT_SESSION}:9002:0",
        }


# ---------------------------------------------------------------------------
# revision and out-of-order knowledge
# ---------------------------------------------------------------------------


class TestRevision:
    def test_revision_is_highest_seq_ingested(self) -> None:
        world = LocalMap()
        assert world.revision == NEVER_SEEN
        world.observe(observed(5))
        world.observe(observed(3))
        assert world.revision == 5

    def test_older_seq_does_not_regress_a_cell(self) -> None:
        world = LocalMap()
        world.observe(observed(5, objects=[an_obstacle(7, 7)]))
        # A stale replay claims the player stood on (7, 7); the cell already
        # holds newer knowledge, so nothing about it may change.
        world.observe(observed(3, player_at=(7, 7, 0)))
        cell = world.cell(7, 7, 0)
        assert cell.obstructed
        assert not cell.visited
        assert cell.last_seen_seq == 5

    def test_older_seq_still_lands_on_never_seen_cells(self) -> None:
        world = LocalMap()
        world.observe(observed(5, player_at=(0, 0, 0)))
        world.observe(observed(3, player_at=(9, 9, 0)))
        assert world.cell(9, 9, 0).visited
        assert world.cell(9, 9, 0).last_seen_seq == 3

    def test_newer_observation_clears_an_obstruction_the_player_walked_over(self) -> None:
        world = LocalMap()
        world.observe(observed(1, objects=[an_obstacle(2, 0)]))
        world.observe(observed(2, player_at=(2, 0, 0)))
        cell = world.cell(2, 0, 0)
        assert cell.visited
        assert not cell.obstructed
        assert cell.walkable_hint


# ---------------------------------------------------------------------------
# door knowledge over time
# ---------------------------------------------------------------------------


class TestDoorKnowledge:
    def test_open_to_closed_transition_keeps_newest(self) -> None:
        world = LocalMap()
        world.observe(observed(1, objects=[a_door(3, 0, open=True)]))
        world.observe(observed(2, objects=[a_door(3, 0, open=False)]))
        door = world.cell(3, 0, 0).door
        assert door is not None
        assert door.open is False
        assert door.last_seen_seq == 2
        # Replaying the older open reading must not resurrect it.
        world.observe(observed(1, objects=[a_door(3, 0, open=True)]))
        door = world.cell(3, 0, 0).door
        assert door is not None
        assert door.open is False

    def test_absent_reading_does_not_erase_known_state(self) -> None:
        world = LocalMap()
        world.observe(observed(1, objects=[a_door(3, 0, open=False, locked=True)]))
        # A newer observation whose build could not read the lock says nothing
        # about the lock; "unreadable" must never overwrite "observed locked".
        world.observe(observed(2, objects=[a_door(3, 0, open=False, locked=None)]))
        door = world.cell(3, 0, 0).door
        assert door is not None
        assert door.locked is True
        assert door.last_seen_seq == 2

    def test_positively_read_unlock_overwrites_known_lock(self) -> None:
        world = LocalMap()
        world.observe(observed(1, objects=[a_door(3, 0, open=False, locked=True)]))
        world.observe(observed(2, objects=[a_door(3, 0, open=False, locked=False)]))
        door = world.cell(3, 0, 0).door
        assert door is not None
        assert door.locked is False

    def test_learn_door_locked_updates_a_remembered_door(self) -> None:
        world = LocalMap()
        world.observe(observed(1, objects=[a_door(3, 0, open=False, locked=None)]))
        assert world.learn_door_locked(DOOR_REF)
        door = world.cell(3, 0, 0).door
        assert door is not None
        assert door.locked is True
        assert not door.passable_hint

    def test_learn_door_barricaded_updates_a_remembered_door(self) -> None:
        world = LocalMap()
        world.observe(observed(1, objects=[a_door(3, 0, open=False)]))
        assert world.learn_door_barricaded(DOOR_REF)
        door = world.cell(3, 0, 0).door
        assert door is not None
        assert door.barricaded is True
        assert not door.passable_hint

    def test_learning_about_an_unremembered_door_is_refused(self) -> None:
        world = LocalMap()
        world.observe(observed(1))
        assert not world.learn_door_locked(f"object:{DEFAULT_SESSION}:404:0")
        assert not world.learn_door_barricaded(f"object:{DEFAULT_SESSION}:404:0")

    def test_learned_lock_survives_later_unreadable_observations(self) -> None:
        world = LocalMap()
        world.observe(observed(1, objects=[a_door(3, 0, open=False, locked=None)]))
        world.learn_door_locked(DOOR_REF)
        world.observe(observed(2, objects=[a_door(3, 0, open=False, locked=None)]))
        door = world.cell(3, 0, 0).door
        assert door is not None
        assert door.locked is True


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------


class TestBounds:
    def test_max_cells_plus_one_evicts_exactly_the_oldest(self) -> None:
        world = LocalMap(max_cells=3)
        world.observe(observed(1, player_at=(0, 0, 0)))
        world.observe(observed(2, player_at=(1, 0, 0)))
        world.observe(observed(3, player_at=(2, 0, 0)))
        assert len(world) == 3
        world.observe(observed(4, player_at=(3, 0, 0)))
        assert len(world) == 3
        # Honest forgetting: the oldest-seen cell went back to unknown.
        assert not world.cell(0, 0, 0).known
        assert world.cell(1, 0, 0).known
        assert world.cell(2, 0, 0).known
        assert world.cell(3, 0, 0).known

    def test_eviction_tie_breaks_deterministically_on_coordinates(self) -> None:
        world = LocalMap(max_cells=2)
        world.observe(observed(1, player_at=(0, 0, 0), objects=[an_obstacle(5, 5)]))
        # Both remembered cells carry seq 1; the smaller coordinate goes first.
        world.observe(observed(2, player_at=(9, 9, 0)))
        assert not world.cell(0, 0, 0).known
        assert world.cell(5, 5, 0).known
        assert world.cell(9, 9, 0).known

    def test_default_bound_holds_at_scale(self) -> None:
        world = LocalMap()
        crowd = [an_obstacle(10 + i, 20, ref_id=f"t{i}") for i in range(4095)]
        world.observe(observed(1, player_at=(0, 0, 0), objects=crowd))
        assert len(world) == 4096
        world.observe(observed(2, player_at=(5000, 5000, 0)))
        assert len(world) == 4096
        # The evicted cell is the oldest-seen with the smallest coordinates.
        assert not world.cell(0, 0, 0).known
        assert world.cell(5000, 5000, 0).known
        assert world.cell(10, 20, 0).known

    def test_max_cells_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_cells"):
            LocalMap(max_cells=0)
