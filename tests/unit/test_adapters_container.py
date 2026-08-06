"""``container.inspect`` and ``container.open_nearby``.

The pair exists to keep two facts apart that a single "open the crate" action
would blur: reading what a container holds changes nothing, and getting to it
moves the character through a world with zombies in it. So the inspect tests are
about a report naming the right container, and the open tests are about the
character actually standing next to it — never about the walk having finished.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.actions.adapters import ContainerInspectAdapter, ContainerOpenNearbyAdapter
from pz_agent_core.capabilities import MOVE_TO_SQUARE
from pz_agent_core.protocol import (
    ActionName,
    Command,
    ContainerView,
    ItemView,
    Observation,
    Position,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.adapter_worlds import (
    BAG_REF,
    CRATE_REF,
    FAR_CRATE_REF,
    HOME_X,
    HOME_Y,
    HOME_Z,
    MAIN_REF,
    a_command,
    a_world,
    an_item,
    bag_container,
    crate_container,
    main_container,
    prepare,
)

BEANS = an_item("42", container_ref=MAIN_REF)
IN_BAG = an_item("43", container_ref=BAG_REF, display_name="Water Bottle")
IN_CRATE = an_item("44", container_ref=CRATE_REF, display_name="Hammer")


def carrying(
    *items: ItemView,
    containers: list[ContainerView] | None = None,
    seq: int = 1,
    position: Position | None = None,
    no_inventory: bool = False,
) -> Observation:
    return a_world(
        seq=seq,
        items=list(items),
        containers=containers if containers is not None else [main_container(), bag_container()],
        position=position,
        no_inventory=no_inventory,
    )


def inspect_command(container_ref: str = BAG_REF, **extra: object) -> Command:
    args: dict[str, object] = {"container_ref": container_ref}
    args.update(extra)
    return a_command(ActionName.CONTAINER_INSPECT, args)


def open_command(container_ref: str = CRATE_REF, **extra: object) -> Command:
    args: dict[str, object] = {"container_ref": container_ref}
    args.update(extra)
    return a_command(ActionName.CONTAINER_OPEN_NEARBY, args)


def at_crate(*items: ItemView, seq: int = 2, accessible: bool = True) -> Observation:
    """The world once the character is standing on the crate's own square."""
    return carrying(
        *items,
        containers=[main_container(), crate_container(accessible=accessible)],
        seq=seq,
    )


def away_from_crate(*items: ItemView, seq: int = 1) -> Observation:
    return carrying(
        *items,
        containers=[main_container(), crate_container()],
        position=Position(x=float(HOME_X + 6), y=float(HOME_Y), z=HOME_Z, direction="S"),
        seq=seq,
    )


# --------------------------------------------------------------------------
# container.inspect
# --------------------------------------------------------------------------


def test_the_listed_contents_are_the_evidence() -> None:
    adapter = ContainerInspectAdapter()
    before = carrying(IN_BAG)
    command = prepare(adapter, inspect_command(), before)

    evidence = adapter.verify(command, before, carrying(IN_BAG, seq=2))

    assert evidence is not None
    assert evidence.kind == "container_contents_described"
    assert evidence.observed["container_ref"] == BAG_REF
    assert evidence.observed["item_count"] == 1
    assert evidence.observed["items"][0]["item_ref"] == IN_BAG.ref


def test_an_empty_container_is_a_reading_not_a_failure() -> None:
    """ "The bag is empty" and "the bag was not described" are opposite facts."""
    adapter = ContainerInspectAdapter()
    before = carrying(BEANS)
    command = prepare(adapter, inspect_command(), before)

    evidence = adapter.verify(command, before, carrying(BEANS, seq=2))

    assert evidence is not None
    assert evidence.observed["item_count"] == 0
    assert evidence.observed["total_items"] == 0


def test_a_container_the_observation_does_not_describe_proves_nothing() -> None:
    adapter = ContainerInspectAdapter()
    before = carrying(IN_BAG)
    command = prepare(adapter, inspect_command(), before)

    gone = carrying(seq=2, containers=[main_container()])

    assert adapter.verify(command, before, gone) is None


def test_an_observation_with_no_inventory_describes_no_container() -> None:
    adapter = ContainerInspectAdapter()
    before = carrying(IN_BAG)
    command = prepare(adapter, inspect_command(), before)

    assert adapter.verify(command, before, carrying(seq=2, no_inventory=True)) is None


def test_a_listing_reports_the_total_it_could_not_fit() -> None:
    adapter = ContainerInspectAdapter()
    crowded = [an_item(str(50 + n), container_ref=BAG_REF) for n in range(4)]
    before = carrying(*crowded)
    command = prepare(adapter, inspect_command(limit=2), before)

    evidence = adapter.verify(command, before, carrying(*crowded, seq=2))

    assert evidence is not None
    assert evidence.observed["item_count"] == 2
    assert evidence.observed["total_items"] == 4
    assert evidence.observed["truncated"] is True


