"""Crafting selection: what is known, what is held, and whose it is.

Every refusal here is checked for the *reason* it reports, because the
explanation is the deliverable: "you are one plank short, and two more are
reserved" is what the agent says out loud when it declines to spend anything.

The three tri-states get their own tests. An unreadable ``known`` is not a
learned recipe, an unreadable ``needs_surface`` is a craft that may need a
workbench, and a material list that cannot be read whole makes the recipe no
candidate at all — three different ways of not knowing, none of which is
allowed to read as the convenient answer.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from pz_agent_core.policy.config import PolicyConfig
from pz_agent_core.policy.crafting import (
    CRAFTING_KEY,
    MAX_CANDIDATE_RECIPES,
    MAX_MATERIALS_PER_RECIPE,
    MAX_RECIPES_PER_ITEM,
    CraftingDecision,
    CraftingRefusal,
    CraftingVerdict,
    MaterialNeed,
    RecipeView,
    assess_craft,
    assess_recipe,
    observed_recipes,
    recipe_named,
    recipes_for_product,
)
from pz_agent_core.protocol import ContainerKind, InventoryView, ItemView, Observation
from tests.fixtures.adapter_worlds import (
    CRATE_REF,
    MAIN_REF,
    a_world,
    an_item,
    crate_container,
    main_container,
)

SPEAR = "Base.SpearCrude"
BRANCH = "Base.TreeBranch"
TWINE = "Base.Twine"


def recipe_payload(
    name: str = "MakeSpear",
    *,
    product: str = SPEAR,
    materials: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A recipe the character knows, needing one branch, craftable in hand."""
    payload: dict[str, Any] = {
        "name": name,
        "product": product,
        "display_name": "Make Crude Spear",
        "known": True,
        "needs_surface": False,
        "materials": materials if materials is not None else [{"full_type": BRANCH, "count": 1}],
    }
    payload.update(overrides)
    return payload


