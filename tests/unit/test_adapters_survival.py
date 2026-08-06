"""``survival.rest`` and ``survival.sleep``.

Both actions look like waiting, and waiting is the easiest thing in the world to
report as done. So the tests are about what separates a rest from standing
around and a night from an afternoon: endurance that actually reached the target
it was given, and fatigue that fell *while the world clock moved*. Fatigue alone
falls whenever the character is idle, which is exactly why it is not enough.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.actions.adapters import RestAdapter, SleepAdapter
from pz_agent_core.capabilities.probes import SURVIVAL_REST, SURVIVAL_SLEEP
from pz_agent_core.protocol import (
    ActionName,
    ActionOwnership,
    ActionState,
    Command,
    DangerLevel,
    Observation,
    PlayerState,
    Position,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    make_action_state,
    make_game,
    make_observation,
    make_safety,
)
from tests.fixtures.adapter_worlds import HOME_X, HOME_Y, a_command, prepare, square_ref

BED = square_ref(HOME_X + 2, HOME_Y)
CHAIR = square_ref(HOME_X + 1, HOME_Y)


#: A sitting entry the mod authored, as the observer reports one.
SEATED = ActionState(
    ownership=ActionOwnership.MOD, busy=True, action_id="a1", type="ISSitOnChairAction"
)


def resting(
    *,
    endurance: float | None = 0.3,
    fatigue: float | None = 0.5,
    seq: int = 1,
    world_time: str | None = "1993-07-09T14:20:00",
    danger: DangerLevel = DangerLevel.NONE,
    action: ActionState | None = None,
) -> Observation:
    """A world where both actions' happy paths hold.

    ``endurance``/``fatigue`` of None means the mod did not report the stat at
    all — a different fact from reporting zero, and the one several of these
    tests turn on. The player is built here rather than through ``make_player``
    for exactly that reason: the shared builder always supplies both.
    """
    stats: dict[str, float | int | bool | None] = {"health": 1.0}
    if endurance is not None:
        stats["endurance"] = endurance
    if fatigue is not None:
        stats["fatigue"] = fatigue
    return make_observation(
        seq=seq,
        player=PlayerState(
            present=True,
            alive=True,
            position=Position(x=float(HOME_X), y=float(HOME_Y), z=0, direction="S"),
            stats=stats,
        ),
        game=make_game(world_time=world_time),
        safety=make_safety(danger_level=danger),
        action=action if action is not None else make_action_state(),
    )


def sitting(*, endurance: float | None = 0.3, seq: int = 2) -> Observation:
    """The mod's own queue entry says the character is sitting down."""
    return resting(endurance=endurance, seq=seq, action=SEATED)


def rest_command(**args: object) -> Command:
    return a_command(ActionName.SURVIVAL_REST, dict(args))


def sleep_command(**args: object) -> Command:
    args.setdefault("bed_ref", BED)
    return a_command(ActionName.SURVIVAL_SLEEP, dict(args))


# --------------------------------------------------------------------------
# survival.rest
# --------------------------------------------------------------------------


def test_endurance_reaching_the_target_is_the_evidence() -> None:
    adapter = RestAdapter()
    before = resting(endurance=0.3)
    command = prepare(adapter, rest_command(target_endurance=0.8), before)

    evidence = adapter.verify(command, before, resting(endurance=0.85, seq=2))

    assert evidence is not None
    assert evidence.kind == "endurance_recovered"
    assert evidence.observed["endurance_before"] == pytest.approx(0.3)
    assert evidence.observed["endurance_after"] == pytest.approx(0.85)


def test_endurance_short_of_the_target_is_not_a_finished_rest() -> None:
    adapter = RestAdapter()
    before = resting(endurance=0.3)
    command = prepare(adapter, rest_command(target_endurance=0.8), before)

    assert adapter.verify(command, before, resting(endurance=0.5, seq=2)) is None


def test_endurance_that_did_not_move_is_not_a_rest() -> None:
    adapter = RestAdapter()
    before = resting(endurance=0.95)
    command = prepare(adapter, rest_command(target_endurance=0.9), before)

    assert adapter.verify(command, before, resting(endurance=0.95, seq=2)) is None


