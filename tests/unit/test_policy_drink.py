"""Drink selection: tainted water, alcohol, the last container, world sources.

Two behaviours here are safety rules and get the most attention: tainted water
is never chosen, and the character's last container is not emptied for a thirst
that can wait. The second one changes shape depending on whether the game can
pour half a bottle, so both branches are covered.
"""

from __future__ import annotations

import random
from dataclasses import replace
from typing import Any

import pytest

from pz_agent_core.policy import (
    CAPABILITY_DRINK_PERCENTAGE,
    CAPABILITY_DRINK_WORLD_SOURCE,
    CapabilitySnapshot,
    DrinkSelection,
    PolicyConfig,
    RejectionReason,
    select_drink,
)
from pz_agent_core.protocol import CapabilityState, InventoryView, ReasonCode
from tests.fixtures import make_item
from tests.fixtures.policy_items import (
    BACKPACK_REF,
    MAIN_REF,
    backpack_container,
    drink_item,
    food_item,
    inventory,
    main_container,
    policy_item_ref,
    thirsty_player,
)

PARTIAL = CapabilitySnapshot.verified(CAPABILITY_DRINK_PERCENTAGE)
WORLD_SOURCE = CapabilitySnapshot.verified(CAPABILITY_DRINK_WORLD_SOURCE)


def reason_for(selection: DrinkSelection, item_ref: str) -> RejectionReason:
    for rejection in selection.rejections:
        if rejection.item_ref == item_ref:
            return rejection.reason
    raise AssertionError(
        f"{item_ref} was not rejected; got {[r.as_dict() for r in selection.rejections]}"
    )


def two_bottles(**overrides: Any) -> tuple[Any, Any]:
    """A pair of interchangeable bottles, so no last-container rule applies."""
    return (
        drink_item("first", **overrides),
        drink_item("second", full_type="Base.WaterBottleSpare", **overrides),
    )


def test_empty_inventory_refuses_with_no_safe_drink() -> None:
    selection = select_drink(InventoryView(), thirsty_player(0.5))

    assert selection.is_refusal
    assert selection.reason_code is ReasonCode.NO_SAFE_DRINK
    assert selection.rejections == ()


def test_clean_water_is_chosen_over_a_sugary_drink() -> None:
    water, spare = two_bottles()
    soda = drink_item(
        "soda",
        display_name="Pop",
        full_type="Base.Pop",
        type="soda",
        water=False,
        unhappy_change=5.0,
    )

    selection = select_drink(inventory(soda, water, spare), thirsty_player(0.5))

    assert selection.choice is not None
    assert selection.choice.item_ref in {water.ref, spare.ref}
    water_factor = selection.ranked[0].breakdown.factor("water")
    assert water_factor is not None
    assert water_factor.contribution > 0.0


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"drinkable": False, "water": False, "type": "", "thirst_change": 0.0},
            RejectionReason.NOT_DRINKABLE,
        ),
        ({"destroyed": True}, RejectionReason.DESTROYED),
        ({"remaining_units": 0.0}, RejectionReason.EMPTY_CONTAINER),
        ({"tainted": True}, RejectionReason.TAINTED),
        ({"poisonous": True}, RejectionReason.POISONOUS),
        ({"alcoholic": True}, RejectionReason.ALCOHOL_NOT_PERMITTED),
        ({"thirst_change": 0.0}, RejectionReason.NO_THIRST_RELIEF),
    ],
)
def test_each_hard_filter_rejects_and_reports_its_reason(
    overrides: dict[str, Any], expected: RejectionReason
) -> None:
    bad = drink_item("bad", **overrides)
    spare = drink_item("spare", full_type="Base.WaterBottleSpare")

    selection = select_drink(inventory(bad, spare), thirsty_player(0.5))

    assert reason_for(selection, bad.ref) is expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"freshness": "rotten"}, RejectionReason.ROTTEN),
        ({"rot_progress": 0.95}, RejectionReason.ROTTEN),
        ({"frozen": True}, RejectionReason.FROZEN),
    ],
)
def test_a_drinkable_food_item_is_held_to_the_food_safety_rules(
    overrides: dict[str, Any], expected: RejectionReason
) -> None:
    # A soup or a carton of milk arrives as a `food` sub-object, and this
    # policy is the one that selects it — so spoilage has to be filtered here
    # or nothing filters it at all.
    soup = food_item(
        "soup",
        display_name="Vegetable Soup",
        full_type="Base.Soup",
        thirst_change=-0.4,
        **overrides,
    )

    selection = select_drink(inventory(soup), thirsty_player(0.9))

    assert selection.is_refusal
    assert reason_for(selection, soup.ref) is expected


