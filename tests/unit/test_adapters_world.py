"""``world.inspect``.

A read-only action has one way to lie and it is not the obvious one: there is no
stat to move, so the temptation is to call the command done because it came
back. These tests pin the opposite — a reading is proven by the observation
describing the squares that were asked about, and an observation that describes
none of them proves nothing at all.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.actions.adapters import WorldInspectAdapter
from pz_agent_core.actions.adapters.world import MAX_INSPECT_RADIUS
from pz_agent_core.protocol import (
    ActionName,
    Command,
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
    a_command,
    a_square,
    a_world,
    prepare,
    square_ref,
)

OTHER_SESSION = "11111111-2222-3333-4444-555555555555"


def looking(*squares: NearbyObject, seq: int = 1, no_nearby: bool = False) -> Observation:
    return a_world(seq=seq, objects=list(squares), no_nearby=no_nearby)


def inspect_command(**args: object) -> Command:
    return a_command(ActionName.WORLD_INSPECT, dict(args))


def home_block(radius: int = 1) -> list[NearbyObject]:
    return [
        a_square(HOME_X + dx, HOME_Y + dy)
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
    ]


# --------------------------------------------------------------------------
# the postcondition
# --------------------------------------------------------------------------


def test_the_described_squares_are_the_evidence() -> None:
    adapter = WorldInspectAdapter()
    before = looking(*home_block())
    command = prepare(adapter, inspect_command(), before)

    evidence = adapter.verify(command, before, looking(*home_block(), seq=2))

    assert evidence is not None
    assert evidence.kind == "squares_described"
    assert evidence.observed["squares_requested"] == 9
    assert evidence.observed["squares_described"] == 9
    assert evidence.observed["centre"] == {"x": HOME_X, "y": HOME_Y, "z": HOME_Z}
    described = {entry["ref"] for entry in evidence.observed["squares"]}
    assert square_ref(HOME_X, HOME_Y) in described


def test_a_partly_loaded_block_is_still_a_reading_and_says_how_much_was_missing() -> None:
    """A block at the edge of the loaded world always has holes in it."""
    adapter = WorldInspectAdapter()
    before = looking(*home_block())
    command = prepare(adapter, inspect_command(), before)

    evidence = adapter.verify(command, before, looking(a_square(HOME_X, HOME_Y), seq=2))

    assert evidence is not None
    assert evidence.observed["squares_described"] == 1
    assert evidence.observed["squares_not_reported"] == 8


def test_a_block_where_nothing_answered_is_not_a_reading() -> None:
    adapter = WorldInspectAdapter()
    before = looking(*home_block())
    command = prepare(adapter, inspect_command(), before)

    far_away = looking(a_square(HOME_X + 40, HOME_Y + 40), seq=2)

    assert adapter.verify(command, before, far_away) is None


def test_an_observation_with_no_surroundings_describes_nothing() -> None:
    """The whole difference between "empty room" and "unobserved" is this test."""
    adapter = WorldInspectAdapter()
    before = looking(*home_block())
    command = prepare(adapter, inspect_command(), before)

    assert adapter.verify(command, before, looking(seq=2, no_nearby=True)) is None


def test_squares_on_another_floor_do_not_answer_for_the_ones_asked_about() -> None:
    adapter = WorldInspectAdapter()
    before = looking(*home_block())
    command = prepare(adapter, inspect_command(), before)

    upstairs = looking(*[a_square(HOME_X + dx, HOME_Y, 1) for dx in (-1, 0, 1)], seq=2)

    assert adapter.verify(command, before, upstairs) is None


def test_a_named_centre_is_the_block_that_gets_verified() -> None:
    adapter = WorldInspectAdapter()
    elsewhere = square_ref(HOME_X + 5, HOME_Y)
    before = looking(a_square(HOME_X + 5, HOME_Y), a_square(HOME_X, HOME_Y))
    command = prepare(adapter, inspect_command(ref=elsewhere, radius=0), before)

    evidence = adapter.verify(command, before, looking(a_square(HOME_X + 5, HOME_Y), seq=2))

    assert evidence is not None
    assert evidence.observed["centre"] == {"x": HOME_X + 5, "y": HOME_Y, "z": HOME_Z}
    assert evidence.observed["squares_requested"] == 1


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_a_missing_nearby_tier_is_refused_before_anything_is_sent() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        WorldInspectAdapter().validate(inspect_command(), looking(no_nearby=True))
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_a_radius_past_the_ceiling_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        WorldInspectAdapter().validate(
            inspect_command(radius=MAX_INSPECT_RADIUS + 1), looking(*home_block())
        )
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_an_unknown_argument_is_refused_rather_than_dropped() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        WorldInspectAdapter().validate(inspect_command(depth=3), looking(*home_block()))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_a_centre_minted_by_another_session_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        WorldInspectAdapter().validate(
            inspect_command(ref=f"square:{OTHER_SESSION}:1200:3400:0"), looking(*home_block())
        )
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_a_reference_of_the_wrong_kind_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        WorldInspectAdapter().validate(
            inspect_command(ref=f"container:{DEFAULT_SESSION}:player-main"), looking(*home_block())
        )
    assert caught.value.reason_code is ReasonCode.INVALID_REF


# --------------------------------------------------------------------------
# what the mod receives
# --------------------------------------------------------------------------


def test_the_centre_the_mod_receives_is_the_square_the_character_stands_on() -> None:
    args = WorldInspectAdapter().build_args(inspect_command(), looking(*home_block()))

    assert args == {"ref": square_ref(HOME_X, HOME_Y), "radius": 1}


def test_looking_around_needs_no_capability_and_no_permission_tier() -> None:
    """It reads; it does not queue, move or change anything (§4.11)."""
    adapter = WorldInspectAdapter()

    assert adapter.required_capability is None
    assert adapter.risk is RiskClass.P0