def test_a_container_outside_the_reported_tree_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        ContainerInspectAdapter().validate(inspect_command(CRATE_REF), carrying(BEANS))
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_inspecting_without_an_inventory_tier_is_refused_before_anything_is_sent() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        ContainerInspectAdapter().validate(inspect_command(), carrying(no_inventory=True))
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_reading_a_container_needs_no_capability_and_no_permission_tier() -> None:
    adapter = ContainerInspectAdapter()

    assert adapter.required_capability is None
    assert adapter.risk is RiskClass.P0


# --------------------------------------------------------------------------
# container.open_nearby
# --------------------------------------------------------------------------


def test_a_container_in_reach_and_readable_is_the_evidence() -> None:
    adapter = ContainerOpenNearbyAdapter()
    before = away_from_crate()
    command = prepare(adapter, open_command(), before)

    evidence = adapter.verify(command, before, at_crate(IN_CRATE))

    assert evidence is not None
    assert evidence.kind == "container_within_reach"
    assert evidence.observed["container_ref"] == CRATE_REF
    assert evidence.observed["distance"] == pytest.approx(0.0)
    assert evidence.observed["item_count"] == 1


def test_standing_too_far_away_is_not_an_opened_container() -> None:
    """The walk ending is a fact about the queue, not about where the character is."""
    adapter = ContainerOpenNearbyAdapter()
    before = away_from_crate()
    command = prepare(adapter, open_command(), before)

    assert adapter.verify(command, before, away_from_crate(IN_CRATE, seq=2)) is None


def test_a_container_that_stopped_being_reported_is_not_an_opened_container() -> None:
    adapter = ContainerOpenNearbyAdapter()
    before = away_from_crate()
    command = prepare(adapter, open_command(), before)

    unreported = carrying(seq=2, containers=[main_container()])

    assert adapter.verify(command, before, unreported) is None


def test_an_inaccessible_container_within_reach_is_not_an_opened_container() -> None:
    adapter = ContainerOpenNearbyAdapter()
    before = away_from_crate()
    command = prepare(adapter, open_command(), before)

    assert adapter.verify(command, before, at_crate(IN_CRATE, accessible=False)) is None


def test_a_container_on_another_floor_is_not_an_opened_container() -> None:
    adapter = ContainerOpenNearbyAdapter()
    before = away_from_crate()
    command = prepare(adapter, open_command(), before)

    upstairs = carrying(
        IN_CRATE,
        containers=[main_container(), crate_container()],
        position=Position(x=float(HOME_X), y=float(HOME_Y), z=1, direction="S"),
        seq=2,
    )

    assert adapter.verify(command, before, upstairs) is None


def test_an_observation_with_no_inventory_cannot_show_a_container_opened() -> None:
    adapter = ContainerOpenNearbyAdapter()
    before = away_from_crate()
    command = prepare(adapter, open_command(), before)

    assert adapter.verify(command, before, carrying(seq=2, no_inventory=True)) is None


def test_a_carried_container_is_refused_as_already_in_reach() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        ContainerOpenNearbyAdapter().validate(open_command(BAG_REF), away_from_crate())
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_a_container_with_no_world_square_is_refused_rather_than_approximated() -> None:
    vehicle = f"container:{DEFAULT_SESSION}:vehicle-3"

    with pytest.raises(PreconditionFailed) as caught:
        ContainerOpenNearbyAdapter().validate(open_command(vehicle), away_from_crate())
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_a_container_past_the_single_approach_budget_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        ContainerOpenNearbyAdapter().validate(open_command(FAR_CRATE_REF), away_from_crate())
    assert caught.value.reason_code is ReasonCode.TARGET_OUT_OF_RANGE


def test_a_container_reported_as_shut_is_refused_before_the_walk() -> None:
    shut = carrying(
        containers=[main_container(), crate_container(accessible=False)],
        position=Position(x=float(HOME_X + 6), y=float(HOME_Y), z=HOME_Z, direction="S"),
    )

    with pytest.raises(PreconditionFailed) as caught:
        ContainerOpenNearbyAdapter().validate(open_command(), shut)
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_progress_is_the_distance_closed() -> None:
    adapter = ContainerOpenNearbyAdapter()
    before = away_from_crate()
    command = prepare(adapter, open_command(), before)

    halfway = carrying(
        containers=[main_container(), crate_container()],
        position=Position(x=float(HOME_X + 3), y=float(HOME_Y), z=HOME_Z, direction="S"),
        seq=2,
    )

    assert adapter.progress(command, before, halfway) == pytest.approx(0.5)


def test_approaching_a_container_rides_on_the_movement_capability() -> None:
    """Walking to it is the whole of the work, so it is the movement probe or nothing."""
    adapter = ContainerOpenNearbyAdapter()

    assert adapter.required_capability == MOVE_TO_SQUARE
    assert adapter.risk is RiskClass.P3
