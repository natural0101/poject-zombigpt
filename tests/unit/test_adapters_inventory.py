"""``inventory.transfer`` and ``inventory.ensure_main``.

The tests that matter most are the two that look almost the same: an item
observed in the destination, and an item observed in the destination *and still
in the bag*. The first is a success; the second is a duplication, and the
adapter must refuse to call it anything.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import (
    ActionEngine,
    ActionRequest,
    AdapterRegistry,
    PreconditionFailed,
)
from pz_agent_core.actions.adapters import ContainerChain, EnsureMainAdapter, TransferAdapter
from pz_agent_core.actions.adapters.common import MAX_CONTAINER_DEPTH
from pz_agent_core.actions.adapters.inventory import MAX_TRANSFER_QUANTITY
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    ContainerKind,
    ContainerView,
    Hands,
    InventoryView,
    ItemView,
    Observation,
    Position,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import DEFAULT_SESSION, make_container
from tests.fixtures.action_doubles import AckPlan, FakeClock, FakeCommandSink, FakeObservationSource
from tests.fixtures.adapter_worlds import (
    BAG_REF,
    CRATE_REF,
    FAR_CRATE_REF,
    HOME_X,
    HOME_Y,
    MAIN_REF,
    a_command,
    a_world,
    an_item,
    bag_container,
    crate_container,
    item_ref_in,
    main_container,
    moved,
    prepare,
)

BEANS = an_item("42", container_ref=BAG_REF)


def carrying(
    *items: ItemView,
    containers: list[ContainerView] | None = None,
    seq: int = 1,
    hands: Hands | None = None,
    position: Position | None = None,
) -> Observation:
    """A world in which the character carries *items* in a bag and an inventory."""
    return a_world(
        seq=seq,
        items=list(items),
        containers=containers if containers is not None else [main_container(), bag_container()],
        position=position,
        hands=hands,
    )


def transfer_command(
    *,
    item_ref: str = BEANS.ref,
    destination: str = MAIN_REF,
    **extra: object,
) -> Command:
    args: dict[str, object] = {"item_ref": item_ref, "destination_container_ref": destination}
    args.update(extra)
    return a_command(ActionName.INVENTORY_TRANSFER, args)


# --------------------------------------------------------------------------
# the postcondition
# --------------------------------------------------------------------------


def test_the_item_observed_in_the_destination_and_nowhere_else_is_the_evidence() -> None:
    adapter = TransferAdapter()
    before = carrying(BEANS)
    command = prepare(adapter, transfer_command(), before)

    after = carrying(moved(BEANS, MAIN_REF), seq=2)
    evidence = adapter.verify(command, before, after)

    assert evidence is not None
    assert evidence.kind == "item_in_destination_container"
    assert evidence.observed["container_ref"] == MAIN_REF
    assert evidence.observed["runtime_id"] == "42"
    assert evidence.observed["origin"]["container_ref"] == BAG_REF


def test_an_item_still_in_the_bag_is_not_a_completed_transfer() -> None:
    adapter = TransferAdapter()
    before = carrying(BEANS)
    command = prepare(adapter, transfer_command(), before)

    assert adapter.verify(command, before, carrying(BEANS, seq=2)) is None


def test_an_item_observed_in_both_containers_is_never_reported_as_success() -> None:
    """Two entries with one runtime id is a duplication, not a half-finished move."""
    adapter = TransferAdapter()
    before = carrying(BEANS)
    command = prepare(adapter, transfer_command(), before)

    duplicated = carrying(BEANS, moved(BEANS, MAIN_REF), seq=2)

    assert adapter.verify(command, before, duplicated) is None


def test_the_duplicate_check_does_not_depend_on_enumeration_order() -> None:
    """The destination copy listed first must not shortcut the count."""
    adapter = TransferAdapter()
    before = carrying(BEANS)
    command = prepare(adapter, transfer_command(), before)

    destination_first = carrying(moved(BEANS, MAIN_REF), BEANS, seq=2)

    assert adapter.verify(command, before, destination_first) is None


def test_an_observation_without_an_inventory_proves_nothing() -> None:
    adapter = TransferAdapter()
    before = carrying(BEANS)
    command = prepare(adapter, transfer_command(), before)

    assert adapter.verify(command, before, a_world(seq=2, no_inventory=True)) is None


def test_the_prepared_command_records_where_the_item_came_from() -> None:
    """Origin metadata is what makes putting the item back possible (§4.6)."""
    args = TransferAdapter().build_args(transfer_command(), carrying(BEANS))

    assert args["source_container_ref"] == BAG_REF
    assert args["origin"]["container_kind"] == ContainerKind.CARRIED.value
    assert args["quantity"] == MAX_TRANSFER_QUANTITY


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_an_item_that_is_not_in_the_observed_inventory_is_an_invalid_ref() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(), carrying())
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_a_source_that_does_not_hold_the_item_is_an_invalid_ref() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(
            transfer_command(source_container_ref=CRATE_REF),
            carrying(BEANS, containers=[main_container(), bag_container(), crate_container()]),
        )
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_an_unknown_destination_is_an_invalid_ref() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(
            transfer_command(destination=f"container:{DEFAULT_SESSION}:worn:Back:9"),
            carrying(BEANS),
        )
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_a_reference_from_another_session_is_an_invalid_ref() -> None:
    foreign = "item:11111111-2222-3333-4444-555555555555:player-main:42:0"
    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(item_ref=foreign), carrying(BEANS))
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_moving_an_item_to_where_it_already_is_is_refused() -> None:
    in_main = an_item("42", container_ref=MAIN_REF)
    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(item_ref=in_main.ref), carrying(in_main))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_an_equipped_item_is_refused_and_names_the_unequip_it_needs() -> None:
    equipped = an_item("42", container_ref=BAG_REF, equipped=True)

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(item_ref=equipped.ref), carrying(equipped))

    assert caught.value.reason_code is ReasonCode.POLICY_DENIED
    prerequisites = caught.value.evidence["prerequisites"]
    assert prerequisites[0]["action"] == ActionName.INVENTORY_UNEQUIP.value
    assert prerequisites[0]["args"]["item_ref"] == equipped.ref


def test_an_item_in_the_characters_hands_is_refused_the_same_way() -> None:
    held = an_item("42", container_ref=BAG_REF)
    observation = carrying(held, hands=Hands(primary=held.ref))

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(item_ref=held.ref), observation)
    assert caught.value.reason_code is ReasonCode.POLICY_DENIED


def test_a_destination_without_room_is_container_full() -> None:
    heavy = an_item("42", container_ref=BAG_REF, weight=5.0)
    observation = carrying(
        heavy,
        containers=[main_container(capacity=10.0, used_capacity=9.5), bag_container()],
    )

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(item_ref=heavy.ref), observation)
    assert caught.value.reason_code is ReasonCode.CONTAINER_FULL
    assert caught.value.evidence["free_capacity"] == pytest.approx(0.5)


def test_a_destination_whose_capacity_the_game_does_not_report_is_not_guessed_at() -> None:
    observation = carrying(
        BEANS,
        containers=[
            main_container(capacity=None, used_capacity=None),
            bag_container(),
        ],
    )

    TransferAdapter().validate(transfer_command(), observation)


def test_a_bag_cannot_be_put_inside_itself() -> None:
    bag_item = an_item("77001", container_ref=MAIN_REF, display_name="Duffel Bag")
    observation = carrying(bag_item, containers=[main_container(), bag_container()])

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(
            transfer_command(item_ref=bag_item.ref, destination=BAG_REF), observation
        )
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_more_than_one_item_per_command_is_refused_with_the_reason_why() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(quantity=3), carrying(BEANS))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT
    assert "one item" in str(caught.value)


def test_an_inaccessible_container_is_refused() -> None:
    observation = carrying(BEANS, containers=[main_container(), bag_container(accessible=False)])

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(), observation)
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_an_inaccessible_ancestor_makes_the_whole_chain_unreachable() -> None:
    """Accessibility is a property of every link, not just of the bag itself."""
    nested_bag = make_container(
        BAG_REF, ContainerKind.CARRIED, name="Duffel Bag", parent_ref=CRATE_REF
    )
    observation = carrying(
        BEANS,
        containers=[main_container(), nested_bag, crate_container(accessible=False)],
    )

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(), observation)
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED
    assert caught.value.evidence["chain"] == [BAG_REF, CRATE_REF]


def test_an_incomplete_chain_is_neither_reachable_nor_on_the_person() -> None:
    """Both properties are read directly, and an undescribed link is not a safe one."""
    partial = ContainerChain(containers=(main_container(),), complete=False)

    assert partial.accessible is False
    assert partial.on_person is False


def test_the_origin_of_a_container_standing_in_the_world_carries_its_square() -> None:
    """Putting the item back needs coordinates, whatever kind the mod called it."""
    on_the_floor = make_container(CRATE_REF, ContainerKind.FLOOR, name="Floor")
    item = an_item("42", container_ref=CRATE_REF)
    observation = carrying(item, containers=[main_container(), on_the_floor])

    args = TransferAdapter().build_args(transfer_command(item_ref=item.ref), observation)

    assert args["origin"]["container_kind"] == ContainerKind.FLOOR.value
    assert args["origin"]["square"] == {"x": HOME_X, "y": HOME_Y, "z": 0}


def test_an_observation_with_no_inventory_tier_cannot_support_a_transfer() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(), a_world(no_inventory=True))
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


# --------------------------------------------------------------------------
# nesting and world containers
# --------------------------------------------------------------------------


def test_a_bag_inside_a_world_crate_is_not_on_the_person() -> None:
    """Reach is a property of the whole chain, not of the container's own kind."""
    nested_bag = make_container(
        BAG_REF, ContainerKind.CARRIED, name="Duffel Bag", parent_ref=FAR_CRATE_REF
    )
    item = an_item("42", container_ref=BAG_REF)
    observation = carrying(
        item, containers=[main_container(), nested_bag, crate_container(FAR_CRATE_REF)]
    )

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(item_ref=item.ref), observation)
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED
    assert caught.value.evidence["prerequisites"][0]["action"] == ActionName.MOVEMENT_MOVE_TO.value


