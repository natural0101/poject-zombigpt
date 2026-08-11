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
from pz_agent_cli.navigation_planner import NavigatingPlanner
from pz_agent_cli.runtime import GoalPlanner, LoopError, SidecarLoop
from pz_agent_core.actions import ActionRequest
from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.builtin import register_builtins
from pz_agent_core.goals import (
    GOALS_FILE_NAME,
    GOALS_QUARANTINE_NAME,
    RESTART_LOST_DETAIL,
    GoalAdmission,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalQueue,
    GoalRecord,
    GoalRequest,
    GoalSnapshot,
    GoalState,
    GoalStore,
    TrainableSkill,
)
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.planner import GoalKind as PlannerGoalKind
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    ContainerKind,
    Hands,
    InventoryView,
    NearbyObject,
    NearbyView,
    NearbyZombie,
    Observation,
    Position,
    ReasonCode,
    SessionMode,
    Wound,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    main_container_ref,
    make_container,
    make_game,
    make_item,
    make_player,
    make_safety,
)
from tests.fixtures.policy_items import food_item
from tests.fixtures.sidecar_worlds import SidecarWorld, arm_for_real, attached_world


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
    arm_for_real(world, mode=SessionMode.AUTONOMOUS)


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
            arm_for_real(world, mode=SessionMode.ASSISTED)
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


def navigate_request(x: int, y: int, z: int = 0, key: str = "nav-key") -> GoalRequest:
    return GoalRequest(
        kind=GoalKind.NAVIGATE_TO,
        idempotency_key=key,
        params=GoalParams(target_x=x, target_y=y, target_z=z),
    )


def navigating_world(
    tmp_path: Path, inner: RecordingGoalPlanner | None = None
) -> tuple[SidecarWorld, NavigatingPlanner]:
    """A goal world whose planner is the navigating wrapper, bound to the loop."""
    world = goal_world(tmp_path)
    wrapper = NavigatingPlanner(inner)
    world.loop.planner = wrapper
    wrapper.bind(world.loop)
    return world, wrapper


