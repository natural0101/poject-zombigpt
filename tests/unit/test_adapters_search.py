"""``inventory.search``.

Kept out of ``test_adapters_inventory.py`` deliberately: that file is the
transfer suite, and a search shares its module but none of its postcondition.
What is pinned here is the one guarantee a list of references has to carry —
every reference in it resolves inside the character's own container tree — plus
the filters, which are only accepted at all when the observation can be checked
against them.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.actions.adapters import SearchAdapter
from pz_agent_core.protocol import (
    ActionName,
    Command,
    ContainerView,
    ItemView,
    Observation,
    ReasonCode,
    RiskClass,
)
from tests.fixtures.adapter_worlds import (
    BAG_REF,
    CRATE_REF,
    MAIN_REF,
    a_command,
    a_world,
    an_item,
    bag_container,
    crate_container,
    main_container,
    prepare,
)
from tests.fixtures.policy_items import food_payload, literature_payload

BEANS = an_item("42", container_ref=MAIN_REF, display_name="Tinned Beans", food=food_payload())
BOOK = an_item(
    "43",
    container_ref=BAG_REF,
    display_name="Carpentry Vol. 1",
    full_type="Base.BookCarpentry1",
    literature=literature_payload(),
)
HAMMER = an_item("44", container_ref=MAIN_REF, display_name="Hammer", full_type="Base.Hammer")
IN_CRATE = an_item("45", container_ref=CRATE_REF, display_name="Tinned Beans", food=food_payload())


def carrying(
    *items: ItemView,
    containers: list[ContainerView] | None = None,
    seq: int = 1,
    no_inventory: bool = False,
) -> Observation:
    return a_world(
        seq=seq,
        items=list(items),
        containers=containers
        if containers is not None
        else [main_container(), bag_container(), crate_container()],
        no_inventory=no_inventory,
    )


def search_command(**args: object) -> Command:
    return a_command(ActionName.INVENTORY_SEARCH, dict(args))


def refs_in(evidence_matches: list[dict[str, object]]) -> set[object]:
    return {match["item_ref"] for match in evidence_matches}


# --------------------------------------------------------------------------
# the postcondition
# --------------------------------------------------------------------------


def test_the_matches_are_the_evidence() -> None:
    adapter = SearchAdapter()
    before = carrying(BEANS, HAMMER)
    command = prepare(adapter, search_command(edible=True), before)

    evidence = adapter.verify(command, before, carrying(BEANS, HAMMER, seq=2))

    assert evidence is not None
    assert evidence.kind == "inventory_searched"
    assert evidence.observed["match_count"] == 1
    assert refs_in(evidence.observed["matches"]) == {BEANS.ref}


def test_a_search_that_matched_nothing_is_still_a_reading() -> None:
    """ "Nothing edible on me" is an answer; it is not a failed search."""
    adapter = SearchAdapter()
    before = carrying(HAMMER)
    command = prepare(adapter, search_command(edible=True), before)

    evidence = adapter.verify(command, before, carrying(HAMMER, seq=2))

    assert evidence is not None
    assert evidence.observed["match_count"] == 0


def test_an_observation_with_no_inventory_searched_nothing() -> None:
    adapter = SearchAdapter()
    before = carrying(BEANS)
    command = prepare(adapter, search_command(), before)

    assert adapter.verify(command, before, carrying(seq=2, no_inventory=True)) is None


def test_a_match_in_a_world_container_never_reaches_the_report() -> None:
    """The reference would look identical and would need a walk to act on."""
    adapter = SearchAdapter()
    before = carrying(BEANS, IN_CRATE)
    command = prepare(adapter, search_command(edible=True), before)

    evidence = adapter.verify(command, before, carrying(BEANS, IN_CRATE, seq=2))

    assert evidence is not None
    assert refs_in(evidence.observed["matches"]) == {BEANS.ref}
    assert evidence.observed["off_person_skipped"] == 1


def test_an_item_whose_container_was_not_reported_is_skipped() -> None:
    adapter = SearchAdapter()
    orphan = an_item("46", container_ref=f"{BAG_REF}-gone", food=food_payload())
    before = carrying(orphan)
    command = prepare(adapter, search_command(edible=True), before)

    evidence = adapter.verify(command, before, carrying(orphan, seq=2))

    assert evidence is not None
    assert evidence.observed["match_count"] == 0
    assert evidence.observed["off_person_skipped"] == 1


def test_a_bag_the_character_carries_is_part_of_their_own_tree() -> None:
    adapter = SearchAdapter()
    before = carrying(BOOK)
    command = prepare(adapter, search_command(readable=True), before)

    evidence = adapter.verify(command, before, carrying(BOOK, seq=2))

    assert evidence is not None
    assert refs_in(evidence.observed["matches"]) == {BOOK.ref}


def test_the_limit_bounds_what_the_report_carries() -> None:
    adapter = SearchAdapter()
    many = [an_item(str(60 + n), container_ref=MAIN_REF, food=food_payload()) for n in range(5)]
    before = carrying(*many)
    command = prepare(adapter, search_command(edible=True, limit=2), before)

    evidence = adapter.verify(command, before, carrying(*many, seq=2))

    assert evidence is not None
    assert evidence.observed["match_count"] == 2
    assert evidence.observed["limit"] == 2


# --------------------------------------------------------------------------
# the filters
# --------------------------------------------------------------------------


def test_a_type_filter_matches_exactly() -> None:
    adapter = SearchAdapter()
    before = carrying(BEANS, HAMMER)
    command = prepare(adapter, search_command(full_type="Base.Hammer"), before)

    evidence = adapter.verify(command, before, carrying(BEANS, HAMMER, seq=2))

    assert evidence is not None
    assert refs_in(evidence.observed["matches"]) == {HAMMER.ref}


def test_a_type_prefix_matches_a_family() -> None:
    adapter = SearchAdapter()
    before = carrying(BEANS, BOOK)
    command = prepare(adapter, search_command(type_prefix="Base.Book"), before)

    evidence = adapter.verify(command, before, carrying(BEANS, BOOK, seq=2))

    assert evidence is not None
    assert refs_in(evidence.observed["matches"]) == {BOOK.ref}


def test_a_flag_left_out_does_not_narrow_the_search() -> None:
    """``edible`` absent is "do not care", which is not ``edible = false``."""
    adapter = SearchAdapter()
    before = carrying(BEANS, HAMMER)
    everything = prepare(adapter, search_command(), before)
    inedible = prepare(adapter, search_command(edible=False), before)
    after = carrying(BEANS, HAMMER, seq=2)

    both = adapter.verify(everything, before, after)
    tools = adapter.verify(inedible, before, after)

    assert both is not None and both.observed["match_count"] == 2
    assert tools is not None and refs_in(tools.observed["matches"]) == {HAMMER.ref}


def test_equipped_items_can_be_excluded() -> None:
    adapter = SearchAdapter()
    held = an_item("47", container_ref=MAIN_REF, display_name="Axe", equipped=True)
    before = carrying(HAMMER, held)
    command = prepare(adapter, search_command(exclude_equipped=True), before)

    evidence = adapter.verify(command, before, carrying(HAMMER, held, seq=2))

    assert evidence is not None
    assert refs_in(evidence.observed["matches"]) == {HAMMER.ref}


# --------------------------------------------------------------------------
# refusals and shape
# --------------------------------------------------------------------------


def test_a_filter_the_observation_cannot_be_checked_against_is_refused() -> None:
    """The mod supports ``min_uses``; nothing in the observation counts uses."""
    with pytest.raises(PreconditionFailed) as caught:
        SearchAdapter().validate(search_command(min_uses=2), carrying(BEANS))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_a_free_text_filter_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        SearchAdapter().validate(search_command(name_contains="bean"), carrying(BEANS))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


@pytest.mark.parametrize("value", ["Base.Beans; DROP", "a" * 200, "", 7])
def test_a_type_filter_outside_the_alphabet_is_refused(value: object) -> None:
    with pytest.raises(PreconditionFailed) as caught:
        SearchAdapter().validate(search_command(full_type=value), carrying(BEANS))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_searching_without_an_inventory_tier_is_refused_before_anything_is_sent() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        SearchAdapter().validate(search_command(), carrying(no_inventory=True))
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_only_the_filters_the_caller_set_are_sent() -> None:
    args = SearchAdapter().build_args(search_command(edible=True), carrying(BEANS))

    assert args == {"edible": True, "exclude_equipped": False, "limit": 32}


def test_searching_needs_no_capability_and_no_permission_tier() -> None:
    adapter = SearchAdapter()

    assert adapter.required_capability is None
    assert adapter.risk is RiskClass.P0