def test_a_world_container_underfoot_needs_no_walking() -> None:
    item = an_item("42", container_ref=CRATE_REF)
    observation = carrying(item, containers=[main_container(), crate_container()])

    TransferAdapter().validate(transfer_command(item_ref=item.ref), observation)


def test_a_world_container_across_the_street_asks_to_be_walked_to_first() -> None:
    item = an_item("42", container_ref=FAR_CRATE_REF)
    observation = carrying(item, containers=[main_container(), crate_container(FAR_CRATE_REF)])

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(item_ref=item.ref), observation)

    prerequisite = caught.value.evidence["prerequisites"][0]
    assert prerequisite["action"] == ActionName.MOVEMENT_MOVE_TO.value
    assert prerequisite["args"]["target"] == {"x": 1240, "y": HOME_Y, "z": 0}


def test_a_world_container_on_another_floor_asks_to_be_walked_to_first() -> None:
    upstairs_ref = f"container:{DEFAULT_SESSION}:world:{HOME_X}:{HOME_Y}:1:1:0"
    item = an_item("42", container_ref=upstairs_ref)
    observation = carrying(item, containers=[main_container(), crate_container(upstairs_ref)])

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(item_ref=item.ref), observation)
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_container_hanging_off_one_the_mod_never_described_is_an_invalid_ref() -> None:
    orphan = make_container(
        BAG_REF, ContainerKind.CARRIED, parent_ref=f"container:{DEFAULT_SESSION}:worn:Back:404"
    )
    observation = carrying(BEANS, containers=[main_container(), orphan])

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(), observation)
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_a_container_tree_that_loops_is_refused_rather_than_walked_forever() -> None:
    left = f"container:{DEFAULT_SESSION}:carried:1"
    right = f"container:{DEFAULT_SESSION}:carried:2"
    observation = carrying(
        an_item("42", container_ref=left),
        containers=[
            main_container(),
            make_container(left, ContainerKind.CARRIED, parent_ref=right),
            make_container(right, ContainerKind.CARRIED, parent_ref=left),
        ],
    )

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(transfer_command(item_ref=item_ref_in("42", left)), observation)
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED
    assert "loops" in str(caught.value)


