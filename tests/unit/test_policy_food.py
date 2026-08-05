"""Food selection: every hard filter, the reserve rule, portions, determinism.

The happy path is two tests. The rest of the file is the part that matters: a
character who eats the rotten ham because a filter silently stopped firing is
the failure this module exists to prevent, so every rejection reason gets its
own case and asserts that the reason is *reported*, not merely that the item
was skipped.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from pz_agent_core.policy import (
    CAPABILITY_EAT_PERCENTAGE,
    CapabilitySnapshot,
    FoodChoice,
    FoodSelection,
    PolicyConfig,
    RejectionReason,
    select_food,
)
from pz_agent_core.protocol import CapabilityState, InventoryView, ReasonCode
from tests.fixtures import make_item
from tests.fixtures.policy_items import (
    BACKPACK_REF,
    MAIN_REF,
    WORLD_REF,
    backpack_container,
    food_item,
    hungry_player,
    inventory,
    main_container,
    policy_item_ref,
    world_container,
)

PERCENTAGE = CapabilitySnapshot.verified(CAPABILITY_EAT_PERCENTAGE)


def reason_for(selection: FoodSelection, item_ref: str) -> RejectionReason:
    """The reported reason for one item, or a failure naming what was reported."""
    for rejection in selection.rejections:
        if rejection.item_ref == item_ref:
            return rejection.reason
    raise AssertionError(
        f"{item_ref} was not rejected; got {[r.as_dict() for r in selection.rejections]}"
    )


def test_empty_inventory_refuses_with_no_safe_food() -> None:
    selection = select_food(InventoryView(), hungry_player(0.5))

    assert selection.is_refusal
    assert selection.reason_code is ReasonCode.NO_SAFE_FOOD
    assert selection.rejections == ()
    assert "no food" in selection.explain()


def test_non_food_items_are_not_reported_as_rejections() -> None:
    hammer = make_item(
        policy_item_ref("hammer"), MAIN_REF, full_type="Base.Hammer", category="Item"
    )

    selection = select_food(inventory(hammer), hungry_player(0.5))

    assert selection.is_refusal
    assert selection.rejections == ()


def test_a_safe_item_is_chosen_and_carries_its_breakdown() -> None:
    beans = food_item("beans")

    selection = select_food(inventory(beans), hungry_player(0.5))

    assert not selection.is_refusal
    assert selection.reason_code is None
    choice = selection.choice
    assert choice is not None
    assert choice.item_ref == beans.ref
    assert choice.container_ref == MAIN_REF
    assert not choice.needs_transfer_to_main
    assert {f.name for f in choice.breakdown.factors} == {
        "hunger_fit",
        "urgency",
        "calories",
        "freshness",
        "unhappiness",
        "boredom",
        "thirst_side_effect",
        "weight",
        "scarcity",
        "cooking",
        "portions",
        "waste",
    }
    assert selection.explain() == choice.rationale


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"edible": False}, RejectionReason.NOT_EDIBLE),
        ({"destroyed": True}, RejectionReason.DESTROYED),
        ({"poisonous": True}, RejectionReason.POISONOUS),
        ({"poison_power": 2.0}, RejectionReason.POISONOUS),
        ({"tainted": True}, RejectionReason.TAINTED),
        ({"freshness": "rotten"}, RejectionReason.ROTTEN),
        ({"rot_progress": 0.9}, RejectionReason.ROTTEN),
        ({"raw": True, "raw_unsafe": True, "cooked": False}, RejectionReason.RAW_UNSAFE),
        ({"requires_cooking": True, "cooked": False}, RejectionReason.REQUIRES_COOKING),
        ({"burnt": True}, RejectionReason.BURNT),
        ({"burn_progress": 0.5}, RejectionReason.BURNT),
        ({"frozen": True}, RejectionReason.FROZEN),
        ({"required_tool": "canopener"}, RejectionReason.TOOL_UNAVAILABLE),
        ({"hunger_change": 0.0}, RejectionReason.NO_HUNGER_RELIEF),
    ],
)
def test_each_hard_filter_rejects_and_reports_its_reason(
    overrides: dict[str, Any], expected: RejectionReason
) -> None:
    bad = food_item("bad", **overrides)

    selection = select_food(inventory(bad), hungry_player(0.5))

    assert selection.is_refusal
    assert selection.reason_code is ReasonCode.NO_SAFE_FOOD
    assert reason_for(selection, bad.ref) is expected
    assert selection.rejections[0].detail
    assert bad.display_name in selection.explain()


@pytest.mark.parametrize(
    "marker",
    [
        {"favorite": True},
        {"tags": ["reserved"]},
        {"tags": ["KEEP"]},
        {"extra": {"reserved": True}},
    ],
)
def test_user_reserved_items_are_never_taken(marker: dict[str, Any]) -> None:
    kept = food_item("kept", **marker)

    selection = select_food(inventory(kept), hungry_player(0.9))

    assert selection.is_refusal
    assert reason_for(selection, kept.ref) is RejectionReason.USER_RESERVED


def test_strategic_reserve_is_held_back_below_critical_hunger() -> None:
    reserve = food_item("reserve", tags=["emergency"])

    selection = select_food(inventory(reserve), hungry_player(0.5))

    assert selection.is_refusal
    assert reason_for(selection, reserve.ref) is RejectionReason.STRATEGIC_RESERVE
    assert "critical" in selection.rejections[0].detail


def test_strategic_reserve_opens_up_at_critical_hunger() -> None:
    reserve = food_item("reserve", tags=["emergency"])

    selection = select_food(inventory(reserve), hungry_player(0.85))

    assert not selection.is_refusal
    assert selection.choice is not None
    assert selection.choice.item_ref == reserve.ref


def test_required_tool_present_in_the_inventory_clears_the_filter() -> None:
    tin = food_item("tin", required_tool="canopener")
    opener = make_item(
        policy_item_ref("opener"),
        MAIN_REF,
        full_type="Base.TinOpener",
        display_name="Tin Opener",
        category="Item",
        tags=["canopener"],
    )

    selection = select_food(inventory(tin, opener), hungry_player(0.5))

    assert not selection.is_refusal
    assert selection.choice is not None
    assert selection.choice.item_ref == tin.ref


def test_item_in_an_unreported_container_is_refused_rather_than_guessed_at() -> None:
    stray = food_item("stray", container_ref=BACKPACK_REF)

    selection = select_food(inventory(stray, containers=[main_container()]), hungry_player(0.5))

    assert selection.is_refusal
    assert reason_for(selection, stray.ref) is RejectionReason.CONTAINER_UNREACHABLE


def test_inaccessible_container_is_refused() -> None:
    packed = food_item("packed", container_ref=BACKPACK_REF)

    selection = select_food(
        inventory(packed, containers=[main_container(), backpack_container(accessible=False)]),
        hungry_player(0.5),
    )

    assert selection.is_refusal
    assert reason_for(selection, packed.ref) is RejectionReason.CONTAINER_UNREACHABLE


def test_world_containers_are_out_of_scope_by_default_and_opt_in_by_config() -> None:
    shelved = food_item("shelved", container_ref=WORLD_REF)
    view = inventory(shelved, containers=[main_container(), world_container()])

    refused = select_food(view, hungry_player(0.5))
    assert refused.is_refusal
    assert reason_for(refused, shelved.ref) is RejectionReason.CONTAINER_UNREACHABLE

    allowed = select_food(view, hungry_player(0.5), PolicyConfig(allow_world_containers=True))
    assert allowed.choice is not None
    assert allowed.choice.needs_transfer_to_main


def test_rot_tolerance_is_configurable() -> None:
    rotten = food_item("rotten", freshness="rotten")

    assert select_food(inventory(rotten), hungry_player(0.5)).is_refusal

    permitted = select_food(inventory(rotten), hungry_player(0.5), PolicyConfig(allow_rotten=True))
    assert permitted.choice is not None
    assert permitted.choice.item_ref == rotten.ref


def test_fresher_of_two_plausible_candidates_wins() -> None:
    fresh = food_item("fresh", display_name="Fresh Sandwich", full_type="Base.Sandwich")
    stale = food_item(
        "stale",
        display_name="Stale Sandwich",
        full_type="Base.SandwichOld",
        freshness="stale",
        rot_progress=0.5,
    )

    selection = select_food(inventory(stale, fresh), hungry_player(0.5))

    assert selection.choice is not None
    assert selection.choice.item_ref == fresh.ref
    assert [c.item_ref for c in selection.ranked] == [fresh.ref, stale.ref]
    fresh_score = selection.ranked[0].breakdown.factor("freshness")
    stale_score = selection.ranked[1].breakdown.factor("freshness")
    assert fresh_score is not None and stale_score is not None
    assert fresh_score.contribution > stale_score.contribution


def test_the_portion_that_fits_the_deficit_is_preferred_over_the_oversized_one() -> None:
    # Hunger 0.5 leaves a 0.35 deficit; the feast overshoots it and is wasted.
    right_sized = food_item(
        "sized", display_name="Sandwich", full_type="Base.Sandwich", hunger_change=-0.35
    )
    feast = food_item("feast", display_name="Roast", full_type="Base.Roast", hunger_change=-1.0)

    selection = select_food(inventory(feast, right_sized), hungry_player(0.5))

    assert selection.choice is not None
    assert selection.choice.item_ref == right_sized.ref


def test_whole_units_are_used_when_percentage_eating_is_unavailable() -> None:
    big = food_item("big", hunger_change=-1.0)

    selection = select_food(inventory(big), hungry_player(0.5))

    choice = selection.choice
    assert choice is not None
    assert choice.fraction == 1.0
    assert not choice.portioned
    assert CAPABILITY_EAT_PERCENTAGE in choice.rationale
    assert CapabilityState.UNSUPPORTED.value in choice.rationale


def test_percentage_eating_sizes_the_portion_to_the_deficit() -> None:
    big = food_item("big", hunger_change=-1.0)

    selection = select_food(inventory(big), hungry_player(0.5), capabilities=PERCENTAGE)

    choice = selection.choice
    assert choice is not None
    # Deficit 0.35 of a 1.0 item, rounded up to the 0.25 quantum.
    assert choice.fraction == 0.5
    assert choice.portioned
    assert "50%" in choice.rationale


def test_a_barely_hungry_character_takes_the_smallest_portion() -> None:
    big = food_item("big", hunger_change=-1.0)

    selection = select_food(inventory(big), hungry_player(0.1), capabilities=PERCENTAGE)

    choice = selection.choice
    assert choice is not None
    assert choice.fraction == PolicyConfig().min_eat_fraction


def test_an_unprobed_capability_is_not_assumed_to_work() -> None:
    snapshot = CapabilitySnapshot.from_mapping(
        {CAPABILITY_EAT_PERCENTAGE: CapabilityState.UNSUPPORTED}
    )
    big = food_item("big", hunger_change=-1.0)

    selection = select_food(inventory(big), hungry_player(0.5), capabilities=snapshot)

    assert selection.choice is not None
    assert not selection.choice.portioned


def test_selection_is_identical_under_shuffled_input() -> None:
    items = [
        food_item("a", display_name="Beans", full_type="Base.TinnedBeans"),
        food_item("b", display_name="Bread", full_type="Base.Bread", hunger_change=-0.2),
        food_item(
            "c", display_name="Ham", full_type="Base.Ham", freshness="stale", rot_progress=0.4
        ),
        food_item("d", display_name="Rotten Ham", full_type="Base.HamOld", freshness="rotten"),
        food_item("e", display_name="Chips", full_type="Base.Chips", thirst_change=0.3),
        food_item("f", display_name="Steak", full_type="Base.Steak", raw=True, raw_unsafe=True),
        food_item("g", display_name="Emergency Bar", full_type="Base.Bar", tags=["emergency"]),
        food_item("h", display_name="Pie", full_type="Base.Pie", container_ref=BACKPACK_REF),
    ]
    player = hungry_player(0.6)
    baseline = select_food(inventory(*items), player, capabilities=PERCENTAGE)

    for seed in range(8):
        shuffled = list(items)
        random.Random(seed).shuffle(shuffled)
        result = select_food(inventory(*shuffled), player, capabilities=PERCENTAGE)

        assert result.choice is not None and baseline.choice is not None
        assert result.choice.item_ref == baseline.choice.item_ref
        assert result.choice.fraction == baseline.choice.fraction
        assert [c.item_ref for c in result.ranked] == [c.item_ref for c in baseline.ranked]
        assert result.choice.rationale == baseline.choice.rationale


def test_ties_break_on_the_item_reference_in_both_input_orders() -> None:
    first = food_item("aaa", display_name="Twin", full_type="Base.Twin")
    second = food_item("zzz", display_name="Twin", full_type="Base.Twin")

    forward = select_food(inventory(first, second), hungry_player(0.5))
    backward = select_food(inventory(second, first), hungry_player(0.5))

    assert forward.choice is not None and backward.choice is not None
    assert forward.ranked[0].score == pytest.approx(forward.ranked[1].score)
    assert forward.choice.item_ref == backward.choice.item_ref == min(first.ref, second.ref)


def test_the_rejection_list_is_bounded() -> None:
    config = PolicyConfig(max_reported_rejections=3)
    rotten = [
        food_item(f"r{index}", full_type=f"Base.Rot{index}", freshness="rotten")
        for index in range(10)
    ]

    selection = select_food(inventory(*rotten), hungry_player(0.5), config)

    assert selection.is_refusal
    assert len(selection.rejections) == 3


def test_a_choice_cannot_claim_a_portion_it_cannot_take() -> None:
    selection = select_food(inventory(food_item("beans")), hungry_player(0.5))
    assert selection.choice is not None
    breakdown = selection.choice.breakdown

    with pytest.raises(ValueError, match="whole-unit"):
        FoodChoice(
            item_ref="item:x",
            display_name="x",
            container_ref=MAIN_REF,
            fraction=0.5,
            portioned=False,
            needs_transfer_to_main=False,
            rationale="r",
            breakdown=breakdown,
        )
    with pytest.raises(ValueError, match="fraction"):
        FoodChoice(
            item_ref="item:x",
            display_name="x",
            container_ref=MAIN_REF,
            fraction=0.0,
            portioned=True,
            needs_transfer_to_main=False,
            rationale="r",
            breakdown=breakdown,
        )


def test_a_selection_is_either_a_choice_or_a_refusal() -> None:
    with pytest.raises(ValueError, match="never both or neither"):
        FoodSelection(choice=None, reason_code=None, rejections=(), ranked=())


def test_the_refusal_payload_names_every_rejected_item() -> None:
    rotten = food_item("rotten", display_name="Old Ham", freshness="rotten")
    tainted = food_item(
        "tainted", display_name="Bad Water Stew", full_type="Base.Stew", tainted=True
    )

    payload = select_food(inventory(rotten, tainted), hungry_player(0.5)).as_dict()

    assert payload["choice"] is None
    assert payload["reason_code"] == ReasonCode.NO_SAFE_FOOD.value
    reasons = {entry["reason"] for entry in payload["rejections"]}
    assert reasons == {RejectionReason.ROTTEN.value, RejectionReason.TAINTED.value}
