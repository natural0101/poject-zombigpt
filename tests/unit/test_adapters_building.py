"""The two building adapters: the gate before, the square after.

Four properties carry this wave's safety weight and all four live here. The
build's tier is P4 and it is a constant — there is no ``risk_for`` on the
adapter at all, which is the statement that no argument makes placing a
permanent object cheaper. The policy refusal happens in ``validate``, before any
command exists to send, so a placement that would seal the character in costs no
wire traffic and no world change. ``verify`` answers only to the square reading
back as occupied, never to the mod saying it finished. And the command carries
no ``count``: one command builds one structure once, and there is no number in
the args for a loop in the mod to read.

The argument shapes are pinned literally rather than by round-trip, because the
mod's own dumper is compared against them byte for byte.
"""

from __future__ import annotations

from typing import Any

import pytest

from pz_agent_core.actions.adapter import PreconditionFailed
from pz_agent_core.actions.adapters.building import (
    _REFUSAL_REASONS,
    MAX_BLUEPRINT_NAME_LEN,
    MAX_LISTED_STRUCTURES,
    BuildingBuildAdapter,
    BuildingInspectAdapter,
)
from pz_agent_core.capabilities.probes import BUILDING, PROBES_BY_NAME
from pz_agent_core.policy.building import (
    BUILDING_KEY,
    SEMANTIC_BLOCKED,
    SEMANTIC_LOADED,
    BuildingRefusal,
)
from pz_agent_core.protocol import (
    READ_ONLY_ACTIONS,
    ActionName,
    CapabilityState,
    Command,
    ItemView,
    NearbyObject,
    Observation,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.adapter_worlds import (
    HOME_X,
    HOME_Y,
    HOME_Z,
    MAIN_REF,
    a_command,
    a_square,
    a_world,
    a_world_object,
    an_item,
    crate_container,
    main_container,
    object_ref,
    prepare,
    square_ref,
)

WALL = "WoodenWall"
PLANK = "Base.Plank"
TARGET_X = HOME_X + 1
TARGET_Y = HOME_Y
TARGET_REF = square_ref(TARGET_X, TARGET_Y, HOME_Z)


def structure_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": WALL,
        "display_name": "Wooden Wall",
        "known": True,
        "blocks_movement": True,
        "materials": [{"full_type": PLANK, "count": 1}],
    }
    payload.update(overrides)
    return payload


