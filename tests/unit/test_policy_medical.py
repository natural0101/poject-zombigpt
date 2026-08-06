"""Deterministic triage.

Four claims are worth a test each, because each of them is a decision that
would otherwise be made by whichever wound the mod happened to list first:
bleeding outranks not bleeding whatever the severities, deeper outranks
shallow, a sterile dressing beats a dirty one, and a dirty one is refused
outright while a clean one is in reach. The last is the one that matters most —
an infected bandage trades bleeding, which stops, for an infection, which does
not.
"""

from __future__ import annotations

from typing import Any

import pytest

from pz_agent_core.policy.medical import (
    DIRTY_DRESSING_TYPES,
    Dressing,
    TreatmentSelection,
    select_treatment,
    wound_body_part,
)
from pz_agent_core.policy.selection import RejectionReason
from pz_agent_core.protocol import (
    ContainerView,
    InventoryView,
    ItemView,
    PlayerState,
    ReasonCode,
    Wound,
)
from tests.fixtures import DEFAULT_SESSION, make_player
from tests.fixtures.policy_items import (
    BACKPACK_REF,
    MAIN_REF,
    WORLD_REF,
    backpack_container,
    main_container,
    policy_item_ref,
    world_container,
)

ARM = "ForeArm_L"
LEG = "LowerLeg_R"
HEAD = "Head"


def dressing(
    runtime_id: str = "42",
    *,
    full_type: str = "Base.Bandage",
    display_name: str = "Bandage",
    container_ref: str = MAIN_REF,
    **overrides: Any,
) -> ItemView:
    base: dict[str, Any] = {
        "ref": policy_item_ref(runtime_id, container_ref),
        "container_ref": container_ref,
        "full_type": full_type,
        "display_name": display_name,
        "category": "Medical",
        "weight": 0.1,
    }
    base.update(overrides)
    return ItemView(**base)


def hurt(*parts: str, bleeding: bool = True, severity: float = 0.4) -> PlayerState:
    return make_player(
        wounds=[
            Wound(
                ref=f"wound:{DEFAULT_SESSION}:{part}",
                kind="cut",
                severity=severity,
                bleeding=bleeding,
            )
            for part in parts
        ]
    )


def wounded(*wounds: Wound) -> PlayerState:
    return make_player(wounds=list(wounds))


def a_wound(part: str, *, bleeding: bool, severity: float, kind: str = "cut") -> Wound:
    return Wound(
        ref=f"wound:{DEFAULT_SESSION}:{part}", kind=kind, severity=severity, bleeding=bleeding
    )


def supplies(*items: ItemView, containers: list[ContainerView] | None = None) -> InventoryView:
    return InventoryView(
        containers=containers
        if containers is not None
        else [main_container(), backpack_container(), world_container()],
        items=list(items),
    )


# --------------------------------------------------------------------------
# which wound
# --------------------------------------------------------------------------


def test_a_bleeding_wound_outranks_a_worse_one_that_is_not_bleeding() -> None:
    """Only one of the two is still losing blood."""
    player = wounded(
        a_wound(HEAD, bleeding=False, severity=0.9),
        a_wound(ARM, bleeding=True, severity=0.2),
    )

    selection = select_treatment(supplies(dressing()), player)

    assert selection.choice is not None
    assert selection.choice.body_part == ARM


def test_the_deeper_of_two_bleeding_wounds_is_treated_first() -> None:
    player = wounded(
        a_wound(ARM, bleeding=True, severity=0.3),
        a_wound(LEG, bleeding=True, severity=0.8),
    )

    selection = select_treatment(supplies(dressing()), player)

    assert selection.choice is not None
    assert selection.choice.body_part == LEG


def test_two_equal_wounds_are_treated_in_a_fixed_order() -> None:
    """A retry after an interrupted attempt has to pick the same one again."""
    player = wounded(
        a_wound("ForeArm_R", bleeding=True, severity=0.5),
        a_wound("ForeArm_L", bleeding=True, severity=0.5),
    )

    first = select_treatment(supplies(dressing()), player)
    again = select_treatment(supplies(dressing()), player)

    assert first.choice is not None and again.choice is not None
    assert first.choice.body_part == again.choice.body_part == "ForeArm_L"


def test_a_character_with_nothing_bleeding_is_refused_with_a_reason() -> None:
    selection = select_treatment(supplies(dressing()), hurt(ARM, bleeding=False))

    assert selection.is_refusal
    assert selection.reason_code is ReasonCode.PRECONDITION_FAILED
    assert "bleeding" in selection.explain()


def test_an_uninjured_character_is_refused() -> None:
    selection = select_treatment(supplies(dressing()), make_player())

    assert selection.is_refusal
    assert selection.choice is None


def test_a_wound_reference_without_a_part_is_not_treatable() -> None:
    """A part nothing can name is a part no command could target."""
    nameless = Wound(ref="wound-3", kind="cut", severity=0.9, bleeding=True)

    selection = select_treatment(supplies(dressing()), wounded(nameless))

    assert selection.is_refusal


# --------------------------------------------------------------------------
# which dressing
# --------------------------------------------------------------------------


def test_a_sterile_dressing_is_preferred_over_a_dirty_one() -> None:
    clean = dressing("42")
    dirty = dressing("43", full_type=DIRTY_DRESSING_TYPES[0], display_name="Dirty Rag")

    selection = select_treatment(supplies(dirty, clean), hurt(ARM))

    assert selection.choice is not None
    assert selection.choice.item_ref == clean.ref
    assert selection.choice.sterile is True


