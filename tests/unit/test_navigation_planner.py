"""The navigating planner wrapper: ``navigate_to`` served with no planner at all.

The executor's own behaviour — routing, doors, stairs, stuck detection — is
``tests/unit/test_navigation_executor.py``'s subject, and the loop-side wiring
is ``tests/unit/test_cli_goal_wiring.py``'s. These tests own the wrapper's own
promises, driven against a real :class:`~pz_agent_core.goals.GoalQueue` and a
real :class:`~pz_agent_cli.runtime.ActionChannel` with no loop in between:

* a ``navigate_to`` goal is answered by the executor and the wrapped planner is
  never asked — not for this goal, not for its own initiative;
* every other kind passes through the wrapper untouched, and a wrapper around
  nothing proposes nothing rather than inventing;
* intermediate legs travel the action channel and their terminal results feed
  the journey, so an *observed* arrival ends the goal ``SUCCEEDED`` through the
  queue's one evidence-carrying route;
* an unroutable target ends the goal with the executor's typed reason instead
  of hanging it until a budget notices;
* journeys die with their goals and the tracked set stays bounded.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from pz_agent_cli.navigation_planner import MAX_TRACKED_JOURNEYS, NavigatingPlanner
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
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    Observation,
    Position,
    ReasonCode,
)
from tests.fixtures import DEFAULT_SESSION, make_observation, make_player
from tests.fixtures.action_doubles import FakeClock


@dataclass
class Host:
    """The loop attributes the wrapper binds to, with no loop around them."""

    goals: GoalQueue | None
    goal_lock: threading.Lock = field(default_factory=threading.Lock)
    actions: ActionChannel | None = None


class SpyPlanner:
    """A goal-capable planner that records every ask and answers a sentinel."""

    def __init__(self, next_request: ActionRequest | None = None) -> None:
        self.next_request = next_request
        self.propose_calls = 0
        self.goal_calls: list[PlannerGoal] = []

    def propose(self, observation: Observation) -> ActionRequest | None:
        self.propose_calls += 1
        return self.next_request

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        self.goal_calls.append(goal)
        return self.next_request


def observed(seq: int, x: float, y: float, z: int = 0) -> Observation:
    return make_observation(
        seq=seq, player=make_player(position=Position(x=x, y=y, z=z, direction="S"))
    )


def navigate_goal(
    queue: GoalQueue, *, x: int, y: int, z: int = 0, key: str = "nav-key"
) -> GoalRecord:
    admission = queue.submit(
        GoalRequest(
            kind=GoalKind.NAVIGATE_TO,
            idempotency_key=key,
            params=GoalParams(target_x=x, target_y=y, target_z=z),
        )
    )
    assert admission.goal is not None, admission.refusal
    started = queue.activate_next()
    assert started.goal is not None, started.refusal
    return started.goal


def bound_wrapper(
    inner: SpyPlanner | None = None,
) -> tuple[NavigatingPlanner, GoalQueue, ActionChannel]:
    clock = FakeClock()
    queue = GoalQueue(clock=clock)
    channel = ActionChannel(clock=clock)
    wrapper = NavigatingPlanner(inner)
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


class TestTheWrapperServesNavigationItself:
    def test_navigate_is_served_with_zero_wrapped_planner_calls(self) -> None:
        spy = SpyPlanner()
        wrapper, queue, _ = bound_wrapper(spy)
        record = navigate_goal(queue, x=1205, y=3400)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        assert value is not None
        assert value.action is ActionName.MOVEMENT_MOVE_TO
        assert value.args["target"] == {"x": 1205, "y": 3400, "z": 0}
        assert value.idempotency_key.startswith(f"nav:{record.goal_id}")
        assert spy.goal_calls == [] and spy.propose_calls == 0

    def test_other_kinds_delegate_untouched(self) -> None:
        # read_for_boredom, because the consume kinds no longer delegate: the
        # satisfy goals are served by the wrapper's own consume mission now,
        # and tests/unit/test_consume_mission.py owns that half of the seam.
        sentinel = ActionRequest(
            action=ActionName.ACTION_WAIT,
            session_id=DEFAULT_SESSION,
            idempotency_key="inner-1",
            args={"game_seconds": 1.0},
        )
        spy = SpyPlanner(sentinel)
        wrapper, queue, _ = bound_wrapper(spy)
        admission = queue.submit(
            GoalRequest(kind=GoalKind.READ_FOR_BOREDOM, idempotency_key="read")
        )
        assert admission.goal is not None
        goal = to_planner_goal(admission.goal)

        value = wrapper.propose_for_goal(goal, observed(1, 1200.0, 3400.0))

        assert value is sentinel
        assert spy.goal_calls == [goal], "the goal must cross unmodified"

    def test_a_wrapper_around_nothing_still_navigates_and_invents_nothing(self) -> None:
        wrapper, queue, _ = bound_wrapper(None)
        record = navigate_goal(queue, x=1205, y=3400)
        observation = observed(1, 1200.0, 3400.0)

        assert wrapper.propose(observation) is None
        value = wrapper.propose_for_goal(to_planner_goal(record), observation)

        assert value is not None and value.action is ActionName.MOVEMENT_MOVE_TO
        # And a non-navigation goal over no planner is honestly nothing.
        eaten = queue.record(record.goal_id)
        assert eaten is not None

    def test_an_unbound_wrapper_declines_rather_than_guessing(self) -> None:
        queue = GoalQueue(clock=FakeClock())
        wrapper = NavigatingPlanner(None)
        record = navigate_goal(queue, x=1205, y=3400)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        assert value is None
        refreshed = queue.record(record.goal_id)
        assert refreshed is not None and refreshed.state is GoalState.ACTIVE


class TestLegsTravelTheChannelAndArrivalEndsTheGoal:
    def test_an_intermediate_leg_is_submitted_not_returned(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = navigate_goal(queue, x=1260, y=3400)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        # Sixty squares needs two legs; the first is not the arrival, so its
        # success must not end the goal — it travels the channel instead.
        assert value is None
        assert channel.pending_count == 1
        taken = channel.take_next()
        assert taken is not None
        _, request = taken
        assert request.action is ActionName.MOVEMENT_MOVE_TO
        assert request.args["target"] == {"x": 1230, "y": 3400, "z": 0}

    def test_an_observed_arrival_succeeds_the_goal_on_the_legs_evidence(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = navigate_goal(queue, x=1260, y=3400)
        goal = to_planner_goal(record)

        assert wrapper.propose_for_goal(goal, observed(1, 1200.0, 3400.0)) is None
        taken = channel.take_next()
        assert taken is not None
        action_id, request = taken
        channel.settle(action_id, leg_success(action_id, request, seq=1, x=1230, y=3400))

        # The leg's terminal result feeds the journey; the next step is the
        # final leg, and it goes out the goal seam because its own observed
        # success would *be* the arrival.
        final = wrapper.propose_for_goal(goal, observed(2, 1230.0, 3400.0))
        assert final is not None
        assert final.args["target"] == {"x": 1260, "y": 3400, "z": 0}

        # The next observation places the character inside the radius: the
        # journey declares arrival from what was observed, and the wrapper ends
        # the goal through the queue's one evidence-carrying route.
        assert wrapper.propose_for_goal(goal, observed(3, 1260.0, 3400.0)) is None
        finished = queue.record(record.goal_id)
        assert finished is not None
        assert finished.state is GoalState.SUCCEEDED
        assert finished.reason_code is ReasonCode.POSTCONDITION_MET
        assert finished.evidence_keys, "arrival must carry the observed evidence keys"
        assert wrapper.tracked_journeys == 0

    def test_arrival_already_observed_probes_the_engine_not_the_queue(self) -> None:
        """Standing on the target already: no result exists to succeed with.

        The wrapper must not mint evidence, so it answers with a bounded move
        the engine will observe — and the loop settles the goal on that real
        result, exactly as it settles any other final leg.
        """
        wrapper, queue, _ = bound_wrapper()
        record = navigate_goal(queue, x=1200, y=3400)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        assert value is not None
        assert value.action is ActionName.MOVEMENT_MOVE_TO
        assert value.args["max_distance"] == 1
        assert value.idempotency_key == f"nav:{record.goal_id}:arrive1"
        refreshed = queue.record(record.goal_id)
        assert refreshed is not None and refreshed.state is GoalState.ACTIVE

    def test_a_full_channel_waits_instead_of_burning_journey_budget(self) -> None:
        clock = FakeClock()
        queue = GoalQueue(clock=clock)
        channel = ActionChannel(clock=clock, max_pending=1, max_remembered=2)
        channel.submit(
            ActionRequest(
                action=ActionName.ACTION_WAIT,
                session_id=DEFAULT_SESSION,
                idempotency_key="occupied",
                args={"game_seconds": 1.0},
            )
        )
        wrapper = NavigatingPlanner(None)
        wrapper.bind(Host(goals=queue, actions=channel))
        record = navigate_goal(queue, x=1260, y=3400)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        assert value is None
        assert channel.pending_count == 1, "nothing was submitted into a full channel"
        refreshed = queue.record(record.goal_id)
        assert refreshed is not None and refreshed.state is GoalState.ACTIVE


class TestRefusalsAreTypedAndJourneysAreBounded:
    def test_an_unroutable_target_ends_the_goal_with_the_typed_reason(self) -> None:
        wrapper, queue, _ = bound_wrapper()
        # One floor up with no stairs anywhere in the map: no link can exist,
        # so the executor's answer is NO_ROUTE / PATH_NOT_FOUND, not a wander.
        record = navigate_goal(queue, x=1200, y=3400, z=1)

        value = wrapper.propose_for_goal(to_planner_goal(record), observed(1, 1200.0, 3400.0))

        assert value is None
        finished = queue.record(record.goal_id)
        assert finished is not None
        assert finished.state is GoalState.FAILED
        assert finished.reason_code is ReasonCode.PATH_NOT_FOUND
        assert "NO_ROUTE" in finished.detail
        assert wrapper.tracked_journeys == 0

    def test_the_journey_dies_with_its_cancelled_goal(self) -> None:
        wrapper, queue, channel = bound_wrapper()
        record = navigate_goal(queue, x=1260, y=3400)
        goal = to_planner_goal(record)
        assert wrapper.propose_for_goal(goal, observed(1, 1200.0, 3400.0)) is None
        assert wrapper.tracked_journeys == 1
        assert channel.pending_count == 1

        assert queue.request_cancel(record.goal_id) is True
        queue.tick()
        # The next call — any call — prunes; nothing new is submitted.
        assert wrapper.propose(observed(2, 1210.0, 3400.0)) is None

        assert wrapper.tracked_journeys == 0
        assert channel.pending_count == 1, "the admitted leg stays the engine's to finish"
        cancelled = queue.record(record.goal_id)
        assert cancelled is not None and cancelled.state is GoalState.CANCELLED

    def test_the_tracked_set_is_bounded_by_construction(self) -> None:
        wrapper, _, _ = bound_wrapper()
        assert wrapper.tracked_journeys <= MAX_TRACKED_JOURNEYS

    def test_the_map_learns_from_every_observation_the_wrapper_sees(self) -> None:
        wrapper, queue, _ = bound_wrapper()
        assert wrapper.map.revision == -1

        wrapper.propose(observed(7, 1200.0, 3400.0))

        assert wrapper.map.revision == 7
        assert len(wrapper.map) >= 1
        record = navigate_goal(queue, x=1205, y=3400)
        wrapper.propose_for_goal(to_planner_goal(record), observed(8, 1200.0, 3400.0))
        assert wrapper.map.revision == 8
