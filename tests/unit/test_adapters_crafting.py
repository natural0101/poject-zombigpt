"""The two crafting adapters: the gate before, the product after.

Three properties carry this wave's safety weight and all three live here. The
policy refusal happens in ``validate`` — before any command exists to send — so
a craft that cannot run costs no wire traffic and, more to the point, no
materials. ``verify`` answers only to a product observed in the inventory, never
to the mod saying it finished. And ``risk_for`` escalates per command: spending
what the character carries is P3, but the moment a surface or a world container
is involved the craft becomes P4, which the permission machinery never grants on
the agent's own initiative.
"""

from __future__ import annotations

from typing import Any

import pytest

from pz_agent_core.actions.adapter import PreconditionFailed
from pz_agent_core.actions.adapters.crafting import (
    _REFUSAL_REASONS,
    MAX_CRAFT_COUNT,
    MAX_RECIPE_NAME_LEN,
    CraftingCraftAdapter,
    CraftingInspectAdapter,
)
from pz_agent_core.capabilities.probes import CRAFTING, PROBES_BY_NAME
from pz_agent_core.policy.crafting import CRAFTING_KEY, CraftingRefusal
from pz_agent_core.protocol import (
    READ_ONLY_ACTIONS,
    ActionName,
    Command,
    ItemView,
    Observation,
    ReasonCode,
    RiskClass,
)
from tests.fixtures.adapter_worlds import (
    CRATE_REF,
    MAIN_REF,
    a_command,
    a_world,
    an_item,
    crate_container,
    main_container,
    prepare,
)

SPEAR = "Base.SpearCrude"
BRANCH = "Base.TreeBranch"
RECIPE = "MakeSpear"


def recipe_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": RECIPE,
        "product": SPEAR,
        "display_name": "Make Crude Spear",
        "known": True,
        "needs_surface": False,
        "materials": [{"full_type": BRANCH, "count": 1}],
    }
    payload.update(overrides)
    return payload


def branch(
    runtime_id: str = "m1",
    *,
    container_ref: str = MAIN_REF,
    recipes: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> ItemView:
    return an_item(
        runtime_id=runtime_id,
        container_ref=container_ref,
        full_type=BRANCH,
        display_name="Tree Branch",
        category="Item",
        extra={CRAFTING_KEY: {"recipes": recipes if recipes is not None else [recipe_payload()]}},
        **overrides,
    )


def spear(runtime_id: str = "p1") -> ItemView:
    return an_item(
        runtime_id=runtime_id,
        full_type=SPEAR,
        display_name="Crude Spear",
        category="Weapon",
    )


def world(*items: ItemView, seq: int = 1, **overrides: Any) -> Observation:
    return a_world(
        seq=seq,
        items=list(items),
        containers=[main_container(), crate_container()],
        **overrides,
    )


def craft_command(**args: Any) -> Command:
    payload: dict[str, Any] = {"recipe": RECIPE}
    payload.update(args)
    return a_command(ActionName.CRAFTING_CRAFT, payload)


def inspect_command(recipe: str = RECIPE) -> Command:
    return a_command(ActionName.CRAFTING_INSPECT, {"recipe": recipe})


# --------------------------------------------------------------------------
# the shape of the two actions
# --------------------------------------------------------------------------


def test_the_inspect_is_read_only_and_gates_on_a_tier_not_a_probe() -> None:
    adapter = CraftingInspectAdapter()

    assert adapter.name is ActionName.CRAFTING_INSPECT
    assert adapter.name in READ_ONLY_ACTIONS
    assert adapter.risk is RiskClass.P0
    assert adapter.required_capability is None


def test_the_craft_is_p3_behind_the_crafting_capability() -> None:
    adapter = CraftingCraftAdapter()

    assert adapter.risk is RiskClass.P3
    assert adapter.required_capability == CRAFTING
    assert CRAFTING in PROBES_BY_NAME


def test_every_policy_refusal_has_a_reason_code_of_its_own() -> None:
    """Total over the enum: a refusal that cannot name itself is an INTERNAL_ERROR."""
    assert set(_REFUSAL_REASONS) == set(CraftingRefusal)
    assert _REFUSAL_REASONS[CraftingRefusal.RECIPE_UNKNOWN] is ReasonCode.RECIPE_UNKNOWN
    assert (
        _REFUSAL_REASONS[CraftingRefusal.MATERIALS_MISSING] is ReasonCode.RECIPE_MATERIALS_MISSING
    )
    assert _REFUSAL_REASONS[CraftingRefusal.MATERIALS_RESERVED] is ReasonCode.RESOURCE_RESERVED


# --------------------------------------------------------------------------
# arguments
# --------------------------------------------------------------------------


def test_the_craft_sends_exactly_the_recipe_and_the_count() -> None:
    observation = world(branch())

    args = CraftingCraftAdapter().build_args(craft_command(), observation)

    assert args == {"recipe": RECIPE, "count": 1}


def test_the_inspect_sends_exactly_the_recipe() -> None:
    observation = world(branch())

    assert CraftingInspectAdapter().build_args(inspect_command(), observation) == {"recipe": RECIPE}


def test_one_command_crafts_one_item_once() -> None:
    """The bound is on the wire, not a default the mod picks."""
    assert MAX_CRAFT_COUNT == 1
    observation = world(branch())

    with pytest.raises(PreconditionFailed) as raised:
        CraftingCraftAdapter().validate(craft_command(count=2), observation)

    assert raised.value.reason_code is ReasonCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param(7, id="not-a-string"),
        pytest.param("Make Spear", id="space"),
        pytest.param("Make*", id="wildcard"),
        pytest.param("a" * (MAX_RECIPE_NAME_LEN + 1), id="too-long"),
    ],
)
def test_a_recipe_name_that_is_not_a_game_identifier_is_refused(value: Any) -> None:
    observation = world(branch())

    with pytest.raises(PreconditionFailed) as raised:
        CraftingCraftAdapter().validate(craft_command(recipe=value), observation)

    assert raised.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_an_argument_neither_adapter_understands_is_refused() -> None:
    observation = world(branch())

    with pytest.raises(PreconditionFailed) as raised:
        CraftingCraftAdapter().validate(craft_command(surface_ref="anything"), observation)

    assert raised.value.reason_code is ReasonCode.INVALID_ARGUMENT


