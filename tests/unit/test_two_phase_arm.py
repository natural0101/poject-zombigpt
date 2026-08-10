"""The two-phase arm: authority only on the game's own confirmation.

Live finding (Build 42.20.2, 2026-08-08), the central one of the run:
``pz_session_arm`` answered ``armed=true`` having armed only the sidecar. The
loop flipped its own flag and published its own heartbeat; no ``session.arm``
command was ever enqueued, and the game kept publishing ``armed=false`` /
``mode=OFF``. Writing a ``session.arm`` record into the queue by hand moved the
game to ``ASSISTED`` — the whole game side existed and worked; the sidecar
simply never asked it.

Every test here drives a real :class:`~pz_agent_cli.runtime.SidecarLoop` over a
real exchange directory with the mod faked at the files, exactly as the loop
suite does: the "mod" is a reader over the command journal plus
:func:`~tests.fixtures.ipc_builders.publish_acks` and the heartbeat file. What
is proven: the arm submits a real ``session.arm`` record (asserted on disk) and
reports nothing until the mod's terminal ``succeeded`` ack *and* a same-session
heartbeat carrying ``armed=true`` in the requested mode are both observed; each
way the confirmation can fail — a refusing ack, an ack with no confirming
heartbeat, no ack at all — resolves ``armed=false`` with the honest reason; a
late ack after the deadline arms nothing; and the levers around the arm (the
control channel's decision, the second-attach refusal, the takeover-paused
goal) stay honest.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pz_agent_cli.runtime import (
    DEFAULT_ARM_CONFIRM_TIMEOUT_MS,
    PendingArm,
)
from pz_agent_cli.supervisor import ControlKind
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.goals import GoalKind, GoalParams, GoalQueue, GoalRequest, GoalState
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.protocol.messages import MIN_LEASE_MS
from tests.fixtures import make_safety
from tests.fixtures.ipc_builders import publish_acks
from tests.fixtures.sidecar_worlds import (
    TEST_LIMITS,
    ScriptedMod,
    arm_for_real,
    attached_world,
)

#: A short confirmation window for the deadline tests, chosen so the clock can
#: cross it without also crossing the 5 s game-heartbeat staleness bound — a
#: stale heartbeat is its own (different) refusal, and these tests must land on
#: the deadline's.
SHORT_CONFIRM_MS = 1_000

SHORT_CONFIRM_LIMITS = replace(TEST_LIMITS, arm_confirm_timeout_ms=SHORT_CONFIRM_MS)


class NoInitiativeGoalPlanner:
    """Goal-capable and idle, so activation is real and nothing dispatches."""

    def propose(self, observation: Observation) -> ActionRequest | None:
        return None

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        return None


class TestTheArmSubmitsAndWaits:
    def test_arm_writes_a_session_arm_command_and_grants_nothing_by_itself(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            outcome = world.loop.arm(SessionMode.ASSISTED)

            assert outcome.pending is True
            assert outcome.armed is False, "queued is not armed; the game has not spoken"
            assert world.loop.armed is False
            command = ScriptedMod(world).only_arm_command()
            assert command.session_id == world.session_id
            assert command.args == {"mode": SessionMode.ASSISTED.value}
            assert command.lease_ms == TEST_LIMITS.arm_confirm_timeout_ms >= MIN_LEASE_MS
            pending = world.loop.pending_arm
            assert pending is not None
            assert pending.command_id == command.command_id
            assert pending.deadline_ms == world.clock.now + TEST_LIMITS.arm_confirm_timeout_ms

    def test_nothing_arms_until_both_the_ack_and_the_heartbeat_confirm(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            mod = ScriptedMod(world)
            assert world.loop.arm(SessionMode.ASSISTED).pending

            # Ticks with no answer at all: still disarmed, still pending. The
            # tick outcomes are read instead of the loop's property so each
            # assertion is against a fresh record, not a narrowed re-read.
            mod.beat()
            silent = world.loop.tick()
            assert silent.armed is False
            assert world.loop.pending_arm is not None

            # The succeeded ack alone is half a confirmation, not authority.
            mod.ack_success(mod.only_arm_command(), mode=SessionMode.ASSISTED)
            mod.beat()  # fresh heartbeat, but it does not report the armed mode
            acked_only = world.loop.tick()
            assert acked_only.armed is False
            pending = world.loop.pending_arm
            assert pending is not None and pending.acked is True

            # The same-session heartbeat reporting armed=true in the requested
            # mode is the other half; with both observed, authority is real.
            mod.beat(armed=True, mode=SessionMode.ASSISTED)
            outcome = world.loop.tick()
            assert world.loop.armed is True
            assert world.loop.mode is SessionMode.ASSISTED
            assert outcome.armed is True
            # Annotated wide on purpose: the tick just consumed the pending
            # arm, which mypy's member narrowing cannot see.
            resolved: PendingArm | None = world.loop.pending_arm
            assert resolved is None
            resolution = world.loop.arm_resolution
            assert resolution is not None and resolution.armed is True
            assert "acked session.arm" in resolution.detail
            assert "armed=true" in resolution.detail

    def test_a_heartbeat_for_another_session_or_mode_confirms_nothing(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            mod = ScriptedMod(world)
            assert world.loop.arm(SessionMode.AUTONOMOUS).pending
            mod.ack_success(mod.only_arm_command(), mode=SessionMode.AUTONOMOUS)

            # Right mode, wrong session: the heartbeat is not this session's word.
            world.beat_game(
                session_id="some-other-session", armed=True, mode=SessionMode.AUTONOMOUS
            )
            world.loop.tick()
            assert world.loop.armed is False

            # Right session, wrong mode: ASSISTED is not what was asked for.
            mod.beat(armed=True, mode=SessionMode.ASSISTED)
            world.loop.tick()
            assert world.loop.armed is False
            assert world.loop.pending_arm is not None, "still waiting, honestly"


class TestTheWaysConfirmationFails:
    def test_an_acked_arm_with_no_confirming_heartbeat_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path, limits=SHORT_CONFIRM_LIMITS) as world:
            mod = ScriptedMod(world)
            assert world.loop.arm(SessionMode.ASSISTED).pending
            mod.ack_success(mod.only_arm_command(), mode=SessionMode.ASSISTED)
            world.loop.tick()
            assert world.loop.pending_arm is not None

            world.clock.advance(SHORT_CONFIRM_MS + 1)
            mod.beat()  # the game is alive and saying nothing about being armed
            deadline_tick = world.loop.tick()

            assert deadline_tick.armed is False
            resolved: PendingArm | None = world.loop.pending_arm
            assert resolved is None
            resolution = world.loop.arm_resolution
            assert resolution is not None and resolution.armed is False
            assert "no heartbeat confirming armed=true" in resolution.detail
            assert f"{SHORT_CONFIRM_MS} ms" in resolution.detail
            # The countermand: the game acked an arm the sidecar refused to
            # keep, so a session.disarm follows it onto the journal.
            assert mod.commands(ActionName.SESSION_DISARM), (
                "an abandoned-but-acked arm must be countermanded"
            )

    def test_a_failed_ack_refuses_the_arm_with_the_mod_s_own_reason(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            mod = ScriptedMod(world)
            assert world.loop.arm(SessionMode.ASSISTED).pending
            mod.ack_failure(
                mod.only_arm_command(),
                reason_code=ReasonCode.USER_TAKEOVER,
                message="the user holds the controls",
            )
            mod.beat()

            world.loop.tick()

            assert world.loop.armed is False
            assert world.loop.pending_arm is None
            resolution = world.loop.arm_resolution
            assert resolution is not None and resolution.armed is False
            assert ReasonCode.USER_TAKEOVER.value in resolution.detail
            assert "the user holds the controls" in resolution.detail

    def test_no_ack_at_all_is_a_deadline_refusal_and_a_late_ack_arms_nothing(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path, limits=SHORT_CONFIRM_LIMITS) as world:
            mod = ScriptedMod(world)
            assert world.loop.arm(SessionMode.ASSISTED).pending
            world.clock.advance(SHORT_CONFIRM_MS + 1)
            mod.beat()
            world.loop.tick()

            assert world.loop.armed is False
            resolution = world.loop.arm_resolution
            assert resolution is not None and resolution.armed is False
            assert "never confirmed the arm" in resolution.detail
            assert "no terminal ack" in resolution.detail

            # The mod answers late — ack and armed heartbeat both — and it must
            # arm nothing: the pending state is gone and nothing re-judges it.
            mod.confirm_arm(SessionMode.ASSISTED)
            world.loop.tick()
            world.loop.tick()
            assert world.loop.armed is False
            assert world.loop.pending_arm is None
            # The abandoned arm was countermanded, so the game's late grant is
            # revoked by its own always-allowed disarm rather than left standing.
            assert mod.commands(ActionName.SESSION_DISARM)

    def test_the_default_confirmation_window_is_the_documented_bound(self) -> None:
        assert TEST_LIMITS.arm_confirm_timeout_ms == DEFAULT_ARM_CONFIRM_TIMEOUT_MS


class TestTheControlChannelSeesOnlyTheOutcome:
    def test_the_decision_for_an_arm_request_waits_for_the_confirmation(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            mod = ScriptedMod(world)
            request = world.control.write(ControlKind.ARM, mode=SessionMode.ASSISTED)

            world.loop.tick()  # consumes the request and submits session.arm

            assert mod.commands(ActionName.SESSION_ARM), "the request became a real command"
            decision = world.loop.control_decision
            assert decision is None or decision.nonce != request.nonce, (
                "no decision may be published before the two-phase outcome is known"
            )

            mod.confirm_arm(SessionMode.ASSISTED)
            world.loop.tick()

            decision = world.loop.control_decision
            assert decision is not None and decision.nonce == request.nonce
            assert decision.applied is True and decision.armed is True
            assert decision.mode is SessionMode.ASSISTED

    def test_a_refusing_ack_reaches_the_requester_in_the_mod_s_words(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            mod = ScriptedMod(world)
            request = world.control.write(ControlKind.ARM, mode=SessionMode.ASSISTED)
            world.loop.tick()
            mod.ack_failure(
                mod.only_arm_command(),
                reason_code=ReasonCode.USER_TAKEOVER,
                message="the user holds the controls",
            )
            mod.beat()

            world.loop.tick()

            decision = world.loop.control_decision
            assert decision is not None and decision.nonce == request.nonce
            assert decision.applied is False and decision.armed is False
            assert "the user holds the controls" in decision.detail


class TestDisarmTellsTheGame:
    def test_disarm_is_immediate_locally_and_submits_session_disarm(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            mod = ScriptedMod(world)
            arm_for_real(world, mod)

            outcome = world.loop.disarm(reason="user asked")

            assert outcome.armed is False, "disarming is never gated on the game"
            assert world.loop.armed is False
            disarms = mod.commands(ActionName.SESSION_DISARM)
            assert len(disarms) == 1
            notice = world.loop.disarm_notice
            assert notice is not None and notice.confirmed is False
            assert notice.command_id == disarms[0].command_id

            # The mod's ack is its own observed postcondition; the notice folds
            # it in without anything having waited on it.
            ack = ActionResult.succeeded(
                session_id=world.session_id,
                seq=2,
                command_id=disarms[0].command_id,
                action=ActionName.SESSION_DISARM.value,
                timestamp_ms=world.clock.now,
                evidence={
                    "armed_before": True,
                    "armed_after": False,
                    "mode_before": SessionMode.ASSISTED.value,
                    "mode_after": SessionMode.OFF.value,
                },
            )
            publish_acks(world.layout, [ack])
            mod.beat(armed=False, mode=SessionMode.OFF)
            world.loop.tick()
            notice = world.loop.disarm_notice
            assert notice is not None and notice.confirmed is True
            assert "mode_after=OFF" in notice.detail

    def test_a_disarm_that_revoked_nothing_sends_nothing(self, tmp_path: Path) -> None:
        with attached_world(tmp_path) as world:
            mod = ScriptedMod(world)

            world.loop.disarm(reason="already observing")

            assert mod.commands(ActionName.SESSION_DISARM) == [], (
                "a no-op disarm must not stream commands at the mod"
            )


class TestTheSecondAttachIsRefused:
    def test_attach_on_an_attached_loop_is_a_typed_refusal_not_a_second_producer(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            before = world.session_id

            again = world.loop.attach()

            assert again.attached is False
            assert before in again.detail
            assert "second producer" in again.detail
            assert "shut down first" in again.detail
            # The refusal cost nothing: the first attachment still ticks.
            assert world.loop.session is not None
            assert world.loop.session.session_id == before
            world.loop.tick()

    def test_attach_after_shutdown_is_still_how_a_loop_comes_back(self, tmp_path: Path) -> None:
        world = attached_world(tmp_path)
        try:
            world.loop.shutdown(reason="making room")
            world.beat_game(session_id=str(world.loop.sessions.load().session_id))  # type: ignore[union-attr]
            outcome = world.loop.attach()
            assert outcome.attached, outcome.detail
        finally:
            if world.loop.session is not None:
                world.loop.shutdown(reason="test finished")


class TestTakeoverPausesTheGoal:
    def test_a_manual_takeover_parks_the_active_goal_instead_of_losing_it(
        self, tmp_path: Path
    ) -> None:
        with attached_world(tmp_path) as world:
            world.loop.goals = GoalQueue(clock=world.clock, armed=False)
            world.loop.planner = NoInitiativeGoalPlanner()
            mod = ScriptedMod(world)
            arm_for_real(world, mod, SessionMode.AUTONOMOUS)

            queue = world.loop.goals
            with world.loop.goal_lock:
                admission = queue.submit(
                    GoalRequest(
                        kind=GoalKind.SATISFY_HUNGER,
                        idempotency_key="takeover-goal",
                        params=GoalParams(satisfy_to=0.4),
                    )
                )
            assert admission.goal is not None
            goal_id = admission.goal.goal_id

            world.observe(safety=make_safety(armed=True, mode=SessionMode.AUTONOMOUS))
            world.loop.tick()
            with world.loop.goal_lock:
                active = queue.active
            assert active is not None and active.goal_id == goal_id

            # The user takes over. The goal must not vanish silently: the loop
            # parks it as paused-by-user and ends it through the queue's own
            # cancel, so both records agree about what happened.
            world.observe(
                safety=make_safety(armed=True, mode=SessionMode.AUTONOMOUS, manual_takeover=True)
            )
            world.loop.tick()

            paused = world.loop.paused_goal
            assert paused is not None
            assert paused.goal_id == goal_id
            assert paused.kind == GoalKind.SATISFY_HUNGER.value
            assert paused.reason == "manual takeover"
            with world.loop.goal_lock:
                record = queue.record(goal_id)
                active = queue.active
            assert active is None
            assert record is not None, "the queue still answers for the goal"
            assert record.state is GoalState.CANCELLED

            # A new arm does not silently resume it: authority returns, the
            # goal does not, and the marker stays visible until an explicit
            # fresh activation replaces it.
            world.loop.disarm(reason="user asked")
            mod.beat()
            arm_for_real(world, mod, SessionMode.AUTONOMOUS)
            world.observe(safety=make_safety(armed=True, mode=SessionMode.AUTONOMOUS))
            world.loop.tick()
            with world.loop.goal_lock:
                active = queue.active
            assert active is None, "nothing resumed the cancelled goal"
            assert world.loop.paused_goal is not None
            assert world.loop.paused_goal.goal_id == goal_id

            # The explicit path: a fresh submission activates and the parked
            # marker has served its purpose.
            with world.loop.goal_lock:
                fresh = queue.submit(
                    GoalRequest(
                        kind=GoalKind.SATISFY_HUNGER,
                        idempotency_key="takeover-goal-resumed",
                        params=GoalParams(satisfy_to=0.4),
                    )
                )
            assert fresh.goal is not None
            world.observe(safety=make_safety(armed=True, mode=SessionMode.AUTONOMOUS))
            world.loop.tick()
            with world.loop.goal_lock:
                active = queue.active
            assert active is not None and active.goal_id == fresh.goal.goal_id
            assert world.loop.paused_goal is None