def plank(
    runtime_id: str = "m1",
    *,
    structures: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> ItemView:
    return an_item(
        runtime_id=runtime_id,
        container_ref=MAIN_REF,
        full_type=PLANK,
        display_name="Plank",
        category="Item",
        extra={
            BUILDING_KEY: {
                "structure_count": 1,
                "known_structure_count": 1,
                "structures": structures if structures is not None else [structure_payload()],
            }
        },
        **overrides,
    )


def block(
    *,
    blocked: tuple[tuple[int, int], ...] = (),
    radius: int = 1,
) -> list[NearbyObject]:
    squares: list[NearbyObject] = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            marks = [SEMANTIC_LOADED]
            if (dx, dy) in blocked:
                marks.append(SEMANTIC_BLOCKED)
            squares.append(a_square(HOME_X + dx, HOME_Y + dy, HOME_Z, semantics=marks))
    return squares


def world(
    *items: ItemView,
    objects: list[NearbyObject] | None = None,
    seq: int = 1,
    **overrides: Any,
) -> Observation:
    return a_world(
        seq=seq,
        items=list(items),
        objects=block() if objects is None else objects,
        containers=[main_container(), crate_container()],
        **overrides,
    )


def build_command(**args: Any) -> Command:
    payload: dict[str, Any] = {"blueprint": WALL, "square": TARGET_REF}
    payload.update(args)
    return a_command(ActionName.BUILDING_BUILD, payload)


def inspect_command(**args: Any) -> Command:
    payload: dict[str, Any] = {"square": TARGET_REF}
    payload.update(args)
    return a_command(ActionName.BUILDING_INSPECT, payload)


# --------------------------------------------------------------------------
# the shape of the two actions
# --------------------------------------------------------------------------


def test_the_inspect_is_read_only_and_gates_on_a_tier_not_a_probe() -> None:
    """The published half, and the agreement it keeps with the mod.

    ``adapters/Building.lua`` declares ``capability = nil`` for this action, and
    the two halves of the wire have to name the same one. Withholding the
    reading would not buy safety anyway: it is what a user consults before
    granting the P4, so taking it away makes that decision less informed.
    """
    adapter = BuildingInspectAdapter()

    assert adapter.name is ActionName.BUILDING_INSPECT
    assert adapter.name in READ_ONLY_ACTIONS
    assert adapter.risk is RiskClass.P0
    assert adapter.required_capability is None


def test_the_build_is_p4_behind_the_building_capability() -> None:
    adapter = BuildingBuildAdapter()

    assert adapter.risk is RiskClass.P4
    assert adapter.required_capability == BUILDING
    assert BUILDING in PROBES_BY_NAME


def test_the_build_has_no_escalation_ladder_because_there_is_no_rung_above_it() -> None:
    """``crafting.craft`` computes its tier per command; this one cannot.

    A craft is P3 until the recipe needs a surface. There is no argument to
    ``building.build`` that makes placing a permanent object worth less than the
    top of the ladder, so the tier is a class constant and the absence of
    ``risk_for`` is the assertion that no such argument exists.
    """
    assert not hasattr(BuildingBuildAdapter(), "risk_for")


def test_the_placement_is_withheld_on_a_clean_install_and_the_reading_is_not() -> None:
    """The split, pinned: an experimental capability is not a usable one."""
    probe = PROBES_BY_NAME[BUILDING]

    assert probe.static_state is CapabilityState.EXPERIMENTAL
    assert probe.static_reason
    assert not probe.static_state.usable
    assert probe.confirmation.action is ActionName.BUILDING_BUILD
    assert probe.confirmation.evidence_keys == ("blueprint", "square")
    assert BuildingBuildAdapter().required_capability == BUILDING
    assert BuildingInspectAdapter().required_capability is None


def test_every_policy_refusal_has_a_reason_code_of_its_own() -> None:
    """Total over the enum: a refusal that cannot name itself is an INTERNAL_ERROR."""
    assert set(_REFUSAL_REASONS) == set(BuildingRefusal)
    assert _REFUSAL_REASONS[BuildingRefusal.SQUARE_OCCUPIED] is ReasonCode.SQUARE_OCCUPIED
    assert _REFUSAL_REASONS[BuildingRefusal.WOULD_TRAP_PLAYER] is ReasonCode.WOULD_TRAP_PLAYER
    assert (
        _REFUSAL_REASONS[BuildingRefusal.MATERIALS_MISSING] is ReasonCode.RECIPE_MATERIALS_MISSING
    )
    assert _REFUSAL_REASONS[BuildingRefusal.MATERIALS_RESERVED] is ReasonCode.RESOURCE_RESERVED
    assert _REFUSAL_REASONS[BuildingRefusal.STRUCTURE_UNKNOWN] is ReasonCode.RECIPE_UNKNOWN
    assert _REFUSAL_REASONS[BuildingRefusal.SQUARE_UNREADABLE] is ReasonCode.TARGET_NOT_LOADED


# --------------------------------------------------------------------------
# the arguments, as the mod receives them
# --------------------------------------------------------------------------


def test_the_build_args_are_the_blueprint_and_the_square_and_nothing_else() -> None:
    """Pinned literally: the mod's dumper is compared against these two keys."""
    adapter = BuildingBuildAdapter()

    assert adapter.build_args(build_command(), world(plank())) == {
        "blueprint": WALL,
        "square": TARGET_REF,
    }


def test_the_inspect_args_are_the_square_and_the_bounded_limit() -> None:
    adapter = BuildingInspectAdapter()

    assert adapter.build_args(inspect_command(), world(plank())) == {
        "square": TARGET_REF,
        "limit": MAX_LISTED_STRUCTURES,
    }
    assert adapter.build_args(inspect_command(limit=3), world(plank())) == {
        "square": TARGET_REF,
        "limit": 3,
    }


def test_there_is_no_count_argument_for_a_loop_to_read() -> None:
    """One command builds one structure once, and the wire cannot say otherwise."""
    with pytest.raises(PreconditionFailed) as caught:
        BuildingBuildAdapter().validate(build_command(count=2), world(plank()))

    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT
    assert "count" in str(caught.value)


@pytest.mark.parametrize("missing", ["blueprint", "square"])
def test_both_arguments_are_required_and_neither_has_a_default(missing: str) -> None:
    """No implicit "here": "here" is the square somebody is standing on."""
    args: dict[str, Any] = {"blueprint": WALL, "square": TARGET_REF}
    del args[missing]

    with pytest.raises(PreconditionFailed) as caught:
        BuildingBuildAdapter().validate(a_command(ActionName.BUILDING_BUILD, args), world(plank()))

    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("x" * (MAX_BLUEPRINT_NAME_LEN + 1), id="too-long"),
        pytest.param("Wooden Wall", id="space"),
        pytest.param("Wooden*", id="wildcard"),
    ],
)
def test_a_blueprint_name_is_an_identifier_not_a_pattern(name: str) -> None:
    with pytest.raises(PreconditionFailed) as caught:
        BuildingBuildAdapter().validate(build_command(blueprint=name), world(plank()))

    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    ("square", "code"),
    [
        pytest.param("not-a-ref", ReasonCode.INVALID_REF, id="not-a-ref"),
        pytest.param(f"item:{DEFAULT_SESSION}:main:1:0", ReasonCode.INVALID_REF, id="wrong-kind"),
        pytest.param(
            "square:00000000-0000-4000-8000-000000000009:1:2:0",
            ReasonCode.INVALID_REF,
            id="other-session",
        ),
    ],
)
def test_the_square_must_be_a_reference_this_session_minted(square: str, code: ReasonCode) -> None:
    """A placement can only target ground this session actually observed."""
    with pytest.raises(PreconditionFailed) as caught:
        BuildingBuildAdapter().validate(build_command(square=square), world(plank()))

    assert caught.value.reason_code is code