def test_sitting_down_does_not_stand_in_for_endurance_that_can_be_read() -> None:
    """A posture is not the point of a rest when the stat is right there."""
    adapter = RestAdapter()
    before = resting(endurance=0.3)
    command = prepare(adapter, rest_command(target_endurance=0.9, seat_ref=CHAIR), before)

    assert adapter.verify(command, before, sitting(endurance=0.4)) is None


def test_an_unreadable_endurance_leaves_the_observed_posture_as_the_proof() -> None:
    adapter = RestAdapter()
    before = resting(endurance=None)
    command = prepare(adapter, rest_command(seat_ref=CHAIR), before)

    evidence = adapter.verify(command, before, sitting(endurance=None))

    assert evidence is not None
    assert evidence.kind == "rest_posture_taken"
    assert evidence.observed["seated"] is True


def test_an_unreadable_endurance_and_no_posture_asked_for_proves_nothing() -> None:
    adapter = RestAdapter()
    before = resting(endurance=None)
    command = prepare(adapter, rest_command(seat_ref=CHAIR), before)

    assert adapter.verify(command, before, resting(endurance=None, seq=2)) is None


def test_a_manual_sitting_action_is_not_this_command_resting() -> None:
    adapter = RestAdapter()
    before = resting(endurance=None)
    command = prepare(adapter, rest_command(seat_ref=CHAIR), before)

    theirs = resting(
        endurance=None,
        seq=2,
        action=ActionState(
            ownership=ActionOwnership.MANUAL, busy=True, action_id="a1", type="ISSitOnChairAction"
        ),
    )

    assert adapter.verify(command, before, theirs) is None


def test_resting_with_no_readable_endurance_and_no_seat_is_refused_up_front() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        RestAdapter().validate(rest_command(), resting(endurance=None))
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_a_target_already_met_is_refused_rather_than_reported_as_a_rest() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        RestAdapter().validate(rest_command(target_endurance=0.5), resting(endurance=0.6))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_target_outside_the_stat_range_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        RestAdapter().validate(rest_command(target_endurance=1.5), resting())
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_a_seat_reference_of_the_wrong_kind_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        RestAdapter().validate(
            rest_command(seat_ref=f"container:{DEFAULT_SESSION}:player-main"), resting()
        )
    assert caught.value.reason_code is ReasonCode.INVALID_REF


def test_progress_is_the_share_of_the_endurance_gap_closed() -> None:
    adapter = RestAdapter()
    before = resting(endurance=0.3)
    command = prepare(adapter, rest_command(target_endurance=0.9), before)

    assert adapter.progress(command, before, resting(endurance=0.6, seq=2)) == pytest.approx(0.5)


def test_resting_declares_the_capability_it_needs() -> None:
    assert RestAdapter().required_capability == SURVIVAL_REST
    assert RestAdapter().risk is RiskClass.P2


# --------------------------------------------------------------------------
# survival.sleep
# --------------------------------------------------------------------------


def test_fatigue_falling_across_a_night_of_world_time_is_the_evidence() -> None:
    adapter = SleepAdapter()
    before = resting(fatigue=0.8, world_time="1993-07-09T22:00:00")
    command = prepare(adapter, sleep_command(hours=8), before)

    morning = resting(fatigue=0.1, world_time="1993-07-10T06:00:00", seq=2)
    evidence = adapter.verify(command, before, morning)

    assert evidence is not None
    assert evidence.kind == "fatigue_fell_over_slept_hours"
    assert evidence.observed["elapsed_game_seconds"] == pytest.approx(8 * 3600)


def test_fatigue_falling_without_the_world_clock_moving_is_not_a_night() -> None:
    """Idling drops fatigue a little; that is a rest at best."""
    adapter = SleepAdapter()
    before = resting(fatigue=0.8, world_time="1993-07-09T22:00:00")
    command = prepare(adapter, sleep_command(hours=8), before)

    dozing = resting(fatigue=0.75, world_time="1993-07-09T22:10:00", seq=2)

    assert adapter.verify(command, before, dozing) is None


