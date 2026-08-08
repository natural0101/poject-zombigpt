"""The goal channel inside the running loop: queue in, planner asked, levers wired.

``pz_agent_core.goals`` proves the queue's own behaviour and
``tests/contract/test_goal_reaches_the_planner.py`` proves the core-side join;
neither ever runs the *loop*. These tests own the loop-side wiring: a real
:class:`~pz_agent_cli.runtime.SidecarLoop` over a real exchange directory (the
mod faked at the files), holding a real :class:`~pz_agent_core.goals.GoalQueue`
on the loop's own clock, with a recording goal planner where the shipped
:class:`~pz_agent_cli.autonomy.AutonomyPlanner` would stand. Every assertion is
on the queue's or the loop's own answers — nothing here reaches around the lock
or fabricates a transition.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pz_agent_cli.core_services import LoopGoalPort
from pz_agent_cli.runtime import GoalPlanner, LoopError
from pz_agent_core.actions import ActionRequest
from pz_agent_core.goals import (
    GoalAdmission,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalQueue,
    GoalRecord,
    GoalRequest,
    GoalState,
    TrainableSkill,
)
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.planner import GoalKind as PlannerGoalKind
from pz_agent_core.protocol import ActionName, ActionStatus, Observation, ReasonCode, SessionMode
from tests.fixtures import make_game, make_player
from tests.fixtures.sidecar_worlds import SidecarWorld, attached_world


@dataclass
class RecordingGoalPlanner:
    """A :class:`~pz_agent_cli.runtime.GoalPlanner` that records what it is asked.

    ``next_request`` is what :meth:`propose_for_goal` answers; ``None`` keeps
    the goal active without spending anything, which is exactly what a planner
    that found nothing suitable this tick answers.
    """

    next_request: ActionRequest | None = None
    goal_calls: list[PlannerGoal] = field(default_factory=list)
    propose_calls: int = 0

    def propose(self, observation: Observation) -> ActionRequest | None:
        self.propose_calls += 1
        return None

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        self.goal_calls.append(goal)
        return self.next_request


def goal_world(tmp_path: Path, planner: RecordingGoalPlanner | None = None) -> SidecarWorld:
    """An attached world whose loop holds a real queue on the loop's own clock."""
    world = attached_world(tmp_path)
    world.loop.goals = GoalQueue(clock=world.clock, armed=False)
    world.loop.planner = planner
    return world


def submit(world: SidecarWorld, request: GoalRequest) -> GoalRecord:
    """Admit *request* through the served port and hand back the real record."""
    admission = LoopGoalPort(loop=world.loop).submit(request)
    assert admission.refusal is None
    assert admission.goal is not None
    return admission.goal


def record_of(world: SidecarWorld, goal_id: str) -> GoalRecord:
    queue = world.loop.goals
    assert queue is not None
    record = queue.record(goal_id)
    assert record is not None, "the queue forgot an open goal"
    return record


def armed_autonomous(world: SidecarWorld) -> None:
    world.beat_game()
    outcome = world.loop.arm(SessionMode.AUTONOMOUS)
    assert outcome.armed, outcome.detail


def wait_request(world: SidecarWorld, key: str) -> ActionRequest:
    """A real, executable request: ``action.wait`` verified against world time."""
    return ActionRequest(
        action=ActionName.ACTION_WAIT,
        session_id=world.session_id,
        idempotency_key=key,
        args={"game_seconds": 1.0},
    )


def a_goal(key: str = "wire-key", satisfy_to: float | None = None) -> GoalRequest:
    return GoalRequest(
        kind=GoalKind.SATISFY_HUNGER,
        idempotency_key=key,
        params=GoalParams(satisfy_to=satisfy_to),
    )


class TestTheQueueTicksWithTheLoop:
    def test_a_pending_goal_expires_on_its_ttl_because_the_loop_ticks_the_queue(
        self, tmp_path: Path
    ) -> None:
        """Nothing asks about the goal; the loop's own tick is what ends it."""
        with goal_world(tmp_path) as world:
            record = submit(world, a_goal())
            ttl = record.budget.pending_ttl_ms

            world.clock.advance(ttl + 1)
            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.EXPIRED
            assert ended.reason_code is ReasonCode.ACTION_TIMEOUT

    def test_an_active_goal_expires_when_its_wall_clock_runs_out(self, tmp_path: Path) -> None:
        planner = RecordingGoalPlanner()
        with goal_world(tmp_path, planner) as world:
            armed_autonomous(world)
            record = submit(world, a_goal())
            world.observe()
            world.loop.tick()
            assert record_of(world, record.goal_id).state is GoalState.ACTIVE

            world.clock.advance(record.budget.max_wall_ms + 1)
            world.beat_game()
            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.EXPIRED
            assert ended.reason_code is ReasonCode.ACTION_TIMEOUT


