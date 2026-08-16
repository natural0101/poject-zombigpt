"""Loot selection: the classification table, every leave reason, determinism.

The dangerous failures here are quiet ones: a reserve rule that ``take_all``
silently overrides, a capacity check that drifts off by one item, an input
item that vanishes without a recorded reason. So every rule gets a case that
asserts the *reason*, not merely the outcome, and two property-style tests pin
the accounting invariant — each input item exactly once, never on both sides.
"""

from __future__ import annotations

import random

import pytest

from pz_agent_core.actions.adapters.inventory import MAX_BATCH_ITEMS
from pz_agent_core.loot import (
    DEFAULT_LOOT_POLICY,
    DEFAULT_WANTED,
    MAX_ITEMS_PER_CONTAINER,
    MAX_SELECT_CONTENTS,
    LeaveReason,
    LootCategory,
    LootPolicy,
    Selection,
    SelectionSummary,
    classify,
    select,
    summarise,
)
from pz_agent_core.protocol import ItemView
from tests.fixtures import make_item

CONTAINER = "container:kitchen:counter"


def _item(
    ref: str,
    *,
    full_type: str = "Base.TinnedBeans",
    display_name: str = "Tinned Beans",
    category: str = "Food",
    weight: float = 0.5,
) -> ItemView:
    return make_item(
        ref,
        CONTAINER,
        full_type=full_type,
        display_name=display_name,
        category=category,
        weight=weight,
    )


def _nothing_reserved(_full_type: str) -> bool:
    return False


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("game_category", "expected"),
    [
        ("Food", LootCategory.FOOD),
        ("Water", LootCategory.WATER),
        ("Weapon", LootCategory.WEAPONS),
        ("FirstAid", LootCategory.MEDICAL),
        ("Medical", LootCategory.MEDICAL),
        ("Tool", LootCategory.TOOLS),
        ("Material", LootCategory.MATERIALS),
        ("Literature", LootCategory.LITERATURE),
        ("Clothing", LootCategory.CLOTHING),
    ],
)
def test_classify_maps_each_game_category(game_category: str, expected: LootCategory) -> None:
    item = _item("item:1", full_type="Base.Whatever", category=game_category)
    assert classify(item) is expected


@pytest.mark.parametrize("spelling", ["food", "FOOD", "  Food  "])
def test_classify_game_category_is_case_and_space_insensitive(spelling: str) -> None:
    assert classify(_item("item:1", category=spelling)) is LootCategory.FOOD


@pytest.mark.parametrize(
    "full_type",
    ["Base.WaterBottleFull", "Base.WaterBottleEmpty", "Base.PopBottle", "Base.PopBottleEmpty"],
)
def test_classify_water_bottle_prefixes(full_type: str) -> None:
    # Display category "Item" is not in the table, so the prefix rule decides.
    item = _item("item:1", full_type=full_type, category="Item")
    assert classify(item) is LootCategory.WATER


def test_classify_game_category_beats_full_type_prefix() -> None:
    # The game's own label is authoritative: a WaterBottle the game files
    # under Food is FOOD, and the prefix heuristic never gets a look.
    item = _item("item:1", full_type="Base.WaterBottleFull", category="Food")
    assert classify(item) is LootCategory.FOOD


@pytest.mark.parametrize(
    ("full_type", "category"),
    [
        ("Base.Spiffo", "Item"),
        ("Base.Doodad", "Junk"),
        ("Base.WaterB", "Item"),  # almost-prefix must not match
        ("Mod.WaterBottleFull", "Item"),  # prefix is anchored at the start
    ],
)
def test_classify_unknown_is_other_not_a_guess(full_type: str, category: str) -> None:
    assert classify(_item("item:1", full_type=full_type, category=category)) is LootCategory.OTHER