def test_the_world_clock_moving_without_fatigue_falling_is_not_a_night() -> None:
    adapter = SleepAdapter()
    before = resting(fatigue=0.8, world_time="1993-07-09T22:00:00")
    command = prepare(adapter, sleep_command(hours=8), before)

    awake = resting(fatigue=0.9, world_time="1993-07-10T06:00:00", seq=2)

    assert adapter.verify(command, before, awake) is None


def test_a_world_clock_that_went_backwards_is_unmeasurable_rather_than_zero() -> None:
    adapter = SleepAdapter()
    before = resting(fatigue=0.8, world_time="1993-07-09T22:00:00")
    command = prepare(adapter, sleep_command(hours=8), before)

    reloaded = resting(fatigue=0.1, world_time="1993-07-08T06:00:00", seq=2)

    assert adapter.verify(command, before, reloaded) is None


def test_an_unreadable_world_clock_afterwards_proves_no_night() -> None:
    adapter = SleepAdapter()
    before = resting(fatigue=0.8, world_time="1993-07-09T22:00:00")
    command = prepare(adapter, sleep_command(hours=8), before)

    blind = resting(fatigue=0.1, world_time=None, seq=2)

    assert adapter.verify(command, before, blind) is None


def test_an_unreadable_fatigue_afterwards_proves_no_night() -> None:
    adapter = SleepAdapter()
    before = resting(fatigue=0.8, world_time="1993-07-09T22:00:00")
    command = prepare(adapter, sleep_command(hours=8), before)

    blind = resting(fatigue=None, world_time="1993-07-10T06:00:00", seq=2)

    assert adapter.verify(command, before, blind) is None


def test_a_night_cut_short_still_counts_once_a_quarter_of_it_has_passed() -> None:
    adapter = SleepAdapter()
    before = resting(fatigue=0.8, world_time="1993-07-09T22:00:00")
    command = prepare(adapter, sleep_command(hours=8), before)

    woken = resting(fatigue=0.5, world_time="1993-07-10T00:10:00", seq=2)

    assert adapter.verify(command, before, woken) is not None


def test_sleeping_with_a_threat_on_the_board_is_refused() -> None:
    """A sleeping character cannot be woken by this mod."""
    with pytest.raises(PreconditionFailed) as caught:
        SleepAdapter().validate(sleep_command(), resting(danger=DangerLevel.LOW))
    assert caught.value.reason_code is ReasonCode.POLICY_DENIED


def test_sleeping_without_a_readable_fatigue_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        SleepAdapter().validate(sleep_command(), resting(fatigue=None))
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_sleeping_without_a_readable_world_clock_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        SleepAdapter().validate(sleep_command(), resting(world_time=None))
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_sleeping_nowhere_in_particular_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        SleepAdapter().validate(a_command(ActionName.SURVIVAL_SLEEP, {}), resting())
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_a_vehicle_seat_has_to_be_allowed_explicitly() -> None:
    SleepAdapter().validate(
        a_command(ActionName.SURVIVAL_SLEEP, {"allow_vehicle_seat": True}), resting()
    )


def test_more_hours_than_a_night_are_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        SleepAdapter().validate(sleep_command(hours=48), resting())
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_progress_is_the_share_of_the_night_the_clock_has_covered() -> None:
    adapter = SleepAdapter()
    before = resting(fatigue=0.8, world_time="1993-07-09T22:00:00")
    command = prepare(adapter, sleep_command(hours=8), before)

    midnight = resting(fatigue=0.6, world_time="1993-07-09T23:00:00", seq=2)

    assert adapter.progress(command, before, midnight) == pytest.approx(0.5)


def test_sleeping_is_never_taken_on_the_agents_own_initiative() -> None:
    """P4: nothing this mod can do wakes a sleeping character."""
    assert SleepAdapter().required_capability == SURVIVAL_SLEEP
    assert SleepAdapter().risk is RiskClass.P4