class TestNavigateGoalsAreServedByTheExecutor:
    """The sixth kind inside the running loop: deterministic, typed, bounded.

    The fake-mod idiom of the rest of this file: a real loop over a real
    exchange directory, the wrapper standing exactly where the shipped
    assembly puts it, and every assertion on the queue's or the loop's own
    answers. The mod is not taught to walk, so the *dispatch* is what these
    tests observe — the engine's terminal refusal for an adapter this
    registry does not carry is still proof the executor's request reached the
    engine with no planner involved.
    """

    def test_an_unroutable_target_ends_the_goal_typed_with_no_planner_at_all(
        self, tmp_path: Path
    ) -> None:
        """planner=None end to end: the wrapper alone activates and serves it."""
        world, wrapper = navigating_world(tmp_path)
        with world:
            armed_autonomous(world)
            # One floor up with no stairs remembered anywhere: the executor can
            # prove NO_ROUTE from the map alone, no action needed.
            record = submit(world, navigate_request(1200, 3400, z=1))
            observation = world.observe()

            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.FAILED
            assert ended.reason_code is ReasonCode.PATH_NOT_FOUND
            assert "NO_ROUTE" in ended.detail
            # The map was fed from the loop's real observation flow, not by a
            # test reaching around it.
            assert wrapper.map.revision == observation.seq
            assert len(wrapper.map) >= 1

    def test_a_navigate_goal_reaches_the_engine_without_asking_the_planner(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world, _wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(world, navigate_request(1205, 3400))
            world.observe()

            outcome = world.loop.tick()

            assert [result.action for result in outcome.results] == ["movement.move_to"]
            assert spy.goal_calls == [], "navigate_to must never reach the wrapped planner"
            assert spy.propose_calls == 0, "the goal outranks the planner's own initiative"
            served = record_of(world, record.goal_id)
            # This registry carries no movement adapter, so the dispatch came
            # back a refusal — which the queue charges as one honest step.
            assert served.steps_used == 1

    def test_the_journey_dies_with_its_cancelled_goal(self, tmp_path: Path) -> None:
        world, wrapper = navigating_world(tmp_path)
        with world:
            armed_autonomous(world)
            # Sixty squares: the first leg is intermediate and travels the
            # loop's action channel, so cancelling mid-route leaves work the
            # wrapper must abandon rather than leak.
            record = submit(world, navigate_request(1260, 3400))
            world.observe()
            world.loop.tick()
            assert wrapper.tracked_journeys == 1
            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 1

            cancellation = LoopGoalPort(loop=world.loop).cancel(record.goal_id)
            assert cancellation.requested is True
            world.beat_game()
            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.CANCELLED
            assert wrapper.tracked_journeys == 0, "the journey must die with its goal"


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


# --------------------------------------------------------------------------
# the seventh kind: loot_area inside the running loop
# --------------------------------------------------------------------------


def loot_request(key: str = "loot-key") -> GoalRequest:
    """The bare goal, exactly as «облутай квартиру» submits it: no params."""
    return GoalRequest(kind=GoalKind.LOOT_AREA, idempotency_key=key)


def loot_world_objects(world: SidecarWorld, room: str, building: str) -> list[NearbyObject]:
    """One world container two squares from the player, inside *room*."""
    return [
        NearbyObject(
            ref=f"container:{world.session_id}:world:1202:3400:0:1:0",
            kind="container",
            distance=2.0,
            position=Position(x=1202.0, y=3400.0, z=0),
            room=room,
            building=building,
        )
    ]


class TestLootGoalsAreServedByTheMission:
    """``loot_area`` end to end through the loop, planner-less and deterministic.

    The idiom of :class:`TestNavigateGoalsAreServedByTheExecutor`: a real
    loop over a real exchange directory, the wrapper standing where the
    shipped assembly puts it, the mod faked at the files. The registry
    carries no container adapters, so an engine refusal is still the proof
    that the mission's request reached the engine with no planner involved.
    """

    def test_an_unreadable_room_ends_the_goal_typed_with_no_planner_at_all(
        self, tmp_path: Path
    ) -> None:
        """planner=None end to end: the wrapper alone activates and refuses it."""
        world, wrapper = navigating_world(tmp_path)
        with world:
            armed_autonomous(world)
            record = submit(world, loot_request())
            # The default observation carries no room reading — exactly the
            # build the scope refusal exists for.
            world.observe()

            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.FAILED
            assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
            assert "scope=radius" in ended.detail
            report = wrapper.loot_report(record.goal_id)
            assert report is not None and report["ended"] == "unpinned"

    def test_a_loot_mission_step_reaches_the_engine_without_asking_the_planner(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(world, loot_request())
            world.observe(
                player=make_player(room="kitchen", building="apartments"),
                nearby=NearbyView(objects=loot_world_objects(world, "kitchen", "apartments")),
            )

            world.loop.tick()

            # Tick one: the mission's first step — container.open_nearby —
            # was submitted into the loop's own action channel.
            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 1
            assert wrapper.tracked_missions == 1
            assert spy.goal_calls == [], "loot_area must never reach the wrapped planner"
            assert spy.propose_calls == 0, "the goal outranks the planner's own initiative"

            world.beat_game()
            outcome = world.loop.tick()

            # Tick two: the loop drained that submission through the same
            # engine every action takes; this registry carries no container
            # adapter, so the dispatch came back the engine's own refusal —
            # still proof of the join. The wrapper folded that refusal into
            # the mission in the same tick's _act, the candidate became a
            # recorded skip, and the mission's completion probe went out the
            # goal seam — the second engine dispatch below, whose refusal
            # charges the goal one honest step.
            assert [result.action for result in outcome.results] == [
                "container.open_nearby",
                "movement.move_to",
            ]
            assert spy.goal_calls == []
            served = record_of(world, record.goal_id)
            assert served.state is GoalState.ACTIVE
            assert served.steps_used == 1
            report = wrapper.loot_report(record.goal_id)
            assert report is not None
            assert len(report["containers_skipped"]) == 1

    def test_the_mission_dies_with_its_cancelled_goal_and_keeps_the_report(
        self, tmp_path: Path
    ) -> None:
        world, wrapper = navigating_world(tmp_path)
        with world:
            armed_autonomous(world)
            record = submit(world, loot_request())
            world.observe(
                player=make_player(room="kitchen", building="apartments"),
                nearby=NearbyView(objects=loot_world_objects(world, "kitchen", "apartments")),
            )
            world.loop.tick()
            assert wrapper.tracked_missions == 1

            cancellation = LoopGoalPort(loop=world.loop).cancel(record.goal_id)
            assert cancellation.requested is True
            world.beat_game()
            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.CANCELLED
            assert wrapper.tracked_missions == 0, "the mission must die with its goal"
            report = wrapper.loot_report(record.goal_id)
            assert report is not None, "the report survives the goal it describes"
            assert report["ended"] == "cancelled"


# --------------------------------------------------------------------------
# the eighth and ninth kinds: return_home and explore_area in the loop
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RememberedSquare:
    """A remembered grid square, as much of one as the wrapper reads."""

    x: int
    y: int
    z: int = 0


@dataclass(frozen=True)
class HomeMemory:
    """A memory answering ``home_point``, standing where the sidecar's would."""

    square: RememberedSquare

    def home_point(self) -> RememberedSquare:
        return self.square


def home_request(key: str = "home-key") -> GoalRequest:
    """The bare goal, exactly as «домой» submits it: no params at all."""
    return GoalRequest(kind=GoalKind.RETURN_HOME, idempotency_key=key)


def explore_request(key: str = "explore-key") -> GoalRequest:
    """The bare goal: no params, and the mission reads the radius default."""
    return GoalRequest(kind=GoalKind.EXPLORE_AREA, idempotency_key=key)


class TestReturnHomeGoalsAreServedByTheExecutor:
    """``return_home`` end to end through the loop, planner-less and typed."""

    def test_no_home_point_ends_the_goal_typed_with_the_remedy(self, tmp_path: Path) -> None:
        """planner=None, memory=None end to end: the refusal *is* the remedy."""
        world, _wrapper = navigating_world(tmp_path)
        with world:
            armed_autonomous(world)
            record = submit(world, home_request())
            world.observe()

            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.FAILED
            assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
            assert "pz-agent remember home" in ended.detail

    def test_a_home_goal_reaches_the_engine_without_asking_the_planner(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world = goal_world(tmp_path)
        wrapper = NavigatingPlanner(spy, loot_memory=HomeMemory(RememberedSquare(1205, 3400)))
        world.loop.planner = wrapper
        wrapper.bind(world.loop)
        with world:
            armed_autonomous(world)
            record = submit(world, home_request())
            world.observe()

            outcome = world.loop.tick()

            # Five squares is one final leg out the goal seam; this registry
            # carries no movement adapter, so the dispatch came back the
            # engine's own refusal — still proof of the join, charged as one
            # honest step against the goal.
            assert [result.action for result in outcome.results] == ["movement.move_to"]
            assert spy.goal_calls == [], "return_home must never reach the wrapped planner"
            assert spy.propose_calls == 0, "the goal outranks the planner's own initiative"
            served = record_of(world, record.goal_id)
            assert served.steps_used == 1


class TestExploreGoalsAreServedByTheMission:
    """``explore_area`` end to end through the loop, planner-less and bounded."""

    def test_an_explore_goal_probes_the_engine_without_asking_the_planner(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(world, explore_request())
            world.observe()

            outcome = world.loop.tick()

            # The default radius sweep around the one observed square
            # resolves within arrival reach, so the mission's completion
            # probe goes out the goal seam; the engine's refusal for an
            # adapter this registry does not carry is still proof of the
            # join, and it charges the goal one honest step.
            assert [result.action for result in outcome.results] == ["movement.move_to"]
            assert spy.goal_calls == [], "explore_area must never reach the wrapped planner"
            assert spy.propose_calls == 0, "the goal outranks the planner's own initiative"
            served = record_of(world, record.goal_id)
            assert served.steps_used == 1
            report = wrapper.explore_report(record.goal_id)
            assert report is not None
            assert report["scope"]["scope"] == "radius"

    def test_the_mission_dies_with_its_cancelled_goal_and_keeps_the_report(
        self, tmp_path: Path
    ) -> None:
        world, wrapper = navigating_world(tmp_path)
        with world:
            armed_autonomous(world)
            record = submit(world, explore_request())
            world.observe()
            world.loop.tick()
            assert wrapper.tracked_explores <= 1

            cancellation = LoopGoalPort(loop=world.loop).cancel(record.goal_id)
            assert cancellation.requested is True
            world.beat_game()
            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.CANCELLED
            assert wrapper.tracked_explores == 0, "the mission must die with its goal"
            report = wrapper.explore_report(record.goal_id)
            assert report is not None, "the report survives the goal it describes"


# --------------------------------------------------------------------------
# the founding kinds rerouted: satisfy_hunger and satisfy_thirst in the loop
# --------------------------------------------------------------------------


def satisfy_request(key: str = "hunger-key") -> GoalRequest:
    """The bare goal, exactly as «я голоден» submits it: no params at all."""
    return GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key=key)


def hungry_inventory() -> InventoryView:
    """One safe tin in the main inventory, main container described."""
    main = make_container(
        main_container_ref(), ContainerKind.PLAYER_MAIN, capacity=20.0, used_capacity=3.0
    )
    return InventoryView(containers=[main], items=[food_item("beans")])


class TestSatisfyGoalsAreServedByTheConsumeMission:
    """``satisfy_hunger`` end to end through the loop, planner-less and typed.

    The idiom of :class:`TestLootGoalsAreServedByTheMission`: a real loop over
    a real exchange directory, the wrapper standing where the shipped assembly
    puts it, the mod faked at the files. The registry carries no consume
    adapter, so an engine refusal is still the proof that the mission's
    request reached the engine with no planner involved.
    """

    def test_an_unreported_stat_ends_the_goal_typed_with_no_planner_at_all(
        self, tmp_path: Path
    ) -> None:
        """planner=None end to end: the wrapper alone activates and refuses it."""
        world, wrapper = navigating_world(tmp_path)
        with world:
            armed_autonomous(world)
            record = submit(world, satisfy_request())
            # A null hunger reading is "this build does not report it", which
            # must never be read as "the character is not hungry".
            world.observe(player=make_player(stats={"hunger": None}))

            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.FAILED
            assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
            assert "does not report hunger" in ended.detail
            report = wrapper.consume_report(record.goal_id)
            assert report is not None and report["ended"] == "unreported"

    def test_a_consume_step_reaches_the_engine_without_asking_the_planner(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(world, satisfy_request())

            def hungry_world() -> None:
                world.observe(
                    player=make_player(stats={"hunger": 0.6}),
                    inventory=hungry_inventory(),
                )

            hungry_world()
            world.loop.tick()

            # Tick one: the mission's step — the ``consume.eat`` the food
            # policy chose — was submitted into the loop's own action channel.
            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 1
            assert wrapper.tracked_consumes == 1
            assert spy.goal_calls == [], "satisfy_hunger must never reach the wrapped planner"
            assert spy.propose_calls == 0, "the goal outranks the planner's own initiative"

            world.beat_game()
            hungry_world()
            world.loop.tick()

            # Tick two: the loop drained that submission through the same
            # engine every action takes; this registry carries no consume
            # adapter, so the dispatch came back the engine's own refusal —
            # still proof of the join. The wrapper folded that refusal into
            # the mission in the same tick's _act, the tin became a recorded
            # skip, no other candidate is reachable, and the mission ended
            # the goal through the queue with its typed reason.
            assert spy.goal_calls == []
            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.FAILED
            assert ended.reason_code is ReasonCode.NO_SAFE_FOOD
            assert "reachable containers" in ended.detail
            report = wrapper.consume_report(record.goal_id)
            assert report is not None
            assert len(report["skipped"]) == 1


# --------------------------------------------------------------------------
# the care kinds: treat_wounds, rest_until and sleep_until_rested in the loop
# --------------------------------------------------------------------------


def bandaged_inventory() -> InventoryView:
    """One sterile dressing in the described main inventory."""
    main = make_container(
        main_container_ref(), ContainerKind.PLAYER_MAIN, capacity=20.0, used_capacity=3.0
    )
    wrap = make_item(
        f"item:{DEFAULT_SESSION}:player-main:wrap:0",
        main_container_ref(),
        full_type="Base.Bandage",
        display_name="Bandage",
        category="FirstAid",
        weight=0.1,
    )
    return InventoryView(containers=[main], items=[wrap])


def a_bleeding_head() -> list[Wound]:
    return [
        Wound(
            ref=f"wound:{DEFAULT_SESSION}:Head",
            kind="scratch",
            severity=0.5,
            bleeding=True,
        )
    ]


class TestCareGoalsAreServedByTheMissions:
    """The three care kinds end to end through the loop, spy planner never asked.

    The idiom of :class:`TestSatisfyGoalsAreServedByTheConsumeMission`: a real
    loop over a real exchange directory, the wrapper standing where the
    shipped assembly puts it, the mod faked at the files. The registry
    carries no medical or survival adapter, so the engine's own
    ``CAPABILITY_UNAVAILABLE`` refusal is still the proof that the mission's
    request reached the engine with no planner involved.
    """

    def test_a_bandage_step_reaches_the_engine_without_asking_the_planner(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(
                world, GoalRequest(kind=GoalKind.TREAT_WOUNDS, idempotency_key="treat-key")
            )

            def hurt_world() -> None:
                world.observe(
                    player=make_player(wounds=a_bleeding_head()),
                    inventory=bandaged_inventory(),
                )

            hurt_world()
            world.loop.tick()

            # Tick one: the mission's step — the ``medical.bandage`` the
            # triage chose — was submitted into the loop's action channel.
            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 1
            assert wrapper.tracked_cares == 1
            assert spy.goal_calls == [], "treat_wounds must never reach the wrapped planner"
            assert spy.propose_calls == 0, "the goal outranks the planner's own initiative"

            world.beat_game()
            hurt_world()
            outcome = world.loop.tick()

            # Tick two: the loop drained that submission through the same
            # engine every action takes; this registry carries no medical
            # adapter, so the dispatch came back the engine's refusal — still
            # proof of the join. The wound still bleeds, so the mission asked
            # again inside its bounded failure streak; the goal stays active.
            assert "medical.bandage" in [result.action for result in outcome.results]
            assert spy.goal_calls == []
            assert channel.pending_count == 1
            assert record_of(world, record.goal_id).state is GoalState.ACTIVE

    def test_a_rest_step_reaches_the_engine_and_a_refusal_rides_through_typed(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(
                world,
                GoalRequest(
                    kind=GoalKind.REST_UNTIL,
                    idempotency_key="rest-key",
                    params=GoalParams(target_endurance=0.8),
                ),
            )
            world.observe(player=make_player(stats={"endurance": 0.3}))

            world.loop.tick()

            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 1
            assert wrapper.tracked_cares == 1
            assert spy.goal_calls == [], "rest_until must never reach the wrapped planner"
            assert spy.propose_calls == 0

            world.beat_game()
            world.observe(player=make_player(stats={"endurance": 0.3}))
            outcome = world.loop.tick()

            # Tick two: the engine's refusal for the adapter this registry
            # does not carry rode to the goal typed and unchanged — the care
            # missions never retry a refused survival action.
            assert "survival.rest" in [result.action for result in outcome.results]
            assert spy.goal_calls == []
            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.FAILED
            assert ended.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
            assert "survival.rest refused" in ended.detail
            report = wrapper.care_report(record.goal_id)
            assert report is not None and report["ended"] == "refused"

    def test_a_sleep_step_reaches_the_engine_without_asking_the_planner(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(
                world,
                GoalRequest(
                    kind=GoalKind.SLEEP_UNTIL_RESTED,
                    idempotency_key="sleep-key",
                    params=GoalParams(hours=6),
                ),
            )
            bed = NearbyObject(
                ref=f"object:{DEFAULT_SESSION}:7001:0",
                kind="bed",
                distance=1.0,
                position=Position(x=1201.0, y=3400.0, z=0),
                semantics=["bed"],
            )

            world.observe(nearby=NearbyView(objects=[bed]))
            world.loop.tick()

            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 1
            assert wrapper.tracked_cares == 1
            assert spy.goal_calls == [], "sleep_until_rested must never reach the wrapped planner"
            assert spy.propose_calls == 0

            world.beat_game()
            world.observe(nearby=NearbyView(objects=[bed]))
            outcome = world.loop.tick()

            # Tick two: the one sleep this goal will ever send was dispatched
            # and refused by the adapterless engine; the refusal rode through
            # typed, never retried — the same structure that keeps a real
            # danger refusal from being slept into twice.
            assert "survival.sleep" in [result.action for result in outcome.results]
            assert spy.goal_calls == []
            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.FAILED
            assert ended.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
            assert "survival.sleep refused" in ended.detail
            report = wrapper.care_report(record.goal_id)
            assert report is not None and report["ended"] == "refused"
            assert wrapper.tracked_cares == 0, "the mission dies with its goal"


# --------------------------------------------------------------------------
# the retreat kind: avoid_threat in the loop
# --------------------------------------------------------------------------


def a_looming_zombie() -> NearbyZombie:
    """Four tiles east, seen, not yet chasing — inside the retreat's safe
    distance but below the reflex guard's block rung.

    Deliberately not chasing: a chaser at four tiles assesses HIGH, and at
    HIGH the loop's reflex gate starts nothing new — that band belongs to
    the guard, and the retreat goal serves the band below it, where the
    threat is real but the loop is still allowed to begin work.
    """
    return NearbyZombie(
        ref=f"zombie:{DEFAULT_SESSION}:z1",
        distance=4.0,
        visible=True,
        chasing=False,
        position=Position(x=1204.0, y=3400.0, z=0),
    )


class TestAvoidGoalsAreServedByTheMission:
    """The thirteenth kind end to end through the loop, spy planner never asked.

    The idiom of :class:`TestCareGoalsAreServedByTheMissions`: a real loop
    over a real exchange directory, the wrapper standing where the shipped
    assembly puts it, the mod faked at the files. The registry carries no
    movement adapter, so the engine's own refusal is still the proof that
    the mission's retreat leg reached the engine with no planner involved.
    """

    def test_a_retreat_step_reaches_the_engine_without_asking_the_planner(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(
                world, GoalRequest(kind=GoalKind.AVOID_THREAT, idempotency_key="avoid-key")
            )

            world.observe(nearby=NearbyView(zombies=[a_looming_zombie()]))
            world.loop.tick()

            # Tick one: the retreat leg — a move away from the observed
            # zombie — was submitted into the loop's action channel, never
            # out the goal seam: arriving at one waypoint is not "safe".
            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 1
            assert wrapper.tracked_avoids == 1
            assert spy.goal_calls == [], "avoid_threat must never reach the wrapped planner"
            assert spy.propose_calls == 0, "the goal outranks the planner's own initiative"

            world.beat_game()
            world.observe(nearby=NearbyView(zombies=[a_looming_zombie()]))
            outcome = world.loop.tick()

            # Tick two: the loop drained the submission through the same
            # engine every action takes; the refusal for the adapter this
            # registry does not carry is still proof of the join, one failed
            # leg inside the mission's bounded streak; the goal stays active.
            assert "movement.move_to" in [result.action for result in outcome.results]
            assert spy.goal_calls == []
            assert record_of(world, record.goal_id).state is GoalState.ACTIVE

    def test_a_threatless_retreat_probes_out_the_goal_seam(self, tmp_path: Path) -> None:
        """No threat observed at activation: the no-work completion probe.

        The probe travels the goal seam — it is the one request whose own
        observed result may settle the goal — so the loop dispatches it as
        the goal's step and charges the queue one honest step for it.
        """
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(
                world, GoalRequest(kind=GoalKind.AVOID_THREAT, idempotency_key="calm-key")
            )
            world.observe()

            outcome = world.loop.tick()

            assert [result.action for result in outcome.results] == ["movement.move_to"]
            assert spy.goal_calls == [] and spy.propose_calls == 0
            served = record_of(world, record.goal_id)
            assert served.steps_used == 1
            report = wrapper.avoid_report(record.goal_id)
            assert report is not None and report["threats_at_start"] == 0


# --------------------------------------------------------------------------
# the combat kind: engage_single_zombie in the loop
# --------------------------------------------------------------------------


def a_standing_zombie(distance: float = 3.0) -> NearbyZombie:
    """Three tiles out, seen, not chasing — engageable, below the guard's
    block rung (a visible non-chaser inside the reaction band assesses LOW),
    so the loop is allowed to begin the user-ordered work."""
    return NearbyZombie(
        ref=f"zombie:{DEFAULT_SESSION}:z1:0",
        distance=distance,
        visible=True,
        chasing=False,
        position=Position(x=1200.0 + distance, y=3400.0, z=0),
        state="standing",
    )


def an_armed_player() -> tuple[object, InventoryView]:
    """A healthy player holding a sound bat the observation can read."""
    bat_ref = f"item:{DEFAULT_SESSION}:player-main:bat1:0"
    bat = make_item(
        bat_ref,
        main_container_ref(),
        full_type="Base.BaseballBat",
        display_name="Baseball Bat",
        category="Weapon",
        extra={"weapon": {"condition": 9, "condition_max": 10}},
    )
    inventory = InventoryView(
        containers=[make_container(main_container_ref(), ContainerKind.PLAYER_MAIN)],
        items=[bat],
    )
    return make_player(hands=Hands(primary=bat_ref)), inventory


class TestCombatGoalsAreServedByTheMission:
    """The fourteenth kind end to end through the loop, spy planner never asked.

    The avoid class's idiom: a real loop over a real exchange directory, the
    wrapper standing where the shipped assembly puts it, the mod faked at
    the files. The registry carries no combat adapter, so the engine's own
    refusal is still the proof that the mission's window reached the engine
    with no planner involved — and the safety half of the pin: because each
    window is one channel command drained per tick *after* the guard runs,
    the stop levers get their turn between windows structurally.
    """

    def test_a_combat_step_reaches_the_engine_without_asking_the_planner(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(
                world,
                GoalRequest(kind=GoalKind.ENGAGE_SINGLE_ZOMBIE, idempotency_key="combat-key"),
            )
            player, inventory = an_armed_player()
            world.observe(
                player=player,
                inventory=inventory,
                nearby=NearbyView(zombies=[a_standing_zombie()]),
            )
            world.loop.tick()

            # Tick one: the mission's first window — engage, because the
            # target stands past contact range — was submitted into the
            # loop's action channel, never out the goal seam: a swing's own
            # success is not the goal's postcondition.
            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 1
            assert wrapper.tracked_combats == 1
            assert spy.goal_calls == [], "engage_single_zombie must never reach the wrapped planner"
            assert spy.propose_calls == 0, "the goal outranks the planner's own initiative"

            world.beat_game()
            world.observe(
                player=player,
                inventory=inventory,
                nearby=NearbyView(zombies=[a_standing_zombie()]),
            )
            outcome = world.loop.tick()

            # Tick two: the loop drained the submission through the same
            # engine every action takes; the refusal for the adapter this
            # registry does not carry is still proof of the join, and the
            # goal stays active while the mission's own budgets decide.
            assert "combat.engage" in [result.action for result in outcome.results]
            assert spy.goal_calls == []
            assert record_of(world, record.goal_id).state is GoalState.ACTIVE
            report = wrapper.combat_report(record.goal_id)
            # Tick two both drained window one (the engine's refusal) and —
            # the picture still approved — aimed window two into the channel:
            # one window per command, re-decided between them, never a loop.
            assert report is not None and report["windows_fought"] == 2

    def test_a_calm_world_fails_the_kill_order_typed(self, tmp_path: Path) -> None:
        """No zombie observed: the goal ends FAILED with the mission's
        reason — never a vacuous success, never a planner consulted."""
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(
                world,
                GoalRequest(kind=GoalKind.ENGAGE_SINGLE_ZOMBIE, idempotency_key="calm-kill"),
            )
            world.observe()

            world.loop.tick()

            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.FAILED
            assert ended.reason_code is ReasonCode.PRECONDITION_FAILED
            assert "no zombie is observed" in ended.detail
            assert spy.goal_calls == [] and spy.propose_calls == 0
            report = wrapper.combat_report(record.goal_id)
            assert report is not None and report["ended"] == "no_target"
            assert wrapper.tracked_combats == 0, "the mission dies with its goal"


# --------------------------------------------------------------------------
# the crafting kind: craft_item in the loop
# --------------------------------------------------------------------------

SPEAR = "Base.SpearCrude"
BRANCH = "Base.TreeBranch"
TWINE = "Base.Twine"


def a_workshop(*, materials: list[dict[str, object]] | None = None) -> InventoryView:
    """One branch carrying the readout for a recipe that makes a spear.

    Every recipe fact the policy reads rides on the observed item, because the
    mod publishes the readout and this side owns no recipe table — which is
    also why a world without one can only ever refuse.
    """
    branch = make_item(
        f"item:{DEFAULT_SESSION}:player-main:branch1:0",
        main_container_ref(),
        full_type=BRANCH,
        display_name="Tree Branch",
        category="Item",
        extra={
            "crafting": {
                "recipe_count": 1,
                "known_recipe_count": 1,
                "recipes": [
                    {
                        "name": "MakeSpear",
                        "product": SPEAR,
                        "display_name": "Make Crude Spear",
                        "known": True,
                        "needs_surface": False,
                        "materials": materials
                        if materials is not None
                        else [{"full_type": BRANCH, "count": 1}],
                    }
                ],
            }
        },
    )
    return InventoryView(
        containers=[make_container(main_container_ref(), ContainerKind.PLAYER_MAIN)],
        items=[branch],
    )


def a_craft_goal(key: str = "craft-key") -> GoalRequest:
    return GoalRequest(
        kind=GoalKind.CRAFT_ITEM,
        idempotency_key=key,
        params=GoalParams(product=SPEAR),
    )


class TestCraftGoalsAreServedByTheMission:
    """The fifteenth kind end to end through the loop, spy planner never asked.

    The combat class's idiom: a real loop over a real exchange directory, the
    wrapper standing where the shipped assembly puts it, the mod faked at the
    files. The registry *does* carry the crafting adapters this wave adds, so
    what these tests observe is the whole dispatch — the mission's one bounded
    run reaching the engine through the ordinary gates, with no planner
    involved — and the second half of the wave's promise: a craft the
    character cannot afford ends the goal before any command exists to send.
    """

    def test_a_craft_step_reaches_the_engine_without_asking_the_planner(
        self, tmp_path: Path
    ) -> None:
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(world, a_craft_goal())
            world.observe(inventory=a_workshop())
            world.loop.tick()

            # Tick one: the mission's one run — the recipe the crafting policy
            # chose from the observed readout — was submitted into the loop's
            # action channel, never out the goal seam: a queued craft is not a
            # product in the bag.
            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 1
            assert wrapper.tracked_crafts == 1
            assert spy.goal_calls == [], "craft_item must never reach the wrapped planner"
            assert spy.propose_calls == 0, "the goal outranks the planner's own initiative"

            world.beat_game()
            world.observe(inventory=a_workshop())
            outcome = world.loop.tick()

            # Tick two: the loop drained the submission through the same
            # engine and the same gates every action takes. Whatever the gates
            # answer for a capability this build has not confirmed, the join
            # is what is pinned here — and the goal stays open while the
            # mission's own budgets decide.
            assert "crafting.craft" in [result.action for result in outcome.results]
            assert spy.goal_calls == []
            report = wrapper.craft_report(record.goal_id)
            assert report is not None and report["recipes_tried"] == ["MakeSpear"]
            assert report["product"] == SPEAR

    def test_a_shortfall_fails_the_goal_typed_and_sends_nobody_looting(
        self, tmp_path: Path
    ) -> None:
        """Materials missing: the goal ends FAILED with the mission's reason
        and the shortfall named — never a step out to go and find them, and
        never a planner consulted."""
        spy = RecordingGoalPlanner()
        world, wrapper = navigating_world(tmp_path, spy)
        with world:
            armed_autonomous(world)
            record = submit(world, a_craft_goal("craft-short"))
            world.observe(
                inventory=a_workshop(
                    materials=[
                        {"full_type": BRANCH, "count": 1},
                        {"full_type": TWINE, "count": 2},
                    ]
                )
            )

            world.loop.tick()

            channel = world.loop.actions
            assert channel is not None and channel.pending_count == 0
            ended = record_of(world, record.goal_id)
            assert ended.state is GoalState.FAILED
            assert ended.reason_code is ReasonCode.RECIPE_MATERIALS_MISSING
            assert TWINE in ended.detail
            assert spy.goal_calls == [] and spy.propose_calls == 0
            report = wrapper.craft_report(record.goal_id)
            assert report is not None and report["ended"] == "refused"
            assert wrapper.tracked_crafts == 0, "the mission dies with its goal"


# --------------------------------------------------------------------------
# goals survive a sidecar restart, honestly
# --------------------------------------------------------------------------


class CountingGoalStore(GoalStore):
    """The real store, counting its writes, so the cadence is pinnable."""

    def __init__(self, state_dir: Path) -> None:
        super().__init__(state_dir)
        self.saves = 0

    def save_snapshot(self, snapshot: GoalSnapshot) -> int:
        self.saves += 1
        return super().save_snapshot(snapshot)


def revenant_loop(world: SidecarWorld) -> SidecarLoop:
    """The next sidecar over the same state directory — the previous one was
    killed, not shut down, so nothing here inherits from the loop in *world*
    except what reached disk."""
    return SidecarLoop(
        layout=world.layout,
        state_dir=world.state_dir,
        registry=register_builtins(AdapterRegistry()),
        clock=world.clock,
        goals=GoalQueue(clock=world.clock, armed=False),
    )


class TestGoalsSurviveARestart:
    def test_a_killed_sidecars_goals_answer_honestly_from_the_next_one(
        self, tmp_path: Path
    ) -> None:
        """The whole restart, loop to loop: the old ACTIVE id answers
        FAILED/SESSION_TERMINATED over the same port a client polls, the
        backlog is still pending, and the old idempotency key still resolves
        to the goal it named."""
        planner = RecordingGoalPlanner()
        with goal_world(tmp_path, planner) as world:
            world.loop.adopt_goal_store(GoalStore(world.state_dir))
            armed_autonomous(world)
            running = submit(world, a_goal("restart-running"))
            waiting = submit(world, a_goal("restart-waiting", satisfy_to=0.5))
            world.observe()
            world.loop.tick()
            assert record_of(world, running.goal_id).state is GoalState.ACTIVE

            # The kill: no shutdown, no disarm. A fresh loop simply comes up
            # over the same state directory and adopts the same store.
            revenant = revenant_loop(world)
            revenant.adopt_goal_store(GoalStore(world.state_dir))
            port = LoopGoalPort(loop=revenant)

            ended = port.status(running.goal_id).named
            assert ended is not None
            assert ended.state is GoalState.FAILED
            assert ended.reason_code is ReasonCode.SESSION_TERMINATED
            assert ended.detail == RESTART_LOST_DETAIL
            assert ended.detail == "the sidecar restarted while this goal was active"

            still = port.status(waiting.goal_id).named
            assert still is not None
            assert still.state is GoalState.PENDING
            assert still.submitted_at_ms == waiting.submitted_at_ms

            again = port.submit(a_goal("restart-waiting", satisfy_to=0.5))
            assert again.duplicate is True
            assert again.goal is not None
            assert again.goal.goal_id == waiting.goal_id

    def test_a_goal_that_expired_while_the_sidecar_was_down_expires_on_the_first_tick(
        self, tmp_path: Path
    ) -> None:
        with goal_world(tmp_path) as world:
            world.loop.adopt_goal_store(GoalStore(world.state_dir))
            record = submit(world, a_goal("downtime-key"))
            world.loop.tick()

            world.clock.advance(record.budget.pending_ttl_ms + 1)
            revenant = revenant_loop(world)
            revenant.adopt_goal_store(GoalStore(world.state_dir))
            queue = revenant.goals
            assert queue is not None
            assert queue.record(record.goal_id) is not None

            transitions = queue.tick()

            assert [(t.goal_id, t.state) for t in transitions] == [
                (record.goal_id, GoalState.EXPIRED)
            ]

    def test_no_write_happens_on_a_tick_without_a_goal_transition(self, tmp_path: Path) -> None:
        """The cadence, pinned: adoption with no file writes nothing, a
        transition writes once on its tick, a tick that moves no goal writes
        nothing at all."""
        with goal_world(tmp_path) as world:
            store = CountingGoalStore(world.state_dir)
            world.loop.adopt_goal_store(store)
            assert store.saves == 0, "a first run with no goals mints no file"

            world.loop.tick()
            assert store.saves == 0

            record = submit(world, a_goal("cadence-key"))
            world.loop.tick()
            assert store.saves == 1, "the submission reaches disk on the next tick"

            world.loop.tick()
            world.loop.tick()
            assert store.saves == 1, "ticks that move no goal must not write"

            world.clock.advance(record.budget.pending_ttl_ms + 1)
            world.loop.tick()
            assert store.saves == 2, "the expiry transition is one more write"
            assert record_of(world, record.goal_id).state is GoalState.EXPIRED

    def test_shutdown_writes_the_channel_as_it_ended(self, tmp_path: Path) -> None:
        """An orderly shutdown persists its own honesty: the disarm's CANCELLED
        record and the surviving backlog, so the next sidecar restores what
        this one actually did rather than inventing a SESSION_TERMINATED."""
        planner = RecordingGoalPlanner()
        with goal_world(tmp_path, planner) as world:
            world.loop.adopt_goal_store(GoalStore(world.state_dir))
            armed_autonomous(world)
            running = submit(world, a_goal("bye-running"))
            waiting = submit(world, a_goal("bye-waiting", satisfy_to=0.5))
            world.observe()
            world.loop.tick()
            assert record_of(world, running.goal_id).state is GoalState.ACTIVE

            world.loop.shutdown(reason="test shutdown")

            loaded = GoalStore(world.state_dir).load()
            assert loaded is not None
            by_id = {record.goal_id: record for record in loaded.records}
            ended = by_id[running.goal_id]
            assert ended.state is GoalState.CANCELLED
            assert ended.reason_code is ReasonCode.NOT_ARMED
            assert by_id[waiting.goal_id].state is GoalState.PENDING

    def test_an_unreadable_goals_file_is_set_aside_and_the_channel_starts_empty(
        self, tmp_path: Path
    ) -> None:
        with goal_world(tmp_path) as world:
            (world.state_dir / GOALS_FILE_NAME).write_text("{broken", encoding="utf-8")

            notes = world.loop.adopt_goal_store(GoalStore(world.state_dir))

            assert any("could not be read" in note for note in notes)
            assert any("could not be read" in problem for problem in world.loop.state_problems)
            aside = world.state_dir / GOALS_QUARANTINE_NAME
            assert aside.read_text(encoding="utf-8") == "{broken", "the evidence survives"
            assert not (world.state_dir / GOALS_FILE_NAME).exists()
            queue = world.loop.goals
            assert queue is not None
            assert queue.open_count == 0

    def test_a_loop_without_a_queue_adopts_nothing_and_says_so(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            notes = world.loop.adopt_goal_store(GoalStore(world.state_dir))

            assert notes == (
                "this sidecar was assembled without a goal queue, so no goals persist",
            )
            assert world.loop.goal_store is None
            assert not (world.state_dir / GOALS_FILE_NAME).exists()


# --------------------------------------------------------------------------
# the needs arbiter in the running loop: interrupt, satisfy, resume
# --------------------------------------------------------------------------


class TestTheNeedsArbiterInTheLoop:
    """The interrupt-satisfy-resume cycle end to end through the real loop.

    The unit half lives in ``test_needs_arbiter.py``; this class owns the
    loop-side joins: the observation's own safety block is what carries
    ``AUTONOMOUS`` to the arbiter, the loop's ordinary activation is what
    serves the preemptor and resumes the original, and the served port is
    where the suspension marker becomes visible to a status reader.
    """

    def autonomous_world(self, seq_hunger: float) -> dict[str, object]:
        """Observation overrides: an autonomous safety block and a hunger stat."""
        return {
            "safety": make_safety(mode=SessionMode.AUTONOMOUS),
            "player": make_player(stats={"hunger": seq_hunger}),
        }

    def test_a_hunger_crossing_suspends_serves_the_meal_and_resumes(self, tmp_path: Path) -> None:
        world, wrapper = navigating_world(tmp_path)
        with world:
            armed_autonomous(world)
            record = submit(world, navigate_request(1260, 3400, key="journey-key"))

            # Tick 1: the journey is the active goal; a leg is in the channel.
            world.observe(**self.autonomous_world(0.5))
            world.loop.tick()
            assert record_of(world, record.goal_id).state is GoalState.ACTIVE
            assert wrapper.tracked_journeys == 1

            # Tick 2: hunger crosses the critical line inside the observation
            # the loop hands the wrapper. The arbiter suspends the journey's
            # goal and injects the meal; nothing new is served this tick.
            world.beat_game()
            world.observe(**self.autonomous_world(0.8))
            world.loop.tick()

            parked = record_of(world, record.goal_id)
            assert parked.state is GoalState.PENDING
            assert parked.suspended_by is not None
            assert parked.suspended_by.startswith("arb.")
            assert wrapper.tracked_journeys == 1, "the journey drive waits out the meal"
            status = LoopGoalPort(loop=world.loop).status(record.goal_id)
            named = status.named
            assert named is not None and named.suspended_by == parked.suspended_by, (
                "the served port reports the marker a status reader needs"
            )
            entry = wrapper.arbiter_log[-1]
            assert entry["trigger"] == "hunger"
            assert entry["suspended_goal"] == record.goal_id

            # Tick 3: ordinary activation serves the injected meal. Nothing
            # edible exists anywhere, so the consume mission ends it typed —
            # a FAILED preemptor, exactly the case that must not orphan the
            # original.
            world.beat_game()
            world.observe(**self.autonomous_world(0.8))
            world.loop.tick()
            queue = world.loop.goals
            assert queue is not None
            meal_id = entry["injected_goal"]
            assert isinstance(meal_id, str)
            meal = record_of(world, meal_id)
            assert meal.kind is GoalKind.SATISFY_HUNGER
            assert meal.state is GoalState.FAILED
            assert meal.reason_code is ReasonCode.NO_SAFE_FOOD

            # Tick 4: the journey's goal resumes through the loop's own
            # activation, marker consumed, drive still tracked.
            world.beat_game()
            world.observe(**self.autonomous_world(0.8))
            world.loop.tick()
            resumed = record_of(world, record.goal_id)
            assert resumed.state is GoalState.ACTIVE
            assert resumed.suspended_by is None
            assert wrapper.tracked_journeys == 1
            settled = wrapper.arbiter_log[-1]
            assert settled["outcome"] == GoalState.FAILED.value

    def test_assisted_observations_never_preempt_in_the_loop(self, tmp_path: Path) -> None:
        """The same crossing with the mod reporting ASSISTED reshuffles nothing.

        The loop itself is armed AUTONOMOUS — the arbiter's gate is the
        observation's own safety block, the mod's word for the session.
        """
        world, wrapper = navigating_world(tmp_path)
        with world:
            armed_autonomous(world)
            record = submit(world, navigate_request(1260, 3400, key="assist-key"))

            world.observe(player=make_player(stats={"hunger": 0.5}))
            world.loop.tick()
            assert record_of(world, record.goal_id).state is GoalState.ACTIVE

            world.beat_game()
            world.observe(player=make_player(stats={"hunger": 0.8}))
            world.loop.tick()

            assert record_of(world, record.goal_id).state is GoalState.ACTIVE
            assert wrapper.arbiter_log == ()