def test_a_sound_drinkable_food_item_is_still_allowed() -> None:
    soup = food_item(
        "soup", display_name="Vegetable Soup", full_type="Base.Soup", thirst_change=-0.4
    )

    selection = select_drink(inventory(soup), thirsty_player(0.9))

    assert selection.choice is not None
    assert selection.choice.item_ref == soup.ref


def test_tainted_water_stays_refused_even_when_it_is_the_only_thing_left() -> None:
    tainted = drink_item("tainted", display_name="Tainted Water", tainted=True)

    selection = select_drink(inventory(tainted), thirsty_player(0.95))

    assert selection.is_refusal
    assert reason_for(selection, tainted.ref) is RejectionReason.TAINTED
    assert "permission" in selection.rejections[0].detail


def test_tainted_water_can_be_permitted_explicitly() -> None:
    tainted = drink_item("tainted", tainted=True)

    selection = select_drink(
        inventory(tainted),
        thirsty_player(0.8),
        PolicyConfig(allow_tainted_water=True),
    )

    assert selection.choice is not None
    assert selection.choice.item_ref == tainted.ref


def test_alcohol_is_permitted_only_by_configuration() -> None:
    beer = drink_item("beer", display_name="Beer", type="alcohol", water=False, alcohol_units=0.6)
    # A second bottle keeps the last-container rule out of this assertion.
    crate = drink_item(
        "crate", display_name="Beer", full_type="Base.BeerCrate", type="alcohol", water=False
    )

    refused = select_drink(inventory(beer, crate), thirsty_player(0.5))
    assert refused.is_refusal
    assert reason_for(refused, beer.ref) is RejectionReason.ALCOHOL_NOT_PERMITTED

    permitted = select_drink(
        inventory(beer, crate),
        thirsty_player(0.5),
        PolicyConfig(allow_alcohol_for_thirst=True),
    )
    assert permitted.choice is not None


@pytest.mark.parametrize(
    "marker",
    [{"favorite": True}, {"tags": ["reserved"]}, {"extra": {"reserved": True}}],
)
def test_user_reserved_containers_are_never_taken(marker: dict[str, Any]) -> None:
    kept = drink_item("kept", **marker)
    spare = drink_item("spare", full_type="Base.WaterBottleSpare")

    selection = select_drink(inventory(kept, spare), thirsty_player(0.9))

    assert reason_for(selection, kept.ref) is RejectionReason.USER_RESERVED


def test_strategic_reserve_opens_only_at_critical_thirst() -> None:
    reserve = drink_item("reserve", tags=["emergency"])

    held = select_drink(inventory(reserve), thirsty_player(0.5))
    assert held.is_refusal
    assert reason_for(held, reserve.ref) is RejectionReason.STRATEGIC_RESERVE

    opened = select_drink(inventory(reserve), thirsty_player(0.85))
    assert opened.choice is not None


def test_unreachable_container_is_refused() -> None:
    stray = drink_item("stray", container_ref=BACKPACK_REF)

    selection = select_drink(inventory(stray, containers=[main_container()]), thirsty_player(0.5))

    assert selection.is_refusal
    assert reason_for(selection, stray.ref) is RejectionReason.CONTAINER_UNREACHABLE