def test_a_container_tree_deeper_than_the_cap_is_refused() -> None:
    depth = MAX_CONTAINER_DEPTH + 2
    refs = [f"container:{DEFAULT_SESSION}:carried:{n}" for n in range(depth)]
    containers = [main_container()]
    for index, ref in enumerate(refs):
        parent = refs[index + 1] if index + 1 < depth else None
        containers.append(make_container(ref, ContainerKind.CARRIED, parent_ref=parent))
    observation = carrying(an_item("42", container_ref=refs[0]), containers=containers)

    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(
            transfer_command(item_ref=item_ref_in("42", refs[0])), observation
        )
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED
    assert str(MAX_CONTAINER_DEPTH) in str(caught.value)


def test_touching_a_world_container_raises_the_permission_tier() -> None:
    item = an_item("42", container_ref=CRATE_REF)
    observation = carrying(item, containers=[main_container(), crate_container()])
    adapter = TransferAdapter()

    assert adapter.risk is RiskClass.P1
    assert adapter.risk_for(transfer_command(item_ref=item.ref), observation) is RiskClass.P3
    assert adapter.risk_for(transfer_command(), carrying(BEANS)) is RiskClass.P1


# --------------------------------------------------------------------------
# ensure_main
# --------------------------------------------------------------------------


