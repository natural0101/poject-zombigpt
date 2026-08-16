"""The deterministic route executor, one decision at a time.

Each test drives a :class:`Journey` exactly the way the goal layer will: a
fresh observation into ``next_step``, the engine's terminal result into
``note_result``, and assertions on the frozen value that comes back. Arrival
must only ever come from an observation, every refusal must be typed, and
every bound must fire at exactly its limit.
"""

from __future__ import annotations

import uuid

import pytest

from pz_agent_core.actions.adapters.movement import MAX_ARRIVAL_RADIUS
from pz_agent_core.navigation import (
    Arrived,
    Journey,
    JourneyLimits,
    JourneyState,
    LocalMap,
    NavigationFailure,
    NavigationTarget,
    NextStep,
    Refused,
    Step,
)
from pz_agent_core.navigation.executor import THREAT_AVOID_RADIUS
from pz_agent_core.navigation.local_map import THREAT_DECAY_SEQS
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    NearbyObject,
    NearbyView,
    NearbyZombie,
    Observation,
    Position,
    ReasonCode,
)
from tests.fixtures import DEFAULT_SESSION, make_observation, make_player

# ---------------------------------------------------------------------------
# builders
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
) -> NearbyObject:
    return NearbyObject(
        ref=f"object:{DEFAULT_SESSION}:{ref_id}:0",
        kind="door",
        distance=float(max(abs(x), abs(y))),
        position=Position(x=float(x), y=float(y), z=z),
        semantics=["door", "obstacle"],
        open=open,
        locked=locked,
        barricaded=barricaded,
        orientation="north",
    )