def test_category_declaration_order_is_the_take_priority() -> None:
    # select() spends capacity in this order; reordering the enum is a
    # behaviour change and must show up as a failing test, not a surprise.
    assert list(LootCategory) == [
        LootCategory.FOOD,
        LootCategory.WATER,
        LootCategory.MEDICAL,
        LootCategory.WEAPONS,
        LootCategory.TOOLS,
        LootCategory.MATERIALS,
        LootCategory.LITERATURE,
        LootCategory.CLOTHING,
        LootCategory.OTHER,
    ]


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------


def test_default_wanted_is_useful_only() -> None:
    expected = frozenset(
        {
            LootCategory.FOOD,
            LootCategory.WATER,
            LootCategory.MEDICAL,
            LootCategory.TOOLS,
            LootCategory.WEAPONS,
            LootCategory.MATERIALS,
            LootCategory.LITERATURE,
        }
    )
    assert expected == DEFAULT_WANTED
    assert LootCategory.CLOTHING not in DEFAULT_WANTED
    assert LootCategory.OTHER not in DEFAULT_WANTED


def test_empty_wanted_means_the_default_set() -> None:
    assert LootPolicy().effective_wanted == DEFAULT_WANTED
    assert DEFAULT_LOOT_POLICY.effective_wanted == DEFAULT_WANTED


def test_explicit_wanted_narrows_the_set() -> None:
    policy = LootPolicy(wanted=frozenset({LootCategory.MEDICAL}))
    assert policy.effective_wanted == frozenset({LootCategory.MEDICAL})


def test_take_all_wants_every_category() -> None:
    assert LootPolicy(take_all=True).effective_wanted == frozenset(LootCategory)


def test_batch_width_matches_the_transfer_batch_bound() -> None:
    # Pinned rather than imported inside the loot package, so loot stays a
    # leaf module; this test is the sync check.
    assert MAX_ITEMS_PER_CONTAINER == MAX_BATCH_ITEMS
    assert LootPolicy().max_items_per_container == MAX_BATCH_ITEMS


@pytest.mark.parametrize("bad", [0, -1, MAX_ITEMS_PER_CONTAINER + 1])
def test_policy_rejects_out_of_bound_batch_width(bad: int) -> None:
    with pytest.raises(ValueError, match="max_items_per_container"):
        LootPolicy(max_items_per_container=bad)


# --------------------------------------------------------------------------
# selection: wanted and reserves
# --------------------------------------------------------------------------


def test_unwanted_categories_left_with_not_wanted() -> None:
    contents = [
        _item("item:beans"),
        _item("item:shirt", full_type="Base.Shirt", display_name="Shirt", category="Clothing"),
        _item("item:junk", full_type="Base.Doodad", display_name="Doodad", category="Junk"),
    ]
    result = select(
        contents,
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=None,
        is_reserved=_nothing_reserved,
    )
    assert result.take_refs == ("item:beans",)
    assert {(entry.item_ref, entry.reason) for entry in result.leave} == {
        ("item:shirt", LeaveReason.NOT_WANTED),
        ("item:junk", LeaveReason.NOT_WANTED),
    }


def test_take_all_takes_clothing_and_other() -> None:
    contents = [
        _item("item:shirt", full_type="Base.Shirt", category="Clothing"),
        _item("item:junk", full_type="Base.Doodad", category="Junk"),
    ]
    result = select(
        contents,
        policy=LootPolicy(take_all=True),
        free_capacity=None,
        is_reserved=_nothing_reserved,
    )
    assert set(result.take_refs) == {"item:shirt", "item:junk"}
    assert result.leave == ()


def test_reserved_never_taken_even_under_take_all() -> None:
    contents = [_item("item:beans"), _item("item:soup", full_type="Base.TinnedSoup")]
    result = select(
        contents,
        policy=LootPolicy(take_all=True),
        free_capacity=None,
        is_reserved=lambda full_type: full_type == "Base.TinnedSoup",
    )
    assert result.take_refs == ("item:beans",)
    (left,) = result.leave
    assert (left.item_ref, left.reason) == ("item:soup", LeaveReason.RESERVED)


