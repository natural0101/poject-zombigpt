"""The avoid mission behind the wrapper: ``avoid_threat`` with no planner.

Threat-aware routing is ``test_navigation_executor.py``'s subject and the
censuses are the contract files'; these tests own the retreat's own promises,
driven the way ``test_explore_mission.py`` drives explore: real protocol
observations in, the mission's frozen moves out, and — for the wrapper joins —
a real :class:`~pz_agent_core.goals.GoalQueue` and a real
:class:`~pz_agent_cli.runtime.ActionChannel` behind a bound
:class:`~pz_agent_cli.navigation_planner.NavigatingPlanner`:

* the nearest remembered user safe zone within range beats open ground, and
  the real memory record shape crosses the port walk;
* the open-ground fallback is deterministic: the same threat picture picks
  the same square, twice over;
* success is only the observed postcondition — distance opened or safe zone
  with nothing chasing — never the fact of having moved;
* no threat observed means the bounded completion probe, the no-work case;
* a cornered retreat is the typed ``THREAT_INTERRUPTED`` naming the nearest
  observed threat distance;
* the wrapped planner is never asked, and the report survives the goal in
  the wrapper's bounded ledger.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from pz_agent_cli.avoid_mission import (
    ENDED_THREATENED,
    MAX_COMPLETION_PROBES,
    SAFE_DISTANCE,
    TARGET_OPEN_GROUND,
    TARGET_SAFE_ZONE,
    AvoidMission,
    RememberedSafeZone,
)
from pz_agent_cli.loot_mission import (
    ENDED_CANCELLED,
    ENDED_COMPLETE,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
)
from pz_agent_cli.navigation_planner import _MAX_KNOWN_SAFE_ZONES, NavigatingPlanner
from pz_agent_cli.runtime import ActionChannel
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.goals import (
    GoalKind,
    GoalQueue,
    GoalRecord,
    GoalRequest,
    GoalState,
    to_planner_goal,
)
from pz_agent_core.memory import CEILINGS, SafeZone, Square
from pz_agent_core.navigation import LocalMap
from pz_agent_core.planner import Goal as PlannerGoal
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
from pz_agent_core.safety.threat import DEFAULT_THREAT_CONFIG
from tests.fixtures import DEFAULT_SESSION, make_observation, make_player
from tests.fixtures.action_doubles import FakeClock

# --------------------------------------------------------------------------
# the world under the mission
# --------------------------------------------------------------------------


def a_zombie(
    x: float,
    y: float,
    *,
    distance: float,
    ref_id: str = "z1",
    chasing: bool = False,
    with_position: bool = True,
) -> NearbyZombie:
    return NearbyZombie(
        ref=f"zombie:{DEFAULT_SESSION}:{ref_id}",
        distance=distance,
        visible=True,
        chasing=chasing,
        position=Position(x=x, y=y, z=0) if with_position else None,
    )


def an_obstacle(x: int, y: int) -> NearbyObject:
    return NearbyObject(
        ref=f"object:{DEFAULT_SESSION}:tree{x}x{y}:0",
        kind="tree",
        distance=float(max(abs(x), abs(y))),
        position=Position(x=float(x), y=float(y), z=0),
        semantics=["tree", "obstacle"],
    )


def observed(
    seq: int,
    *,
    at: tuple[int, int, int] = (0, 0, 0),
    zombies: tuple[NearbyZombie, ...] = (),
    objects: tuple[NearbyObject, ...] = (),
) -> Observation:
    x, y, z = at
    return make_observation(
        seq=seq,
        player=make_player(position=Position(x=float(x), y=float(y), z=z, direction="S")),
        nearby=NearbyView(objects=list(objects), zombies=list(zombies)),
    )


def sealed_ring() -> tuple[NearbyObject, ...]:
    """A closed obstacle ring around (0, 0) at radius 2 — nowhere to run."""
    return tuple(
        an_obstacle(dx, dy)
        for dx in range(-2, 3)
        for dy in range(-2, 3)
        if max(abs(dx), abs(dy)) == 2
    )


def no_zones() -> tuple[RememberedSafeZone, ...]:
    return ()


def mission(
    *,
    zones: tuple[RememberedSafeZone, ...] = (),
    goal_id: str = "avoid-goal",
) -> AvoidMission:
    return AvoidMission(goal_id, local_map=LocalMap(), safe_zones=lambda: zones)


def expect_step(value: object) -> ActionRequest:
    assert isinstance(value, MissionStep), f"expected a MissionStep, got {value!r}"
    return value.request


def succeeded_move(request: ActionRequest) -> ActionResult:
    return ActionResult.succeeded(
        session_id=request.session_id,
        seq=99,
        command_id="cmd-1",
        action=request.action.value,
        timestamp_ms=1_700_000_000_000,
        evidence={"x": 0, "y": 0},
    )


# --------------------------------------------------------------------------
# the pinned relationships other modules own
# --------------------------------------------------------------------------


def test_safe_distance_sits_above_the_threat_ladders_reaction_range() -> None:
    """Twice ``close_distance``: a retreat that stopped at the edge of the
    band the ladder calls HIGH would be interrupted again one shamble later.
    """
    assert 2 * DEFAULT_THREAT_CONFIG.close_distance == SAFE_DISTANCE
    assert DEFAULT_THREAT_CONFIG.close_distance < SAFE_DISTANCE


def test_remembered_zone_containment_is_the_memory_stores_own_rule() -> None:
    """Same floor plus planar Chebyshev within the radius, both spellings."""
    stored = SafeZone(
        key="base",
        centre=Square(x=10, y=10, z=0),
        radius=3,
        label="base",
        recorded_at_ms=1,
    )
    remembered = RememberedSafeZone(centre=(10, 10, 0), radius=3)
    for square in ((10, 10, 0), (13, 7, 0), (14, 10, 0), (10, 10, 1), (13, 13, 0)):
        assert remembered.contains(square) == stored.contains(
            Square(x=square[0], y=square[1], z=square[2])
        ), square


def test_the_port_bound_is_the_memory_stores_own_ceiling() -> None:
    assert CEILINGS["max_safe_zones"] == _MAX_KNOWN_SAFE_ZONES


# --------------------------------------------------------------------------
# target selection
# --------------------------------------------------------------------------


class TestTargetSelection:
    def test_a_safe_zone_in_range_beats_open_ground(self) -> None:
        drive = mission(zones=(RememberedSafeZone(centre=(10, 0, 0), radius=1),))
        request = expect_step(drive.next_step(observed(1, zombies=(a_zombie(4, 1, distance=4.0),))))
        assert request.action is ActionName.MOVEMENT_MOVE_TO
        assert request.args["target"] == {"x": 10, "y": 0, "z": 0}
        assert drive.report["target_kind"] == TARGET_SAFE_ZONE

    def test_the_nearest_zone_wins_and_out_of_range_zones_do_not_count(self) -> None:
        drive = mission(
            zones=(
                RememberedSafeZone(centre=(200, 200, 0), radius=5),  # beyond 30
                RememberedSafeZone(centre=(20, 0, 0), radius=1),
                RememberedSafeZone(centre=(10, 0, 0), radius=1),
            )
        )
        request = expect_step(drive.next_step(observed(1, zombies=(a_zombie(4, 1, distance=4.0),))))
        assert request.args["target"] == {"x": 10, "y": 0, "z": 0}

    def test_the_open_ground_fallback_is_deterministic_twice_over(self) -> None:
        """No zone remembered: the ring square maximising the minimum
        distance to the observed threats, and the same picture picks the
        same square — and the same request — on a second, fresh run."""

        def run() -> tuple[ActionRequest, object]:
            drive = mission(zones=no_zones())
            request = expect_step(
                drive.next_step(observed(1, zombies=(a_zombie(4, 1, distance=4.0),)))
            )
            return request, drive.report["target_kind"]

        first, first_kind = run()
        second, second_kind = run()
        assert first == second
        assert first_kind == second_kind == TARGET_OPEN_GROUND
        # Away from the zombie standing north-east: the chosen ring square
        # sits on the far side, west of the character.
        assert first.args["target"]["x"] < 0

    def test_threats_without_positions_leave_no_honest_away(self) -> None:
        """Zombies observed, none locatable, no zone in range: there is no
        direction that is not a guess, and the answer is the typed failure.
        """
        drive = mission(zones=no_zones())
        value = drive.next_step(
            observed(1, zombies=(a_zombie(0, 0, distance=4.0, with_position=False),))
        )
        assert isinstance(value, MissionRefused)
        assert value.reason_code is ReasonCode.THREAT_INTERRUPTED
        assert "4.0" in value.detail


# --------------------------------------------------------------------------
# the observed postcondition
# --------------------------------------------------------------------------


class TestThePostcondition:
    def test_moving_is_not_success_while_the_distance_stays_shut(self) -> None:
        drive = mission(zones=no_zones())
        request = expect_step(drive.next_step(observed(1, zombies=(a_zombie(4, 1, distance=4.0),))))
        drive.note_result(succeeded_move(request))
        # The walk happened; the zombie followed. Standing at the target
        # proves nothing — the mission retires the leg and keeps going.
        target = request.args["target"]
        chased = drive.next_step(
            observed(
                2,
                at=(target["x"], target["y"], 0),
                zombies=(a_zombie(target["x"] + 4, target["y"], distance=4.0),),
            )
        )
        assert not isinstance(chased, (MissionComplete, MissionProbe))
        assert drive.ended != ENDED_COMPLETE

    def test_the_observed_distance_opening_completes_the_mission(self) -> None:
        drive = mission(zones=no_zones())
        request = expect_step(drive.next_step(observed(1, zombies=(a_zombie(4, 1, distance=4.0),))))
        drive.note_result(succeeded_move(request))
        value = drive.next_step(observed(2, zombies=(a_zombie(20, 1, distance=SAFE_DISTANCE),)))
        # A real channel action succeeded, so completion needs no probe.
        assert isinstance(value, MissionComplete)
        assert drive.ended == ENDED_COMPLETE
        assert drive.report["nearest_before"] == 4.0
        assert drive.report["nearest_after"] == SAFE_DISTANCE

    def test_a_safe_zone_with_nothing_chasing_is_safe_at_any_distance(self) -> None:
        zone = RememberedSafeZone(centre=(0, 0, 0), radius=2)
        drive = mission(zones=(zone,))
        value = drive.next_step(observed(1, zombies=(a_zombie(4, 1, distance=4.0, chasing=False),)))
        # Safe on the spot, nothing ever ran: the completion probe.
        assert isinstance(value, MissionProbe)

    def test_a_chasing_zombie_unmakes_the_safe_zone(self) -> None:
        zone = RememberedSafeZone(centre=(0, 0, 0), radius=2)
        drive = mission(zones=(zone,))
        value = drive.next_step(observed(1, zombies=(a_zombie(4, 1, distance=4.0, chasing=True),)))
        assert not isinstance(value, (MissionComplete, MissionProbe))


# --------------------------------------------------------------------------
# the no-work case and the cornered case
# --------------------------------------------------------------------------


class TestEndings:
    def test_no_threat_observed_is_the_bounded_completion_probe(self) -> None:
        drive = mission(zones=no_zones(), goal_id="calm-goal")
        value = drive.next_step(observed(1))
        assert isinstance(value, MissionProbe)
        assert value.request.idempotency_key == "avoid:calm-goal:done1"
        assert value.request.args["max_distance"] == 1
        assert drive.report["threats_at_start"] == 0

    def test_the_probe_budget_ends_in_a_typed_refusal(self) -> None:
        drive = mission(zones=no_zones())
        for _ in range(MAX_COMPLETION_PROBES):
            assert isinstance(drive.next_step(observed(1)), MissionProbe)
        value = drive.next_step(observed(2))
        assert isinstance(value, MissionRefused)
        assert value.reason_code is ReasonCode.NO_PROGRESS

    def test_cornered_is_threat_interrupted_naming_the_distance(self) -> None:
        """A sealed obstacle ring: every retreat leg refuses, the failure
        streak fills, and the ending is the retreat's own typed reason with
        the nearest observed threat distance in the detail."""
        drive = mission(zones=no_zones())
        value = drive.next_step(
            observed(
                1,
                objects=sealed_ring(),
                zombies=(a_zombie(4, 1, distance=4.0),),
            )
        )
        assert isinstance(value, MissionRefused)
        assert value.reason_code is ReasonCode.THREAT_INTERRUPTED
        assert "nearest observed threat at 4.0 tiles" in value.detail
        assert drive.ended == ENDED_THREATENED
        assert drive.report["ended"] == ENDED_THREATENED

    def test_a_missing_nearby_tier_is_not_a_safe_picture(self) -> None:
        """No nearby tier is "nothing can be observed", never "nothing is
        there". Reading it as safety turns the one tick the mod failed to
        describe the world into a completed retreat — the combat mission
        refuses that same blindness typed, and so must this one."""
        drive = mission(zones=no_zones())
        blind = make_observation(
            seq=1,
            player=make_player(position=Position(x=0.0, y=0.0, z=0, direction="S")),
            nearby=None,
        )

        value = drive.next_step(blind)

        assert isinstance(value, MissionRefused), f"blindness is not safety: {value!r}"
        assert value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert drive.ended != ENDED_COMPLETE

    def test_a_missing_nearby_tier_mid_retreat_does_not_complete_the_goal(self) -> None:
        """The same gap one leg in: a retreat under way must not be reported
        complete because the tier that proves the distance went missing."""
        drive = mission(zones=no_zones())
        request = expect_step(drive.next_step(observed(1, zombies=(a_zombie(4, 1, distance=4.0),))))
        drive.note_result(succeeded_move(request))
        blind = make_observation(
            seq=2,
            player=make_player(position=Position(x=0.0, y=0.0, z=0, direction="S")),
            nearby=None,
        )

        value = drive.next_step(blind)

        assert not isinstance(value, MissionComplete), "a blind tick is not an opened distance"
        assert drive.ended != ENDED_COMPLETE

    def test_a_finished_mission_replays_its_terminal_value(self) -> None:
        drive = mission(zones=no_zones())
        first = drive.next_step(
            observed(1, objects=sealed_ring(), zombies=(a_zombie(4, 1, distance=4.0),))
        )
        second = drive.next_step(observed(2))
        assert first is second


# --------------------------------------------------------------------------
# the wrapper joins: queue in, channel out, planner never asked
# --------------------------------------------------------------------------


@dataclass
class Host:
    """The loop attributes the wrapper binds to, with no loop around them."""

    goals: GoalQueue | None
    goal_lock: threading.Lock = field(default_factory=threading.Lock)
    actions: ActionChannel | None = None


class SpyPlanner:
    """A goal-capable planner that records every ask and answers nothing."""

    def __init__(self) -> None:
        self.propose_calls = 0
        self.goal_calls: list[PlannerGoal] = []

    def propose(self, observation: Observation) -> ActionRequest | None:
        self.propose_calls += 1
        return None

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        self.goal_calls.append(goal)
        return None


class ZoneMemory:
    """A memory answering the two ports the avoid walk reads, with real records."""

    def __init__(self, zones: tuple[SafeZone, ...]) -> None:
        self._zones = zones

    def reserves_item(self, full_type: str) -> bool:
        return False

    def safe_zones(self) -> tuple[SafeZone, ...]:
        return self._zones


def bound_wrapper(
    *, memory: object | None = None
) -> tuple[NavigatingPlanner, SpyPlanner, GoalQueue, ActionChannel]:
    clock = FakeClock()
    queue = GoalQueue(clock=clock)
    channel = ActionChannel(clock=clock)
    spy = SpyPlanner()
    wrapper = NavigatingPlanner(spy, loot_memory=memory)
    wrapper.bind(Host(goals=queue, actions=channel))
    return wrapper, spy, queue, channel


def avoid_goal(queue: GoalQueue, *, key: str = "avoid-key") -> GoalRecord:
    admission = queue.submit(GoalRequest(kind=GoalKind.AVOID_THREAT, idempotency_key=key))
    assert admission.goal is not None, admission.refusal
    started = queue.activate_next()
    assert started.goal is not None, started.refusal
    return started.goal


class TestTheWrapperJoins:
    def test_the_memory_walk_serves_the_real_safe_zone_records(self) -> None:
        """The store's own :class:`SafeZone` shape crosses the port walk and
        decides the target — nearest zone centre, not open ground."""
        base = SafeZone(
            key="base", centre=Square(x=1210, y=3400, z=0), radius=2, label="base", recorded_at_ms=1
        )
        wrapper, spy, queue, channel = bound_wrapper(memory=ZoneMemory((base,)))
        record = avoid_goal(queue)
        chased = make_observation(
            nearby=NearbyView(
                zombies=[
                    NearbyZombie(
                        ref=f"zombie:{DEFAULT_SESSION}:z1",
                        distance=4.0,
                        chasing=True,
                        position=Position(x=1204.0, y=3400.0, z=0),
                    )
                ]
            )
        )

        value = wrapper.propose_for_goal(to_planner_goal(record), chased)

        # Every retreat leg travels the action channel, never the goal seam.
        assert value is None
        assert channel.pending_count == 1
        taken = channel.take_next()
        assert taken is not None
        _, request = taken
        assert request.action is ActionName.MOVEMENT_MOVE_TO
        assert request.args["target"] == {"x": 1210, "y": 3400, "z": 0}
        assert wrapper.tracked_avoids == 1
        report = wrapper.avoid_report(record.goal_id)
        assert report is not None and report["target_kind"] == TARGET_SAFE_ZONE
        assert spy.goal_calls == [] and spy.propose_calls == 0, (
            "avoid_threat must never reach the wrapped planner"
        )

    def test_a_cornered_retreat_fails_the_goal_and_seals_the_report(self) -> None:
        wrapper, spy, queue, _channel = bound_wrapper()
        record = avoid_goal(queue)
        ring = tuple(
            NearbyObject(
                ref=f"object:{DEFAULT_SESSION}:wall{dx}x{dy}:0",
                kind="tree",
                distance=2.0,
                position=Position(x=1200.0 + dx, y=3400.0 + dy, z=0),
                semantics=["tree", "obstacle"],
            )
            for dx in range(-2, 3)
            for dy in range(-2, 3)
            if max(abs(dx), abs(dy)) == 2
        )
        cornered = make_observation(
            nearby=NearbyView(
                objects=list(ring),
                zombies=[
                    NearbyZombie(
                        ref=f"zombie:{DEFAULT_SESSION}:z1",
                        distance=4.0,
                        chasing=True,
                        position=Position(x=1204.0, y=3400.0, z=0),
                    )
                ],
            )
        )

        value = wrapper.propose_for_goal(to_planner_goal(record), cornered)

        assert value is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.THREAT_INTERRUPTED
        assert "nearest observed threat" in ended.detail
        # The drive is gone; the report survives in the bounded ledger.
        assert wrapper.tracked_avoids == 0
        report = wrapper.avoid_report(record.goal_id)
        assert report is not None and report["ended"] == ENDED_THREATENED
        assert spy.goal_calls == []

    def test_a_channel_leg_success_and_a_safe_picture_succeed_the_goal(self) -> None:
        wrapper, spy, queue, channel = bound_wrapper()
        record = avoid_goal(queue)
        chased = make_observation(
            nearby=NearbyView(
                zombies=[
                    NearbyZombie(
                        ref=f"zombie:{DEFAULT_SESSION}:z1",
                        distance=4.0,
                        chasing=True,
                        position=Position(x=1204.0, y=3400.0, z=0),
                    )
                ]
            )
        )
        assert wrapper.propose_for_goal(to_planner_goal(record), chased) is None
        taken = channel.take_next()
        assert taken is not None
        action_id, request = taken
        channel.settle(
            action_id,
            ActionResult.succeeded(
                session_id=request.session_id,
                seq=7,
                command_id=action_id,
                action=request.action.value,
                timestamp_ms=1_700_000_000_100,
                evidence={"x": request.args["target"]["x"], "y": request.args["target"]["y"]},
            ),
        )
        opened = make_observation(
            seq=2,
            nearby=NearbyView(
                zombies=[
                    NearbyZombie(
                        ref=f"zombie:{DEFAULT_SESSION}:z1",
                        distance=SAFE_DISTANCE + 3.0,
                        chasing=False,
                        position=Position(x=1220.0, y=3400.0, z=0),
                    )
                ]
            ),
        )

        value = wrapper.propose_for_goal(to_planner_goal(record), opened)

        assert value is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.SUCCEEDED
        assert ended.reason_code is ReasonCode.POSTCONDITION_MET
        report = wrapper.avoid_report(record.goal_id)
        assert report is not None and report["ended"] == ENDED_COMPLETE
        assert spy.goal_calls == [] and spy.propose_calls == 0


# --------------------------------------------------------------------------
# C1: a lost channel record must end the goal, not wedge the drive
# --------------------------------------------------------------------------


def chased_by_one(seq: int) -> Observation:
    """The same picture every tick: one zombie chasing, four tiles out."""
    return make_observation(
        seq=seq,
        nearby=NearbyView(
            zombies=[
                NearbyZombie(
                    ref=f"zombie:{DEFAULT_SESSION}:z1",
                    distance=4.0,
                    chasing=True,
                    position=Position(x=1204.0, y=3400.0, z=0),
                )
            ]
        ),
    )


def a_filler(channel: ActionChannel, index: int) -> None:
    """One unrelated action, submitted and settled, to age the record store."""
    request = ActionRequest(
        action=ActionName.ACTION_WAIT,
        session_id=DEFAULT_SESSION,
        idempotency_key=f"filler{index}",
        args={"game_seconds": 1.0},
    )
    channel.submit(request)
    taken = channel.take_next()
    assert taken is not None
    action_id, taken_request = taken
    channel.settle(
        action_id,
        ActionResult.failure(
            session_id=taken_request.session_id,
            seq=100 + index,
            command_id=action_id,
            action=taken_request.action.value,
            timestamp_ms=1_700_000_001_000 + index,
            reason_code=ReasonCode.CANCELLED_BY_REQUEST,
            message="filler",
        ),
    )


class TestALostStepRecordEndsTheRetreat:
    """A retreat whose step record the channel can no longer read.

    ``ActionChannel.status`` answers ``None`` for three truths — never minted
    here, evicted after turning terminal, minted by a previous process — and
    all three mean the same thing to a mission: what became of that step was
    never observed. The mission's own ``_pending_action`` is still set, so it
    can neither fold the outcome in nor step past it; the goal must therefore
    end, typed, on the very next tick. A retreat that silently stops
    retreating while a zombie closes is the worst ending this stack has.
    """

    def test_an_evicted_step_record_fails_the_goal_instead_of_wedging(self) -> None:
        clock = FakeClock()
        queue = GoalQueue(clock=clock)
        # Small enough that a couple of later actions push the settled retreat
        # leg out of the channel's bounded history.
        channel = ActionChannel(clock=clock, max_pending=1, max_remembered=2)
        spy = SpyPlanner()
        wrapper = NavigatingPlanner(spy)
        wrapper.bind(Host(goals=queue, actions=channel))
        record = avoid_goal(queue)
        goal = to_planner_goal(record)

        assert wrapper.propose_for_goal(goal, chased_by_one(1)) is None
        taken = channel.take_next()
        assert taken is not None
        action_id, request = taken
        channel.settle(
            action_id,
            ActionResult.succeeded(
                session_id=request.session_id,
                seq=7,
                command_id=action_id,
                action=request.action.value,
                timestamp_ms=1_700_000_000_100,
                evidence={"x": request.args["target"]["x"], "y": request.args["target"]["y"]},
            ),
        )
        for index in range(1, 5):
            a_filler(channel, index)
        assert channel.status(action_id) is None, "the leg's record is gone from the channel"
        assert wrapper.tracked_avoids == 1

        value = wrapper.propose_for_goal(goal, chased_by_one(2))

        assert value is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED, "the wedge: the goal must not sit ACTIVE"
        assert ended.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert "the step's outcome was never observed" in ended.detail
        # The drive is gone and its partial report survives, sealed.
        assert wrapper.tracked_avoids == 0
        report = wrapper.avoid_report(record.goal_id)
        assert report is not None and report["ended"] == ENDED_CANCELLED
        assert channel.pending_count == 0, "nothing new was dispatched"
        assert spy.goal_calls == [] and spy.propose_calls == 0

    def test_the_ended_goal_is_not_revived_by_further_ticks(self) -> None:
        """The audit's driver: five consecutive ticks with the zombie still on us."""
        clock = FakeClock()
        queue = GoalQueue(clock=clock)
        channel = ActionChannel(clock=clock, max_pending=1, max_remembered=2)
        wrapper = NavigatingPlanner(None)
        wrapper.bind(Host(goals=queue, actions=channel))
        record = avoid_goal(queue)
        goal = to_planner_goal(record)

        assert wrapper.propose_for_goal(goal, chased_by_one(1)) is None
        taken = channel.take_next()
        assert taken is not None
        action_id, request = taken
        channel.settle(action_id, succeeded_move(request))
        for index in range(1, 5):
            a_filler(channel, index)
        assert channel.status(action_id) is None

        for tick in range(5):
            assert wrapper.propose_for_goal(goal, chased_by_one(2 + tick)) is None
            state = queue.record(record.goal_id)
            assert state is not None
            assert state.state is GoalState.FAILED, (
                "the goal must reach a terminal state on the first tick after the loss"
            )
        assert channel.pending_count == 0