# --------------------------------------------------------------------------
# the gate in validate
# --------------------------------------------------------------------------


def test_a_craft_whose_materials_are_held_passes_validation() -> None:
    CraftingCraftAdapter().validate(craft_command(), world(branch()))


def test_a_recipe_no_observed_item_participates_in_is_refused_before_anything_is_sent() -> None:
    with pytest.raises(PreconditionFailed) as raised:
        CraftingCraftAdapter().validate(craft_command(recipe="MakeKatana"), world(branch()))

    assert raised.value.reason_code is ReasonCode.RECIPE_UNKNOWN
    assert raised.value.evidence["recipe"] == "MakeKatana"


def test_an_unlearned_recipe_is_refused_with_the_policys_own_token() -> None:
    observation = world(branch(recipes=[recipe_payload(known=False)]))

    with pytest.raises(PreconditionFailed) as raised:
        CraftingCraftAdapter().validate(craft_command(), observation)

    assert raised.value.reason_code is ReasonCode.RECIPE_UNKNOWN
    assert raised.value.evidence["refusal"] == CraftingRefusal.RECIPE_UNKNOWN.value


def test_missing_materials_are_refused_and_the_shortfall_travels_as_evidence() -> None:
    payload = recipe_payload(materials=[{"full_type": BRANCH, "count": 3}])
    observation = world(branch(recipes=[payload]))

    with pytest.raises(PreconditionFailed) as raised:
        CraftingCraftAdapter().validate(craft_command(), observation)

    assert raised.value.reason_code is ReasonCode.RECIPE_MATERIALS_MISSING
    assert raised.value.evidence["shortfalls"] == [
        {"full_type": BRANCH, "needed": 3, "free": 1, "reserved": 0}
    ]


def test_a_craft_that_would_spend_a_reserved_item_is_refused_as_reserved() -> None:
    observation = world(branch(favorite=True))

    with pytest.raises(PreconditionFailed) as raised:
        CraftingCraftAdapter().validate(craft_command(), observation)

    assert raised.value.reason_code is ReasonCode.RESOURCE_RESERVED


def test_neither_adapter_runs_without_an_inventory_to_verify_against() -> None:
    blind = a_world(no_inventory=True)

    for adapter, command in (
        (CraftingCraftAdapter(), craft_command()),
        (CraftingInspectAdapter(), inspect_command()),
    ):
        with pytest.raises(PreconditionFailed) as raised:
            adapter.validate(command, blind)
        assert raised.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


# --------------------------------------------------------------------------
# risk escalation, by argument
# --------------------------------------------------------------------------


def test_a_craft_from_what_the_character_carries_stays_at_p3() -> None:
    observation = world(branch())

    assert CraftingCraftAdapter().risk_for(craft_command(), observation) is RiskClass.P3


def test_a_recipe_that_needs_a_surface_escalates_to_p4() -> None:
    observation = world(branch(recipes=[recipe_payload(needs_surface=True)]))

    assert CraftingCraftAdapter().risk_for(craft_command(), observation) is RiskClass.P4


def test_an_unreadable_surface_flag_escalates_to_p4() -> None:
    payload = recipe_payload()
    del payload["needs_surface"]
    observation = world(branch(recipes=[payload]))

    assert CraftingCraftAdapter().risk_for(craft_command(), observation) is RiskClass.P4


def test_a_material_that_lives_in_a_world_container_escalates_to_p4() -> None:
    observation = world(branch(container_ref=CRATE_REF))

    assert CraftingCraftAdapter().risk_for(craft_command(), observation) is RiskClass.P4


# --------------------------------------------------------------------------
# the postcondition
# --------------------------------------------------------------------------


