"""The explore mission behind the wrapper: ``explore_area`` with no planner.

Journeys are ``test_navigation_executor.py``'s subject and the loop-side
wiring is ``test_cli_goal_wiring.py``'s; these tests own the mission's own
promises, driven the way ``test_loot_mission.py`` drives loot: a real
:class:`~pz_agent_core.goals.GoalQueue` and a real
:class:`~pz_agent_cli.runtime.ActionChannel` behind a bound
:class:`~pz_agent_cli.navigation_planner.NavigatingPlanner`, scripted
observations in, channel submissions settled by hand, every assertion on the
queue's, the channel's or the report's own answers:

* the default scope is ``radius`` — not loot's ``room`` — because the room
  the character stands in is already observed and exploring it is a no-op;
* ``scope=room`` with the room unreadable is the typed ``PRECONDITION_FAILED``
  naming ``scope=radius``, never a guess;
* frontier selection is deterministic: the same map and position name the
  same waypoints in the same order, twice over;
* a locked door sealing the only route to a frontier square is a recorded
  skip naming the door, and the sweep continues to completion;
* a waypoint the approach's own observations swept into the map is done
  without arrival;
* the mission completes on frontier exhaustion with the sealed report, ends
  ``no_progress`` after three consecutive approach failures, is bounded by
  its waypoint budget, dies with its goal, and never asks the wrapped
  planner.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pytest

from pz_agent_cli.explore_mission import (
    DEFAULT_EXPLORE_RADIUS,
    ExploreMission,
    ExploreMissionLimits,
)
from pz_agent_cli.loot_mission import ENDED_CANCELLED, ENDED_COMPLETE, ENDED_UNPINNED
from pz_agent_cli.navigation_planner import NavigatingPlanner
from pz_agent_cli.runtime import ActionChannel
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.goals import (
    GoalKind,
    GoalParams,
    GoalQueue,
    GoalRecord,
    GoalRequest,
    GoalState,
    to_planner_goal,
)
from pz_agent_core.goals.model import AreaScope
from pz_agent_core.navigation import JourneyLimits, LocalMap
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    JsonDict,
    NearbyObject,
    NearbyView,
    Observation,
    Position,
    ReasonCode,
)
from tests.fixtures import DEFAULT_SESSION, make_observation, make_player
from tests.fixtures.action_doubles import FakeClock

# --------------------------------------------------------------------------
# the world under the mission
# --------------------------------------------------------------------------


def observed(
    seq: int,
    *,
    at: tuple[int, int, int] = (0, 0, 0),
    room: str | None = None,
    building: str | None = None,
    objects: tuple[NearbyObject, ...] = (),
) -> Observation:
    x, y, z = at
    return make_observation(
        seq=seq,
        player=make_player(
            position=Position(x=float(x), y=float(y), z=z, direction="S"),
            room=room,
            building=building,
        ),
        nearby=NearbyView(objects=list(objects)),
    )


DOOR_REF = f"object:{DEFAULT_SESSION}:9001:0"


def a_door(x: int, y: int, *, locked: bool | None = None) -> NearbyObject:
    return NearbyObject(
        ref=DOOR_REF,
        kind="door",
        distance=float(max(abs(x), abs(y))),
        position=Position(x=float(x), y=float(y), z=0),
        semantics=["door", "obstacle"],
        open=False,
        locked=locked,
        orientation="north",
    )


def an_obstacle(x: int, y: int) -> NearbyObject:
    return NearbyObject(
        ref=f"object:{DEFAULT_SESSION}:tree{x}x{y}:0",
        kind="tree",
        distance=float(max(abs(x), abs(y))),
        position=Position(x=float(x), y=float(y), z=0),
        semantics=["tree", "obstacle"],
    )


def sealed_room_at_6_0(*, locked: bool) -> tuple[NearbyObject, ...]:
    """The visited square (6,0) walled in, its one unknown neighbour (7,0)
    reachable only through the door at (5,0)."""
    objects: list[NearbyObject] = [a_door(5, 0, locked=locked)]
    for x, y in (
        (5, -1),
        (5, 1),
        (6, -1),
        (6, 1),
        (7, -1),
        (7, 1),
        (8, -1),
        (8, 0),
        (8, 1),
    ):
        objects.append(an_obstacle(x, y))
    return tuple(objects)


# --------------------------------------------------------------------------
# harness
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


def bound_wrapper(
    inner: SpyPlanner | None = None,
    *,
    limits: JourneyLimits | None = None,
) -> tuple[NavigatingPlanner, GoalQueue, ActionChannel]:
    clock = FakeClock()
    queue = GoalQueue(clock=clock)
    channel = ActionChannel(clock=clock)
    wrapper = NavigatingPlanner(inner, limits=limits)
    wrapper.bind(Host(goals=queue, actions=channel))
    return wrapper, queue, channel


def explore_goal(
    queue: GoalQueue,
    *,
    scope: AreaScope | None = None,
    radius: int | None = None,
    key: str = "explore-key",
) -> GoalRecord:
    admission = queue.submit(
        GoalRequest(
            kind=GoalKind.EXPLORE_AREA,
            idempotency_key=key,
            params=GoalParams(scope=scope, radius=radius),
        )
    )
    assert admission.goal is not None, admission.refusal
    started = queue.activate_next()
    assert started.goal is not None, started.refusal
    return started.goal


def settle_success(channel: ActionChannel, *, seq: int) -> ActionRequest:
    """Settle the one waiting submission with a real succeeded engine result."""
    taken = channel.take_next()
    assert taken is not None, "expected a channel submission to settle"
    action_id, request = taken
    channel.settle(
        action_id,
        ActionResult.succeeded(
            session_id=request.session_id,
            seq=seq,
            command_id=action_id,
            action=request.action.value,
            timestamp_ms=1_700_000_000_000 + seq,
            evidence={"x": request.args["target"]["x"], "y": request.args["target"]["y"]},
        ),
    )
    return request


def settle_failure(channel: ActionChannel, *, seq: int) -> ActionRequest:
    taken = channel.take_next()
    assert taken is not None, "expected a channel submission to settle"
    action_id, request = taken
    channel.settle(
        action_id,
        ActionResult.failure(
            session_id=request.session_id,
            seq=seq,
            command_id=action_id,
            action=request.action.value,
            timestamp_ms=1_700_000_000_000 + seq,
            reason_code=ReasonCode.ACTION_TIMEOUT,
        ),
    )
    return request


def live_report(wrapper: NavigatingPlanner, goal_id: str) -> JsonDict:
    report = wrapper.explore_report(goal_id)
    assert report is not None
    return report


# --------------------------------------------------------------------------
# scope pinning and the default
# --------------------------------------------------------------------------


class TestScopePinning:
    def test_room_scope_with_no_room_is_the_typed_refusal(self) -> None:
        """Never guess: outdoors and no-reader are the same answer, and both refuse."""
        wrapper, queue, channel = bound_wrapper()
        record = explore_goal(queue, scope=AreaScope.ROOM)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, room=None))

        assert value is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
        assert "no room" in ended.detail and "scope=radius" in ended.detail
        assert channel.pending_count == 0, "nothing was submitted for an unpinnable scope"
        assert live_report(wrapper, record.goal_id)["ended"] == ENDED_UNPINNED

    def test_building_scope_with_no_building_refuses_the_same_way(self) -> None:
        wrapper, queue, _ = bound_wrapper()
        record = explore_goal(queue, scope=AreaScope.BUILDING)

        wrapper.propose_for_goal(to_planner_goal(record), observed(1, building=None))

        ended = queue.record(record.goal_id)
        assert ended is not None and ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
        assert "scope=radius" in ended.detail

    def test_the_default_scope_is_radius_not_loots_room(self) -> None:
        """A bare explore over a build that *does* report the room still sweeps.

        If the absent scope read as ROOM (loot's default), this goal would pin
        the reported room; the report proves it pinned the radius sweep
        instead, centred on where the character stood, at the default radius.
        """
        wrapper, queue, _ = bound_wrapper()
        record = explore_goal(queue)

        value = wrapper.propose_for_goal(
            to_planner_goal(record), observed(1, room="kitchen", building="apartments")
        )

        # The one known square's whole neighbourhood is within arrival reach,
        # so the sweep finishes without an action and the completion probe
        # goes out the goal seam — the no-op the radius default anticipates.
        assert value is not None
        assert value.action is ActionName.MOVEMENT_MOVE_TO
        assert value.idempotency_key == f"explore:{record.goal_id}:done1"
        report = live_report(wrapper, record.goal_id)
        assert report["scope"] == {
            "scope": "radius",
            "centre": {"x": 0, "y": 0, "z": 0},
            "radius": DEFAULT_EXPLORE_RADIUS,
        }
        assert report["ended"] == ENDED_COMPLETE
        assert report["waypoints_visited"] == 8
        assert report["skipped"] == []


# --------------------------------------------------------------------------
# the frontier
# --------------------------------------------------------------------------


class TestFrontierSelection:
    def run_first_waypoints(self) -> tuple[JsonDict, dict[str, object], JsonDict]:
        """One scripted run: pre-taught map, one journey waypoint, its report."""
        wrapper, queue, channel = bound_wrapper()
        # Teach the map a distant visited square before the goal exists —
        # the wrapper's own Planner half feeds the map on every observation.
        assert wrapper.propose(observed(1, at=(8, 0, 0))) is None
        record = explore_goal(queue)
        goal = to_planner_goal(record)

        value = wrapper.propose_for_goal(goal, observed(2, at=(0, 0, 0)))

        # The near frontier (the eight neighbours of (0,0)) is already within
        # arrival reach and resolves without an action; the first *walked*
        # waypoint is the nearest unknown neighbour of the distant square,
        # and its approach leg travels the action channel.
        assert value is None
        assert channel.pending_count == 1
        taken = channel.take_next()
        assert taken is not None
        _, request = taken
        assert request.action is ActionName.MOVEMENT_MOVE_TO
        target = dict(request.args["target"])
        report = dict(live_report(wrapper, record.goal_id))
        return report, target, report["scope"]

    def test_the_same_map_names_the_same_waypoints_in_the_same_order(self) -> None:
        """Determinism, run twice: identical worlds, identical decisions."""
        first_report, first_target, first_scope = self.run_first_waypoints()
        second_report, second_target, second_scope = self.run_first_waypoints()

        assert first_target == second_target
        assert first_scope == second_scope
        assert first_report == second_report
        # And the tie-break is the documented one: nearest first, then the
        # coordinate order — (7,-1) beats (7,0) and (7,1) at equal distance.
        assert first_target == {"x": 7, "y": -1, "z": 0}

    def test_a_waypoint_known_mid_approach_is_done_without_arrival(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        assert wrapper.propose(observed(1, at=(10, 0, 0))) is None
        record = explore_goal(queue, radius=20)
        goal = to_planner_goal(record)

        assert wrapper.propose_for_goal(goal, observed(2, at=(0, 0, 0))) is None
        first = settle_success(channel, seq=2)
        assert first.args["target"] == {"x": 9, "y": -1, "z": 0}

        # The next observation shows something standing on the waypoint: the
        # square is known now, so the visit's purpose already happened. The
        # mission counts it visited, skips nothing, and walks at the *next*
        # frontier square instead of finishing the approach.
        value = wrapper.propose_for_goal(
            goal, observed(3, at=(0, 0, 0), objects=(an_obstacle(9, -1),))
        )

        assert value is None
        assert channel.pending_count == 1
        taken = channel.take_next()
        assert taken is not None
        _, request = taken
        assert request.args["target"] == {"x": 9, "y": 0, "z": 0}
        report = live_report(wrapper, record.goal_id)
        assert report["waypoints_visited"] == 9
        assert report["skipped"] == []


# --------------------------------------------------------------------------
# doors, completion, no progress
# --------------------------------------------------------------------------


class TestSkipsAndEndings:
    def test_a_locked_door_sealing_the_route_is_a_recorded_skip_naming_it(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        # The character once stood inside a sealed room; the map remembers
        # its walls, its locked door, and one unknown square behind it.
        taught = wrapper.propose(observed(1, at=(6, 0, 0), objects=sealed_room_at_6_0(locked=True)))
        assert taught is None
        record = explore_goal(queue)
        goal = to_planner_goal(record)

        value = wrapper.propose_for_goal(goal, observed(2, at=(0, 0, 0)))

        # The near frontier resolves in place; the one walked candidate is
        # (7,0), whose only route runs through the locked door — a recorded
        # skip naming the door, not a failure. Every scope cell is then known
        # or skipped-with-reason, so completion goes out as the probe.
        assert value is not None
        assert value.idempotency_key == f"explore:{record.goal_id}:done1"
        assert channel.pending_count == 0
        report = live_report(wrapper, record.goal_id)
        assert report["skipped"] == [
            {"square": {"x": 7, "y": 0, "z": 0}, "reason": f"door {DOOR_REF} is locked"}
        ]
        assert report["ended"] == ENDED_COMPLETE

    def test_frontier_exhaustion_completes_the_goal_with_the_sealed_report(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        assert wrapper.propose(observed(1, at=(3, 3, 0))) is None
        record = explore_goal(queue, radius=3)
        goal = to_planner_goal(record)

        # One waypoint needs a walk: the distant square's nearest unknown
        # neighbour inside the sweep. Its approach travels the channel.
        assert wrapper.propose_for_goal(goal, observed(2, at=(0, 0, 0))) is None
        request = settle_success(channel, seq=2)
        assert request.args["target"] == {"x": 2, "y": 2, "z": 0}

        # Arrival is observed, the last near cells resolve in reach, and the
        # frontier is empty: the mission completes on the channel result's
        # real evidence — the queue's one route to SUCCEEDED.
        value = wrapper.propose_for_goal(goal, observed(3, at=(2, 2, 0)))

        assert value is None
        finished = queue.record(record.goal_id)
        assert finished is not None
        assert finished.state is GoalState.SUCCEEDED
        assert finished.reason_code is ReasonCode.POSTCONDITION_MET
        assert finished.evidence_keys
        report = live_report(wrapper, record.goal_id)
        assert report["ended"] == ENDED_COMPLETE
        assert report["skipped"] == []
        assert report["cells_discovered"] == 1, "the walked square is the one new cell"
        assert report["waypoints_visited"] >= 9
        assert wrapper.tracked_explores == 0, "a finished mission is sealed and dropped"

    def test_three_consecutive_approach_failures_end_the_mission_no_progress(self) -> None:
        # A one-failure journey budget makes each failed leg a refused
        # approach; the mission's own bound then ends the third streak typed.
        wrapper, queue, channel = bound_wrapper(limits=JourneyLimits(max_consecutive_failures=1))
        assert wrapper.propose(observed(1, at=(10, 0, 0))) is None
        record = explore_goal(queue, radius=20)
        goal = to_planner_goal(record)

        seq = 2
        assert wrapper.propose_for_goal(goal, observed(seq, at=(0, 0, 0))) is None
        for _ in range(4):
            settle_failure(channel, seq=seq)
            seq += 1
            value = wrapper.propose_for_goal(goal, observed(seq, at=(0, 0, 0)))
            if value is not None or channel.pending_count == 0:
                break

        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.NO_PROGRESS
        assert "3 consecutive approaches failed" in ended.detail
        report = live_report(wrapper, record.goal_id)
        assert report["ended"] == "no_progress"
        assert len(report["skipped"]) == 3

    def test_the_waypoint_budget_is_a_typed_ending_not_a_grind(self) -> None:
        """Standalone mission: budget of one waypoint, frontier remaining."""
        local_map = LocalMap()
        local_map.observe(observed(1, at=(0, 0, 0)))
        mission = ExploreMission(
            "explore-goal-1",
            local_map=local_map,
            scope=AreaScope.RADIUS,
            limits=ExploreMissionLimits(max_waypoints=1),
        )

        value = mission.next_step(observed(2, at=(0, 0, 0)))

        # The first waypoint resolves in reach and spends the whole budget;
        # frontier remains, so completion cannot honestly be claimed.
        assert value is not None and not isinstance(value, ActionRequest)
        assert mission.report["ended"] == "no_progress"
        assert "waypoint budget (1) ran out" in getattr(value, "detail", "")

    def test_limits_below_one_are_refused(self) -> None:
        with pytest.raises(ValueError, match="max_waypoints"):
            ExploreMissionLimits(max_waypoints=0)


# --------------------------------------------------------------------------
# lifecycle and the planner seam
# --------------------------------------------------------------------------


class TestLifecycleAndSeam:
    def test_the_mission_dies_with_its_cancelled_goal_and_keeps_the_report(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        assert wrapper.propose(observed(1, at=(10, 0, 0))) is None
        record = explore_goal(queue, radius=20)
        assert wrapper.propose_for_goal(to_planner_goal(record), observed(2, at=(0, 0, 0))) is None
        assert wrapper.tracked_explores == 1
        assert channel.pending_count == 1

        assert queue.request_cancel(record.goal_id) is True
        queue.tick()
        assert wrapper.propose(observed(3, at=(0, 0, 0))) is None

        assert wrapper.tracked_explores == 0, "the mission must die with its goal"
        cancelled = queue.record(record.goal_id)
        assert cancelled is not None and cancelled.state is GoalState.CANCELLED
        report = wrapper.explore_report(record.goal_id)
        assert report is not None, "the report survives the goal it describes"
        assert report["ended"] == ENDED_CANCELLED

    def test_explore_never_reaches_the_wrapped_planner(self) -> None:
        spy = SpyPlanner()
        wrapper, queue, _ = bound_wrapper(spy)
        record = explore_goal(queue)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, at=(0, 0, 0)))

        assert value is not None, "the no-op sweep still probes its completion"
        assert spy.goal_calls == [] and spy.propose_calls == 0, (
            "explore_area must never reach a plan provider"
        )
