"""``door.open``, ``door.close`` and ``door.unlock``.

The tri-state door fields carry every test here: a gate may fire only on an
observed ``True`` or ``False``, and an absent field must pass the command
through to the mod rather than being read as either answer. The verify half is
the same rule pointed the other way — the postcondition is the observed state
in the *after* snapshot, and a door that is out of view, unreadable, or still
in its old state proves nothing, however the toggle sounded.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.actions.adapters.doors import (
    DEFAULT_DOOR_RADIUS,
    DoorCloseAdapter,
    DoorOpenAdapter,
    DoorUnlockAdapter,
)
from pz_agent_core.capabilities.probes import DOOR_TOGGLE
from pz_agent_core.protocol import (
    ActionName,
    Command,
    NearbyObject,
    Observation,
    Position,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.adapter_worlds import HOME_X, HOME_Y, a_command, a_world, prepare

DOOR_X = HOME_X + 3
DOOR_Y = HOME_Y
#: The object reference the doors epic added: the square the door stands on,
#: plus the engine's own index into that square's object list.
DOOR = f"object:{DEFAULT_SESSION}:{DOOR_X}:{DOOR_Y}:0:2"

DoorAdapter = DoorOpenAdapter | DoorCloseAdapter | DoorUnlockAdapter


def a_door(
    *,
    ref: str = DOOR,
    kind: str = "door",
    open: bool | None = False,  # the observation field's own name, builtin or not
    locked: bool | None = None,
    barricaded: bool | None = None,
    distance: float = 3.0,
) -> NearbyObject:
    return NearbyObject(
        ref=ref,
        kind=kind,
        distance=distance,
        position=Position(x=float(DOOR_X), y=float(DOOR_Y), z=0),
        semantics=["door"],
        open=open,
        locked=locked,
        barricaded=barricaded,
        orientation="north",
    )


def door_world(door: NearbyObject | None, *, seq: int = 1, at_door: bool = False) -> Observation:
    position = Position(x=float(DOOR_X), y=float(DOOR_Y), z=0, direction="S") if at_door else None
    return a_world(seq=seq, objects=[] if door is None else [door], position=position)


def door_command(action: ActionName, **args: object) -> Command:
    payload: dict[str, object] = {"door_ref": DOOR}
    payload.update(args)
    return a_command(action, payload)


ADAPTERS: list[DoorAdapter] = [DoorOpenAdapter(), DoorCloseAdapter(), DoorUnlockAdapter()]
IDS = [adapter.name.value for adapter in ADAPTERS]


# --------------------------------------------------------------------------
# what all three declare
# --------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_touching_a_door_is_the_world_tier_behind_the_one_door_capability(
    adapter: DoorAdapter,
) -> None:
    assert adapter.risk is RiskClass.P3
    assert adapter.required_capability == DOOR_TOGGLE


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_the_payload_is_the_reference_and_the_reach(adapter: DoorAdapter) -> None:
    args = adapter.build_args(door_command(adapter.name), door_world(a_door()))

    assert args == {"door_ref": DOOR, "radius": DEFAULT_DOOR_RADIUS}


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_the_adapter_can_read_back_the_arguments_it_ships(adapter: DoorAdapter) -> None:
    """``build_args`` output must survive the adapter's own parser, unchanged."""
    world = door_world(a_door())
    prepared = prepare(adapter, door_command(adapter.name), world)

    assert adapter.build_args(prepared, world) == prepared.args
    adapter.validate(prepared, world)


# --------------------------------------------------------------------------
# refusals shared by all three
# --------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_a_door_the_observer_did_not_report_is_target_not_loaded(adapter: DoorAdapter) -> None:
    """The observation is the only truth this side has about the door."""
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(door_command(adapter.name), door_world(None))
    assert caught.value.reason_code is ReasonCode.TARGET_NOT_LOADED


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_an_object_observed_as_something_else_is_an_invalid_ref(adapter: DoorAdapter) -> None:
    """The reference indexes the square's object list; a non-door there means it shifted."""
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(door_command(adapter.name), door_world(a_door(kind="window")))
    assert caught.value.reason_code is ReasonCode.INVALID_REF


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_an_observed_barricade_demands_a_detour(adapter: DoorAdapter) -> None:
    """Planks hold the door where it is, whichever way the command pushes."""
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(door_command(adapter.name), door_world(a_door(barricaded=True)))
    assert caught.value.reason_code is ReasonCode.DOOR_BARRICADED


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_an_unreadable_barricade_state_is_the_mods_to_judge(adapter: DoorAdapter) -> None:
    """``None`` is "no reader on this build", which is not an observed barricade."""
    adapter.validate(door_command(adapter.name), door_world(a_door(barricaded=None)))


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_a_reference_of_another_kind_is_an_invalid_ref(adapter: DoorAdapter) -> None:
    square = f"square:{DEFAULT_SESSION}:{DOOR_X}:{DOOR_Y}:0"
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(
            door_command(adapter.name, door_ref=square), door_world(a_door(ref=square))
        )
    assert caught.value.reason_code is ReasonCode.INVALID_REF


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_a_reference_from_another_session_is_an_invalid_ref(adapter: DoorAdapter) -> None:
    foreign = f"object:11111111-2222-3333-4444-555555555555:{DOOR_X}:{DOOR_Y}:0:2"
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(door_command(adapter.name, door_ref=foreign), door_world(a_door()))
    assert caught.value.reason_code is ReasonCode.INVALID_REF


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_an_argument_the_adapter_does_not_understand_is_refused(adapter: DoorAdapter) -> None:
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(door_command(adapter.name, force=True), door_world(a_door()))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_a_reach_wider_than_movements_own_is_refused(adapter: DoorAdapter) -> None:
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(door_command(adapter.name, radius=3.5), door_world(a_door()))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_a_world_without_the_nearby_tier_cannot_be_validated(adapter: DoorAdapter) -> None:
    without_tier = replace(a_world(), nearby=None)
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(door_command(adapter.name), without_tier)
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