def test_the_craft_is_proven_by_the_product_appearing_in_the_inventory() -> None:
    adapter = CraftingCraftAdapter()
    before = world(branch())
    after = world(spear(), seq=2)
    command = prepare(adapter, craft_command(), before)

    evidence = adapter.verify(command, before, after)

    assert evidence is not None
    assert evidence.kind == "product_in_inventory"
    assert evidence.observation_seq == 2
    assert evidence.observed["recipe"] == RECIPE
    assert evidence.observed["product"] == SPEAR
    assert evidence.observed["count_before"] == 0
    assert evidence.observed["count_after"] == 1


def test_an_unchanged_product_count_proves_nothing_however_the_mod_feels() -> None:
    adapter = CraftingCraftAdapter()
    before = world(branch())
    after = world(branch(), seq=2)
    command = prepare(adapter, craft_command(), before)

    assert adapter.verify(command, before, after) is None


def test_a_product_the_character_already_had_is_counted_as_a_delta() -> None:
    adapter = CraftingCraftAdapter()
    before = world(branch(), spear("p1"))
    after = world(spear("p1"), spear("p2"), seq=2)
    command = prepare(adapter, craft_command(), before)

    evidence = adapter.verify(command, before, after)

    assert evidence is not None
    assert evidence.observed["count_before"] == 1
    assert evidence.observed["count_after"] == 2


def test_an_unreadable_inventory_afterwards_leaves_the_craft_unproven() -> None:
    adapter = CraftingCraftAdapter()
    before = world(branch())
    command = prepare(adapter, craft_command(), before)

    assert adapter.verify(command, before, a_world(no_inventory=True, seq=2)) is None


def test_the_product_is_read_from_before_because_the_craft_consumes_the_readout() -> None:
    """The materials are gone afterwards, and the readout goes with them."""
    adapter = CraftingCraftAdapter()
    before = world(branch())
    after = world(spear(), seq=2)
    command = prepare(adapter, craft_command(), before)

    assert after.inventory is not None
    assert all(CRAFTING_KEY not in item.extra for item in after.inventory.items)
    assert adapter.verify(command, before, after) is not None


def test_a_recipe_absent_from_the_before_observation_cannot_be_verified() -> None:
    adapter = CraftingCraftAdapter()
    before = world(an_item("x1", full_type=BRANCH))
    after = world(spear(), seq=2)
    command = a_command(ActionName.CRAFTING_CRAFT, {"recipe": RECIPE, "count": 1})

    assert adapter.verify(command, before, after) is None


# --------------------------------------------------------------------------
# the reading
# --------------------------------------------------------------------------


def test_the_inspect_reports_what_the_recipe_needs_and_whether_it_may_run() -> None:
    adapter = CraftingInspectAdapter()
    before = world(branch())
    command = prepare(adapter, inspect_command(), before)

    evidence = adapter.verify(command, before, before)

    assert evidence is not None
    assert evidence.kind == "recipe_described"
    assert evidence.observed["recipe"] == RECIPE
    assert evidence.observed["found"] is True
    assert evidence.observed["known"] is True
    assert evidence.observed["product"] == SPEAR
    assert evidence.observed["runnable"] is True
    assert evidence.observed["shortfalls"] == []


def test_a_recipe_the_reading_did_not_find_is_a_finding_not_a_failure() -> None:
    """The empty-crate rule: "not there" is an answer, "nobody looked" is not."""
    adapter = CraftingInspectAdapter()
    observation = world(branch())
    command = prepare(adapter, inspect_command("MakeKatana"), observation)

    evidence = adapter.verify(command, observation, observation)

    assert evidence is not None
    assert evidence.observed["found"] is False
    assert "product" not in evidence.observed


def test_an_observation_where_nothing_carries_a_readout_proves_no_reading() -> None:
    adapter = CraftingInspectAdapter()
    before = world(branch())
    command = prepare(adapter, inspect_command(), before)

    assert adapter.verify(command, before, world(an_item("x1"), seq=2)) is None


def test_the_reading_reports_the_refusal_a_craft_would_hit() -> None:
    adapter = CraftingInspectAdapter()
    payload = recipe_payload(materials=[{"full_type": BRANCH, "count": 4}])
    observation = world(branch(recipes=[payload]))
    command = prepare(adapter, inspect_command(), observation)

    evidence = adapter.verify(command, observation, observation)

    assert evidence is not None
    assert evidence.observed["runnable"] is False
    assert evidence.observed["refusal"] == CraftingRefusal.MATERIALS_MISSING.value
    assert evidence.observed["shortfalls"] == [
        {"full_type": BRANCH, "needed": 4, "free": 1, "reserved": 0}
    ]


def test_both_adapters_bound_their_own_polling() -> None:
    for adapter in (CraftingInspectAdapter(), CraftingCraftAdapter()):
        assert adapter.poll_interval_ms > 0
        assert adapter.timeout_ms >= adapter.poll_interval_ms