def test_the_last_container_is_not_emptied_for_a_thirst_that_can_wait() -> None:
    only = drink_item("only", display_name="Last Bottle")

    selection = select_drink(inventory(only), thirsty_player(0.5))

    assert selection.is_refusal
    assert reason_for(selection, only.ref) is RejectionReason.LAST_CONTAINER_RESERVE
    assert CAPABILITY_DRINK_PERCENTAGE in selection.rejections[0].detail


def test_the_last_container_is_emptied_when_thirst_is_critical() -> None:
    only = drink_item("only")

    selection = select_drink(inventory(only), thirsty_player(0.85))

    choice = selection.choice
    assert choice is not None
    assert choice.fraction == 1.0
    assert not choice.portioned


def test_a_whole_container_choice_cannot_carry_a_partial_fraction() -> None:
    """The capability-honesty rule, expressed as an invariant on the type.

    ``_choose_fraction`` returns a whole container precisely when the build has
    no ``drink_percentage`` probe: such a build cannot pour half a bottle, so a
    choice that says "not portioned" and "half" at once describes a command the
    mod cannot execute. The constructor is what stops any producer — this one or
    a later one — from contradicting that.

    The range check beside it is not the same rule and does not cover this: 0.5
    is a perfectly good fraction, and the adapter's own clamp checks the range
    too. What neither checks is the *pairing*. Deleting this half left the whole
    suite green.

    Built by taking a real portioned choice apart with ``replace``, which re-runs
    ``__post_init__`` — so what is asserted is that the invariant holds for
    anything anyone constructs, not only for what this module happens to return
    today.
    """
    only = drink_item("only", thirst_change=-1.0)
    selection = select_drink(inventory(only), thirsty_player(0.6), capabilities=PARTIAL)
    choice = selection.choice
    assert choice is not None and choice.portioned and choice.fraction < 1.0

    with pytest.raises(ValueError, match=r"whole-container choice must have fraction 1\.0"):
        replace(choice, portioned=False)


def test_partial_drinking_caps_the_last_container_instead_of_refusing_it() -> None:
    only = drink_item("only", thirst_change=-1.0)

    # Thirst 0.6 leaves a 0.45 deficit, which would otherwise take 75% of the
    # bottle; the last-container rule holds it to half.
    selection = select_drink(inventory(only), thirsty_player(0.6), capabilities=PARTIAL)

    choice = selection.choice
    assert choice is not None
    assert choice.portioned
    assert choice.fraction == PolicyConfig().max_last_container_fraction
    assert "last drink in reach" in choice.rationale


def test_a_second_container_lifts_the_last_container_rule() -> None:
    first, second = two_bottles()

    selection = select_drink(inventory(first, second), thirsty_player(0.5))

    assert selection.choice is not None
    assert not selection.is_refusal


def test_remaining_volume_is_scored() -> None:
    full = drink_item("full", display_name="Full Bottle", full_type="Base.BottleA")
    nearly_empty = drink_item(
        "low",
        display_name="Nearly Empty Bottle",
        full_type="Base.BottleB",
        remaining_units=0.2,
    )

    selection = select_drink(inventory(nearly_empty, full), thirsty_player(0.5))

    assert selection.choice is not None
    assert selection.choice.item_ref == full.ref


def test_a_container_in_a_bag_reports_that_it_must_be_moved_first() -> None:
    bagged = drink_item("bagged", container_ref=BACKPACK_REF)
    spare = drink_item("spare", container_ref=BACKPACK_REF, full_type="Base.BottleSpare")

    selection = select_drink(
        inventory(bagged, spare, containers=[main_container(), backpack_container()]),
        thirsty_player(0.5),
    )

    choice = selection.choice
    assert choice is not None
    assert choice.needs_transfer_to_main
    assert "main inventory" in choice.rationale