# --------------------------------------------------------------------------
# the state gates that differ
# --------------------------------------------------------------------------


def test_an_observed_lock_refuses_the_open_before_the_walk() -> None:
    """The toggle would be swallowed at the far end; the observation already says so."""
    with pytest.raises(PreconditionFailed) as caught:
        DoorOpenAdapter().validate(
            door_command(ActionName.DOOR_OPEN), door_world(a_door(open=False, locked=True))
        )
    assert caught.value.reason_code is ReasonCode.DOOR_LOCKED


def test_an_unreadable_lock_does_not_refuse_the_open() -> None:
    """Only an observed ``True`` saves the round trip; ``None`` is the mod's call."""
    DoorOpenAdapter().validate(
        door_command(ActionName.DOOR_OPEN), door_world(a_door(open=False, locked=None))
    )


def test_a_door_already_open_is_not_refused_even_locked() -> None:
    """A lock holds a door closed; on an open door it forbids nothing.

    The mod answers this command as an unchanged success, so a pre-refusal here
    would report an error for a state the mod calls done.
    """
    DoorOpenAdapter().validate(
        door_command(ActionName.DOOR_OPEN), door_world(a_door(open=True, locked=True))
    )


def test_a_lock_never_blocks_the_close() -> None:
    DoorCloseAdapter().validate(
        door_command(ActionName.DOOR_CLOSE), door_world(a_door(open=True, locked=True))
    )


def test_a_door_observed_unlocked_still_passes_the_unlock() -> None:
    """The documented choice: "already unlocked" is the mod's unchanged success.

    Refusing it here would make ``door.unlock`` the only door action that
    reports "already done" as an error; instead it rides through and comes
    back succeeded with identical before/after lock readings.
    """
    DoorUnlockAdapter().validate(
        door_command(ActionName.DOOR_UNLOCK), door_world(a_door(locked=False))
    )


def test_an_unreadable_lock_passes_the_unlock_through_to_the_mod() -> None:
    """The mod refuses with its own precise reason; this side has no observed fact."""
    DoorUnlockAdapter().validate(
        door_command(ActionName.DOOR_UNLOCK), door_world(a_door(locked=None))
    )


# --------------------------------------------------------------------------
# verify: the postcondition is the after-observation
# --------------------------------------------------------------------------


def test_the_door_observed_open_afterwards_is_the_evidence() -> None:
    adapter = DoorOpenAdapter()
    before = door_world(a_door(open=False))
    command = prepare(adapter, door_command(ActionName.DOOR_OPEN), before)
    after = door_world(a_door(open=True, distance=1.0), seq=2, at_door=True)

    evidence = adapter.verify(command, before, after)

    assert evidence is not None
    assert evidence.kind == "door_state_changed"
    assert evidence.observation_seq == after.seq
    assert evidence.observed["door_ref"] == DOOR
    assert evidence.observed["open_before"] is False
    assert evidence.observed["open_after"] is True
    assert evidence.observed["walked"] is True
    assert evidence.observed["distance"] == 1.0


def test_a_door_already_open_verifies_with_identical_readings() -> None:
    """The unchanged success: before and after agree, and that is the proof."""
    adapter = DoorOpenAdapter()
    before = door_world(a_door(open=True))
    command = prepare(adapter, door_command(ActionName.DOOR_OPEN), before)
    after = door_world(a_door(open=True), seq=2)

    evidence = adapter.verify(command, before, after)

    assert evidence is not None
    assert evidence.observed["open_before"] is True
    assert evidence.observed["open_after"] is True
    assert evidence.observed["walked"] is False


