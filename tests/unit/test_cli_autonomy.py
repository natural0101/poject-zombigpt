"""The bridge from an observation to one action request, one rule at a time.

``tests/contract/test_sidecar_planner_wiring.py`` proves the bridge is connected
to the loop ``pz-agent start`` builds. This file is about what it decides once it
is: which refusals are the gate's, which are the critic's, which are this
module's own, and that the one decision it makes for itself — refusing a plan
that names an item the selection policy did not choose — actually fires.

Everything is driven through :meth:`AutonomyPlanner.propose`, because that is the
whole public surface: one observation in, one request or nothing out.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.autonomy import (
    PLANNER_FILE_NAME,
    AutonomyPlanner,
    LedgerCapabilities,
    PlannerError,
    PlannerRecord,
    autonomy_config,
    build_planner,
    no_attributable_backup,
    publish_planner_record,
    read_planner_record,
    resolve_provider,
)
from pz_agent_cli.config import AgentConfig, default_config, validate_document
from pz_agent_cli.runtime import CAPABILITY_FILE_NAME, CapabilityLedger
from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.actions.builtin import register_builtins
from pz_agent_core.capabilities.probes import (
    DRINK_CARRIED,
    EAT_PERCENTAGE,
    INVENTORY_TRANSFER,
    READ_LITERATURE,
)
from pz_agent_core.planner import (
    ConsumeArgs,
    FailureMode,
    NullProvider,
    Plan,
    PlanProposal,
    PlanRequest,
    PlanStep,
    SuccessCriterion,
    SuccessKind,
)
from pz_agent_core.policy.autonomy import AutonomyOutcome, BackupEvidence
from pz_agent_core.policy.selection import CapabilitySnapshot
from pz_agent_core.protocol import (
    ActionName,
    CapabilityState,
    DangerLevel,
    InventoryView,
    Observation,
    ReasonCode,
    SessionMode,
)
from tests.fixtures import DEFAULT_SAVE, DEFAULT_SESSION, make_safety
from tests.fixtures.autonomy_worlds import (
    FakeMemory,
    autonomous_observation,
    calm_player,
    verified_backup,
)
from tests.fixtures.policy_items import (
    BACKPACK_REF,
    drink_item,
    food_item,
    inventory,
    literature_item,
    policy_item_ref,
)

NOW_MS: Final = 1_700_000_000_000

#: Everything the three servable needs require, proven the way a live run proves
#: it. §7.7 refuses ``available_unverified`` on the agent's own initiative, so a
#: planner built against anything less refuses every case in this file — which is
#: itself asserted, once, below.
PROVEN: Final = CapabilitySnapshot.from_mapping(
    {
        EAT_PERCENTAGE: CapabilityState.VERIFIED,
        DRINK_CARRIED: CapabilityState.VERIFIED,
        READ_LITERATURE: CapabilityState.VERIFIED,
        INVENTORY_TRANSFER: CapabilityState.VERIFIED,
    }
)


def registry() -> AdapterRegistry:
    return register_game_adapters(register_builtins(AdapterRegistry()))


def planner(**overrides: Any) -> AutonomyPlanner:
    """A bridge with everything the autonomous path needs already satisfied.

    The backup witness is the one the production assembly cannot supply; it is
    stood in for here so that a test about hunger is about hunger. Every other
    part is the real one.
    """
    base: dict[str, Any] = {
        "registry": registry(),
        "capabilities": PROVEN,
        "backup": lambda save_id: BackupEvidence(
            save_id=save_id, created_at_ms=NOW_MS, verified=True
        ),
        "clock": lambda: NOW_MS,
    }
    base.update(overrides)
    return AutonomyPlanner(**base)


def hungry_world(**overrides: Any) -> Observation:
    return autonomous_observation(
        player=calm_player(hunger=0.6),
        inventory=inventory(food_item("beans-1")),
        **overrides,
    )


# ---------------------------------------------------------------------------
# what it proposes
# ---------------------------------------------------------------------------


def test_a_hungry_character_is_offered_the_item_the_food_policy_chose() -> None:
    bridge = planner()

    request = bridge.propose(hungry_world())

    assert request is not None, bridge.last_detail
    assert request.action is ActionName.CONSUME_EAT
    assert request.args["item_ref"] == policy_item_ref("beans-1")
    assert request.session_id == DEFAULT_SESSION


def test_thirst_outranks_hunger_the_way_the_arbiter_says_and_not_this_module() -> None:
    """Both needs are real; the priority ladder in the gate picks between them."""
    bridge = planner()
    world = autonomous_observation(
        player=calm_player(hunger=0.6, thirst=0.95),
        inventory=inventory(food_item("beans-1"), drink_item("bottle-1")),
    )

    request = bridge.propose(world)

    assert request is not None, bridge.last_detail
    assert request.action is ActionName.CONSUME_DRINK


def test_an_item_in_a_container_is_fetched_before_it_is_eaten_and_that_is_the_whole_tick() -> None:
    """The plan is two steps; exactly one of them is proposed."""
    bridge = planner()
    world = autonomous_observation(
        player=calm_player(hunger=0.6),
        inventory=inventory(food_item("beans-1", container_ref=BACKPACK_REF)),
    )

    request = bridge.propose(world)

    assert request is not None, bridge.last_detail
    assert request.action is ActionName.INVENTORY_ENSURE_MAIN
    assert request.args == {"item_ref": policy_item_ref("beans-1", BACKPACK_REF)}


def test_boredom_is_served_by_reading_when_nothing_more_urgent_is_wrong() -> None:
    bridge = planner()
    world = autonomous_observation(
        player=calm_player(moodles={"Bored": 3}),
        inventory=inventory(literature_item("book-1")),
    )

    request = bridge.propose(world)

    assert request is not None, bridge.last_detail
    assert request.action is ActionName.LITERATURE_READ


def test_every_tick_composes_its_own_request_rather_than_replaying_one() -> None:
    """Two ticks of the same world are two attempts, not one repeated.

    The idempotency cache replays the first terminal result for a key, so a
    stable key would answer the second meal with the first meal's outcome and
    the character would go hungry beside a shelf of tins.
    """
    bridge = planner()
    world = hungry_world()

    first = bridge.propose(world)
    second = bridge.propose(world)

    assert first is not None and second is not None
    assert first.idempotency_key != second.idempotency_key


# ---------------------------------------------------------------------------
# what it refuses, and whose refusal it is
# ---------------------------------------------------------------------------


def test_an_unarmed_observation_is_refused_by_the_gate_not_by_the_planner() -> None:
    bridge = planner()
    world = hungry_world(safety=make_safety(mode=SessionMode.AUTONOMOUS, armed=False))

    assert bridge.propose(world) is None
    decision = bridge.last_decision
    assert decision is not None
    assert decision.outcome is AutonomyOutcome.REFUSE
    assert decision.reason_code is ReasonCode.NOT_ARMED


def test_assisted_mode_starts_nothing_unasked() -> None:
    """§7.5: only AUTONOMOUS acts on its own initiative, armed or not."""
    bridge = planner()
    world = hungry_world(safety=make_safety(mode=SessionMode.ASSISTED, armed=True))

    assert bridge.propose(world) is None
    decision = bridge.last_decision
    assert decision is not None
    assert decision.reason_code is ReasonCode.POLICY_DENIED


def test_a_character_with_no_needs_is_left_alone() -> None:
    bridge = planner()
    world = autonomous_observation(player=calm_player(), inventory=inventory(food_item("beans-1")))

    assert bridge.propose(world) is None
    decision = bridge.last_decision
    assert decision is not None
    assert decision.outcome is AutonomyOutcome.HOLD


def test_a_threat_stops_a_long_action_being_started() -> None:
    """The gate's danger ceiling, not a second copy of the reflex guard.

    Reading is the long action this build can plan — the adapter's budget runs to
    minutes — and starting one while something is nearby is the case that matters.
    """
    bridge = planner()
    world = autonomous_observation(
        player=calm_player(moodles={"Bored": 3}),
        inventory=inventory(literature_item("book-1")),
        safety=make_safety(
            mode=SessionMode.AUTONOMOUS, armed=True, danger_level=DangerLevel.MEDIUM
        ),
    )

    assert bridge.propose(world) is None
    decision = bridge.last_decision
    assert decision is not None
    assert decision.reason_code is ReasonCode.THREAT_INTERRUPTED


def test_a_capability_a_scan_only_saw_is_not_spent_unattended() -> None:
    """§7.7 draws its line at verified, and the bridge inherits it whole."""
    bridge = planner(
        capabilities=CapabilitySnapshot.from_mapping(
            {EAT_PERCENTAGE: CapabilityState.AVAILABLE_UNVERIFIED}
        )
    )

    assert bridge.propose(hungry_world()) is None
    decision = bridge.last_decision
    assert decision is not None
    assert decision.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_without_backup_evidence_the_agent_asks_instead_of_acting() -> None:
    """What ``pz-agent start`` actually wires today, and why it stays quiet."""
    bridge = planner(backup=no_attributable_backup)

    assert bridge.propose(hungry_world()) is None
    decision = bridge.last_decision
    assert decision is not None
    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_backup_of_another_save_is_not_a_safety_net_for_this_one() -> None:
    bridge = planner(backup=lambda save_id: verified_backup("some-other-save"))

    assert bridge.propose(hungry_world()) is None
    decision = bridge.last_decision
    assert decision is not None
    assert decision.reason_code is ReasonCode.SAVE_CHANGED


def test_an_item_the_user_reserved_is_asked_about_rather_than_eaten() -> None:
    """§7.9, applied by the gate before the provider is ever asked."""
    bridge = planner(memory=FakeMemory(reserved=frozenset({"Base.TinnedBeans"})))

    assert bridge.propose(hungry_world()) is None
    decision = bridge.last_decision
    assert decision is not None
    assert decision.reason_code is ReasonCode.RESOURCE_RESERVED


def test_an_empty_inventory_holds_rather_than_proposing_something_to_fetch() -> None:
    bridge = planner()
    world = autonomous_observation(player=calm_player(hunger=0.6), inventory=InventoryView())

    assert bridge.propose(world) is None
    decision = bridge.last_decision
    assert decision is not None
    assert decision.outcome is AutonomyOutcome.HOLD


# ---------------------------------------------------------------------------
# the one judgement the bridge makes for itself
# ---------------------------------------------------------------------------


class WrongItem:
    """A provider that plans a real action against an item nobody authorised.

    The shape a model-backed provider could produce at any time: a valid plan,
    a parseable reference, an item that exists — and not the one the
    deterministic policy chose after removing what the user set aside.
    """

    name = "wrong_item"

    def __init__(self, item_ref: str) -> None:
        self._item_ref = item_ref

    def propose(self, request: PlanRequest) -> PlanProposal:
        plan = Plan(
            goal_id=request.goal.goal_id,
            summary="Eat something else",
            steps=(
                PlanStep(
                    step_id="s1",
                    action=ActionName.CONSUME_EAT,
                    args=ConsumeArgs(item_ref=self._item_ref, fraction=1.0),
                    success=SuccessCriterion(kind=SuccessKind.HUNGER_AT_MOST, value=0.2),
                    on_failure=FailureMode.ASK_USER,
                ),
            ),
        )
        return PlanProposal.proposed(plan, plan.summary)


class Refusing:
    """A provider that answers every request with a refusal, as a model may."""

    name = "refusing"

    def propose(self, request: PlanRequest) -> PlanProposal:
        return PlanProposal.refusal(ReasonCode.CAPABILITY_UNAVAILABLE, "the endpoint is not there.")


def test_a_plan_naming_an_item_the_policy_did_not_choose_is_not_run() -> None:
    bridge = planner(provider=WrongItem(policy_item_ref("beans-2")))
    world = autonomous_observation(
        player=calm_player(hunger=0.6),
        inventory=inventory(food_item("beans-1"), food_item("beans-2", full_type="Base.Crisps")),
    )

    assert bridge.propose(world) is None
    assert "the selection policy did not choose" in bridge.last_detail


def test_the_same_plan_is_run_when_it_names_the_item_the_policy_chose() -> None:
    """The mismatch rule refuses a difference, not every injected provider."""
    bridge = planner(provider=WrongItem(policy_item_ref("beans-1")))

    request = bridge.propose(hungry_world())

    assert request is not None, bridge.last_detail
    assert request.args["item_ref"] == policy_item_ref("beans-1")


def test_a_provider_that_refuses_leaves_the_tick_unserved_and_says_why() -> None:
    bridge = planner(provider=Refusing())

    assert bridge.propose(hungry_world()) is None
    assert "the endpoint is not there" in bridge.last_detail


def test_a_plan_the_critic_refuses_is_not_run() -> None:
    """A reference from another session: §3.7 makes it INVALID_REF, not a retry."""
    foreign = f"item:{'0' * 8}-0000-0000-0000-00000000ffff:player-main:beans-1:0"
    bridge = planner(provider=WrongItem(foreign))

    assert bridge.propose(hungry_world()) is None
    assert "the critic refused the plan" in bridge.last_detail


def test_max_steps_outside_the_plan_type_s_own_ceiling_is_refused() -> None:
    with pytest.raises(PlannerError):
        planner(max_steps=99)


# ---------------------------------------------------------------------------
# assembly: configuration, providers and the record
# ---------------------------------------------------------------------------


def config_with(**tables: Mapping[str, Any]) -> AgentConfig:
    validation = validate_document(dict(tables), path=Path("config.toml"))
    assert validation.config is not None, validation.errors
    return validation.config


MODEL_SECTION: Final[dict[str, Any]] = {
    "base_url": "http://127.0.0.1:8080",
    "model": "qwen2.5-7b-instruct",
    "api_key_env": "PZ_AGENT_TEST_KEY",
}


def test_the_default_configuration_selects_the_deterministic_path() -> None:
    provider, fallback = resolve_provider(default_config(), env={})

    assert isinstance(provider, NullProvider)
    assert fallback == ""


def test_a_configured_provider_with_its_key_present_is_the_one_returned() -> None:
    config = config_with(
        planner={"provider": "openai_compatible"}, **{"planner.openai_compatible": MODEL_SECTION}
    )

    provider, fallback = resolve_provider(config, env={"PZ_AGENT_TEST_KEY": "a-key"})

    assert provider.name == "openai_compatible"
    assert fallback == ""


def test_a_configured_provider_with_no_key_falls_back_and_names_the_variable() -> None:
    config = config_with(
        planner={"provider": "openai_compatible"}, **{"planner.openai_compatible": MODEL_SECTION}
    )

    provider, fallback = resolve_provider(config, env={})

    assert isinstance(provider, NullProvider)
    assert "PZ_AGENT_TEST_KEY" in fallback


def test_a_key_that_cannot_be_sent_as_a_header_falls_back_rather_than_trying() -> None:
    """A pasted key with a space in it is caught before the first request."""
    config = config_with(
        planner={"provider": "openai_compatible"}, **{"planner.openai_compatible": MODEL_SECTION}
    )

    provider, fallback = resolve_provider(config, env={"PZ_AGENT_TEST_KEY": "two words"})

    assert isinstance(provider, NullProvider)
    assert "PZ_AGENT_TEST_KEY" in fallback


def test_the_autonomy_bounds_come_from_configuration_and_not_from_a_second_copy() -> None:
    config = config_with(safety={"max_autonomous_radius": 12}, session={"require_backup": False})

    bounds = autonomy_config(config)

    assert bounds.home_radius == 12
    assert bounds.require_verified_backup is False


def test_build_planner_reports_the_provider_it_ended_up_with(tmp_path: Path) -> None:
    ledger = CapabilityLedger(record_path=tmp_path / CAPABILITY_FILE_NAME, detail="a test ledger")
    config = config_with(
        planner={"provider": "openai_compatible"}, **{"planner.openai_compatible": MODEL_SECTION}
    )

    built, record = build_planner(registry=registry(), capabilities=ledger, config=config, env={})

    assert isinstance(built.provider, NullProvider)
    assert record.configured == "openai_compatible"
    assert record.active == "none"
    assert record.fell_back
    # Left for the caller that builds the loop: only it can see whether the
    # planner reached it.
    assert record.wired is False


def test_a_ledger_with_no_report_answers_unsupported_to_everything(tmp_path: Path) -> None:
    """Fail-closed, so a scan that could not run cannot become permission."""
    ledger = CapabilityLedger(record_path=tmp_path / CAPABILITY_FILE_NAME, detail="no scan ran")
    lookup = LedgerCapabilities(ledger)

    assert lookup.state(EAT_PERCENTAGE) is CapabilityState.UNSUPPORTED
    assert lookup.usable(EAT_PERCENTAGE) is False


def test_a_planner_record_survives_a_round_trip(tmp_path: Path) -> None:
    path = tmp_path / PLANNER_FILE_NAME
    record = PlannerRecord(
        wired=True,
        configured="teamon",
        active="none",
        detail="teamon is not answering",
        fallback_reason="no API key",
        notes=("a note",),
    )

    publish_planner_record(path, record)

    assert read_planner_record(path) == record


def test_a_record_that_is_not_one_reads_as_absent_rather_than_as_a_verdict(
    tmp_path: Path,
) -> None:
    path = tmp_path / PLANNER_FILE_NAME
    path.write_text('{"configured": "none"}', encoding="utf-8")

    assert read_planner_record(path) is None


def test_a_record_must_say_how_it_reached_the_planner_it_names() -> None:
    with pytest.raises(PlannerError):
        PlannerRecord(wired=True, configured="none", active="none", detail="  ")


def test_the_production_witness_answers_for_no_save() -> None:
    """The gap, pinned: if this ever answers, it must be because it can."""
    assert no_attributable_backup(DEFAULT_SAVE) is None