def test_world_water_sources_are_reported_but_never_assumed() -> None:
    unprobed = select_drink(InventoryView(), thirsty_player(0.6))
    assert unprobed.world_source_state is CapabilityState.UNSUPPORTED
    assert not unprobed.world_source_usable
    assert CAPABILITY_DRINK_WORLD_SOURCE in unprobed.explain()

    probed = select_drink(InventoryView(), thirsty_player(0.6), capabilities=WORLD_SOURCE)
    assert probed.is_refusal
    assert probed.world_source_usable
    assert "world water source" in probed.explain()


def test_non_drink_items_are_not_reported_as_rejections() -> None:
    hammer = make_item(
        policy_item_ref("hammer"), MAIN_REF, full_type="Base.Hammer", category="Item", food=None
    )

    selection = select_drink(inventory(hammer), thirsty_player(0.5))

    assert selection.is_refusal
    assert selection.rejections == ()


def test_selection_is_identical_under_shuffled_input() -> None:
    items = [
        drink_item("a", display_name="Bottle A", full_type="Base.BottleA"),
        drink_item("b", display_name="Bottle B", full_type="Base.BottleB", remaining_units=0.5),
        drink_item("c", display_name="Tainted", full_type="Base.Tainted", tainted=True),
        drink_item("d", display_name="Beer", full_type="Base.Beer", water=False, alcoholic=True),
        drink_item("e", display_name="Pop", full_type="Base.Pop", water=False, type="soda"),
        drink_item("f", display_name="Emergency", full_type="Base.Emerg", tags=["emergency"]),
    ]
    player = thirsty_player(0.6)
    baseline = select_drink(inventory(*items), player, capabilities=PARTIAL)

    for seed in range(8):
        shuffled = list(items)
        random.Random(seed).shuffle(shuffled)
        result = select_drink(inventory(*shuffled), player, capabilities=PARTIAL)

        assert result.choice is not None and baseline.choice is not None
        assert result.choice.item_ref == baseline.choice.item_ref
        assert result.choice.fraction == baseline.choice.fraction
        assert [c.item_ref for c in result.ranked] == [c.item_ref for c in baseline.ranked]


def test_ties_break_on_the_item_reference() -> None:
    first = drink_item("aaa", display_name="Twin", full_type="Base.Twin")
    second = drink_item("zzz", display_name="Twin", full_type="Base.Twin")

    forward = select_drink(inventory(first, second), thirsty_player(0.5))
    backward = select_drink(inventory(second, first), thirsty_player(0.5))

    assert forward.choice is not None and backward.choice is not None
    assert forward.choice.item_ref == backward.choice.item_ref == min(first.ref, second.ref)


def test_the_rejection_list_is_bounded_and_says_what_it_left_out() -> None:
    config = PolicyConfig(max_reported_rejections=2)
    tainted = [
        drink_item(f"t{index}", full_type=f"Base.Tainted{index}", tainted=True)
        for index in range(9)
    ]

    selection = select_drink(inventory(*tainted), thirsty_player(0.5), config)

    assert selection.is_refusal
    assert len(selection.rejections) == 2
    assert selection.rejected_count == 9
    assert selection.rejections_truncated
    assert "7 further rejected candidates" in selection.explain()
    assert selection.as_dict()["rejected_count"] == 9


def test_a_selection_cannot_undercount_the_candidates_it_refused() -> None:
    rejection = select_drink(
        inventory(drink_item("t", tainted=True)), thirsty_player(0.5)
    ).rejections[0]

    with pytest.raises(ValueError, match="is below the"):
        DrinkSelection(
            choice=None,
            reason_code=ReasonCode.NO_SAFE_DRINK,
            rejections=(rejection,),
            ranked=(),
            world_source_state=CapabilityState.UNSUPPORTED,
            rejected_count=0,
        )


def test_a_selection_is_either_a_choice_or_a_refusal() -> None:
    with pytest.raises(ValueError, match="never both or neither"):
        DrinkSelection(
            choice=None,
            reason_code=None,
            rejections=(),
            ranked=(),
            world_source_state=CapabilityState.UNSUPPORTED,
        )