def test_a_door_that_left_the_view_proves_nothing() -> None:
    """Out of radius is not open: the engine reports the failure, not a guess."""
    adapter = DoorOpenAdapter()
    before = door_world(a_door(open=False))
    command = prepare(adapter, door_command(ActionName.DOOR_OPEN), before)

    assert adapter.verify(command, before, door_world(None, seq=2)) is None


def test_a_door_still_closed_is_not_an_open_door() -> None:
    """The swallowed-toggle case: the call returned and the door did not move."""
    adapter = DoorOpenAdapter()
    before = door_world(a_door(open=False))
    command = prepare(adapter, door_command(ActionName.DOOR_OPEN), before)

    assert adapter.verify(command, before, door_world(a_door(open=False), seq=2)) is None


def test_an_unreadable_open_state_is_not_an_open_door() -> None:
    """Absent must never read as either answer, in verify as in validate."""
    adapter = DoorOpenAdapter()
    before = door_world(a_door(open=False))
    command = prepare(adapter, door_command(ActionName.DOOR_OPEN), before)

    assert adapter.verify(command, before, door_world(a_door(open=None), seq=2)) is None


def test_a_world_that_lost_its_nearby_tier_proves_nothing() -> None:
    adapter = DoorOpenAdapter()
    before = door_world(a_door(open=False))
    command = prepare(adapter, door_command(ActionName.DOOR_OPEN), before)

    assert adapter.verify(command, before, replace(a_world(seq=2), nearby=None)) is None


def test_the_close_demands_an_observed_false_not_a_missing_true() -> None:
    adapter = DoorCloseAdapter()
    before = door_world(a_door(open=True))
    command = prepare(adapter, door_command(ActionName.DOOR_CLOSE), before)

    assert adapter.verify(command, before, door_world(a_door(open=None), seq=2)) is None
    assert adapter.verify(command, before, door_world(a_door(open=True), seq=2)) is None

    evidence = adapter.verify(command, before, door_world(a_door(open=False), seq=2))
    assert evidence is not None
    assert evidence.kind == "door_state_changed"
    assert evidence.observed["open_before"] is True
    assert evidence.observed["open_after"] is False


def test_the_unlock_is_proven_by_the_lock_reading_false() -> None:
    adapter = DoorUnlockAdapter()
    before = door_world(a_door(locked=True))
    command = prepare(adapter, door_command(ActionName.DOOR_UNLOCK), before)
    after = door_world(a_door(locked=False), seq=2)

    evidence = adapter.verify(command, before, after)

    assert evidence is not None
    assert evidence.kind == "door_unlocked"
    assert evidence.observation_seq == after.seq
    assert evidence.observed == {"door_ref": DOOR, "locked_before": True, "locked_after": False}


def test_a_lock_still_on_or_unreadable_is_not_an_unlocked_door() -> None:
    adapter = DoorUnlockAdapter()
    before = door_world(a_door(locked=True))
    command = prepare(adapter, door_command(ActionName.DOOR_UNLOCK), before)

    assert adapter.verify(command, before, door_world(a_door(locked=True), seq=2)) is None
    assert adapter.verify(command, before, door_world(a_door(locked=None), seq=2)) is None
    assert adapter.verify(command, before, door_world(None, seq=2)) is None


def test_evidence_reports_an_unobserved_before_state_as_such() -> None:
    """A door that entered the view mid-action has no before-reading to fake."""
    adapter = DoorOpenAdapter()
    before = door_world(a_door(open=False))
    command = prepare(adapter, door_command(ActionName.DOOR_OPEN), before)
    blind_before = door_world(None)

    evidence = adapter.verify(command, blind_before, door_world(a_door(open=True), seq=2))

    assert evidence is not None
    assert evidence.observed["open_before"] is None
    assert evidence.observed["open_after"] is True


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------


def test_progress_is_the_fraction_of_the_reported_gap_that_closed() -> None:
    adapter = DoorOpenAdapter()
    before = door_world(a_door(open=False, distance=4.0))
    command = prepare(adapter, door_command(ActionName.DOOR_OPEN), before)

    halfway = door_world(a_door(open=False, distance=2.0), seq=2)
    assert adapter.progress(command, before, halfway) == pytest.approx(0.5)

    arrived = door_world(a_door(open=False, distance=1.0), seq=3)
    assert adapter.progress(command, arrived, arrived) == 1.0


def test_progress_has_no_opinion_without_the_door_in_both_snapshots() -> None:
    adapter = DoorOpenAdapter()
    before = door_world(a_door(open=False, distance=4.0))
    command = prepare(adapter, door_command(ActionName.DOOR_OPEN), before)

    assert adapter.progress(command, before, door_world(None, seq=2)) is None
    assert adapter.progress(command, replace(before, nearby=None), before) is None