def ensure_command(item_ref: str = BEANS.ref) -> Command:
    return a_command(ActionName.INVENTORY_ENSURE_MAIN, {"item_ref": item_ref})


def test_ensure_main_is_proven_by_the_item_being_in_player_main() -> None:
    adapter = EnsureMainAdapter()
    before = carrying(BEANS)
    command = prepare(adapter, ensure_command(), before)

    evidence = adapter.verify(command, before, carrying(moved(BEANS, MAIN_REF), seq=2))

    assert evidence is not None
    assert evidence.kind == "item_in_player_main"
    assert evidence.observed["container_ref"] == MAIN_REF


def test_ensure_main_for_an_item_already_there_is_still_observed_not_assumed() -> None:
    adapter = EnsureMainAdapter()
    in_main = an_item("42", container_ref=MAIN_REF)
    before = carrying(in_main)
    command = prepare(adapter, ensure_command(in_main.ref), before)

    adapter.validate(command, before)

    assert adapter.verify(command, before, carrying(in_main, seq=2)) is not None
    assert adapter.verify(command, before, carrying(seq=2)) is None


def test_ensure_main_refuses_a_destination_that_is_not_the_main_inventory() -> None:
    """The argument the adapter mints is accepted; a contradicting one is not ignored."""
    command = a_command(
        ActionName.INVENTORY_ENSURE_MAIN,
        {"item_ref": BEANS.ref, "destination_container_ref": BAG_REF},
    )

    with pytest.raises(PreconditionFailed) as caught:
        EnsureMainAdapter().validate(command, carrying(BEANS))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT
    assert caught.value.evidence["player_main_ref"] == MAIN_REF


def test_the_prepared_ensure_main_command_still_passes_its_own_checks() -> None:
    adapter = EnsureMainAdapter()
    before = carrying(BEANS)

    prepared = prepare(adapter, ensure_command(), before)

    adapter.validate(prepared, before)
    assert prepared.args["destination_container_ref"] == MAIN_REF


def test_ensure_main_refuses_an_equipped_item() -> None:
    equipped = an_item("42", container_ref=BAG_REF, equipped=True)

    with pytest.raises(PreconditionFailed) as caught:
        EnsureMainAdapter().validate(ensure_command(equipped.ref), carrying(equipped))
    assert caught.value.reason_code is ReasonCode.POLICY_DENIED


