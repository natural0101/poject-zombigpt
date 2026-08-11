"""``return_home`` behind the wrapper: one journey to the remembered square.

The executor's own behaviour is ``test_navigation_executor.py``'s subject and
the wrapper's navigation promises are ``test_navigation_planner.py``'s; these
tests own what is new about the homeward kind, driven the same way — a real
:class:`~pz_agent_core.goals.GoalQueue` and a real
:class:`~pz_agent_cli.runtime.ActionChannel` behind a bound
:class:`~pz_agent_cli.navigation_planner.NavigatingPlanner`, no loop around
them:

* the target comes from the memory-port walk, not from the submission — a
  memory handed in directly and a memory hanging off the wrapped planner
  chain both serve it, and the wrapped planner is never asked;
* no home point readable is a typed ``PRECONDITION_FAILED`` whose detail *is*
  the remedy, whether no memory is wired or the memory holds no home;
* a home on a floor no remembered stairs reach is the Journey's own
  ``NO_ROUTE``, ended exactly as a navigation goal's would be;
* the final-leg/goal-seam rule, the observed arrival and the
  already-standing-there probe all behave as navigation's do, with the
  homeward probe key prefix;
* homeward journeys die with their goals and the tracked set stays bounded.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from pz_agent_cli.navigation_planner import (
    HOME_ARRIVAL_RADIUS,
    MAX_TRACKED_RETURNS,
    NO_HOME_DETAIL,
    NavigatingPlanner,
)
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
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    Observation,
    Position,
    ReasonCode,
)
from tests.fixtures import make_observation, make_player
from tests.fixtures.action_doubles import FakeClock


@dataclass
class Host:
    """The loop attributes the wrapper binds to, with no loop around them."""

    goals: GoalQueue | None
    goal_lock: threading.Lock = field(default_factory=threading.Lock)
    actions: ActionChannel | None = None


@dataclass(frozen=True)
class HomeAt:
    """A remembered square, as much of one as the wrapper reads."""

    x: int
    y: int
    z: int = 0


@dataclass
class HomeMemory:
    """A memory that answers ``home_point`` with what it was told."""

    home: HomeAt | None = None

    def home_point(self) -> HomeAt | None:
        return self.home


class SpyPlanner:
    """A goal-capable planner that records every ask and answers nothing.

    ``memory`` mirrors where the shipped assembly hangs the sidecar memory,
    so the wrapper's memory-port walk can be exercised through the chain.
    """

    def __init__(self, memory: object | None = None) -> None:
        self.memory = memory
        self.propose_calls = 0
        self.goal_calls: list[PlannerGoal] = []

    def propose(self, observation: Observation) -> ActionRequest | None:
        self.propose_calls += 1
        return None

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        self.goal_calls.append(goal)
        return None


def observed(seq: int, x: float, y: float, z: int = 0) -> Observation:
    return make_observation(
        seq=seq, player=make_player(position=Position(x=x, y=y, z=z, direction="S"))
    )


def home_goal(queue: GoalQueue, *, key: str = "home-key") -> GoalRecord:
    admission = queue.submit(GoalRequest(kind=GoalKind.RETURN_HOME, idempotency_key=key))
    assert admission.goal is not None, admission.refusal
    started = queue.activate_next()
    assert started.goal is not None, started.refusal
    return started.goal


def bound_wrapper(
    inner: SpyPlanner | None = None,
    *,
    loot_memory: object | None = None,
) -> tuple[NavigatingPlanner, GoalQueue, ActionChannel]:
    clock = FakeClock()
    queue = GoalQueue(clock=clock)
    channel = ActionChannel(clock=clock)
    wrapper = NavigatingPlanner(inner, loot_memory=loot_memory)
    wrapper.bind(Host(goals=queue, actions=channel))
    return wrapper, queue, channel


def leg_success(
    action_id: str, request: ActionRequest, *, seq: int, x: int, y: int
) -> ActionResult:
    """A terminal engine result for one channel leg, evidence and all."""
    return ActionResult.succeeded(
        session_id=request.session_id,
        seq=seq,
        command_id=action_id,
        action=request.action.value,
        timestamp_ms=1_700_000_000_000 + seq,
        evidence={"x": x, "y": y, "z": 0},
    )


class TestTheHomePointComesFromMemory:
    def test_the_happy_path_walks_home_and_succeeds_on_the_observed_arrival(self) -> None:
        """Home set → journey → observed arrival → SUCCEEDED, planner never asked."""
        spy = SpyPlanner()
        wrapper, queue, channel = bound_wrapper(spy, loot_memory=HomeMemory(HomeAt(1260, 3400)))
        record = home_goal(queue)
        goal = to_planner_goal(record)

        # Sixty squares needs two legs; the first is not the arrival, so it
        # travels the action channel exactly as a navigation leg would.
        assert wrapper.propose_for_goal(goal, observed(1, 1200.0, 3400.0)) is None
        assert wrapper.tracked_returns == 1
        taken = channel.take_next()
        assert taken is not None
        action_id, request = taken
        assert request.action is ActionName.MOVEMENT_MOVE_TO
        assert request.args["target"] == {"x": 1230, "y": 3400, "z": 0}
        channel.settle(action_id, leg_success(action_id, request, seq=1, x=1230, y=3400))

        # The final leg goes out the goal seam and carries the home square
        # and the homeward arrival radius: its observed success is home.
        final = wrapper.propose_for_goal(goal, observed(2, 1230.0, 3400.0))
        assert final is not None
        assert final.args["target"] == {"x": 1260, "y": 3400, "z": 0}
        assert final.args["radius"] == HOME_ARRIVAL_RADIUS

        # The next observation places the character at home: the journey
        # declares arrival from what was observed, and the wrapper ends the
        # goal through the queue's one evidence-carrying route.
        assert wrapper.propose_for_goal(goal, observed(3, 1260.0, 3400.0)) is None
        finished = queue.record(record.goal_id)
        assert finished is not None
        assert finished.state is GoalState.SUCCEEDED
        assert finished.reason_code is ReasonCode.POSTCONDITION_MET
        assert finished.evidence_keys, "arrival must carry the observed evidence keys"
        assert wrapper.tracked_returns == 0
        assert spy.goal_calls == [] and spy.propose_calls == 0

    def test_the_memory_is_found_through_the_wrapped_planner_chain(self) -> None:
        """The same walk the loot ports use: ``inner.memory`` serves the home."""
        spy = SpyPlanner(memory=HomeMemory(HomeAt(1205, 3400)))
        wrapper, queue, _ = bound_wrapper(spy)
        record = home_goal(queue)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        # Five squares is one final leg, so it goes out the goal seam.
        assert value is not None
        assert value.action is ActionName.MOVEMENT_MOVE_TO
        assert value.args["target"] == {"x": 1205, "y": 3400, "z": 0}
        assert spy.goal_calls == [] and spy.propose_calls == 0


class TestNoHomeIsTheTypedRefusalWithTheRemedy:
    def test_a_memory_with_no_home_fails_the_goal_with_the_remedy(self) -> None:
        wrapper, queue, channel = bound_wrapper(loot_memory=HomeMemory(None))
        record = home_goal(queue)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        assert value is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
        assert ended.detail == NO_HOME_DETAIL
        assert "pz-agent remember home" in ended.detail
        assert channel.pending_count == 0, "nothing was submitted for a goal with no target"
        assert wrapper.tracked_returns == 0

    def test_no_memory_wired_is_the_same_refusal(self) -> None:
        """A wrapper around nothing holds no home, and says exactly that."""
        wrapper, queue, _ = bound_wrapper(None)
        record = home_goal(queue)

        wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        ended = queue.record(record.goal_id)
        assert ended is not None and ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
        assert ended.detail == NO_HOME_DETAIL


class TestTheJourneyOwnsTheRouteAnswers:
    def test_a_home_on_an_unreachable_floor_is_the_executors_no_route(self) -> None:
        """One floor up with no stairs remembered anywhere: NO_ROUTE, typed."""
        wrapper, queue, _ = bound_wrapper(loot_memory=HomeMemory(HomeAt(1200, 3400, z=1)))
        record = home_goal(queue)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        assert value is None
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.PATH_NOT_FOUND
        assert "NO_ROUTE" in ended.detail
        assert wrapper.tracked_returns == 0

    def test_already_standing_at_home_probes_the_engine_not_the_queue(self) -> None:
        """No result exists to succeed with, so the fact is observed instead."""
        wrapper, queue, _ = bound_wrapper(loot_memory=HomeMemory(HomeAt(1200, 3400)))
        record = home_goal(queue)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        assert value is not None
        assert value.action is ActionName.MOVEMENT_MOVE_TO
        assert value.args["max_distance"] == 1
        assert value.idempotency_key == f"home:{record.goal_id}:arrive1"
        refreshed = queue.record(record.goal_id)
        assert refreshed is not None and refreshed.state is GoalState.ACTIVE

    def test_the_journey_dies_with_its_cancelled_goal(self) -> None:
        wrapper, queue, channel = bound_wrapper(loot_memory=HomeMemory(HomeAt(1260, 3400)))
        record = home_goal(queue)
        goal = to_planner_goal(record)
        assert wrapper.propose_for_goal(goal, observed(1, 1200.0, 3400.0)) is None
        assert wrapper.tracked_returns == 1
        assert channel.pending_count == 1

        assert queue.request_cancel(record.goal_id) is True
        queue.tick()
        # The next call — any call — prunes; nothing new is submitted.
        assert wrapper.propose(observed(2, 1210.0, 3400.0)) is None

        assert wrapper.tracked_returns == 0
        assert channel.pending_count == 1, "the admitted leg stays the engine's to finish"
        cancelled = queue.record(record.goal_id)
        assert cancelled is not None and cancelled.state is GoalState.CANCELLED

    def test_the_tracked_set_is_bounded_by_construction(self) -> None:
        wrapper, _, _ = bound_wrapper()
        assert wrapper.tracked_returns <= MAX_TRACKED_RETURNS