class TestTheGoalReachesThePlanner:
    def test_the_plan_asked_for_is_the_goal_the_queue_activated(self, tmp_path: Path) -> None:
        """Kind, parameter and identity all cross the seam, none defaulted."""
        planner = RecordingGoalPlanner()
        with goal_world(tmp_path, planner) as world:
            armed_autonomous(world)
            record = submit(
                world,
                GoalRequest(
                    kind=GoalKind.TRAIN_SKILL,
                    idempotency_key="train-key",
                    params=GoalParams(skill=TrainableSkill.METALWORKING),
                ),
            )
            world.observe()

            world.loop.tick()

            queue = world.loop.goals
            assert queue is not None
            active = queue.active
            assert active is not None and active.goal_id == record.goal_id
            assert [goal.goal_id for goal in planner.goal_calls] == [record.goal_id]
            asked = planner.goal_calls[0]
            assert asked.kind is PlannerGoalKind.TRAIN_SKILL
            assert asked.skill == TrainableSkill.METALWORKING.value
            assert planner.propose_calls == 0, "the goal outranks the planner's own initiative"

    def test_assisted_mode_serves_no_goal_and_autonomy_still_proposes(self, tmp_path: Path) -> None:
        planner = RecordingGoalPlanner()
        with goal_world(tmp_path, planner) as world:
            world.beat_game()
            assert world.loop.arm(SessionMode.ASSISTED).armed is True
            record = submit(world, a_goal())
            world.observe()

            world.loop.tick()

            assert planner.goal_calls == []
            assert planner.propose_calls == 1
            queue = world.loop.goals
            assert queue is not None
            assert queue.active is None
            assert record_of(world, record.goal_id).state is GoalState.PENDING

    def test_a_planner_without_the_goal_seam_activates_nothing(self, tmp_path: Path) -> None:
        """A plain planner leaves goals pending; the TTL — not silence — ends them."""

        class PlainPlanner:
            def propose(self, observation: Observation) -> ActionRequest | None:
                return None

        with goal_world(tmp_path) as world:
            world.loop.planner = PlainPlanner()
            assert not isinstance(world.loop.planner, GoalPlanner)
            armed_autonomous(world)
            record = submit(world, a_goal())
            world.observe()

            world.loop.tick()

            queue = world.loop.goals
            assert queue is not None
            assert queue.active is None
            assert record_of(world, record.goal_id).state is GoalState.PENDING


class TestActionOutcomesSettleTheGoal:
    def test_an_observed_success_ends_the_goal_succeeded_on_the_engines_evidence(
        self, tmp_path: Path
    ) -> None:
        """A real wait, verified against a world clock the fake mod advances."""
        planner = RecordingGoalPlanner()
        with goal_world(tmp_path, planner) as world:
            planner.next_request = wait_request(world, "goal-step-1")
            armed_autonomous(world)
            record = submit(world, a_goal())
            minutes = itertools.count(21)

            def world_moves() -> None:
                world.observe(game=make_game(world_time=f"1993-07-09T14:{next(minutes):02d}:00"))

            world.observe()
            world.sleeper.while_waiting = world_moves

            outcome = world.loop.tick()

            assert [result.status for result in outcome.results] == [ActionStatus.SUCCEEDED]
            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.SUCCEEDED
            assert ended.reason_code is ReasonCode.POSTCONDITION_MET
            assert ended.evidence_keys, "a succeeded goal carries the observed evidence keys"

    def test_a_failed_step_is_charged_and_the_last_one_expires_the_goal(
        self, tmp_path: Path
    ) -> None:
        """The world stands still, the wait fails, and the one-step budget is spent."""
        planner = RecordingGoalPlanner()
        with goal_world(tmp_path, planner) as world:
            planner.next_request = wait_request(world, "goal-step-fails")
            armed_autonomous(world)
            record = submit(
                world,
                GoalRequest(
                    kind=GoalKind.SATISFY_HUNGER,
                    idempotency_key="one-step-key",
                    budget=GoalBudget(max_wall_ms=300_000, max_steps=1, pending_ttl_ms=60_000),
                ),
            )
            world.observe()

            outcome = world.loop.tick()

            assert outcome.results, "the loop dispatched nothing for the active goal"
            assert all(r.status is not ActionStatus.SUCCEEDED for r in outcome.results)
            ended = record_of(world, record.goal_id)
            assert ended.steps_used == 1
            assert ended.state is GoalState.EXPIRED
            assert ended.reason_code is ReasonCode.NO_PROGRESS