def test_reserved_beats_not_wanted_in_reason_precedence() -> None:
    # A reserved shirt is RESERVED, not NOT_WANTED: the user's word is the
    # most fundamental obstacle and the one the report must show.
    contents = [_item("item:shirt", full_type="Base.Shirt", category="Clothing")]
    result = select(
        contents,
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=None,
        is_reserved=lambda full_type: full_type == "Base.Shirt",
    )
    assert result.leave[0].reason is LeaveReason.RESERVED


def test_respect_reserves_off_is_an_explicit_override() -> None:
    contents = [_item("item:soup", full_type="Base.TinnedSoup")]
    result = select(
        contents,
        policy=LootPolicy(respect_reserves=False),
        free_capacity=None,
        is_reserved=lambda _full_type: True,
    )
    assert result.take_refs == ("item:soup",)


# --------------------------------------------------------------------------
# selection: capacity
# --------------------------------------------------------------------------


def test_capacity_greedy_boundary_exact_fit_taken_next_over_capacity() -> None:
    contents = [
        _item("item:a", weight=0.4),
        _item("item:b", weight=0.6),
        _item("item:c", weight=0.7),
    ]
    result = select(
        contents,
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=1.0,
        is_reserved=_nothing_reserved,
    )
    # Weight-ascending order: 0.4 then 0.6 lands exactly on the budget —
    # an exact fit is a fit — and 0.7 no longer fits but would have alone.
    assert result.take_refs == ("item:a", "item:b")
    (left,) = result.leave
    assert (left.item_ref, left.reason) == ("item:c", LeaveReason.OVER_CAPACITY)


@pytest.mark.parametrize("poison", [float("nan"), float("inf"), -1.0])
def test_a_weight_that_is_not_a_number_cannot_poison_the_whole_selection(
    poison: float,
) -> None:
    """Item weights come off the wire and the wire is untrusted.

    ``protocol/messages.py::_as_float`` checks the JSON type and nothing else,
    so NaN, infinity and negatives all reach here. One of them does two things
    at once inside ``select``: NaN compares false against everything, which
    destroys the total order the weight-ascending sort depends on, and it
    poisons the running capacity budget for every later item — a whole
    selection corrupted by one broken entry.

    The policy's answer is to read such a weight as weightless, which is the
    smaller loss and is what the docstring promises. Deleting the guard left the
    whole suite green.

    What is pinned is that the *other* items are still decided correctly, not
    the fate of the broken one: over-taking one item is the accepted cost.
    """
    contents = [
        _item("item:light", weight=0.6),
        _item("item:broken", weight=poison),
        _item("item:heavy", weight=0.7),
    ]

    result = select(
        contents,
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=1.0,
        is_reserved=_nothing_reserved,
    )

    # Two assertions, because the three kinds of rubbish fail differently and
    # neither alone catches all of them: NaN and a negative make the budget
    # accept an item that does not fit, infinity makes it reject one that does.
    # Measured — a first version asserted only that every item was decided, and
    # passed with the guard deleted.
    assert "item:broken" in result.take_refs, (
        "an unusable weight was allowed into the capacity arithmetic instead of "
        "being read as weightless"
    )
    assert "item:heavy" in {entry.item_ref for entry in result.leave}, (
        "0.6 + 0.7 does not fit in 1.0, but one broken weight made the budget say it did"
    )


def test_uncarriable_is_distinct_from_over_capacity() -> None:
    contents = [
        _item("item:light", weight=0.6),
        _item("item:crate", weight=1.5),  # exceeds the whole budget alone
        _item("item:mid", weight=0.7),  # would fit alone, not after 0.6
    ]
    result = select(
        contents,
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=1.0,
        is_reserved=_nothing_reserved,
    )
    assert result.take_refs == ("item:light",)
    reasons = {entry.item_ref: entry.reason for entry in result.leave}
    assert reasons == {
        "item:crate": LeaveReason.UNCARRIABLE,
        "item:mid": LeaveReason.OVER_CAPACITY,
    }