def test_ensure_main_refuses_when_the_main_inventory_is_full() -> None:
    heavy = an_item("42", container_ref=BAG_REF, weight=5.0)
    observation = carrying(
        heavy, containers=[main_container(capacity=10.0, used_capacity=9.9), bag_container()]
    )

    with pytest.raises(PreconditionFailed) as caught:
        EnsureMainAdapter().validate(ensure_command(heavy.ref), observation)
    assert caught.value.reason_code is ReasonCode.CONTAINER_FULL


def test_ensure_main_needs_a_reported_main_container() -> None:
    observation = carrying(BEANS, containers=[bag_container()])

    with pytest.raises(PreconditionFailed) as caught:
        EnsureMainAdapter().validate(ensure_command(), observation)
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_ensure_main_from_a_world_container_is_the_world_tier() -> None:
    item = an_item("42", container_ref=CRATE_REF)
    observation = carrying(item, containers=[main_container(), crate_container()])
    adapter = EnsureMainAdapter()

    assert adapter.risk_for(ensure_command(item.ref), observation) is RiskClass.P3
    assert adapter.risk_for(ensure_command(), carrying(BEANS)) is RiskClass.P1


# --------------------------------------------------------------------------
# through the engine
# --------------------------------------------------------------------------


def run_transfer(
    observations: list[Observation],
    *,
    repeated: Observation | None = None,
    acks: list[AckPlan] | None = None,
    capability: bool = True,
) -> tuple[FakeCommandSink, ActionResult]:
    clock = FakeClock()
    sink = FakeCommandSink(clock, acks=acks or [])
    source = FakeObservationSource(clock)
    source.push(*observations)
    if repeated is not None:
        source.repeat(repeated)
    registry = AdapterRegistry()
    registry.register(TransferAdapter(timeout_ms=2_000, poll_interval_ms=250))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        capability_check=lambda _name: capability,
    )
    result = engine.execute(
        ActionRequest(
            action=ActionName.INVENTORY_TRANSFER,
            session_id=DEFAULT_SESSION,
            idempotency_key="transfer",
            args={"item_ref": BEANS.ref, "destination_container_ref": MAIN_REF},
            policy=CommandPolicy(max_retries=0),
        )
    )
    return sink, result


def test_a_mod_that_acks_success_while_the_item_stays_put_fails() -> None:
    unchanged = carrying(BEANS)
    _sink, result = run_transfer(
        [unchanged],
        repeated=unchanged,
        acks=[AckPlan(status=ActionStatus.SUCCEEDED, after_polls=1)],
    )

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.POSTCONDITION_FAILED


def test_the_transfer_succeeds_once_the_item_is_observed_in_the_destination() -> None:
    landed = carrying(moved(BEANS, MAIN_REF), seq=2)
    _sink, result = run_transfer([carrying(BEANS), landed], repeated=landed)

    assert result.status is ActionStatus.SUCCEEDED
    assert result.evidence["kind"] == "item_in_destination_container"


def test_an_unverified_transfer_capability_sends_nothing() -> None:
    sink, result = run_transfer([carrying(BEANS)], capability=False)

    assert result.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert sink.sent == []


def test_a_precondition_failure_sends_nothing() -> None:
    sink, result = run_transfer([carrying()])

    assert result.reason_code is ReasonCode.INVALID_REF
    assert result.status is ActionStatus.REJECTED
    assert sink.sent == []


def test_the_registry_publishes_one_adapter_per_inventory_action() -> None:
    registry = AdapterRegistry()
    registry.register(TransferAdapter())
    registry.register(EnsureMainAdapter())

    assert registry.get(ActionName.INVENTORY_TRANSFER).required_capability == "inventory_transfer"
    assert registry.get(ActionName.INVENTORY_ENSURE_MAIN).risk is RiskClass.P1


def test_an_empty_inventory_view_is_not_a_missing_one() -> None:
    """The distinction is what keeps 'nothing there' from reading as 'no tier'."""
    with pytest.raises(PreconditionFailed) as caught:
        TransferAdapter().validate(
            transfer_command(), a_world(inventory=InventoryView(containers=[main_container()]))
        )
    assert caught.value.reason_code is ReasonCode.INVALID_REF