def an_obstacle(x: int, y: int, z: int = 0) -> NearbyObject:
    return NearbyObject(
        ref=f"object:{DEFAULT_SESSION}:tree{x}x{y}:0",
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


DOOR_REF = f"object:{DEFAULT_SESSION}:9001:0"


def succeeded(action: ActionName) -> ActionResult:
    return ActionResult.succeeded(
        session_id=DEFAULT_SESSION,
        seq=0,
        command_id=str(uuid.uuid4()),
        action=action.value,
        timestamp_ms=1_700_000_000_000,
        evidence={"observed": "by the engine's own verify step"},
    )


def failed(action: ActionName, reason: ReasonCode) -> ActionResult:
    return ActionResult.failure(
        session_id=DEFAULT_SESSION,
        seq=0,
        command_id=str(uuid.uuid4()),
        action=action.value,
        timestamp_ms=1_700_000_000_000,
        reason_code=reason,
    )


def door_wall(*, gap_at: int | None = 5, door_locked: bool | None = None) -> list[NearbyObject]:
    """A wall on x=3 from y=-6..6 with a door at (3, 0) and an optional gap.

    The geometry is chosen so the costs are unambiguous: through the closed
    door the route to (6, 0) costs 6 steps plus the door toll of 2; through
    the gap at (3, 5) it costs 10; around either end of the wall it costs 14.
    The door route wins while the door is passable, the gap wins once it is
    known locked.
    """
    objects: list[NearbyObject] = [a_door(3, 0, open=False, locked=door_locked)]
    for y in range(-6, 7):
        if y == 0 or y == gap_at:
            continue
        objects.append(an_obstacle(3, y))
    return objects


def sealed_target_ring(
    *, locked: bool | None = None, barricaded: bool | None = None
) -> list[NearbyObject]:
    """The target (6, 0) walled in on all eight sides except a door at (5, 0)."""
    objects: list[NearbyObject] = [a_door(5, 0, open=False, locked=locked, barricaded=barricaded)]
    for x, y in ((5, -1), (5, 1), (6, -1), (6, 1), (7, -1), (7, 0), (7, 1)):
        objects.append(an_obstacle(x, y))
    return objects


def expect_step(value: NextStep) -> Step:
    assert isinstance(value, Step), f"expected a Step, got {value!r}"
    return value


def expect_refused(value: NextStep) -> Refused:
    assert isinstance(value, Refused), f"expected a Refused, got {value!r}"
    return value


def plane_route(step: Step) -> set[tuple[int, int, int]]:
    return set(step.route)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


class TestTheArrivalRadius:
    """The radius is the only thing that turns a position into ``Arrived``."""

    def test_a_radius_outside_the_bound_is_refused_at_construction(self) -> None:
        """An oversized radius declares arrival from an arbitrary distance.

        ``Journey._check_arrived`` compares squared plane distance against
        ``radius ** 2`` before any action is emitted, so a target built with a
        radius of a hundred is "arrived" the moment the journey starts —
        without the character having moved and without the mod ever being asked.
        The bound is borrowed from the movement adapter so the executor and the
        mod cannot disagree about what "close enough" means.

        The partial lever does not cover this direction: ``_emit_leg`` passes
        the radius into ``movement.move_to``, where the adapter validates it,
        but an arrival declared here emits no leg at all.

        Deleting the validator left the whole suite green.
        """
        with pytest.raises(ValueError, match="radius must be within"):
            NavigationTarget(5, 0, 0, radius=MAX_ARRIVAL_RADIUS + 1.0)

        with pytest.raises(ValueError, match="radius must be within"):
            NavigationTarget(5, 0, 0, radius=0.0)


class TestStraightLine:
    def test_short_journey_is_a_single_move_leg(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(5, 0, 0))
        step = expect_step(journey.next_step(observed(1)))
        assert step.request.action is ActionName.MOVEMENT_MOVE_TO
        assert step.request.args["target"] == {"x": 5, "y": 0, "z": 0}
        assert step.request.args["allow_doors"] is True
        assert step.request.args["max_distance"] <= 30
        assert step.door_ref is None
        assert journey.state is JourneyState.MOVING
        assert journey.legs_used == 1

    def test_arrival_comes_only_from_an_observation(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(5, 0, 0))
        journey.next_step(observed(1))
        journey.note_result(succeeded(ActionName.MOVEMENT_MOVE_TO))
        # The engine said the move succeeded, but the fresh observation still
        # places the character short of the radius: not arrived.
        again = journey.next_step(observed(2, player_at=(3, 0, 0)))
        assert isinstance(again, Step)
        arrived = journey.next_step(observed(3, player_at=(5, 0, 0)))
        assert isinstance(arrived, Arrived)
        assert arrived.observation_seq == 3
        assert journey.state is JourneyState.ARRIVED

    def test_terminal_outcome_is_replayed(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(5, 0, 0))
        journey.next_step(observed(1))
        arrived = journey.next_step(observed(2, player_at=(5, 0, 0)))
        assert isinstance(arrived, Arrived)
        assert journey.next_step(observed(3)) is arrived

    def test_long_route_is_split_into_legs_bounded_by_thirty(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(70, 0, 0))
        position = (0, 0, 0)
        legs: list[Step] = []
        for seq in range(1, 5):
            value = journey.next_step(observed(seq, player_at=position))
            if isinstance(value, Arrived):
                break
            step = expect_step(value)
            legs.append(step)
            span = max(abs(step.leg_target[0] - position[0]), abs(step.leg_target[1] - position[1]))
            assert span <= 30
            assert step.request.args["max_distance"] <= 30
            journey.note_result(succeeded(ActionName.MOVEMENT_MOVE_TO))
            position = step.leg_target
        assert [leg.leg_target for leg in legs] == [(30, 0, 0), (60, 0, 0), (70, 0, 0)]
        assert isinstance(journey.next_step(observed(9, player_at=(70, 0, 0))), Arrived)
        assert journey.legs_used == 3


# ---------------------------------------------------------------------------
# doors
# ---------------------------------------------------------------------------


class TestDoors:
    def test_known_closed_unlocked_door_is_walked_not_pre_opened(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(6, 0, 0))
        step = expect_step(journey.next_step(observed(1, objects=door_wall())))
        # The route runs through the door square, and the step is still a
        # plain move with allow_doors — the mod opens it mid-walk.
        assert step.request.action is ActionName.MOVEMENT_MOVE_TO
        assert step.request.args["allow_doors"] is True
        assert (3, 0, 0) in plane_route(step)
        assert step.door_ref is None

    def test_stuck_walk_retries_with_explicit_door_open(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(6, 0, 0))
        expect_step(journey.next_step(observed(1, objects=door_wall())))
        journey.note_result(failed(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_STUCK))
        # The newer observation still shows the closed door on the failed leg:
        # the retry is an explicit door.open on it, not another blind walk.
        retry = expect_step(journey.next_step(observed(2, objects=door_wall())))
        assert retry.request.action is ActionName.DOOR_OPEN
        assert retry.request.args == {"door_ref": DOOR_REF}
        assert retry.door_ref == DOOR_REF
        assert retry.leg_target == (3, 0, 0)

    def test_locked_door_open_lands_in_map_and_replans_the_corridor(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(6, 0, 0))
        expect_step(journey.next_step(observed(1, objects=door_wall())))
        journey.note_result(failed(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_STUCK))
        retry = expect_step(journey.next_step(observed(2, objects=door_wall())))
        assert retry.request.action is ActionName.DOOR_OPEN
        journey.note_result(failed(ActionName.DOOR_OPEN, ReasonCode.DOOR_LOCKED))
        # The refusal taught the map the lock; the next plan takes the gap in
        # the wall instead of the door.
        replanned = expect_step(journey.next_step(observed(3, objects=door_wall())))
        assert replanned.request.action is ActionName.MOVEMENT_MOVE_TO
        route = plane_route(replanned)
        assert (3, 0, 0) not in route
        assert (3, 5, 0) in route

    def test_locked_door_with_no_alternative_is_a_typed_refusal(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(6, 0, 0))
        refused = expect_refused(
            journey.next_step(observed(1, objects=sealed_target_ring(locked=True)))
        )
        assert refused.error.failure is NavigationFailure.DOOR_LOCKED
        assert refused.error.reason_code is ReasonCode.DOOR_LOCKED
        assert refused.error.door_ref == f"object:{DEFAULT_SESSION}:9001:0"
        assert refused.error.square == (5, 0, 0)
        assert "locked" in refused.error.message
        assert journey.state is JourneyState.REFUSED

    def test_barricaded_door_with_no_alternative_mirrors_the_refusal(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(6, 0, 0))
        refused = expect_refused(
            journey.next_step(observed(1, objects=sealed_target_ring(barricaded=True)))
        )
        assert refused.error.failure is NavigationFailure.DOOR_BARRICADED
        assert refused.error.reason_code is ReasonCode.DOOR_BARRICADED
        assert refused.error.square == (5, 0, 0)
        assert "barricaded" in refused.error.message


# ---------------------------------------------------------------------------
# unknown squares and replanning
# ---------------------------------------------------------------------------


class TestOptimism:
    def test_unknown_gap_is_planned_through_then_replanned_when_observed(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(10, 0, 0))
        first = expect_step(journey.next_step(observed(1)))
        # Nothing is known about (5, 0), and the planner walks through it.
        assert (5, 0, 0) in plane_route(first)
        journey.note_result(failed(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_STUCK))
        wall = [an_obstacle(5, y) for y in range(-2, 3)]
        replanned = expect_step(journey.next_step(observed(2, objects=wall)))
        route = plane_route(replanned)
        assert (5, 0, 0) not in route
        assert not any(square in route for square in {(5, y, 0) for y in range(-2, 3)})


# ---------------------------------------------------------------------------
# stairs
# ---------------------------------------------------------------------------


class TestStairs:
    def test_floor_change_goes_through_the_remembered_stairs(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(4, 0, 1))
        step = expect_step(journey.next_step(observed(1, objects=[some_stairs(2, 0)])))
        assert step.leg_target == (2, 0, 0)
        journey.note_result(succeeded(ActionName.MOVEMENT_MOVE_TO))
        transition = expect_step(
            journey.next_step(observed(2, player_at=(2, 0, 0), objects=[some_stairs(2, 0)]))
        )
        # The floor change is a one-step leg across the remembered stairs link.
        assert transition.leg_target == (2, 0, 1)
        assert transition.route == ((2, 0, 1),)
        journey.note_result(succeeded(ActionName.MOVEMENT_MOVE_TO))
        upstairs = expect_step(journey.next_step(observed(3, player_at=(2, 0, 1))))
        assert upstairs.leg_target == (4, 0, 1)
        assert isinstance(journey.next_step(observed(4, player_at=(4, 0, 1))), Arrived)

    def test_floor_change_without_remembered_stairs_is_refused(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(4, 0, 1))
        refused = expect_refused(journey.next_step(observed(1)))
        assert refused.error.failure is NavigationFailure.NO_ROUTE
        assert refused.error.reason_code is ReasonCode.PATH_NOT_FOUND


# ---------------------------------------------------------------------------
# bounds, each at its exact limit
# ---------------------------------------------------------------------------


class TestBounds:
    def test_node_budget_exhaustion_is_a_typed_refusal(self) -> None:
        limits = JourneyLimits(max_expanded_nodes=1)
        journey = Journey(LocalMap(), NavigationTarget(20, 0, 0), limits=limits)
        refused = expect_refused(journey.next_step(observed(1)))
        assert refused.error.failure is NavigationFailure.SEARCH_BUDGET_EXHAUSTED
        assert "the route search exhausted its budget" in refused.error.message
        # No protocol code fits: the budget ran out, nothing was proven absent.
        assert refused.error.reason_code is None

    def test_leg_budget_is_exact(self) -> None:
        limits = JourneyLimits(max_legs=2, leg_distance=5)
        journey = Journey(LocalMap(), NavigationTarget(20, 0, 0), limits=limits)
        expect_step(journey.next_step(observed(1)))
        journey.note_result(succeeded(ActionName.MOVEMENT_MOVE_TO))
        expect_step(journey.next_step(observed(2, player_at=(5, 0, 0))))
        journey.note_result(succeeded(ActionName.MOVEMENT_MOVE_TO))
        refused = expect_refused(journey.next_step(observed(3, player_at=(10, 0, 0))))
        assert refused.error.failure is NavigationFailure.LEG_BUDGET_EXHAUSTED
        assert journey.legs_used == 2

    def test_replan_budget_is_exact(self) -> None:
        limits = JourneyLimits(max_replans=1, max_consecutive_failures=5)
        journey = Journey(LocalMap(), NavigationTarget(20, 0, 0), limits=limits)
        expect_step(journey.next_step(observed(1)))
        journey.note_result(failed(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_NOT_FOUND))
        # One replan is within budget…
        expect_step(journey.next_step(observed(2)))
        journey.note_result(failed(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_NOT_FOUND))
        # …the second is past it.
        refused = expect_refused(journey.next_step(observed(3)))
        assert refused.error.failure is NavigationFailure.REPLAN_BUDGET_EXHAUSTED
        assert journey.replans_used == 2

    def test_stuck_detection_names_the_last_obstacle(self) -> None:
        limits = JourneyLimits(max_consecutive_failures=2)
        journey = Journey(LocalMap(), NavigationTarget(20, 0, 0), limits=limits)
        first = expect_step(journey.next_step(observed(1)))
        journey.note_result(failed(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_STUCK))
        expect_step(journey.next_step(observed(2)))
        journey.note_result(failed(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_STUCK))
        # Two completed legs, zero net progress: exactly the limit.
        refused = expect_refused(journey.next_step(observed(3)))
        assert refused.error.failure is NavigationFailure.STUCK
        assert refused.error.reason_code is ReasonCode.PATH_STUCK
        assert refused.error.square == first.leg_target
        assert "PATH_STUCK" in refused.error.message

    def test_legs_that_succeed_without_approaching_are_also_stuck(self) -> None:
        """The other counter, and the case the failure counter cannot see.

        The mod acks ``movement.move_to`` as SUCCEEDED when the queue entry
        completed, not when the character reached the square. So an oscillating
        route, a repeatedly re-observed obstruction or a character being shoved
        back produces a run of *successful* legs that get no closer — the
        NEVER TERMINAL family in its purest form, and invisible to
        ``_consecutive_failures``, which never leaves zero here.

        The test above drives failures; this one drives successes and moves the
        player nowhere. Deleting the ``_legs_without_progress`` conjunct left
        the whole suite green.

        Honest about what is lost: ``max_legs`` still stops the journey after
        sixty-four legs, so termination survives. What does not is the honest
        STUCK ending — the caller would be told the journey ran out of legs
        rather than that it was getting nowhere, which is a different diagnosis
        and a worse one.
        """
        limits = JourneyLimits(max_consecutive_failures=2)
        journey = Journey(LocalMap(), NavigationTarget(20, 0, 0), limits=limits)
        expect_step(journey.next_step(observed(1)))
        journey.note_result(succeeded(ActionName.MOVEMENT_MOVE_TO))
        expect_step(journey.next_step(observed(2)))
        journey.note_result(succeeded(ActionName.MOVEMENT_MOVE_TO))

        refused = expect_refused(journey.next_step(observed(3)))

        assert refused.error.failure is NavigationFailure.STUCK, (
            "two legs completed with no net progress and the journey kept walking"
        )
        assert journey.legs_used < limits.max_legs, (
            "the journey only stopped because it ran out of legs, which is a "
            "different answer from 'this is getting nowhere'"
        )

    def test_progress_resets_the_stuck_counter(self) -> None:
        limits = JourneyLimits(max_consecutive_failures=2, leg_distance=5)
        journey = Journey(LocalMap(), NavigationTarget(20, 0, 0), limits=limits)
        expect_step(journey.next_step(observed(1)))
        journey.note_result(failed(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_STUCK))
        expect_step(journey.next_step(observed(2)))
        # This leg moved the character closer, so the stuck counter resets.
        journey.note_result(succeeded(ActionName.MOVEMENT_MOVE_TO))
        expect_step(journey.next_step(observed(3, player_at=(5, 0, 0))))
        journey.note_result(failed(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_STUCK))
        value = journey.next_step(observed(4, player_at=(5, 0, 0)))
        assert isinstance(value, Step)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_map_and_observations_produce_identical_steps(self) -> None:
        def run() -> list[NextStep]:
            journey = Journey(LocalMap(), NavigationTarget(6, 0, 0), journey_id="twin")
            outputs: list[NextStep] = [journey.next_step(observed(1, objects=door_wall()))]
            journey.note_result(failed(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_STUCK))
            outputs.append(journey.next_step(observed(2, objects=door_wall())))
            journey.note_result(failed(ActionName.DOOR_OPEN, ReasonCode.DOOR_LOCKED))
            outputs.append(journey.next_step(observed(3, objects=door_wall())))
            outputs.append(journey.next_step(observed(4, player_at=(6, 0, 0))))
            return outputs

        first, second = run(), run()
        assert first == second
        assert isinstance(first[-1], Arrived)

    def test_idempotency_keys_are_deterministic_and_distinct(self) -> None:
        journey = Journey(LocalMap(), NavigationTarget(70, 0, 0), journey_id="keys")
        first = expect_step(journey.next_step(observed(1)))
        journey.note_result(succeeded(ActionName.MOVEMENT_MOVE_TO))
        second = expect_step(journey.next_step(observed(2, player_at=(30, 0, 0))))
        assert first.request.idempotency_key == "nav:keys:step1"
        assert second.request.idempotency_key == "nav:keys:step2"


# ---------------------------------------------------------------------------
# threat-aware routing: detours are preferred, walls are never invented
# ---------------------------------------------------------------------------


def a_zombie(x: float, y: float, z: int = 0, *, chasing: bool = False) -> NearbyZombie:
    return NearbyZombie(
        ref=f"zombie:{DEFAULT_SESSION}:z{int(x)}x{int(y)}",
        distance=float(max(abs(x), abs(y))),
        visible=True,
        chasing=chasing,
        position=Position(x=x, y=y, z=z),
    )


def threatened(
    seq: int,
    *,
    player_at: tuple[int, int, int] = (0, 0, 0),
    objects: list[NearbyObject] | None = None,
    zombies: list[NearbyZombie] | None = None,
) -> Observation:
    x, y, z = player_at
    return make_observation(
        seq=seq,
        player=make_player(position=Position(x=float(x), y=float(y), z=z, direction="S")),
        nearby=NearbyView(objects=list(objects or []), zombies=list(zombies or [])),
    )


def boxed_in(gap: tuple[int, int]) -> list[NearbyObject]:
    """A sealed ring of obstacles around (0, 0) at radius 2, minus *gap*."""
    ring: list[NearbyObject] = []
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            if max(abs(dx), abs(dy)) != 2 or (dx, dy) == gap:
                continue
            ring.append(an_obstacle(dx, dy))
    return ring


class TestThreatAwareRouting:
    def test_the_route_detours_around_a_remembered_zombie(self) -> None:
        """Two routes to (6, 0): the straight line brushes the sighting at
        (3, 0); the threat toll (8.0 per tainted square, several tainted)
        makes the clean detour past the avoid radius strictly cheaper."""
        journey = Journey(LocalMap(), NavigationTarget(6, 0, 0), journey_id="detour")
        step = expect_step(journey.next_step(threatened(1, zombies=[a_zombie(3.5, 0.5)])))
        assert all(
            max(abs(square[0] - 3), abs(square[1] - 0)) > THREAT_AVOID_RADIUS
            for square in step.route
            if square != (6, 0, 0)
        ), f"route {step.route} brushes the sighting at (3, 0)"
        # The target itself sits outside the tainted neighbourhood too.
        assert max(abs(6 - 3), 0) > THREAT_AVOID_RADIUS

    def test_without_avoid_threats_the_short_way_wins(self) -> None:
        """The same world with the knob off: the straight line through the
        sighting's neighbourhood is the cheapest route again."""
        journey = Journey(
            LocalMap(),
            NavigationTarget(6, 0, 0),
            journey_id="straight",
            limits=JourneyLimits(avoid_threats=False),
        )
        step = expect_step(journey.next_step(threatened(1, zombies=[a_zombie(3.5, 0.5)])))
        assert step.route == tuple((x, 0, 0) for x in range(1, 7))

    def test_a_chasing_square_is_least_preferred_but_never_a_wall(self) -> None:
        """Cornered: a sealed obstacle ring whose only gap holds a chasing
        sighting. The chaser's square costs a lot (64.0) but not infinity,
        so the journey still finds the least-bad way out through it instead
        of refusing NO_ROUTE — being surrounded must not read as walls."""
        journey = Journey(LocalMap(), NavigationTarget(6, 0, 0), journey_id="cornered")
        world = threatened(
            1,
            objects=boxed_in(gap=(2, 0)),
            zombies=[a_zombie(2.5, 0.5, chasing=True)],
        )
        step = expect_step(journey.next_step(world))
        assert (2, 0, 0) in step.route, f"route {step.route} missed the only gap"

    def test_a_decayed_sighting_stops_costing(self) -> None:
        """Past the decay horizon the straight route is cheap again: the
        toll follows the map's live sightings, not its history."""
        shared = LocalMap()
        journey = Journey(shared, NavigationTarget(6, 0, 0), journey_id="decayed")
        # Age the sighting past the horizon with empty observations, without
        # ever asking for a step (the journey would cache an outcome).
        shared.observe(threatened(1, zombies=[a_zombie(3.5, 0.5)]))
        shared.observe(threatened(2 + THREAT_DECAY_SEQS))
        step = expect_step(journey.next_step(threatened(3 + THREAT_DECAY_SEQS)))
        assert step.route == tuple((x, 0, 0) for x in range(1, 7))