def test_greedy_keeps_scanning_lower_priority_items_after_a_miss() -> None:
    contents = [
        _item("item:ham", weight=0.9),
        _item("item:bottle", full_type="Base.WaterBottleFull", category="Water", weight=0.5),
        _item("item:cup", full_type="Base.WaterBottleFull", category="Water", weight=0.1),
    ]
    result = select(
        contents,
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=1.0,
        is_reserved=_nothing_reserved,
    )
    # FOOD outranks WATER, then within WATER the lighter bottle fits the
    # 0.1 that is left; the 0.5 one is refused but not the whole scan.
    assert result.take_refs == ("item:ham", "item:cup")
    (left,) = result.leave
    assert (left.item_ref, left.reason) == ("item:bottle", LeaveReason.OVER_CAPACITY)


def test_unknown_capacity_takes_up_to_the_batch_cap() -> None:
    contents = [_item(f"item:{index}", weight=50.0) for index in range(10)]
    result = select(
        contents,
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=None,
        is_reserved=_nothing_reserved,
    )
    assert len(result.take) == MAX_ITEMS_PER_CONTAINER
    assert {entry.reason for entry in result.leave} == {LeaveReason.BATCH_FULL}


def test_batch_cap_leaves_the_rest_as_batch_full() -> None:
    contents = [_item(f"item:{index}", weight=0.1) for index in range(10)]
    result = select(
        contents,
        policy=LootPolicy(max_items_per_container=3),
        free_capacity=100.0,
        is_reserved=_nothing_reserved,
    )
    assert len(result.take) == 3
    assert [entry.reason for entry in result.leave] == [LeaveReason.BATCH_FULL] * 7


def test_uncarriable_reported_even_when_the_batch_is_already_full() -> None:
    # UNCARRIABLE is a property of the item, BATCH_FULL of the moment; the
    # fixed precedence keeps the more fundamental reason on the report.
    contents = [_item(f"item:{index}", weight=0.1) for index in range(8)]
    contents.append(_item("item:crate", weight=9.0))
    result = select(
        contents,
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=5.0,
        is_reserved=_nothing_reserved,
    )
    reasons = {entry.item_ref: entry.reason for entry in result.leave}
    assert reasons["item:crate"] is LeaveReason.UNCARRIABLE


@pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf")])
def test_select_refuses_a_nonsense_capacity(bad: float) -> None:
    with pytest.raises(ValueError, match="free_capacity"):
        select(
            [_item("item:1")],
            policy=DEFAULT_LOOT_POLICY,
            free_capacity=bad,
            is_reserved=_nothing_reserved,
        )


def test_select_is_bounded() -> None:
    contents = [_item(f"item:{index}") for index in range(MAX_SELECT_CONTENTS + 1)]
    with pytest.raises(ValueError, match="bounded"):
        select(
            contents,
            policy=DEFAULT_LOOT_POLICY,
            free_capacity=None,
            is_reserved=_nothing_reserved,
        )


# --------------------------------------------------------------------------
# selection: deterministic order
# --------------------------------------------------------------------------


def _mixed_contents() -> list[ItemView]:
    return [
        _item("item:bandage", full_type="Base.Bandage", category="FirstAid", weight=0.1),
        _item("item:beans", weight=0.8),
        _item("item:chips", full_type="Base.Crisps", display_name="Crisps", weight=0.3),
        _item("item:bottle", full_type="Base.WaterBottleFull", category="Water", weight=1.0),
        _item("item:hammer", full_type="Base.Hammer", category="Tool", weight=1.5),
        _item("item:shirt", full_type="Base.Shirt", category="Clothing", weight=0.5),
        _item("item:soup", full_type="Base.TinnedSoup", display_name="Tinned Soup", weight=0.8),
        _item("item:junk", full_type="Base.Doodad", category="Junk", weight=0.2),
    ]


def test_take_order_is_priority_then_weight_then_type_then_name() -> None:
    result = select(
        _mixed_contents(),
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=None,
        is_reserved=_nothing_reserved,
    )
    # FOOD by weight (0.3 crisps, then the two 0.8s split on full_type:
    # Base.TinnedBeans < Base.TinnedSoup), then WATER, MEDICAL, TOOLS.
    assert result.take_refs == (
        "item:chips",
        "item:beans",
        "item:soup",
        "item:bottle",
        "item:bandage",
        "item:hammer",
    )


