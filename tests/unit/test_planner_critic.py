"""The critic: the last gate, and what it will not let through.

Every test here builds a plan that would otherwise be perfectly runnable and
breaks exactly one thing, because a refusal test whose plan was already bad for
two reasons cannot tell you which rule fired.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import AdapterRegistry, register_builtins
from pz_agent_core.capabilities import EAT_PERCENTAGE, INVENTORY_TRANSFER, MOVE_TO_SQUARE
from pz_agent_core.memory import Square
from pz_agent_core.planner.critic import (
    DEFAULT_HOME_RADIUS,
    CriticRule,
    CriticVerdict,
    PlanCritic,
    RiskAssessor,
)
from pz_agent_core.planner.plan import (
    MAX_PLAN_STEPS,
    ConsumeArgs,
    ItemArgs,
    MoveNearArgs,
    MoveToArgs,
    Plan,
    PlanStep,
    StepFailure,
    SuccessCriterion,
    SuccessKind,
    TransferArgs,
)
from pz_agent_core.policy.permissions import Initiative, PermissionConfig
from pz_agent_core.policy.selection import CapabilitySnapshot
from pz_agent_core.protocol import (
    ActionName,
    CapabilityState,
    ContainerView,
    NearbyObject,
    NearbyView,
    Observation,
    Position,
    ReasonCode,
    RiskClass,
    SessionMode,
)
from tests.fixtures import DEFAULT_SESSION, make_safety
from tests.fixtures import make_observation as _make_observation
from tests.fixtures.planner_worlds import (
    ALL_CAPABILITIES,
    NO_PROBES,
    eat_plan,
    eat_step,
    foreign_item_ref,
    full_registry,
    item_ref,
    planner_observation,
)
from tests.fixtures.planner_worlds import item_ref as planner_item_ref
from tests.fixtures.policy_items import (
    BACKPACK_REF,
    MAIN_REF,
    WORLD_REF,
    backpack_container,
    food_item,
    inventory,
    main_container,
    world_container,
)

OBJECT_REF = f"object:{DEFAULT_SESSION}:1200:3400:0:1"

#: A deployment that has opted in to autonomous movement, which is the only way
#: §7.5's "ограниченный P3" is ever granted. Without it the permission ladder
#: refuses every travelling step before the radius is even looked at, and these
#: tests would pass while asserting nothing about §7.10.
MOVEMENT_GRANTED = PermissionConfig(
    autonomous_p3_actions=frozenset({ActionName.MOVEMENT_MOVE_TO, ActionName.MOVEMENT_MOVE_NEAR})
)


def critic(**overrides: object) -> PlanCritic:
    settings: dict[str, object] = {
        "session_id": DEFAULT_SESSION,
        "registry": full_registry(),
        "capabilities": ALL_CAPABILITIES,
    }
    settings.update(overrides)
    return PlanCritic(**settings)  # type: ignore[arg-type]


def move_step(step_id: str = "s1", **overrides: object) -> PlanStep:
    settings: dict[str, object] = {
        "step_id": step_id,
        "action": ActionName.MOVEMENT_MOVE_TO,
        "args": MoveToArgs(x=1205, y=3405, z=0),
        "success": SuccessCriterion(kind=SuccessKind.POSITION_REACHED),
    }
    settings.update(overrides)
    return PlanStep(**settings)  # type: ignore[arg-type]


class TestApproval:
    def test_a_runnable_plan_is_approved_and_reports_its_tier(self) -> None:
        verdict = critic().review(eat_plan(), planner_observation())
        assert verdict.approved
        assert verdict.risk is RiskClass.P2
        assert verdict.rule is None and verdict.reason_code is None

    def test_the_reported_tier_is_the_highest_step_not_the_first(self) -> None:
        world_item = food_item("tin", container_ref=WORLD_REF)
        plan = eat_plan(
            PlanStep(
                step_id="s1",
                action=ActionName.INVENTORY_TRANSFER,
                args=TransferArgs(item_ref=world_item.ref, destination_container_ref=MAIN_REF),
                success=SuccessCriterion(kind=SuccessKind.ITEM_IN_MAIN_INVENTORY),
            ),
        )
        observation = planner_observation(
            inventory=inventory(world_item, containers=_containers_with_world())
        )
        verdict = critic().review(plan, observation)
        # The adapter, not the action name, says a world container is P3.
        assert verdict.approved
        assert verdict.risk is RiskClass.P3


class TestStepCap:
    def test_a_plan_over_the_configured_cap_is_refused(self) -> None:
        plan = eat_plan(*[eat_step(f"s{index}") for index in range(3)])
        verdict = critic(max_steps=2).review(plan, planner_observation())
        assert verdict.rule is CriticRule.STEP_CAP_EXCEEDED
        assert verdict.reason_code is ReasonCode.POLICY_DENIED
        assert "at most 2" in verdict.detail

    def test_a_cap_above_the_plan_types_own_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match=rf"max_steps must be within 1\.\.{MAX_PLAN_STEPS}"):
            critic(max_steps=MAX_PLAN_STEPS + 1)


class TestUnavailableAction:
    def test_an_action_with_no_adapter_is_refused_before_anything_runs(self) -> None:
        # A registry holding only the API-free builtins: nothing can eat.
        verdict = critic(registry=register_builtins(AdapterRegistry())).review(
            eat_plan(), planner_observation()
        )
        assert verdict.rule is CriticRule.ACTION_UNAVAILABLE
        assert verdict.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert verdict.step_id == "s1"


class TestCapability:
    def test_an_unprobed_capability_is_refused_as_a_capability_problem(self) -> None:
        verdict = critic(capabilities=NO_PROBES).review(eat_plan(), planner_observation())
        assert verdict.rule is CriticRule.CAPABILITY_UNUSABLE
        assert verdict.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert EAT_PERCENTAGE in verdict.detail

    def test_a_capability_switched_off_by_policy_is_refused_without_asking(self) -> None:
        probes = CapabilitySnapshot.from_mapping(
            {EAT_PERCENTAGE: CapabilityState.DISABLED_BY_POLICY}
        )
        verdict = critic(capabilities=probes).review(eat_plan(), planner_observation())
        assert verdict.rule is CriticRule.CAPABILITY_UNUSABLE
        assert verdict.requires_grant is False

    def test_an_unverified_capability_is_enough_for_a_user_directed_plan(self) -> None:
        probes = CapabilitySnapshot.from_mapping(
            {EAT_PERCENTAGE: CapabilityState.AVAILABLE_UNVERIFIED}
        )
        assert critic(capabilities=probes).review(eat_plan(), planner_observation()).approved

    def test_an_unverified_capability_is_not_enough_unattended(self) -> None:
        probes = CapabilitySnapshot.from_mapping(
            {EAT_PERCENTAGE: CapabilityState.AVAILABLE_UNVERIFIED}
        )
        verdict = critic(
            capabilities=probes,
            initiative=Initiative.SELF_DIRECTED,
            home=Square(x=1200, y=3400, z=0),
        ).review(eat_plan(), _autonomous())
        assert verdict.rule is CriticRule.CAPABILITY_UNUSABLE
        assert verdict.requires_grant is True


class TestPermission:
    def test_an_observation_mode_refuses_a_world_changing_step(self) -> None:
        observation = planner_observation(safety=make_safety(mode=SessionMode.OBSERVE))
        verdict = critic().review(eat_plan(), observation)
        assert verdict.rule is CriticRule.PERMISSION_DENIED
        assert verdict.reason_code is ReasonCode.POLICY_DENIED

    def test_the_permission_engine_answers_rather_than_a_second_copy_of_it(self) -> None:
        # OFF is the ladder's own first gate; the critic reports its code
        # verbatim instead of collapsing every refusal to POLICY_DENIED.
        observation = planner_observation(safety=make_safety(mode=SessionMode.OFF))
        verdict = critic().review(eat_plan(), observation)
        assert verdict.reason_code is ReasonCode.NOT_ARMED

    def test_a_p3_step_is_refused_on_the_agents_own_initiative(self) -> None:
        world_item = food_item("tin", container_ref=WORLD_REF)
        plan = eat_plan(
            PlanStep(
                step_id="s1",
                action=ActionName.INVENTORY_TRANSFER,
                args=TransferArgs(item_ref=world_item.ref, destination_container_ref=MAIN_REF),
                success=SuccessCriterion(kind=SuccessKind.ITEM_IN_MAIN_INVENTORY),
            ),
        )
        observation = _autonomous(
            inventory=inventory(world_item, containers=_containers_with_world())
        )
        verdict = critic(
            initiative=Initiative.SELF_DIRECTED,
            home=Square(x=1200, y=3400, z=0),
            capabilities=CapabilitySnapshot.verified(INVENTORY_TRANSFER),
        ).review(plan, observation)
        assert verdict.rule is CriticRule.PERMISSION_DENIED
        assert "P3" in verdict.detail


class TestForeignReference:
    def test_a_reference_from_another_session_is_refused(self) -> None:
        plan = eat_plan(eat_step(args=ConsumeArgs(item_ref=foreign_item_ref())))
        verdict = critic().review(plan, planner_observation())
        assert verdict.rule is CriticRule.FOREIGN_REF
        assert verdict.reason_code is ReasonCode.INVALID_REF

    def test_a_foreign_container_in_a_transfer_is_refused_too(self) -> None:
        plan = eat_plan(
            PlanStep(
                step_id="s1",
                action=ActionName.INVENTORY_TRANSFER,
                args=TransferArgs(
                    item_ref=item_ref("beans"),
                    destination_container_ref="container:00000000-0000-0000-0000-00000000a11e"
                    ":player-main",
                ),
                success=SuccessCriterion(kind=SuccessKind.ITEM_IN_MAIN_INVENTORY),
            )
        )
        verdict = critic().review(plan, planner_observation())
        assert verdict.rule is CriticRule.FOREIGN_REF
        assert "container reference" in verdict.detail

    def test_the_session_check_runs_before_the_permission_ladder(self) -> None:
        # An unusable capability *and* a foreign ref: the reference is the more
        # fundamental problem, and it is what gets reported.
        plan = eat_plan(eat_step(args=ConsumeArgs(item_ref=foreign_item_ref())))
        verdict = critic(capabilities=NO_PROBES).review(plan, planner_observation())
        assert verdict.rule is CriticRule.FOREIGN_REF


class TestUnresolvableStep:
    def test_a_transfer_whose_item_is_not_in_the_observation_is_refused(self) -> None:
        plan = eat_plan(
            PlanStep(
                step_id="s1",
                action=ActionName.INVENTORY_TRANSFER,
                args=TransferArgs(item_ref=item_ref("ghost"), destination_container_ref=MAIN_REF),
                success=SuccessCriterion(kind=SuccessKind.ITEM_IN_MAIN_INVENTORY),
            )
        )
        verdict = critic().review(plan, planner_observation())
        assert verdict.rule is CriticRule.UNRESOLVABLE_STEP
        assert verdict.reason_code is ReasonCode.INVALID_REF

    def test_the_adapters_that_read_their_arguments_are_the_ones_asked(self) -> None:
        registry = full_registry()
        assessing = [
            action
            for action in (
                ActionName.INVENTORY_TRANSFER,
                ActionName.MOVEMENT_MOVE_TO,
                ActionName.MOVEMENT_MOVE_NEAR,
            )
            if isinstance(registry.get(action), RiskAssessor)
        ]
        assert len(assessing) == 3
        assert not isinstance(registry.get(ActionName.CONSUME_EAT), RiskAssessor)


class TestHomeRadius:
    def test_a_user_directed_plan_may_travel_anywhere(self) -> None:
        # §7.10 bounds what the agent starts unasked; being told to go is the
        # explicit goal that clause exempts.
        plan = eat_plan(move_step(args=MoveToArgs(x=9999, y=9999, z=0)))
        assert critic().review(plan, planner_observation()).approved

    def test_an_autonomous_plan_outside_the_radius_is_refused(self) -> None:
        plan = eat_plan(move_step(args=MoveToArgs(x=1200 + DEFAULT_HOME_RADIUS + 1, y=3400, z=0)))
        verdict = _autonomous_critic().review(plan, _autonomous())
        assert verdict.rule is CriticRule.OUTSIDE_HOME_RADIUS
        assert verdict.reason_code is ReasonCode.TARGET_OUT_OF_RANGE

    def test_an_autonomous_plan_inside_the_radius_is_approved(self) -> None:
        plan = eat_plan(move_step(args=MoveToArgs(x=1200 + DEFAULT_HOME_RADIUS, y=3400, z=0)))
        assert _autonomous_critic().review(plan, _autonomous()).approved

    def test_a_floor_change_is_not_zero_squares_away(self) -> None:
        plan = eat_plan(move_step(args=MoveToArgs(x=1200, y=3400, z=1, allow_stairs=True)))
        verdict = _autonomous_critic().review(plan, _autonomous())
        assert verdict.rule is CriticRule.OUTSIDE_HOME_RADIUS

    def test_travelling_with_no_home_point_is_refused_rather_than_allowed(self) -> None:
        plan = eat_plan(move_step())
        verdict = _autonomous_critic(home=None).review(plan, _autonomous())
        assert verdict.rule is CriticRule.OUTSIDE_HOME_RADIUS
        assert verdict.reason_code is ReasonCode.PRECONDITION_FAILED

    def test_an_object_the_observation_cannot_place_is_refused_not_waved_past(self) -> None:
        plan = eat_plan(
            PlanStep(
                step_id="s1",
                action=ActionName.MOVEMENT_MOVE_NEAR,
                args=MoveNearArgs(object_ref=OBJECT_REF),
                success=SuccessCriterion(kind=SuccessKind.ADAPTER_EVIDENCE),
            )
        )
        verdict = _autonomous_critic().review(plan, _autonomous(nearby=NearbyView()))
        assert verdict.rule is CriticRule.OUTSIDE_HOME_RADIUS

    def test_a_placed_object_inside_the_radius_is_approved(self) -> None:
        nearby = NearbyView(
            objects=[
                NearbyObject(
                    ref=OBJECT_REF,
                    kind="container",
                    distance=3.0,
                    position=Position(x=1203.0, y=3400.0, z=0),
                )
            ]
        )
        plan = eat_plan(
            PlanStep(
                step_id="s1",
                action=ActionName.MOVEMENT_MOVE_NEAR,
                args=MoveNearArgs(object_ref=OBJECT_REF),
                success=SuccessCriterion(kind=SuccessKind.ADAPTER_EVIDENCE),
            )
        )
        assert _autonomous_critic().review(plan, _autonomous(nearby=nearby)).approved

    def test_a_radius_beyond_the_product_ceiling_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match=r"home_radius must be within 1\.\.100"):
            critic(home_radius=101)


class TestRepeatedFailure:
    def test_a_step_that_just_failed_non_retryably_is_refused(self) -> None:
        plan = eat_plan()
        history = (
            StepFailure(
                signature=plan.steps[0].signature,
                reason_code=ReasonCode.INVALID_REF,
                detail="the tin was gone",
            ),
        )
        verdict = critic().review(plan, planner_observation(), recent_failures=history)
        assert verdict.rule is CriticRule.REPEATED_FAILURE
        assert verdict.reason_code is ReasonCode.PRECONDITION_FAILED

    def test_a_step_that_failed_retryably_may_be_proposed_again(self) -> None:
        plan = eat_plan()
        history = (
            StepFailure(
                signature=plan.steps[0].signature,
                reason_code=ReasonCode.PATH_STUCK,
                detail="the path was blocked",
            ),
        )
        assert critic().review(plan, planner_observation(), recent_failures=history).approved

    def test_a_different_step_is_not_the_failed_one(self) -> None:
        failed = eat_step(args=ConsumeArgs(item_ref=item_ref("beans"), fraction=0.5))
        history = (StepFailure(signature=failed.signature, reason_code=ReasonCode.POLICY_DENIED),)
        assert critic().review(eat_plan(), planner_observation(), recent_failures=history).approved


class TestVerdict:
    def test_an_approval_may_not_carry_a_rule(self) -> None:
        with pytest.raises(ValueError, match="names the rule it broke"):
            CriticVerdict(approved=True, detail="fine", rule=CriticRule.STEP_CAP_EXCEEDED)

    def test_a_refusal_must_carry_a_reason_code(self) -> None:
        with pytest.raises(ValueError, match="carries a reason code"):
            CriticVerdict(approved=False, detail="no", rule=CriticRule.FOREIGN_REF)

    def test_a_verdict_must_explain_itself(self) -> None:
        with pytest.raises(ValueError, match="must explain itself"):
            CriticVerdict(approved=True, detail="  ")

    def test_an_approved_plan_is_not_waiting_on_a_grant(self) -> None:
        with pytest.raises(ValueError, match="not waiting on a grant"):
            CriticVerdict(approved=True, detail="fine", requires_grant=True)


def _containers_with_world() -> list[ContainerView]:
    return [main_container(), backpack_container(), world_container()]


def _autonomous(**overrides: object) -> Observation:
    overrides.setdefault("safety", make_safety(mode=SessionMode.AUTONOMOUS, armed=True))
    return planner_observation(**overrides)


def _autonomous_critic(**overrides: object) -> PlanCritic:
    settings: dict[str, object] = {
        "initiative": Initiative.SELF_DIRECTED,
        "home": Square(x=1200, y=3400, z=0),
        "capabilities": CapabilitySnapshot.verified(MOVE_TO_SQUARE),
        "permissions": MOVEMENT_GRANTED,
    }
    settings.update(overrides)
    return critic(**settings)


def test_the_ensure_main_step_of_a_two_step_plan_is_reviewed_too() -> None:
    in_bag = food_item("beans", container_ref=BACKPACK_REF)
    plan = eat_plan(
        PlanStep(
            step_id="s1",
            action=ActionName.INVENTORY_ENSURE_MAIN,
            args=ItemArgs(item_ref=in_bag.ref),
            success=SuccessCriterion(kind=SuccessKind.ITEM_IN_MAIN_INVENTORY),
        ),
        eat_step("s2", args=ConsumeArgs(item_ref=in_bag.ref)),
    )
    observation = planner_observation(inventory=inventory(in_bag))
    # `inventory.transfer` is what backs ensure_main, and its probe is off here.
    probes = CapabilitySnapshot.verified(EAT_PERCENTAGE)
    verdict = critic(capabilities=probes).review(plan, observation)
    assert verdict.rule is CriticRule.CAPABILITY_UNUSABLE
    assert verdict.step_id == "s1"


class TestAnUnobservedItemReference:
    """AGENTS.md: the model may emit a typed plan "and nothing else … no raw
    refs it invented". The plan type enforces most of that structurally and
    ``_foreign_ref`` catches another session's reference. A well-formed
    reference from *this* session naming an item nobody ever reported was
    approved — measured — and the executor then skipped the step as already
    satisfied, because ``item_consumed`` is satisfied by absence and the skip
    check runs before ``_ref_gate``. A hallucinated reference produced a
    finished plan and a silent nothing.
    """

    @staticmethod
    def _eat(ref: str) -> Plan:
        return Plan(
            goal_id="00000000-0000-0000-0000-000000000001",
            summary="eat",
            steps=(
                PlanStep(
                    step_id="s1",
                    action=ActionName.CONSUME_EAT,
                    args=ConsumeArgs(item_ref=ref),
                    success=SuccessCriterion(kind=SuccessKind.ITEM_CONSUMED),
                ),
            ),
        )

    def test_an_item_the_observation_never_carried_is_refused(self) -> None:

        verdict = critic().review(self._eat(planner_item_ref("999999")), planner_observation())

        assert verdict.refused
        assert verdict.rule is CriticRule.UNOBSERVED_REF
        assert verdict.reason_code is ReasonCode.INVALID_REF

    def test_an_item_the_observation_carries_is_approved(self) -> None:
        """The control: without it a gate that refused every plan would pass above."""

        verdict = critic().review(self._eat(planner_item_ref("beans")), planner_observation())

        assert verdict.approved

    def test_a_gap_in_observation_is_not_evidence_that_the_item_is_gone(self) -> None:
        """No inventory tier means the mod sent none this tick, not that it vanished.

        Refusing here would refuse every plan made during a gap, which is the
        opposite mistake and the one the executor's own ref gate avoids the same
        way.
        """

        verdict = critic().review(
            self._eat(planner_item_ref("999999")), _make_observation(inventory=None)
        )

        assert verdict.approved
