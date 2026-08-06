"""``equipment.equip`` and ``equipment.unequip``.

Three of these tests exist because the naive postcondition is wrong in three
different ways: an item that left the hand and also left the inventory was
*dropped*; an item that appears twice cannot be pointed at at all; and a worn
garment with no inventory of its own is never described, so nothing about it can
be proven either way. Only the first is a failure the mod could avoid — the
other two are the observation being unable to answer, which is still not a
success.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.actions.adapters import EquipAdapter, UnequipAdapter
from pz_agent_core.capabilities.probes import EQUIPMENT_EQUIP, EQUIPMENT_UNEQUIP
from pz_agent_core.protocol import (
    ActionName,
    Command,
    ContainerKind,
    ContainerView,
    Hands,
    ItemView,
    Observation,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import DEFAULT_SESSION, make_container
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
    moved,
    prepare,
)

AXE = an_item("42", container_ref=MAIN_REF, display_name="Axe")
BACKPACK = an_item("77", container_ref=MAIN_REF, display_name="Backpack")

WORN_BACK_REF = f"container:{DEFAULT_SESSION}:worn:Back:77"


def worn_container(ref: str = WORN_BACK_REF) -> ContainerView:
    return make_container(ref, ContainerKind.WORN, name="Backpack")


def gear(
    *items: ItemView,
    hands: Hands | None = None,
    containers: list[ContainerView] | None = None,
    seq: int = 1,
    no_inventory: bool = False,
) -> Observation:
    return a_world(
        seq=seq,
        items=list(items),
        containers=containers if containers is not None else [main_container(), bag_container()],
        hands=hands,
        no_inventory=no_inventory,
    )


def equip_command(item_ref: str = AXE.ref, **extra: object) -> Command:
    args: dict[str, object] = {"item_ref": item_ref}
    args.update(extra)
    return a_command(ActionName.EQUIPMENT_EQUIP, args)


def unequip_command(**args: object) -> Command:
    return a_command(ActionName.EQUIPMENT_UNEQUIP, dict(args))


# --------------------------------------------------------------------------
# equip: the postcondition
# --------------------------------------------------------------------------


def test_the_hand_holding_the_item_is_the_evidence() -> None:
    adapter = EquipAdapter()
    before = gear(AXE)
    command = prepare(adapter, equip_command(), before)

    evidence = adapter.verify(command, before, gear(AXE, hands=Hands(primary=AXE.ref), seq=2))

    assert evidence is not None
    assert evidence.kind == "item_in_hand"
    assert evidence.observed["slot"] == "primary"
    assert evidence.observed["runtime_id"] == "42"


def test_an_item_re_minted_into_the_main_inventory_is_still_the_same_item() -> None:
    """Equipping moves the item first, so its reference changes on the way."""
    adapter = EquipAdapter()
    in_bag = an_item("42", container_ref=BAG_REF, display_name="Axe")
    before = gear(in_bag)
    command = prepare(adapter, equip_command(in_bag.ref), before)
    fetched = moved(in_bag, MAIN_REF)

    evidence = adapter.verify(
        command, before, gear(fetched, hands=Hands(primary=fetched.ref), seq=2)
    )

    assert evidence is not None
    assert evidence.observed["runtime_id"] == "42"


def test_an_empty_hand_is_not_an_equipped_item() -> None:
    adapter = EquipAdapter()
    before = gear(AXE)
    command = prepare(adapter, equip_command(), before)

    assert adapter.verify(command, before, gear(AXE, seq=2)) is None


def test_the_other_hand_holding_it_does_not_answer_for_the_one_asked_for() -> None:
    adapter = EquipAdapter()
    before = gear(AXE)
    command = prepare(adapter, equip_command(hand="secondary"), before)

    assert adapter.verify(command, before, gear(AXE, hands=Hands(primary=AXE.ref), seq=2)) is None


def test_a_two_handed_equip_is_verified_against_the_primary_hand() -> None:
    adapter = EquipAdapter()
    before = gear(AXE)
    command = prepare(adapter, equip_command(hand="both"), before)

    evidence = adapter.verify(command, before, gear(AXE, hands=Hands(primary=AXE.ref), seq=2))

    assert evidence is not None
    assert evidence.observed["requested_hand"] == "both"


def test_a_worn_slot_reporting_the_item_is_the_evidence_for_a_garment() -> None:
    adapter = EquipAdapter()
    before = gear(BACKPACK)
    command = prepare(adapter, equip_command(BACKPACK.ref), before)

    now_worn = gear(
        containers=[main_container(), worn_container()],
        seq=2,
    )

    evidence = adapter.verify(command, before, now_worn)

    assert evidence is not None
    assert evidence.kind == "item_worn"
    assert evidence.observed["slot"] == "Back"


def test_a_garment_the_observation_never_describes_is_not_a_success() -> None:
    """A worn item with no inventory of its own has no reference to point at."""
    adapter = EquipAdapter()
    shirt = an_item("88", container_ref=MAIN_REF, display_name="T-Shirt")
    before = gear(shirt)
    command = prepare(adapter, equip_command(shirt.ref), before)

    vanished = gear(seq=2)

    assert adapter.verify(command, before, vanished) is None


def test_a_command_naming_a_hand_is_not_satisfied_by_a_body_slot() -> None:
    adapter = EquipAdapter()
    before = gear(BACKPACK)
    command = prepare(adapter, equip_command(BACKPACK.ref, hand="primary"), before)

    now_worn = gear(containers=[main_container(), worn_container()], seq=2)

    assert adapter.verify(command, before, now_worn) is None


# --------------------------------------------------------------------------
# equip: refusals
# --------------------------------------------------------------------------


def test_an_item_already_in_that_hand_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        EquipAdapter().validate(equip_command(), gear(AXE, hands=Hands(primary=AXE.ref)))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_an_item_already_worn_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        EquipAdapter().validate(
            equip_command(BACKPACK.ref),
            gear(BACKPACK, containers=[main_container(), worn_container()]),
        )
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_an_item_in_a_bag_names_the_transfer_it_depends_on() -> None:
    in_bag = an_item("42", container_ref=BAG_REF, display_name="Axe")

    with pytest.raises(PreconditionFailed) as caught:
        EquipAdapter().validate(equip_command(in_bag.ref), gear(in_bag))

    prerequisite = caught.value.evidence["prerequisites"][0]
    assert prerequisite["action"] == ActionName.INVENTORY_ENSURE_MAIN.value


def test_an_unknown_hand_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        EquipAdapter().validate(equip_command(hand="teeth"), gear(AXE))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_an_unresolvable_item_is_refused_as_invalid() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        EquipAdapter().validate(equip_command(), gear())
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_the_hand_is_only_sent_when_the_caller_named_one() -> None:
    """The mod decides hand or body slot from the item; a default would refuse garments."""
    assert EquipAdapter().build_args(equip_command(), gear(AXE)) == {"item_ref": AXE.ref}
    assert EquipAdapter().build_args(equip_command(hand="secondary"), gear(AXE)) == {
        "item_ref": AXE.ref,
        "hand": "secondary",
    }


def test_equipping_declares_the_capability_it_needs() -> None:
    assert EquipAdapter().required_capability == EQUIPMENT_EQUIP
    assert EquipAdapter().risk is RiskClass.P2


# --------------------------------------------------------------------------
# unequip
# --------------------------------------------------------------------------


def test_an_item_out_of_the_hand_and_still_carried_is_the_evidence() -> None:
    adapter = UnequipAdapter()
    before = gear(AXE, hands=Hands(primary=AXE.ref))
    command = prepare(adapter, unequip_command(hand="primary"), before)

    evidence = adapter.verify(command, before, gear(AXE, seq=2))

    assert evidence is not None
    assert evidence.kind == "item_unequipped"
    assert evidence.observed["container_ref"] == MAIN_REF
    assert evidence.observed["runtime_id"] == "42"


def test_an_item_still_in_the_hand_is_not_unequipped() -> None:
    adapter = UnequipAdapter()
    before = gear(AXE, hands=Hands(primary=AXE.ref))
    command = prepare(adapter, unequip_command(hand="primary"), before)

    assert adapter.verify(command, before, gear(AXE, hands=Hands(primary=AXE.ref), seq=2)) is None


def test_an_item_that_left_the_hand_and_the_inventory_was_dropped_not_unequipped() -> None:
    """This is the test the second half of the postcondition exists for."""
    adapter = UnequipAdapter()
    before = gear(AXE, hands=Hands(primary=AXE.ref))
    command = prepare(adapter, unequip_command(hand="primary"), before)

    assert adapter.verify(command, before, gear(seq=2)) is None


def test_an_item_that_ended_up_in_a_world_container_is_not_unequipped() -> None:
    adapter = UnequipAdapter()
    before = gear(AXE, hands=Hands(primary=AXE.ref))
    command = prepare(adapter, unequip_command(hand="primary"), before)

    on_the_floor = gear(
        moved(AXE, CRATE_REF),
        containers=[main_container(), crate_container()],
        seq=2,
    )

    assert adapter.verify(command, before, on_the_floor) is None


def test_a_duplicated_item_cannot_be_shown_to_have_been_put_away() -> None:
    adapter = UnequipAdapter()
    before = gear(AXE, hands=Hands(primary=AXE.ref))
    command = prepare(adapter, unequip_command(hand="primary"), before)

    duplicated = gear(AXE, moved(AXE, BAG_REF), seq=2)

    assert adapter.verify(command, before, duplicated) is None


def test_a_garment_still_in_its_slot_is_not_unequipped() -> None:
    adapter = UnequipAdapter()
    before = gear(BACKPACK, containers=[main_container(), worn_container()])
    command = prepare(adapter, unequip_command(slot="Back"), before)

    assert adapter.verify(command, before, before) is None


def test_a_garment_out_of_its_slot_and_back_in_the_inventory_is_the_evidence() -> None:
    adapter = UnequipAdapter()
    before = gear(BACKPACK, containers=[main_container(), worn_container()])
    command = prepare(adapter, unequip_command(slot="Back"), before)

    stowed = gear(BACKPACK, seq=2)

    evidence = adapter.verify(command, before, stowed)

    assert evidence is not None
    assert evidence.observed["slot"] == "Back"


def test_an_observation_with_no_inventory_cannot_show_the_item_was_kept() -> None:
    adapter = UnequipAdapter()
    before = gear(AXE, hands=Hands(primary=AXE.ref))
    command = prepare(adapter, unequip_command(hand="primary"), before)

    assert adapter.verify(command, before, gear(seq=2, no_inventory=True)) is None


def test_naming_nothing_to_take_off_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        UnequipAdapter().validate(unequip_command(), gear(AXE, hands=Hands(primary=AXE.ref)))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_naming_two_things_to_take_off_is_refused_rather_than_reconciled() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        UnequipAdapter().validate(
            unequip_command(hand="primary", item_ref=AXE.ref),
            gear(AXE, hands=Hands(primary=AXE.ref)),
        )
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_an_empty_hand_has_nothing_to_take_off() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        UnequipAdapter().validate(unequip_command(hand="primary"), gear(AXE))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_an_item_that_is_neither_held_nor_worn_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        UnequipAdapter().validate(unequip_command(item_ref=AXE.ref), gear(AXE))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_held_item_in_no_reported_container_cannot_be_shown_to_be_kept() -> None:
    held_only = a_world(seq=1, items=[], hands=Hands(primary=AXE.ref))

    with pytest.raises(PreconditionFailed) as caught:
        UnequipAdapter().validate(unequip_command(item_ref=AXE.ref), held_only)
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_the_naming_the_caller_used_is_the_naming_the_mod_receives() -> None:
    """A held weapon may sit in no container, so a hand must not become a reference."""
    before = gear(AXE, hands=Hands(primary=AXE.ref))

    assert UnequipAdapter().build_args(unequip_command(hand="primary"), before) == {
        "hand": "primary"
    }


def test_unequipping_declares_the_capability_it_needs() -> None:
    assert UnequipAdapter().required_capability == EQUIPMENT_UNEQUIP
    assert UnequipAdapter().risk is RiskClass.P2