def material(
    runtime_id: str = "m1",
    *,
    full_type: str = BRANCH,
    container_ref: str = MAIN_REF,
    recipes: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> ItemView:
    """One material item, carrying the crafting readout it participates in."""
    block: dict[str, Any] = {
        "recipe_count": 1,
        "known_recipe_count": 1,
        "recipes": recipes if recipes is not None else [recipe_payload()],
    }
    return an_item(
        runtime_id=runtime_id,
        container_ref=container_ref,
        full_type=full_type,
        display_name=full_type,
        category="Item",
        extra={CRAFTING_KEY: block},
        **overrides,
    )


def plain(runtime_id: str, full_type: str = BRANCH, **overrides: Any) -> ItemView:
    """A material item carrying no readout of its own — most of them do not."""
    return an_item(
        runtime_id=runtime_id,
        full_type=full_type,
        display_name=full_type,
        category="Item",
        **overrides,
    )


def world(*items: ItemView, containers: list[Any] | None = None) -> Observation:
    return a_world(
        items=list(items),
        containers=containers if containers is not None else [main_container(), crate_container()],
    )


def only_recipe(observation: Observation) -> RecipeView:
    inventory = observation.inventory
    assert inventory is not None
    recipes = observed_recipes(inventory)
    assert len(recipes) == 1
    return recipes[0]


# --------------------------------------------------------------------------
# reading the readout
# --------------------------------------------------------------------------


def test_the_same_recipe_reported_by_two_materials_is_read_once() -> None:
    payload = recipe_payload(
        materials=[{"full_type": BRANCH, "count": 1}, {"full_type": TWINE, "count": 1}]
    )
    observation = world(
        material("m1", full_type=BRANCH, recipes=[payload]),
        material("m2", full_type=TWINE, recipes=[payload]),
    )

    inventory = observation.inventory
    assert inventory is not None
    assert [r.name for r in observed_recipes(inventory)] == ["MakeSpear"]


def test_a_recipe_that_cannot_say_what_it_makes_is_not_a_candidate() -> None:
    observation = world(material("m1", recipes=[recipe_payload(product="")]))

    inventory = observation.inventory
    assert inventory is not None
    assert observed_recipes(inventory) == ()


@pytest.mark.parametrize(
    "materials",
    [
        pytest.param([], id="empty"),
        pytest.param([{"full_type": BRANCH, "count": 0}], id="zero-count"),
        pytest.param([{"full_type": "", "count": 1}], id="unnamed-type"),
        pytest.param(["Base.TreeBranch"], id="not-an-object"),
        pytest.param(
            [
                {"full_type": f"Base.Thing{n}", "count": 1}
                for n in range(MAX_MATERIALS_PER_RECIPE + 1)
            ],
            id="past-the-bound",
        ),
    ],
)
def test_a_requirement_list_that_cannot_be_read_whole_drops_the_recipe(
    materials: list[Any],
) -> None:
    """All-or-nothing: a half-read list would understate what the craft spends."""
    observation = world(material("m1", recipes=[recipe_payload(materials=materials)]))

    inventory = observation.inventory
    assert inventory is not None
    assert observed_recipes(inventory) == ()


def test_the_readout_is_bounded_per_item() -> None:
    many = [
        recipe_payload(f"Recipe{index:02d}", product=f"Base.Thing{index:02d}")
        for index in range(MAX_RECIPES_PER_ITEM + 5)
    ]
    observation = world(material("m1", recipes=many))

    inventory = observation.inventory
    assert inventory is not None
    assert len(observed_recipes(inventory)) == MAX_RECIPES_PER_ITEM


def test_recipe_named_finds_the_exact_name_and_nothing_else() -> None:
    observation = world(material("m1", recipes=[recipe_payload("MakeSpear")]))
    inventory = observation.inventory
    assert inventory is not None

    assert recipe_named(inventory, "MakeSpear") is not None
    assert recipe_named(inventory, "makespear") is None


# --------------------------------------------------------------------------
# the three refusals
# --------------------------------------------------------------------------


def test_a_known_recipe_with_its_materials_held_may_run() -> None:
    observation = world(material("m1"))

    decision = assess_craft(observation, SPEAR)

    assert decision.verdict is CraftingVerdict.CRAFT
    assert decision.refusal is None
    assert decision.recipe is not None
    assert decision.recipe.name == "MakeSpear"
    assert decision.shortfalls == ()
    assert not decision.needs_travel


def test_a_recipe_the_character_has_not_learned_refuses_as_unknown() -> None:
    observation = world(material("m1", recipes=[recipe_payload(known=False)]))

    decision = assess_craft(observation, SPEAR)

    assert decision.refusal is CraftingRefusal.RECIPE_UNKNOWN
    assert "has not learned" in decision.detail


def test_an_unreadable_known_flag_is_not_a_learned_recipe() -> None:
    """Absent is unknown. Assuming otherwise spends materials on a guess."""
    payload = recipe_payload()
    del payload["known"]
    observation = world(material("m1", recipes=[payload]))

    decision = assess_craft(observation, SPEAR)

    assert decision.refusal is CraftingRefusal.RECIPE_UNKNOWN
    assert "did not report" in decision.detail
    assert decision.recipe is not None
    assert decision.recipe.known is None


def test_a_product_nothing_participates_in_refuses_as_unknown() -> None:
    observation = world(material("m1"))

    decision = assess_craft(observation, "Base.Katana")

    assert decision.refusal is CraftingRefusal.RECIPE_UNKNOWN
    assert decision.recipe is None
    assert decision.candidates_considered == 0


def test_missing_materials_are_named_with_how_many_are_short() -> None:
    payload = recipe_payload(materials=[{"full_type": BRANCH, "count": 3}])
    observation = world(material("m1", recipes=[payload]))

    decision = assess_craft(observation, SPEAR)

    assert decision.refusal is CraftingRefusal.MATERIALS_MISSING
    assert [s.as_dict() for s in decision.shortfalls] == [
        {"full_type": BRANCH, "needed": 3, "free": 1, "reserved": 0}
    ]
    assert "1 of 3 Base.TreeBranch" in decision.detail


@pytest.mark.parametrize(
    "marker",
    [{"favorite": True}, {"tags": ["reserved"]}, {"extra_reserved": True}],
)
def test_a_reserve_is_never_spent_and_gets_its_own_refusal(marker: dict[str, Any]) -> None:
    """A reserved item outranks a craving, exactly as it does for hunger."""
    payload = recipe_payload(materials=[{"full_type": BRANCH, "count": 2}])
    keeper: dict[str, Any] = dict(marker)
    extra: dict[str, Any] = {CRAFTING_KEY: {"recipes": [payload]}}
    if keeper.pop("extra_reserved", False):
        extra["reserved"] = True
    observation = world(
        material("m1", recipes=[payload]),
        an_item(
            runtime_id="m2",
            full_type=BRANCH,
            display_name=BRANCH,
            category="Item",
            extra=extra,
            **keeper,
        ),
    )

    decision = assess_craft(observation, SPEAR)

    assert decision.refusal is CraftingRefusal.MATERIALS_RESERVED
    assert decision.shortfalls[0].reserved == 1
    assert "you reserved" in decision.detail


def test_a_genuine_shortage_outranks_a_reserved_one() -> None:
    """Releasing the reserve would not fix this, so it is not the reserve's fault."""
    payload = recipe_payload(
        materials=[{"full_type": BRANCH, "count": 2}, {"full_type": TWINE, "count": 1}]
    )
    observation = world(
        material("m1", full_type=BRANCH, recipes=[payload]),
        an_item(
            runtime_id="m2",
            full_type=BRANCH,
            display_name=BRANCH,
            category="Item",
            favorite=True,
        ),
    )

    decision = assess_craft(observation, SPEAR)

    assert decision.refusal is CraftingRefusal.MATERIALS_MISSING


def test_a_reserve_can_be_released_by_configuration_only_by_untagging_it() -> None:
    """``user_reserve_tags`` is the switch; need never is."""
    payload = recipe_payload()
    observation = world(material("m1", recipes=[payload], tags=["reserved"]))

    assert assess_craft(observation, SPEAR).refusal is CraftingRefusal.MATERIALS_RESERVED
    permissive = PolicyConfig(user_reserve_tags=frozenset())
    assert assess_craft(observation, SPEAR, permissive).verdict is CraftingVerdict.CRAFT


# --------------------------------------------------------------------------
# reach, travel and the risk escalation this feeds
# --------------------------------------------------------------------------


def test_a_material_in_a_world_container_counts_but_means_travel() -> None:
    payload = recipe_payload()
    observation = world(
        material("m1", container_ref=CRATE_REF, recipes=[payload]),
        containers=[main_container(), crate_container()],
    )

    decision = assess_craft(observation, SPEAR)

    assert decision.verdict is CraftingVerdict.CRAFT
    assert decision.needs_travel


def test_a_material_in_a_container_the_mod_never_described_is_not_counted() -> None:
    payload = recipe_payload()
    observation = world(
        material("m1", recipes=[payload]),
        material("m2", container_ref=CRATE_REF, recipes=[payload]),
        containers=[main_container()],
    )

    decision = assess_recipe(observation, only_recipe(observation))

    # The one on the character is enough for a count of 1; the stray in the
    # undescribed crate simply is not there as far as this side is concerned.
    assert decision.verdict is CraftingVerdict.CRAFT
    assert decision.tallies[0].on_person == 1
    assert decision.tallies[0].in_world == 0


def test_a_material_in_an_inaccessible_container_is_not_counted() -> None:
    payload = recipe_payload(materials=[{"full_type": BRANCH, "count": 1}])
    observation = world(
        material("m1", container_ref=CRATE_REF, recipes=[payload]),
        containers=[main_container(), crate_container(accessible=False)],
    )

    decision = assess_craft(observation, SPEAR)

    assert decision.refusal is CraftingRefusal.MATERIALS_MISSING


def test_a_craft_that_may_need_a_surface_means_travel_even_with_everything_in_hand() -> None:
    payload = recipe_payload(needs_surface=True)
    observation = world(material("m1", recipes=[payload]))

    decision = assess_craft(observation, SPEAR)

    assert decision.verdict is CraftingVerdict.CRAFT
    assert decision.needs_travel


def test_an_unreadable_surface_flag_is_read_as_needing_one() -> None:
    payload = recipe_payload()
    del payload["needs_surface"]
    observation = world(material("m1", recipes=[payload]))

    decision = assess_craft(observation, SPEAR)

    assert decision.recipe is not None
    assert decision.recipe.needs_surface is None
    assert decision.needs_travel


# --------------------------------------------------------------------------
# choosing between recipes, deterministically and boundedly
# --------------------------------------------------------------------------


def test_a_known_recipe_is_preferred_over_one_that_may_not_be() -> None:
    unknown = recipe_payload("Aaa", known=None)
    del unknown["known"]
    known = recipe_payload("Zzz")
    observation = world(material("m1", recipes=[unknown, known]))

    decision = assess_craft(observation, SPEAR)

    assert decision.verdict is CraftingVerdict.CRAFT
    assert decision.recipe is not None
    assert decision.recipe.name == "Zzz"


def test_a_recipe_needing_no_surface_is_preferred_over_one_that_does() -> None:
    bench = recipe_payload("Aaa", needs_surface=True)
    in_hand = recipe_payload("Zzz", needs_surface=False)
    observation = world(material("m1", recipes=[bench, in_hand]))

    decision = assess_craft(observation, SPEAR)

    assert decision.recipe is not None
    assert decision.recipe.name == "Zzz"
    assert not decision.needs_travel


def test_the_shorter_requirement_list_wins_a_tie() -> None:
    long = recipe_payload(
        "Aaa", materials=[{"full_type": BRANCH, "count": 1}, {"full_type": TWINE, "count": 1}]
    )
    short = recipe_payload("Bbb", materials=[{"full_type": BRANCH, "count": 1}])
    observation = world(
        material("m1", full_type=BRANCH, recipes=[long, short]),
        plain("m2", full_type=TWINE),
    )

    decision = assess_craft(observation, SPEAR)

    assert decision.recipe is not None
    assert decision.recipe.name == "Bbb"


def test_a_short_first_choice_falls_through_to_one_that_can_actually_run() -> None:
    unaffordable = recipe_payload("Aaa", materials=[{"full_type": TWINE, "count": 4}])
    affordable = recipe_payload("Bbb", materials=[{"full_type": BRANCH, "count": 1}])
    observation = world(material("m1", full_type=BRANCH, recipes=[unaffordable, affordable]))

    decision = assess_craft(observation, SPEAR)

    assert decision.verdict is CraftingVerdict.CRAFT
    assert decision.recipe is not None
    assert decision.recipe.name == "Bbb"


def test_when_nothing_can_run_the_best_candidates_refusal_is_the_one_reported() -> None:
    """Known-first ranking is what makes the reported refusal the actionable one."""
    unknown = recipe_payload("Aaa", known=False, materials=[{"full_type": BRANCH, "count": 1}])
    known_but_short = recipe_payload("Zzz", materials=[{"full_type": TWINE, "count": 9}])
    observation = world(material("m1", full_type=BRANCH, recipes=[unknown, known_but_short]))

    decision = assess_craft(observation, SPEAR)

    assert decision.refusal is CraftingRefusal.MATERIALS_MISSING
    assert decision.recipe is not None
    assert decision.recipe.name == "Zzz"


def test_the_candidate_list_is_bounded_and_says_when_it_was_trimmed() -> None:
    many = [
        recipe_payload(f"Recipe{index:02d}", known=False)
        for index in range(MAX_CANDIDATE_RECIPES + 3)
    ]
    observation = world(material("m1", recipes=many))

    decision = assess_craft(observation, SPEAR)

    assert decision.candidates_considered == MAX_CANDIDATE_RECIPES
    assert decision.candidates_truncated


def test_the_decision_is_identical_under_shuffled_input() -> None:
    payloads = [
        recipe_payload("Bbb", materials=[{"full_type": TWINE, "count": 1}]),
        recipe_payload("Aaa", materials=[{"full_type": BRANCH, "count": 1}]),
        recipe_payload("Ccc", needs_surface=True),
    ]
    items = [
        material("m1", full_type=BRANCH, recipes=payloads),
        material("m2", full_type=TWINE, recipes=payloads),
        plain("m3", full_type=BRANCH),
    ]
    baseline = assess_craft(world(*items), SPEAR)

    for seed in range(8):
        shuffled = list(items)
        random.Random(seed).shuffle(shuffled)
        result = assess_craft(world(*shuffled), SPEAR)

        assert result.recipe is not None and baseline.recipe is not None
        assert result.recipe.name == baseline.recipe.name
        assert result.detail == baseline.detail


def test_ranking_is_a_total_order_over_the_recipe_name() -> None:
    first = recipe_payload("Aaa")
    second = recipe_payload("Bbb")
    observation = world(material("m1", recipes=[second, first]))
    inventory = observation.inventory
    assert inventory is not None

    assert [r.name for r in recipes_for_product(inventory, SPEAR)] == ["Aaa", "Bbb"]


# --------------------------------------------------------------------------
# totality and the value objects
# --------------------------------------------------------------------------


def test_an_observation_without_an_inventory_refuses_rather_than_raising() -> None:
    recipe = RecipeView(
        name="MakeSpear",
        product=SPEAR,
        display_name="Make Crude Spear",
        known=True,
        needs_surface=False,
        materials=(MaterialNeed(full_type=BRANCH, count=1),),
    )
    blind = a_world(no_inventory=True)

    assert assess_recipe(blind, recipe).refusal is CraftingRefusal.MATERIALS_MISSING
    assert assess_craft(blind, SPEAR).refusal is CraftingRefusal.RECIPE_UNKNOWN


def test_an_item_with_no_readout_contributes_nothing_and_raises_nothing() -> None:
    observation = world(plain("m1"), an_item("m2", extra={CRAFTING_KEY: "not a table"}))

    inventory = observation.inventory
    assert inventory is not None
    assert observed_recipes(inventory) == ()


def test_a_material_line_must_name_a_type_and_a_real_count() -> None:
    with pytest.raises(ValueError, match="name the item type"):
        MaterialNeed(full_type="", count=1)
    with pytest.raises(ValueError, match="at least one item"):
        MaterialNeed(full_type=BRANCH, count=0)


def test_a_decision_carries_its_refusal_token_and_only_a_refusal_does() -> None:
    with pytest.raises(ValueError, match="a refusal carries its token"):
        CraftingDecision(
            verdict=CraftingVerdict.CRAFT,
            refusal=CraftingRefusal.RECIPE_UNKNOWN,
            detail="contradiction",
            recipe=None,
            tallies=(),
        )
    with pytest.raises(ValueError, match="must say what it saw"):
        CraftingDecision(
            verdict=CraftingVerdict.CRAFT,
            refusal=None,
            detail="   ",
            recipe=None,
            tallies=(),
        )


def test_the_payload_says_what_it_saw_without_recomputation() -> None:
    payload = recipe_payload(materials=[{"full_type": BRANCH, "count": 2}])
    observation = world(material("m1", recipes=[payload]))

    document = assess_craft(observation, SPEAR).as_dict()

    assert document["verdict"] == CraftingVerdict.REFUSE.value
    assert document["refusal"] == CraftingRefusal.MATERIALS_MISSING.value
    assert document["shortfalls"] == [{"full_type": BRANCH, "needed": 2, "free": 1, "reserved": 0}]
    assert document["recipe"]["product"] == SPEAR


def test_an_empty_inventory_is_a_refusal_not_a_crash() -> None:
    empty = a_world(inventory=InventoryView(containers=[main_container()], items=[]))

    assert assess_craft(empty, SPEAR).refusal is CraftingRefusal.RECIPE_UNKNOWN


def test_the_on_person_test_is_the_container_kind_not_the_item() -> None:
    payload = recipe_payload()
    observation = a_world(
        items=[material("m1", container_ref=CRATE_REF, recipes=[payload])],
        containers=[
            main_container(),
            crate_container(),
        ],
    )

    decision = assess_recipe(observation, only_recipe(observation))

    assert decision.tallies[0].in_world == 1
    assert decision.tallies[0].on_person == 0
    assert crate_container().kind is ContainerKind.WORLD