# --------------------------------------------------------------------------
# the gate before anything is queued
# --------------------------------------------------------------------------


def test_a_well_formed_build_on_a_free_square_validates() -> None:
    BuildingBuildAdapter().validate(build_command(), world(plank()))


def test_a_missing_tier_is_a_capability_refusal_not_a_guess() -> None:
    for observation in (
        world(plank(), no_inventory=True),
        world(plank(), no_nearby=True),
    ):
        with pytest.raises(PreconditionFailed) as caught:
            BuildingBuildAdapter().validate(build_command(), observation)

        assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_a_structure_nothing_observed_mentions_is_refused_by_name() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        BuildingBuildAdapter().validate(build_command(), world())

    assert caught.value.reason_code is ReasonCode.RECIPE_UNKNOWN
    assert caught.value.evidence["structure"] == WALL


def test_an_occupied_square_refuses_before_anything_is_queued_and_names_what_stands_there() -> None:
    crate = a_world_object(object_ref("c1"), x=TARGET_X, y=TARGET_Y)

    with pytest.raises(PreconditionFailed) as caught:
        BuildingBuildAdapter().validate(build_command(), world(plank(), objects=[*block(), crate]))

    assert caught.value.reason_code is ReasonCode.SQUARE_OCCUPIED
    assert caught.value.evidence["refusal"] == BuildingRefusal.SQUARE_OCCUPIED.value
    assert caught.value.evidence["occupied_by"] == [f"container:{crate.ref}"]


def test_a_wall_that_would_seal_the_character_in_refuses_and_carries_its_claim() -> None:
    """The refusal this wave exists for, with the honesty about the bound attached."""
    sealed = world(plank(), objects=block(blocked=((-1, 0), (0, 1), (0, -1))))

    with pytest.raises(PreconditionFailed) as caught:
        BuildingBuildAdapter().validate(build_command(), sealed)

    assert caught.value.reason_code is ReasonCode.WOULD_TRAP_PLAYER
    enclosure = caught.value.evidence["enclosure"]
    assert enclosure["passed"] is False
    assert "no route remains" in enclosure["claim"]
    assert enclosure["window_squares"] == 9


def test_a_shortfall_refuses_with_the_crafting_rungs_reason_code() -> None:
    hungry = world(
        plank(structures=[structure_payload(materials=[{"full_type": PLANK, "count": 4}])])
    )

    with pytest.raises(PreconditionFailed) as caught:
        BuildingBuildAdapter().validate(build_command(), hungry)

    assert caught.value.reason_code is ReasonCode.RECIPE_MATERIALS_MISSING
    assert caught.value.evidence["shortfalls"][0]["needed"] == 4
    assert caught.value.evidence["square"] == {"x": TARGET_X, "y": TARGET_Y, "z": HOME_Z}


def test_a_reserved_material_refuses_with_its_own_code_so_the_user_can_lift_it() -> None:
    reserved = world(plank(favorite=True))

    with pytest.raises(PreconditionFailed) as caught:
        BuildingBuildAdapter().validate(build_command(), reserved)

    assert caught.value.reason_code is ReasonCode.RESOURCE_RESERVED


# --------------------------------------------------------------------------
# the proof afterwards
# --------------------------------------------------------------------------


def test_a_square_that_reads_exactly_as_before_proves_nothing() -> None:
    adapter = BuildingBuildAdapter()
    before = world(plank())
    command = prepare(adapter, build_command(), before)

    assert adapter.verify(command, before, world(plank(), seq=2)) is None


def test_an_object_that_appeared_on_the_square_is_the_proof() -> None:
    adapter = BuildingBuildAdapter()
    before = world(plank())
    raised = a_world_object(
        object_ref("w1"),
        x=TARGET_X,
        y=TARGET_Y,
        kind="wall",
        semantics=["obstacle"],
    )
    after = world(objects=[*block(blocked=((1, 0),)), raised], seq=2)
    command = prepare(adapter, build_command(), before)

    evidence = adapter.verify(command, before, after)

    assert evidence is not None
    assert evidence.kind == "structure_on_square"
    assert evidence.observed["blueprint"] == WALL
    assert evidence.observed["square"] == {"x": TARGET_X, "y": TARGET_Y, "z": HOME_Z}
    assert evidence.observed["object_refs"] == [raised.ref]
    assert evidence.observed["object_kinds"] == ["wall"]
    assert evidence.observed["occupied_before"] is False
    assert evidence.observed["occupied_after"] is True


