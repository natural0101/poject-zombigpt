"""The self-directed gate: when the agent acts unasked, and when it must not."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pz_agent_core.memory import SaveMemory, Square
from pz_agent_core.policy.autonomy import (
    DEFAULT_AUTONOMY_CONFIG,
    MAX_ACTIONS_PER_INTERVAL,
    MAX_HOME_RADIUS,
    MAX_PLAN_STEPS,
    NEED_BLEEDING,
    NEED_BOREDOM,
    NEED_FATIGUE,
    NEED_HUNGER,
    NEED_PLANS,
    NEED_THIRST,
    NO_MEMORY,
    AutonomyConfig,
    AutonomyDecision,
    AutonomyGate,
    AutonomyMemory,
    AutonomyOutcome,
    BackupEvidence,
    NeedPlan,
    derive_needs,
)
from pz_agent_core.policy.config import DEFAULT_POLICY_CONFIG
from pz_agent_core.policy.selection import CapabilitySnapshot
from pz_agent_core.protocol import (
    ActionName,
    ActionOwnership,
    CapabilityState,
    DangerLevel,
    ItemView,
    Observation,
    Priority,
    ReasonCode,
    RiskClass,
    SessionMode,
    Wound,
)
from pz_agent_core.safety.priority import AntiLoopConfig, Arbitration, RunningPlan
from tests.fixtures import DEFAULT_SAVE, DEFAULT_SESSION, make_action_state, make_safety
from tests.fixtures.autonomy_worlds import (
    NOW_MS,
    FakeMemory,
    autonomous_observation,
    calm_player,
    verified_backup,
)
from tests.fixtures.policy_items import (
    drink_item,
    food_item,
    hungry_player,
    inventory,
    literature_item,
    thirsty_player,
)

CAPABILITIES = CapabilitySnapshot.verified("eat_percentage", "drink_carried", "read_literature")

#: Computed once so that passing ``backup=None`` to :func:`_evaluate` means what
#: it says — no backup exists — rather than "use the default".
BACKUP = verified_backup()

BLEEDING_WOUND = Wound(ref=f"wound:{DEFAULT_SESSION}:arm", kind="cut", severity=0.7, bleeding=True)


def _hungry_world(
    *,
    hunger: float = 0.75,
    items: tuple[ItemView, ...] | None = None,
    **overrides: Any,
) -> Observation:
    """An armed AUTONOMOUS session with a hungry character and food to hand."""
    chosen = (food_item("f1"),) if items is None else items
    overrides.setdefault("player", hungry_player(hunger))
    return autonomous_observation(inventory=inventory(*chosen), **overrides)


def _evaluate(
    gate: AutonomyGate,
    observation: Observation,
    *,
    now_ms: int = NOW_MS,
    backup: BackupEvidence | None = BACKUP,
    memory: AutonomyMemory = NO_MEMORY,
    running: RunningPlan | None = None,
) -> AutonomyDecision:
    return gate.evaluate(
        observation,
        now_ms=now_ms,
        backup=backup,
        capabilities=CAPABILITIES,
        memory=memory,
        running=running,
    )


# --------------------------------------------------------------------------
# needs derived from the observation
# --------------------------------------------------------------------------


def test_a_calm_character_reports_no_need() -> None:
    assert derive_needs(autonomous_observation(player=calm_player())) == ()


def test_hunger_below_its_trigger_is_not_a_need() -> None:
    assert derive_needs(autonomous_observation(player=hungry_player(0.30))) == ()


def test_ordinary_hunger_is_maintenance_and_may_not_interrupt() -> None:
    (need,) = derive_needs(autonomous_observation(player=hungry_player(0.50)))

    assert need.key == NEED_HUNGER
    assert need.priority is Priority.BASE_MAINTENANCE
    assert not need.preemptive


def test_critical_hunger_outranks_a_user_plan_and_may_interrupt() -> None:
    (need,) = derive_needs(autonomous_observation(player=hungry_player(0.80)))

    assert need.priority is Priority.CRITICAL_HUNGER
    assert need.priority < Priority.USER_COMMAND
    assert need.preemptive


def test_bleeding_is_reported_above_every_other_need() -> None:
    player = hungry_player(0.9, wounds=[BLEEDING_WOUND])
    needs = derive_needs(autonomous_observation(player=player))

    assert needs[0].key == NEED_BLEEDING
    assert needs[0].priority is Priority.CRITICAL_BLEEDING


def test_critical_fatigue_is_a_need_of_its_own() -> None:
    needs = derive_needs(autonomous_observation(player=calm_player(fatigue=0.85)))

    assert [need.key for need in needs] == [NEED_FATIGUE]


def test_boredom_comes_from_the_moodle_the_configuration_names() -> None:
    player = calm_player(moodles={"Bored": 4})
    needs = derive_needs(autonomous_observation(player=player))

    assert [need.key for need in needs] == [NEED_BOREDOM]


def test_a_boredom_moodle_below_the_trigger_is_not_a_need() -> None:
    player = calm_player(moodles={"Bored": 1})

    assert derive_needs(autonomous_observation(player=player)) == ()


def test_the_state_signature_is_quantised_so_drift_is_not_progress() -> None:
    """Raw floats move every tick; a signature that followed them never repeats."""
    first = derive_needs(autonomous_observation(player=thirsty_player(0.5001)))
    second = derive_needs(autonomous_observation(player=thirsty_player(0.5049)))

    assert first[0].key == NEED_THIRST
    assert first[0].state_signature == second[0].state_signature


# --------------------------------------------------------------------------
# the session gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    [
        SessionMode.OFF,
        SessionMode.OBSERVE,
        SessionMode.ASSISTED,
        SessionMode.REFLEX_ONLY,
        SessionMode.EXPERIMENTAL_INPUT,
    ],
)
def test_only_autonomous_acts_unasked(mode: SessionMode) -> None:
    decision = _evaluate(AutonomyGate(), _hungry_world(safety=make_safety(mode=mode)))

    assert decision.outcome is AutonomyOutcome.REFUSE
    assert decision.reason_code is ReasonCode.POLICY_DENIED


@pytest.mark.parametrize(
    "flags",
    [{"armed": False}, {"manual_takeover": True}, {"sidecar_stale": True}],
)
def test_autonomy_refuses_unless_armed_and_attached(flags: dict[str, bool]) -> None:
    safety = make_safety(mode=SessionMode.AUTONOMOUS, **flags)
    decision = _evaluate(AutonomyGate(), _hungry_world(safety=safety))

    assert decision.outcome is AutonomyOutcome.REFUSE
    assert decision.reason_code is ReasonCode.NOT_ARMED


def test_a_dead_character_ends_autonomy() -> None:
    decision = _evaluate(AutonomyGate(), _hungry_world(player=calm_player(alive=False)))

    assert decision.reason_code is ReasonCode.SESSION_TERMINATED


# --------------------------------------------------------------------------
# autonomy depends on a backup existing
# --------------------------------------------------------------------------


def test_without_a_backup_autonomy_asks_instead_of_acting() -> None:
    decision = _evaluate(AutonomyGate(), _hungry_world(), backup=None)

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.PRECONDITION_FAILED
    assert "no backup" in decision.detail


def test_a_backup_of_another_save_is_not_a_safety_net_for_this_one() -> None:
    other = BackupEvidence(save_id="some other save", created_at_ms=1, verified=True)
    decision = _evaluate(AutonomyGate(), _hungry_world(), backup=other)

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.SAVE_CHANGED


def test_an_unverified_backup_is_refused_by_default() -> None:
    unverified = BackupEvidence(save_id=DEFAULT_SAVE, created_at_ms=1, verified=False)
    decision = _evaluate(AutonomyGate(), _hungry_world(), backup=unverified)

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert "never been verified" in decision.detail


def test_an_unverified_backup_can_be_accepted_by_configuration() -> None:
    gate = AutonomyGate(config=AutonomyConfig(require_verified_backup=False))
    unverified = BackupEvidence(save_id=DEFAULT_SAVE, created_at_ms=1, verified=False)

    assert _evaluate(gate, _hungry_world(), backup=unverified).should_act


def test_backup_evidence_must_name_its_save() -> None:
    with pytest.raises(ValueError, match="must name the save"):
        BackupEvidence(save_id="", created_at_ms=1)


# --------------------------------------------------------------------------
# the tick gate
# --------------------------------------------------------------------------


def test_a_manual_action_holds_the_tick() -> None:
    busy = make_action_state(ownership=ActionOwnership.MANUAL, busy=True)
    decision = _evaluate(AutonomyGate(), _hungry_world(action=busy))

    assert decision.outcome is AutonomyOutcome.HOLD
    assert decision.reason_code is ReasonCode.PLAYER_BUSY_MANUAL_ACTION


def test_danger_leaves_the_tick_to_the_reflex_guard() -> None:
    safety = make_safety(mode=SessionMode.AUTONOMOUS, danger_level=DangerLevel.HIGH)
    decision = _evaluate(AutonomyGate(), _hungry_world(safety=safety))

    assert decision.outcome is AutonomyOutcome.HOLD
    assert decision.reason_code is ReasonCode.THREAT_INTERRUPTED


def test_nothing_to_do_is_a_hold_not_a_refusal() -> None:
    decision = _evaluate(AutonomyGate(), autonomous_observation(player=calm_player()))

    assert decision.outcome is AutonomyOutcome.HOLD
    assert decision.need is None


# --------------------------------------------------------------------------
# acting
# --------------------------------------------------------------------------


def test_hunger_with_food_in_the_bag_is_acted_on() -> None:
    decision = _evaluate(AutonomyGate(), _hungry_world())

    assert decision.outcome is AutonomyOutcome.ACT
    assert decision.plan is not None
    assert decision.plan.actions == (ActionName.CONSUME_EAT,)
    assert decision.item_ref is not None


def test_thirst_defers_the_item_choice_to_the_drink_policy() -> None:
    observation = autonomous_observation(
        player=thirsty_player(0.8), inventory=inventory(drink_item("d1"))
    )
    decision = _evaluate(AutonomyGate(), observation)

    assert decision.outcome is AutonomyOutcome.ACT
    assert decision.plan is not None
    assert decision.plan.actions == (ActionName.CONSUME_DRINK,)


def test_boredom_reads_something() -> None:
    observation = autonomous_observation(
        player=calm_player(moodles={"Bored": 4}), inventory=inventory(literature_item("b1"))
    )
    decision = _evaluate(AutonomyGate(), observation)

    assert decision.outcome is AutonomyOutcome.ACT
    assert decision.plan is not None
    assert decision.plan.actions == (ActionName.LITERATURE_READ,)


def test_an_empty_bag_holds_with_the_policys_own_reason() -> None:
    observation = autonomous_observation(player=hungry_player(0.8), inventory=inventory())
    decision = _evaluate(AutonomyGate(), observation)

    assert decision.outcome is AutonomyOutcome.HOLD
    assert decision.reason_code is ReasonCode.NO_SAFE_FOOD


def test_a_missing_inventory_tier_holds_rather_than_guessing() -> None:
    observation = autonomous_observation(player=hungry_player(0.8), inventory=None)
    decision = _evaluate(AutonomyGate(), observation)

    assert decision.outcome is AutonomyOutcome.HOLD
    assert decision.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


# --------------------------------------------------------------------------
# needs with no plan
# --------------------------------------------------------------------------


def test_bleeding_escalates_because_there_is_no_verified_action_for_it() -> None:
    observation = autonomous_observation(player=calm_player(wounds=[BLEEDING_WOUND]))
    decision = _evaluate(AutonomyGate(), observation)

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert decision.need is not None
    assert decision.need.key == NEED_BLEEDING


def test_the_registry_only_holds_needs_the_agent_can_actually_serve() -> None:
    assert set(NEED_PLANS) == {NEED_THIRST, NEED_HUNGER, NEED_BOREDOM}


# --------------------------------------------------------------------------
# resource reservation (§7.9)
# --------------------------------------------------------------------------


def test_a_reserved_item_type_is_never_consumed_autonomously() -> None:
    memory = FakeMemory(reserved=frozenset({"Base.TinnedBeans"}))
    decision = _evaluate(AutonomyGate(), _hungry_world(), memory=memory)

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.RESOURCE_RESERVED
    assert decision.item_ref is None


def test_a_reserved_item_does_not_block_an_unreserved_one() -> None:
    memory = FakeMemory(reserved=frozenset({"Base.Whiskey"}))
    items = (food_item("f1"), food_item("f2", full_type="Base.Whiskey", display_name="Whiskey"))
    decision = _evaluate(AutonomyGate(), _hungry_world(items=items), memory=memory)

    assert decision.outcome is AutonomyOutcome.ACT
    assert decision.item_ref is not None
    assert ":f1:" in decision.item_ref


def test_a_favourited_item_is_reported_as_reserved_rather_than_as_missing() -> None:
    """The selection policy already refuses it; autonomy turns that into a question."""
    decision = _evaluate(AutonomyGate(), _hungry_world(items=(food_item("f1", favorite=True),)))

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.RESOURCE_RESERVED


def test_a_reserved_item_that_could_not_serve_the_need_is_not_blamed_for_it() -> None:
    """A reserved book is not an answer to hunger.

    "Everything that would serve it is reserved, say the word" reads as a fact:
    saying the word has to actually produce something to eat.
    """
    book = literature_item("b1")
    observation = autonomous_observation(
        player=hungry_player(0.80), inventory=inventory(book, drink_item("d1"))
    )
    memory = FakeMemory(reserved=frozenset({book.full_type, "Base.WaterBottleFull"}))

    decision = _evaluate(AutonomyGate(), observation, memory=memory)

    assert decision.outcome is AutonomyOutcome.HOLD
    assert decision.reason_code is ReasonCode.NO_SAFE_FOOD
    assert "reserved" not in decision.detail


def test_a_reserved_item_that_is_also_inedible_is_not_offered_either() -> None:
    """Lifting the reservation would still leave nothing safe, so it is not asked about."""
    rotten = food_item("f1", rot_progress=1.0, freshness="rotten")
    memory = FakeMemory(reserved=frozenset({rotten.full_type}))

    decision = _evaluate(AutonomyGate(), _hungry_world(items=(rotten,)), memory=memory)

    assert decision.outcome is AutonomyOutcome.HOLD
    assert decision.reason_code is ReasonCode.NO_SAFE_FOOD


def test_a_reserve_the_rejection_list_had_to_trim_is_still_asked_about() -> None:
    """The reported rejections are bounded, so they cannot be the only signal."""
    tight = replace(DEFAULT_POLICY_CONFIG, max_reported_rejections=1)
    rotten = tuple(
        food_item(f"r{index}", rot_progress=1.0, freshness="rotten") for index in range(4)
    )
    good = food_item("keep", favorite=True)
    gate = AutonomyGate(policy=tight)

    decision = _evaluate(gate, _hungry_world(items=(*rotten, good)))

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.RESOURCE_RESERVED


def test_the_real_memory_store_satisfies_the_protocol_the_gate_declares() -> None:
    memory = SaveMemory(DEFAULT_SAVE)
    memory.reserve(full_type="Base.TinnedBeans", reason="for the road", now_ms=NOW_MS)

    assert isinstance(memory, AutonomyMemory)
    assert _evaluate(AutonomyGate(), _hungry_world(), memory=memory).reason_code is (
        ReasonCode.RESOURCE_RESERVED
    )


# --------------------------------------------------------------------------
# anti-loop
# --------------------------------------------------------------------------


def _loop_gate() -> AutonomyGate:
    """A gate whose action budget cannot be what stops a loop test."""
    return AutonomyGate(config=AutonomyConfig(max_actions_per_interval=MAX_ACTIONS_PER_INTERVAL))


def test_a_need_that_keeps_firing_without_improving_is_escalated() -> None:
    """Two triggers ask for a different strategy; the third stops and asks."""
    gate = _loop_gate()
    observation = _hungry_world()

    first = _evaluate(gate, observation, now_ms=NOW_MS)
    second = _evaluate(gate, observation, now_ms=NOW_MS + 1_000)
    third = _evaluate(gate, observation, now_ms=NOW_MS + 2_000)

    assert first.should_act
    assert second.should_act
    assert second.arbitration is not None
    assert second.arbitration.alternate_strategy
    assert third.outcome is AutonomyOutcome.ASK_USER
    assert third.reason_code is ReasonCode.NO_PROGRESS


def test_after_escalating_the_need_is_suppressed_rather_than_retried() -> None:
    gate = _loop_gate()
    observation = _hungry_world()
    for tick in range(3):
        _evaluate(gate, observation, now_ms=NOW_MS + tick * 1_000)

    fourth = _evaluate(gate, observation, now_ms=NOW_MS + 3_000)

    assert fourth.outcome is AutonomyOutcome.HOLD
    assert fourth.arbitration is not None
    assert fourth.arbitration.suppressed


def test_a_need_whose_state_improved_is_not_a_loop() -> None:
    gate = _loop_gate()
    decision = None

    for tick, hunger in enumerate((0.90, 0.80, 0.70, 0.60)):
        decision = _evaluate(gate, _hungry_world(hunger=hunger), now_ms=NOW_MS + tick * 1_000)

    assert decision is not None
    assert decision.should_act


def test_the_escalation_threshold_is_configurable() -> None:
    gate = AutonomyGate(
        config=AutonomyConfig(max_actions_per_interval=MAX_ACTIONS_PER_INTERVAL),
        anti_loop=AntiLoopConfig(alternate_after=1, escalate_after=1),
    )

    assert _evaluate(gate, _hungry_world()).outcome is AutonomyOutcome.ASK_USER


def test_a_need_the_caller_saw_resolved_can_be_cleared() -> None:
    gate = _loop_gate()
    observation = _hungry_world()
    _evaluate(gate, observation, now_ms=NOW_MS)
    _evaluate(gate, observation, now_ms=NOW_MS + 1_000)
    gate.arbiter.note_resolved(NEED_HUNGER)

    assert _evaluate(gate, observation, now_ms=NOW_MS + 2_000).should_act


def test_a_running_user_plan_is_not_interrupted_by_maintenance_hunger() -> None:
    running = RunningPlan(key="build-a-wall", priority=Priority.USER_COMMAND)
    decision = _evaluate(AutonomyGate(), _hungry_world(hunger=0.50), running=running)

    assert decision.outcome is AutonomyOutcome.HOLD
    assert decision.arbitration is not None
    assert decision.arbitration.outcome is Arbitration.CONTINUE


def test_critical_hunger_does_suspend_a_user_plan() -> None:
    running = RunningPlan(key="build-a-wall", priority=Priority.USER_COMMAND)
    decision = _evaluate(AutonomyGate(), _hungry_world(hunger=0.85), running=running)

    assert decision.should_act
    assert decision.arbitration is not None
    assert decision.arbitration.outcome is Arbitration.SUSPEND


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------


def test_the_action_budget_is_spent_and_then_refills() -> None:
    gate = AutonomyGate(config=AutonomyConfig(max_actions_per_interval=2, interval_ms=60_000))
    worlds = [_hungry_world(hunger=h) for h in (0.90, 0.80, 0.70)]

    assert _evaluate(gate, worlds[0], now_ms=NOW_MS).should_act
    assert _evaluate(gate, worlds[1], now_ms=NOW_MS + 1_000).should_act
    spent = _evaluate(gate, worlds[2], now_ms=NOW_MS + 2_000)

    assert spent.outcome is AutonomyOutcome.HOLD
    assert spent.reason_code is ReasonCode.POLICY_DENIED
    assert gate.actions_spent(NOW_MS + 2_000) == 2

    assert _evaluate(gate, worlds[2], now_ms=NOW_MS + 120_000).should_act
    assert gate.actions_spent(NOW_MS + 120_000) == 1


def test_only_an_act_charges_the_budget() -> None:
    gate = AutonomyGate()
    _evaluate(gate, autonomous_observation(player=calm_player()))

    assert gate.actions_spent(NOW_MS) == 0


def test_a_plan_longer_than_the_bound_is_escalated_rather_than_started() -> None:
    long_plan = NeedPlan(
        need_key=NEED_HUNGER,
        actions=(ActionName.INVENTORY_TRANSFER, ActionName.CONSUME_EAT),
        risk=RiskClass.P2,
        capability=None,
    )
    gate = AutonomyGate(config=AutonomyConfig(max_plan_steps=1), plans={NEED_HUNGER: long_plan})
    decision = _evaluate(gate, _hungry_world())

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.POLICY_DENIED
    assert "start at most 1" in decision.detail


def test_a_plan_may_not_exceed_the_hard_step_ceiling() -> None:
    with pytest.raises(ValueError, match=f"at most {MAX_PLAN_STEPS} steps"):
        NeedPlan(
            need_key=NEED_HUNGER,
            actions=tuple([ActionName.ACTION_WAIT] * (MAX_PLAN_STEPS + 1)),
            risk=RiskClass.P1,
            capability=None,
        )


def test_a_plan_must_name_an_action() -> None:
    with pytest.raises(ValueError, match="at least one action"):
        NeedPlan(need_key=NEED_HUNGER, actions=(), risk=RiskClass.P1, capability=None)


def test_a_plan_filed_under_the_wrong_need_is_refused() -> None:
    with pytest.raises(ValueError, match="plans are keyed by need"):
        AutonomyGate(plans={NEED_THIRST: NEED_PLANS[NEED_HUNGER]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("home_radius", 0),
        ("home_radius", MAX_HOME_RADIUS + 1),
        ("max_plan_steps", MAX_PLAN_STEPS + 1),
        ("max_actions_per_interval", MAX_ACTIONS_PER_INTERVAL + 1),
        ("interval_ms", 0),
        ("critical_fatigue", 0.0),
        ("critical_fatigue", 1.5),
        ("boredom_moodle", "  "),
    ],
)
def test_the_configuration_refuses_an_unbounded_value(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        AutonomyConfig(**{field: value})  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# the home radius (§7.10)
# --------------------------------------------------------------------------

_TRAVELLING_PLAN = NeedPlan(
    need_key=NEED_HUNGER,
    actions=(ActionName.CONSUME_EAT,),
    risk=RiskClass.P2,
    capability=None,
    consumable=None,
    requires_travel=True,
)


def test_a_plan_that_travels_needs_a_home_point_to_stay_inside() -> None:
    gate = AutonomyGate(plans={NEED_HUNGER: _TRAVELLING_PLAN})
    decision = _evaluate(gate, _hungry_world(), memory=NO_MEMORY)

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_plan_that_travels_is_refused_outside_the_home_radius() -> None:
    gate = AutonomyGate(
        plans={NEED_HUNGER: _TRAVELLING_PLAN}, config=AutonomyConfig(home_radius=10)
    )
    decision = _evaluate(gate, _hungry_world(), memory=FakeMemory(home=Square(x=1000, y=3400, z=0)))

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.TARGET_OUT_OF_RANGE


def test_a_plan_that_travels_runs_inside_the_home_radius() -> None:
    gate = AutonomyGate(
        plans={NEED_HUNGER: _TRAVELLING_PLAN}, config=AutonomyConfig(home_radius=10)
    )
    at_home = FakeMemory(home=Square(x=1200, y=3400, z=0))

    assert _evaluate(gate, _hungry_world(), memory=at_home).should_act


def test_a_home_point_on_another_floor_is_not_zero_squares_away() -> None:
    gate = AutonomyGate(plans={NEED_HUNGER: _TRAVELLING_PLAN})
    upstairs = FakeMemory(home=Square(x=1200, y=3400, z=1))

    assert _evaluate(gate, _hungry_world(), memory=upstairs).reason_code is (
        ReasonCode.TARGET_OUT_OF_RANGE
    )


def test_eating_is_not_bounded_by_the_radius_because_it_does_not_travel() -> None:
    far_from_home = FakeMemory(home=Square(x=1, y=1, z=0))

    assert _evaluate(AutonomyGate(), _hungry_world(), memory=far_from_home).should_act


# --------------------------------------------------------------------------
# permission is enforced, not restated
# --------------------------------------------------------------------------


def test_an_unverified_capability_stops_the_agent_and_asks() -> None:
    decision = AutonomyGate().evaluate(
        _hungry_world(),
        now_ms=NOW_MS,
        backup=BACKUP,
        capabilities=CapabilitySnapshot.from_mapping(
            {"eat_percentage": CapabilityState.AVAILABLE_UNVERIFIED}
        ),
    )

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_an_unprobed_capability_holds_without_asking() -> None:
    decision = AutonomyGate().evaluate(_hungry_world(), now_ms=NOW_MS, backup=BACKUP)

    assert decision.outcome is AutonomyOutcome.HOLD
    assert decision.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_a_p3_plan_is_refused_autonomously_by_the_permission_ladder() -> None:
    plan = NeedPlan(
        need_key=NEED_HUNGER,
        actions=(ActionName.INVENTORY_TRANSFER,),
        risk=RiskClass.P3,
        capability=None,
    )
    decision = _evaluate(AutonomyGate(plans={NEED_HUNGER: plan}), _hungry_world())

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.POLICY_DENIED


# --------------------------------------------------------------------------
# the decision value itself
# --------------------------------------------------------------------------


def test_an_act_decision_must_name_the_need_and_the_plan() -> None:
    with pytest.raises(ValueError, match="must name the need"):
        AutonomyDecision(outcome=AutonomyOutcome.ACT, detail="go")


def test_a_refusal_must_carry_a_reason_code() -> None:
    with pytest.raises(ValueError, match="must carry a reason code"):
        AutonomyDecision(outcome=AutonomyOutcome.REFUSE, detail="no")


def test_a_decision_must_explain_itself() -> None:
    with pytest.raises(ValueError, match="must explain itself"):
        AutonomyDecision(outcome=AutonomyOutcome.HOLD, detail=" ")


def test_the_default_configuration_is_within_every_ceiling() -> None:
    assert DEFAULT_AUTONOMY_CONFIG.home_radius <= MAX_HOME_RADIUS
    assert DEFAULT_AUTONOMY_CONFIG.max_plan_steps <= MAX_PLAN_STEPS
    assert DEFAULT_AUTONOMY_CONFIG.max_actions_per_interval <= MAX_ACTIONS_PER_INTERVAL