def test_identical_items_fall_back_to_the_unique_ref() -> None:
    # Same type, name and weight: the ref is the last sort key, so even a
    # pathological tie has one deterministic outcome.
    contents = [_item("item:b"), _item("item:a"), _item("item:c")]
    result = select(
        contents,
        policy=LootPolicy(max_items_per_container=2),
        free_capacity=None,
        is_reserved=_nothing_reserved,
    )
    assert result.take_refs == ("item:a", "item:b")
    assert result.leave[0].item_ref == "item:c"


def test_shuffled_input_selects_identically_run_twice() -> None:
    contents = _mixed_contents()
    baseline = select(
        contents,
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=2.5,
        is_reserved=_nothing_reserved,
    )
    rng = random.Random(42)
    for _ in range(5):
        shuffled = list(contents)
        rng.shuffle(shuffled)
        for _run in range(2):
            result = select(
                shuffled,
                policy=DEFAULT_LOOT_POLICY,
                free_capacity=2.5,
                is_reserved=_nothing_reserved,
            )
            assert result == baseline


# --------------------------------------------------------------------------
# accounting invariants
# --------------------------------------------------------------------------


def _assert_accounted_exactly_once(contents: list[ItemView], result: Selection) -> None:
    taken = [pick.item_ref for pick in result.take]
    left = [entry.item_ref for entry in result.leave]
    assert len(taken) + len(left) == len(contents)
    assert not set(taken) & set(left), "an item is both taken and left"
    assert sorted(taken + left) == sorted(item.ref for item in contents)


@pytest.mark.parametrize("free_capacity", [None, 0.0, 1.3, 100.0])
def test_every_item_accounted_exactly_once(free_capacity: float | None) -> None:
    contents = _mixed_contents()
    result = select(
        contents,
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=free_capacity,
        is_reserved=lambda full_type: full_type == "Base.Hammer",
    )
    _assert_accounted_exactly_once(contents, result)


def test_empty_contents_select_to_nothing() -> None:
    result = select(
        [],
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=None,
        is_reserved=_nothing_reserved,
    )
    assert result == Selection(take=(), leave=())
    summary = summarise(result)
    assert summary.taken_count == 0
    assert summary.left_count == 0
    assert summary.taken_weight == 0.0


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


def test_summary_arithmetic() -> None:
    result = select(
        _mixed_contents(),
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=None,
        is_reserved=_nothing_reserved,
    )
    summary = summarise(result)
    assert summary.taken_by_category == {
        LootCategory.FOOD: 3,
        LootCategory.WATER: 1,
        LootCategory.MEDICAL: 1,
        LootCategory.TOOLS: 1,
    }
    assert summary.left_by_category == {
        LootCategory.CLOTHING: 1,
        LootCategory.OTHER: 1,
    }
    assert summary.taken_count == 6
    assert summary.left_count == 2
    assert summary.taken_weight == pytest.approx(0.3 + 0.8 + 0.8 + 1.0 + 0.1 + 1.5)


def test_summary_refuses_totals_that_disagree_with_the_breakdown() -> None:
    with pytest.raises(ValueError, match="taken_count"):
        SelectionSummary(
            taken_by_category={LootCategory.FOOD: 2},
            left_by_category={},
            taken_count=1,
            left_count=0,
            taken_weight=0.5,
        )


def test_summary_as_dict_renders_categories_in_priority_order() -> None:
    result = select(
        _mixed_contents(),
        policy=DEFAULT_LOOT_POLICY,
        free_capacity=None,
        is_reserved=_nothing_reserved,
    )
    payload = summarise(result).as_dict()
    taken = payload["taken_by_category"]
    assert isinstance(taken, dict)
    assert list(taken) == ["FOOD", "WATER", "MEDICAL", "TOOLS"]
    assert payload["taken_count"] == 6
