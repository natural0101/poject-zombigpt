"""The executor: one step, re-observe, and a recovery budget that runs out.

Nothing here sleeps. The clock is injected and moves only when the fake
observation source is waited on, so a timeout, a retry budget and an anti-loop
window are all exercised in microseconds.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pz_agent_core.actions import ActionEngine, AdapterRegistry, register_builtins
from pz_agent_core.planner.critic import PlanCritic
from pz_agent_core.planner.executor import (
    AMBIGUOUS_CODES,
    MAX_ENGINE_CALLS,
    ExecutorConfig,
    PlanExecutor,
    PlanOutcome,
    PlanReport,
    StepReport,
    StepState,
)
from pz_agent_core.planner.plan import (
    ConsumeArgs,
    FailureMode,
    ItemArgs,
    Plan,
    PlanStep,
    SuccessCriterion,
    SuccessKind,
)
from pz_agent_core.planner.provider import PlanProposal
from pz_agent_core.protocol import (
    ActionName,
    ActionOwnership,
    ActionStatus,
    Observation,
    Priority,
    ReasonCode,
    RiskClass,
    SessionMode,
    Wound,
)
from pz_agent_core.safety.priority import AntiLoopConfig
from tests.fixtures import DEFAULT_SESSION, make_action_state, make_safety
from tests.fixtures.action_doubles import (
    AckPlan,
    FakeClock,
    FakeCommandSink,
    FakeObservationSource,
    StubAdapter,
)
from tests.fixtures.planner_worlds import (
    ALL_CAPABILITIES,
    GOAL_ID,
    ScriptedProvider,
    eat_plan,
    eat_step,
    goal,
    item_ref,
    plan_request,
    planner_observation,
)
from tests.fixtures.policy_items import BACKPACK_REF, food_item, hungry_player, inventory


def hungry_observation(seq: int = 1, **overrides: object) -> Observation:
    overrides.setdefault("player", hungry_player(0.5))
    return planner_observation(seq=seq, **overrides)


class Harness:
    """An executor wired to fakes, with the plan the test wants it to run."""

    def __init__(
        self,
        *,
        plan: Plan | None = None,
        proposals: tuple[PlanProposal, ...] = (),
        acks: tuple[AckPlan, ...] = (AckPlan(status=ActionStatus.ACCEPTED),),
        verify_after: int | None = 1,
        config: ExecutorConfig | None = None,
        adapters: tuple[StubAdapter, ...] = (),
        anti_loop: AntiLoopConfig | None = None,
    ) -> None:
        self.clock = FakeClock()
        self.observations = FakeObservationSource(self.clock)
        self.sink = FakeCommandSink(self.clock, acks=acks)
        self.adapters = adapters or (
            StubAdapter(name=ActionName.CONSUME_EAT, risk=RiskClass.P2, verify_after=verify_after),
            StubAdapter(
                name=ActionName.INVENTORY_ENSURE_MAIN,
                risk=RiskClass.P1,
                verify_after=verify_after,
            ),
        )
        registry = register_builtins(AdapterRegistry())
        for adapter in self.adapters:
            registry.register(adapter)
        self.engine = ActionEngine(
            registry=registry,
            sink=self.sink,
            observations=self.observations,
            clock=self.clock,
            capability_check=lambda name: True,
        )
        chosen = plan if plan is not None else eat_plan()
        self.provider = ScriptedProvider(*(proposals or (PlanProposal.proposed(chosen, "eat"),)))
        self.executor = PlanExecutor(
            engine=self.engine,
            observations=self.observations,
            provider=self.provider,
            critic=PlanCritic(
                session_id=DEFAULT_SESSION, registry=registry, capabilities=ALL_CAPABILITIES
            ),
            session_id=DEFAULT_SESSION,
            clock=self.clock,
            config=config or ExecutorConfig(),
            anti_loop=anti_loop or AntiLoopConfig(),
        )

    def world(self, observation: Observation) -> Harness:
        self.observations.repeat(observation)
        return self

    def run(self, **overrides: object) -> PlanReport:
        return self.executor.run(plan_request(hungry_observation(), **overrides))


def calm() -> Observation:
    return hungry_observation()


class TestHappyPath:
    def test_a_one_step_plan_runs_and_reports_observed_evidence(self) -> None:
        harness = Harness().world(calm())
        report = harness.run()
        assert report.outcome is PlanOutcome.COMPLETED
        assert report.reason_code is None
        assert [step.state for step in report.steps] == [StepState.SUCCEEDED]
        evidence = report.steps[0].evidence
        # Succeeded means the adapter observed a postcondition, and the report
        # carries the proof rather than the claim.
        assert evidence is not None and evidence["kind"] == "stub_postcondition"

    def test_it_sends_exactly_one_command_per_step(self) -> None:
        plan = eat_plan(eat_step("s1"), eat_step("s2"))
        harness = Harness(plan=plan).world(calm())
        report = harness.run()
        assert report.outcome is PlanOutcome.COMPLETED
        assert [c.action for c in harness.sink.sent] == [
            ActionName.CONSUME_EAT,
            ActionName.CONSUME_EAT,
        ]
        assert report.engine_calls == 2

    def test_the_engines_own_retry_budget_is_switched_off(self) -> None:
        # Two retry budgets would multiply into a number neither layer chose.
        harness = Harness().world(calm())
        harness.run()
        policy = harness.sink.last.policy
        assert policy is not None and policy.max_retries == 0

    def test_the_world_is_re_read_before_every_step(self) -> None:
        plan = eat_plan(eat_step("s1"), eat_step("s2"))
        harness = Harness(plan=plan).world(calm())
        harness.run()
        # One wait to open the run, then one per step before the engine's own.
        assert len(harness.observations.waits) > len(plan.steps)


class TestPlanRefusals:
    def test_a_provider_refusal_is_reported_without_anything_running(self) -> None:
        refusal = PlanProposal.refusal(ReasonCode.NO_SAFE_FOOD, "nothing edible")
        harness = Harness(proposals=(refusal,)).world(calm())
        report = harness.run()
        assert report.outcome is PlanOutcome.REJECTED
        assert report.reason_code is ReasonCode.NO_SAFE_FOOD
        assert harness.sink.sent == []

    def test_a_critic_refusal_is_reported_without_anything_running(self) -> None:
        harness = Harness().world(hungry_observation(safety=make_safety(mode=SessionMode.OBSERVE)))
        report = harness.run()
        assert report.outcome is PlanOutcome.REJECTED
        assert report.reason_code is ReasonCode.POLICY_DENIED
        assert harness.sink.sent == []

    def test_a_world_that_never_reports_stops_the_run(self) -> None:
        harness = Harness()  # no observations pushed at all
        report = harness.run()
        assert report.outcome is PlanOutcome.STOPPED
        assert report.reason_code is ReasonCode.GAME_DISCONNECTED


class TestPreconditions:
    def test_a_step_whose_item_has_vanished_is_never_sent(self) -> None:
        harness = Harness().world(hungry_observation(inventory=inventory()))
        report = harness.run()
        assert harness.sink.sent == []
        assert report.steps[0].reason_code is ReasonCode.INVALID_REF
        assert "no longer in the inventory" in report.steps[0].detail

    def test_a_step_whose_goal_already_holds_is_skipped_rather_than_spent(self) -> None:
        harness = Harness().world(hungry_observation(player=hungry_player(0.05)))
        report = harness.run()
        assert report.outcome is PlanOutcome.COMPLETED
        assert [step.state for step in report.steps] == [StepState.SKIPPED]
        assert harness.sink.sent == []

    def test_a_manual_takeover_stops_the_plan_without_sending(self) -> None:
        harness = Harness().world(hungry_observation(safety=make_safety(manual_takeover=True)))
        report = harness.run()
        assert report.outcome is PlanOutcome.STOPPED
        assert report.reason_code is ReasonCode.USER_TAKEOVER
        assert harness.sink.sent == []
        assert report.steps[0].state is StepState.CANCELLED

    def test_a_disarmed_session_stops_the_plan(self) -> None:
        harness = Harness().world(hungry_observation(safety=make_safety(armed=False)))
        report = harness.run()
        assert report.reason_code is ReasonCode.NOT_ARMED
        assert harness.sink.sent == []

    def test_a_busy_manual_queue_is_retried_rather_than_treated_as_fatal(self) -> None:
        busy = hungry_observation(
            action=make_action_state(busy=True, ownership=ActionOwnership.MANUAL, type="Foraging")
        )
        harness = Harness().world(busy)
        report = harness.run()
        assert report.reason_code is ReasonCode.PLAYER_BUSY_MANUAL_ACTION
        # Retryable, so the budget was spent on it — and no command was ever
        # queued behind the player's own action.
        assert report.steps[0].attempts == 2
        assert harness.sink.sent == []


class TestRecovery:
    def test_a_retryable_failure_is_retried_within_the_budget(self) -> None:
        harness = Harness(
            acks=(AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.PATH_STUCK),),
            verify_after=None,
        ).world(calm())
        report = harness.run()
        assert report.engine_calls == 2
        assert report.steps[-1].attempts == 2
        assert report.outcome is PlanOutcome.STOPPED
        assert "Recovery for this step is spent" in report.detail

    def test_the_retry_budget_is_a_bound_not_a_suggestion(self) -> None:
        config = ExecutorConfig(max_attempts_per_step=3)
        harness = Harness(
            acks=(AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.PATH_STUCK),),
            verify_after=None,
            config=config,
        ).world(calm())
        report = harness.run()
        assert report.engine_calls == 3

    def test_a_non_retryable_failure_is_not_retried(self) -> None:
        harness = Harness(
            acks=(AckPlan(status=ActionStatus.REJECTED, reason=ReasonCode.INVALID_ARGUMENT),),
            verify_after=None,
        ).world(calm())
        report = harness.run()
        assert report.engine_calls == 1
        assert report.outcome is PlanOutcome.STOPPED

    def test_a_step_that_says_skip_lets_the_plan_carry_on(self) -> None:
        plan = eat_plan(
            eat_step("s1", on_failure=FailureMode.SKIP),
            eat_step("s2"),
        )
        harness = Harness(
            plan=plan,
            acks=(AckPlan(status=ActionStatus.REJECTED, reason=ReasonCode.INVALID_ARGUMENT),),
            verify_after=None,
        ).world(calm())
        report = harness.run()
        assert [step.state for step in report.steps] == [StepState.FAILED, StepState.FAILED]
        assert report.engine_calls == 2

    def test_a_replan_that_proposes_the_step_that_just_failed_is_refused(self) -> None:
        # The scripted provider repeats its last plan, which is exactly the loop
        # §7.8 exists to catch: the critic recognises the signature and the run
        # ends without a second attempt at the same thing.
        failing = eat_plan(eat_step("s1", on_failure=FailureMode.REPLAN_ONCE))
        harness = Harness(
            plan=failing,
            acks=(AckPlan(status=ActionStatus.REJECTED, reason=ReasonCode.INVALID_ARGUMENT),),
            verify_after=None,
        ).world(calm())
        report = harness.run()
        assert report.replans == 1
        assert report.outcome is PlanOutcome.STOPPED
        assert report.engine_calls == 1
        assert "already failed in a way that retrying cannot fix" in report.detail

    def test_a_replan_that_proposes_something_else_runs_it(self) -> None:
        first = eat_plan(eat_step("s1", on_failure=FailureMode.REPLAN_ONCE))
        second = eat_plan(
            eat_step("s1", args=ConsumeArgs(item_ref=item_ref("beans"), fraction=0.5))
        )
        harness = Harness(
            plan=first,
            proposals=(
                PlanProposal.proposed(first, "eat it all"),
                PlanProposal.proposed(second, "eat half"),
            ),
            acks=(AckPlan(status=ActionStatus.REJECTED, reason=ReasonCode.INVALID_ARGUMENT),),
            verify_after=None,
        ).world(calm())
        report = harness.run()
        assert report.replans == 1
        # The first plan's attempt, then the replacement's: two, and no more.
        assert report.engine_calls == 2

    def test_the_replan_is_told_what_already_failed(self) -> None:
        failing = eat_plan(eat_step("s1", on_failure=FailureMode.REPLAN_ONCE))
        harness = Harness(
            plan=failing,
            acks=(AckPlan(status=ActionStatus.REJECTED, reason=ReasonCode.INVALID_ARGUMENT),),
            verify_after=None,
        ).world(calm())
        harness.run()
        assert len(harness.provider.calls) == 2
        assert harness.provider.calls[1].recent_failures[0].reason_code is (
            ReasonCode.INVALID_ARGUMENT
        )

    def test_a_second_replan_is_refused_by_the_budget(self) -> None:
        config = ExecutorConfig(max_replans=0)
        failing = eat_plan(eat_step("s1", on_failure=FailureMode.REPLAN_ONCE))
        harness = Harness(
            plan=failing,
            acks=(AckPlan(status=ActionStatus.REJECTED, reason=ReasonCode.INVALID_ARGUMENT),),
            verify_after=None,
            config=config,
        ).world(calm())
        report = harness.run()
        assert report.replans == 0
        assert report.outcome is PlanOutcome.STOPPED
        assert len(harness.provider.calls) == 1

    def test_the_run_wide_engine_budget_is_a_hard_ceiling(self) -> None:
        plan = eat_plan(*[eat_step(f"s{index}") for index in range(4)])
        config = ExecutorConfig(max_engine_calls=2)
        harness = Harness(plan=plan, config=config).world(calm())
        report = harness.run()
        assert report.engine_calls == 2
        assert report.outcome is PlanOutcome.STOPPED
        assert report.reason_code is ReasonCode.POLICY_DENIED
        assert report.pending_step_ids == ("s2", "s3")

    def test_the_engine_budget_may_not_be_configured_past_its_ceiling(self) -> None:
        with pytest.raises(ValueError, match=rf"within 1\.\.{MAX_ENGINE_CALLS}"):
            ExecutorConfig(max_engine_calls=MAX_ENGINE_CALLS + 1)


class TestAmbiguity:
    @pytest.mark.parametrize("code", sorted(AMBIGUOUS_CODES))
    def test_an_ambiguous_failure_stops_rather_than_retrying_or_replanning(
        self, code: ReasonCode
    ) -> None:
        # `on_failure` says replan; ambiguity overrides it, because replanning
        # means planning against a world nobody has confirmed.
        plan = eat_plan(eat_step("s1", on_failure=FailureMode.REPLAN_ONCE))
        harness = Harness(
            plan=plan,
            acks=(AckPlan(status=ActionStatus.FAILED, reason=code),),
            verify_after=None,
        ).world(calm())
        report = harness.run()
        assert report.outcome is PlanOutcome.STOPPED
        assert report.reason_code is code
        assert report.replans == 0
        assert report.engine_calls == 1
        assert "cannot tell whether that step changed anything" in report.detail

    def test_a_mod_that_claims_success_without_evidence_is_ambiguous(self) -> None:
        # The engine turns "acked succeeded, never observed" into
        # POSTCONDITION_FAILED; the executor refuses to guess past it.
        harness = Harness(
            acks=(AckPlan(status=ActionStatus.SUCCEEDED),),
            verify_after=None,
        ).world(calm())
        report = harness.run()
        assert report.reason_code is ReasonCode.POSTCONDITION_FAILED
        assert report.outcome is PlanOutcome.STOPPED


class TestSuspension:
    def test_a_higher_priority_need_suspends_the_plan_mid_run(self) -> None:
        plan = eat_plan(eat_step("s1"), eat_step("s2"))
        harness = Harness(plan=plan)
        # The first step runs against a merely hungry world; between the steps
        # the character starts bleeding, which outranks a user command.
        calm_world = hungry_observation()
        harness.observations.push(calm_world, replace(calm_world, seq=2))
        harness.observations.repeat(_bleeding())
        report = harness.run()
        assert report.outcome is PlanOutcome.SUSPENDED
        assert report.suspended_by is not None
        assert report.suspended_by.priority is Priority.CRITICAL_BLEEDING
        assert report.reason_code is None

    def test_a_suspended_plan_keeps_the_steps_it_never_ran(self) -> None:
        plan = eat_plan(eat_step("s1"), eat_step("s2"))
        harness = Harness(plan=plan)
        harness.observations.repeat(_bleeding())
        report = harness.run()
        assert report.outcome is PlanOutcome.SUSPENDED
        # §7.2: an emergency suspends a goal, it does not destroy it.
        assert report.pending_step_ids == ("s1", "s2")

    def test_a_need_below_the_plans_priority_does_not_interrupt_it(self) -> None:
        harness = Harness(config=ExecutorConfig(plan_priority=Priority.LETHAL_THREAT)).world(
            _bleeding()
        )
        report = harness.run()
        assert report.outcome is PlanOutcome.COMPLETED

    def test_a_need_that_keeps_firing_without_improving_escalates_to_the_user(self) -> None:
        plan = eat_plan(*[eat_step(f"s{index}") for index in range(4)])
        harness = Harness(
            plan=plan, anti_loop=AntiLoopConfig(alternate_after=1, escalate_after=1)
        ).world(_bleeding())
        report = harness.run()
        assert report.outcome is PlanOutcome.ASK_USER
        assert report.reason_code is ReasonCode.NO_PROGRESS

    def test_the_arbiter_is_shared_so_a_caller_can_clear_a_resolved_need(self) -> None:
        harness = Harness().world(_bleeding())
        harness.run()
        assert harness.executor.arbiter.tracked == 1
        harness.executor.arbiter.note_resolved("bleeding")
        assert harness.executor.arbiter.tracked == 0


class TestReports:
    def test_a_completed_run_may_not_carry_a_reason_code(self) -> None:
        with pytest.raises(ValueError, match="not a failure and carries no code"):
            PlanReport(
                goal_id=GOAL_ID,
                outcome=PlanOutcome.COMPLETED,
                detail="done",
                reason_code=ReasonCode.NO_SAFE_FOOD,
            )

    def test_a_stopped_run_must_say_why(self) -> None:
        with pytest.raises(ValueError, match="must say why"):
            PlanReport(goal_id=GOAL_ID, outcome=PlanOutcome.STOPPED, detail="it stopped")

    def test_a_suspended_run_names_the_need_that_suspended_it(self) -> None:
        with pytest.raises(ValueError, match="names the need that suspended it"):
            PlanReport(goal_id=GOAL_ID, outcome=PlanOutcome.SUSPENDED, detail="paused")

    def test_a_succeeded_step_cannot_be_reported_without_evidence(self) -> None:
        with pytest.raises(ValueError, match="reports the observed evidence"):
            StepReport(
                step_id="s1",
                action=ActionName.CONSUME_EAT,
                state=StepState.SUCCEEDED,
                attempts=1,
                detail="it worked",
            )

    def test_a_step_report_must_explain_itself(self) -> None:
        with pytest.raises(ValueError, match="must explain itself"):
            StepReport(
                step_id="s1",
                action=ActionName.CONSUME_EAT,
                state=StepState.FAILED,
                attempts=1,
                detail="   ",
            )


def _bleeding() -> Observation:
    wound = Wound(ref=f"wound:{DEFAULT_SESSION}:1", kind="cut", severity=0.4, bleeding=True)
    return hungry_observation(player=hungry_player(0.5, wounds=[wound]))


def test_a_two_step_plan_moves_the_item_before_eating_it() -> None:
    stowed = food_item("beans", container_ref=BACKPACK_REF)
    plan = eat_plan(
        PlanStep(
            step_id="s1",
            action=ActionName.INVENTORY_ENSURE_MAIN,
            args=ItemArgs(item_ref=stowed.ref),
            success=SuccessCriterion(kind=SuccessKind.ADAPTER_EVIDENCE),
        ),
        eat_step("s2", args=ConsumeArgs(item_ref=stowed.ref)),
    )
    harness = Harness(plan=plan).world(hungry_observation(inventory=inventory(stowed)))
    report = harness.run(goal=goal())
    assert report.outcome is PlanOutcome.COMPLETED
    assert [c.action for c in harness.sink.sent] == [
        ActionName.INVENTORY_ENSURE_MAIN,
        ActionName.CONSUME_EAT,
    ]