def test_a_square_that_turned_blocked_is_proof_even_with_no_new_object() -> None:
    """Some builds leave nothing separately described; the square itself changed."""
    adapter = BuildingBuildAdapter()
    before = world(plank())
    after = world(objects=block(blocked=((1, 0),)), seq=2)
    command = prepare(adapter, build_command(), before)

    evidence = adapter.verify(command, before, after)

    assert evidence is not None
    assert evidence.observed["object_refs"] == []
    assert evidence.observed["occupied_after"] is True


def test_a_square_that_was_already_occupied_is_not_re_proved_by_being_occupied() -> None:
    adapter = BuildingBuildAdapter()
    before = world(plank(), objects=block(blocked=((1, 0),)))
    after = world(objects=block(blocked=((1, 0),)), seq=2)
    command = prepare(adapter, build_command(), before)

    assert adapter.verify(command, before, after) is None


def test_an_unreadable_window_afterwards_leaves_the_postcondition_unproven() -> None:
    adapter = BuildingBuildAdapter()
    before = world(plank())
    command = prepare(adapter, build_command(), before)

    assert adapter.verify(command, before, world(plank(), no_nearby=True, seq=2)) is None


# --------------------------------------------------------------------------
# the reading half
# --------------------------------------------------------------------------


def test_the_inspect_answers_for_the_square_it_was_asked_about() -> None:
    adapter = BuildingInspectAdapter()
    observation = world(plank())
    command = prepare(adapter, inspect_command(), observation)

    evidence = adapter.verify(command, observation, observation)

    assert evidence is not None
    assert evidence.kind == "placement_described"
    assert evidence.observed["square"] == {"x": TARGET_X, "y": TARGET_Y, "z": HOME_Z}
    assert evidence.observed["square_occupied"] is False
    assert evidence.observed["blueprints"] == [
        {
            "blueprint": WALL,
            "known": True,
            "blocks_when_placed": True,
            "buildable": True,
            "refusal": None,
            "shortfalls": [],
        }
    ]
    assert evidence.observed["enclosure_if_solid"]["passed"] is True


def test_the_inspect_reports_the_refusal_the_build_would_reach_for_this_square() -> None:
    """A caller sees the refusal coming instead of discovering it one P4 later."""
    adapter = BuildingInspectAdapter()
    sealed = world(plank(), objects=block(blocked=((-1, 0), (0, 1), (0, -1))))
    command = prepare(adapter, inspect_command(), sealed)

    evidence = adapter.verify(command, sealed, sealed)

    assert evidence is not None
    entry = evidence.observed["blueprints"][0]
    assert entry["buildable"] is False
    assert entry["refusal"] == BuildingRefusal.WOULD_TRAP_PLAYER.value
    assert evidence.observed["enclosure_if_solid"]["passed"] is False


def test_the_listing_is_bounded_by_the_limit_and_says_when_it_was_trimmed() -> None:
    adapter = BuildingInspectAdapter()
    many = [structure_payload(name=f"Wall{index:02d}") for index in range(4)]
    observation = world(plank(structures=many))
    command = prepare(adapter, inspect_command(limit=2), observation)

    evidence = adapter.verify(command, observation, observation)

    assert evidence is not None
    assert evidence.observed["listed"] == 2
    assert evidence.observed["truncated"] is True


def test_an_observation_carrying_no_readout_at_all_is_a_failed_reading() -> None:
    """The line ``container.inspect`` draws: an empty crate is not an undescribed one."""
    adapter = BuildingInspectAdapter()
    observation = world(an_item("x1"))
    command = prepare(adapter, inspect_command(), observation)

    assert adapter.verify(command, observation, observation) is None


def test_an_observation_with_no_window_is_a_failed_reading() -> None:
    adapter = BuildingInspectAdapter()
    observation = world(plank())
    command = prepare(adapter, inspect_command(), observation)

    assert adapter.verify(command, observation, world(plank(), no_nearby=True, seq=2)) is None


def test_an_unreadable_window_beforehand_leaves_the_postcondition_unproven() -> None:
    """Both proofs are differences, so both need a picture of the square before."""
    adapter = BuildingBuildAdapter()
    before = world(plank(), no_nearby=True)
    after = world(objects=block(blocked=((1, 0),)), seq=2)
    command = build_command()

    assert adapter.verify(command, before, after) is None