class TestTheStopLeversWin:
    def test_a_cancel_over_the_port_ends_the_active_goal_on_the_next_tick(
        self, tmp_path: Path
    ) -> None:
        planner = RecordingGoalPlanner()
        with goal_world(tmp_path, planner) as world:
            armed_autonomous(world)
            record = submit(world, a_goal())
            world.observe()
            world.loop.tick()
            assert record_of(world, record.goal_id).state is GoalState.ACTIVE
            port = LoopGoalPort(loop=world.loop)

            cancellation = port.cancel(record.goal_id)
            assert cancellation.requested is True
            world.beat_game()
            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.CANCELLED
            assert ended.reason_code is ReasonCode.CANCELLED_BY_REQUEST
            queue = world.loop.goals
            assert queue is not None and queue.active is None

    def test_a_guard_forced_disarm_ends_the_goal_in_the_queues_own_vocabulary(
        self, tmp_path: Path
    ) -> None:
        """E08-M02-T007 at the loop: the reflex guard outranks the goal."""
        planner = RecordingGoalPlanner()
        with goal_world(tmp_path, planner) as world:
            armed_autonomous(world)
            record = submit(world, a_goal())
            world.observe()
            world.loop.tick()
            assert record_of(world, record.goal_id).state is GoalState.ACTIVE

            world.beat_game()
            world.observe(player=make_player(alive=False))
            outcome = world.loop.tick()

            assert outcome.disarmed is True
            assert world.loop.armed is False
            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.CANCELLED
            assert ended.reason_code is ReasonCode.NOT_ARMED
            queue = world.loop.goals
            assert queue is not None and queue.armed is False

    def test_a_panic_stop_leaves_nothing_in_flight_at_all(self, tmp_path: Path) -> None:
        """E08-M02-T008 at the loop: no active goal, no backlog, nothing waiting."""
        planner = RecordingGoalPlanner()
        with goal_world(tmp_path, planner) as world:
            armed_autonomous(world)
            active = submit(world, a_goal("panic-key-1"))
            waiting = submit(world, a_goal("panic-key-2"))
            world.observe()
            world.loop.tick()
            assert record_of(world, active.goal_id).state is GoalState.ACTIVE

            world.panic()
            world.beat_game()
            world.loop.tick()

            queue = world.loop.goals
            assert queue is not None
            assert queue.active is None
            assert queue.pending == ()
            assert world.loop.armed is False
            for goal_id in (active.goal_id, waiting.goal_id):
                ended = record_of(world, goal_id)
                assert ended.state is GoalState.CANCELLED
                assert ended.reason_code is ReasonCode.PANIC_STOP


class TestThePortAnswersAreTheQueues:
    def test_a_resubmitted_key_is_the_same_goal_marked_duplicate(self, tmp_path: Path) -> None:
        with goal_world(tmp_path) as world:
            port = LoopGoalPort(loop=world.loop)
            first = port.submit(a_goal(satisfy_to=0.73))

            again = port.submit(a_goal(satisfy_to=0.73))

            assert first.goal is not None and again.goal is not None
            assert again.duplicate is True
            assert again.goal.goal_id == first.goal.goal_id

    def test_a_key_reused_for_different_content_is_the_queues_refusal(self, tmp_path: Path) -> None:
        with goal_world(tmp_path) as world:
            port = LoopGoalPort(loop=world.loop)
            assert port.submit(a_goal(satisfy_to=0.73)).accepted

            reused: GoalAdmission = port.submit(a_goal(satisfy_to=0.5))

            assert reused.refusal is not None
            assert reused.refusal.reason_code is ReasonCode.INVALID_ARGUMENT

    def test_status_is_the_channel_as_it_stands(self, tmp_path: Path) -> None:
        with goal_world(tmp_path) as world:
            port = LoopGoalPort(loop=world.loop)
            record = submit(world, a_goal(satisfy_to=0.73))

            status = port.status(record.goal_id)

            assert status.active is None
            assert [goal.goal_id for goal in status.pending] == [record.goal_id]
            assert status.named is not None
            assert status.named.params == GoalParams(satisfy_to=0.73)
            assert port.status("never-minted").named is None

    def test_cancelling_an_unknown_id_reports_nothing_requested(self, tmp_path: Path) -> None:
        with goal_world(tmp_path) as world:
            cancellation = LoopGoalPort(loop=world.loop).cancel("never-minted")

            assert cancellation.goal is None
            assert cancellation.requested is False

    def test_a_loop_without_a_queue_refuses_by_name(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            port = LoopGoalPort(loop=world.loop)

            with pytest.raises(LoopError, match="without a goal queue"):
                port.status()