def test_a_dirty_dressing_is_refused_outright_while_a_clean_one_exists() -> None:
    clean = dressing("42")
    dirty = dressing("43", full_type=DIRTY_DRESSING_TYPES[0], display_name="Dirty Rag")

    selection = select_treatment(supplies(dirty, clean), hurt(ARM))

    refused = {r.item_ref: r for r in selection.rejections}
    assert refused[dirty.ref].reason is RejectionReason.TAINTED
    assert "infection" in refused[dirty.ref].detail


def test_a_dirty_dressing_is_used_when_it_is_all_there_is() -> None:
    """Refusing to treat a bleeding wound at all is worse than an infection risk."""
    dirty = dressing("43", full_type=DIRTY_DRESSING_TYPES[0], display_name="Dirty Rag")

    selection = select_treatment(supplies(dirty), hurt(ARM))

    assert selection.choice is not None
    assert selection.choice.item_ref == dirty.ref
    assert selection.choice.sterile is False
    assert "nothing sterile" in selection.choice.rationale


def test_the_closed_list_orders_the_clean_dressings() -> None:
    sheet = dressing("43", full_type="Base.RippedSheets", display_name="Ripped Sheets")
    bandage = dressing("44")

    selection = select_treatment(supplies(sheet, bandage), hurt(ARM))

    assert selection.choice is not None
    assert selection.choice.item_ref == bandage.ref


def test_an_item_tagged_as_a_dressing_is_usable_but_never_beats_a_bandage() -> None:
    tagged = dressing("43", full_type="Mod.SterileGauze", display_name="Gauze", tags=["bandage"])
    bandage = dressing("44")

    with_bandage = select_treatment(supplies(tagged, bandage), hurt(ARM))
    without = select_treatment(supplies(tagged), hurt(ARM))

    assert with_bandage.choice is not None and with_bandage.choice.item_ref == bandage.ref
    assert without.choice is not None and without.choice.item_ref == tagged.ref


def test_a_character_carrying_no_dressing_is_refused_with_the_part_named() -> None:
    selection = select_treatment(supplies(), hurt(ARM))

    assert selection.is_refusal
    assert selection.reason_code is ReasonCode.RESOURCE_RESERVED
    assert ARM in selection.explain()


def test_a_reserved_dressing_is_never_taken_automatically() -> None:
    reserved = dressing("42", favorite=True)

    selection = select_treatment(supplies(reserved), hurt(ARM))

    assert selection.is_refusal
    assert selection.rejections[0].reason is RejectionReason.USER_RESERVED


def test_a_dressing_in_a_world_container_is_out_of_reach() -> None:
    far = dressing("42", container_ref=WORLD_REF)

    selection = select_treatment(supplies(far), hurt(ARM))

    assert selection.is_refusal
    assert selection.rejections[0].reason is RejectionReason.CONTAINER_UNREACHABLE


def test_an_equipped_dressing_is_left_alone() -> None:
    held = dressing("42", equipped=True)

    selection = select_treatment(supplies(held), hurt(ARM))

    assert selection.is_refusal
    assert selection.rejections[0].reason is RejectionReason.NOT_REQUESTED_ITEM


def test_a_dressing_in_a_bag_is_chosen_and_says_it_needs_moving_first() -> None:
    in_bag = dressing("42", container_ref=BACKPACK_REF)

    selection = select_treatment(supplies(in_bag), hurt(ARM))

    assert selection.choice is not None
    assert selection.choice.needs_transfer_to_main is True
    assert "main inventory" in selection.choice.rationale


# --------------------------------------------------------------------------
# totality and shape
# --------------------------------------------------------------------------


def test_every_input_yields_a_choice_or_a_typed_refusal() -> None:
    cases = [
        (supplies(), make_player()),
        (supplies(), hurt(ARM)),
        (supplies(dressing()), hurt(ARM)),
        (InventoryView(), hurt(ARM)),
    ]

    for inventory, player in cases:
        selection = select_treatment(inventory, player)
        assert (selection.choice is None) != (selection.reason_code is None)
        assert selection.explain()


def test_a_selection_cannot_be_both_a_choice_and_a_refusal() -> None:
    with pytest.raises(ValueError, match="never both or neither"):
        TreatmentSelection(choice=None, reason_code=None, rejections=())


def test_the_rationale_names_the_wounds_still_waiting() -> None:
    player = wounded(
        a_wound(ARM, bleeding=True, severity=0.8),
        a_wound(LEG, bleeding=True, severity=0.5),
    )

    selection = select_treatment(supplies(dressing()), player)

    assert selection.choice is not None
    assert LEG in selection.choice.rationale


def test_the_choice_serialises_to_something_a_command_can_be_built_from() -> None:
    selection = select_treatment(supplies(dressing()), hurt(ARM))

    assert selection.choice is not None
    payload = selection.choice.as_dict()
    assert payload["body_part"] == ARM
    assert payload["item_ref"].startswith("item:")


def test_a_wound_reference_yields_the_body_part_it_names() -> None:
    assert wound_body_part(a_wound(ARM, bleeding=True, severity=0.1)) == ARM
    assert wound_body_part(Wound(ref="nonsense", kind="cut", severity=0.1)) == ""


def test_anything_that_is_not_a_dressing_is_not_a_candidate_at_all() -> None:
    hammer = dressing("50", full_type="Base.Hammer", display_name="Hammer")

    selection = select_treatment(supplies(hammer), hurt(ARM))

    assert selection.is_refusal
    assert selection.rejections == ()
    assert Dressing.classify(hammer) is None
