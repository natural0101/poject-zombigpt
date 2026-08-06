"""The reflex guard: every reaction, one at a time, with no planner in sight."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pz_agent_core.protocol import (
    ActionOwnership,
    DangerLevel,
    Observation,
    Position,
    Priority,
    ReasonCode,
)
from pz_agent_core.safety.reflex import (
    MAX_EVENTS,
    ReflexConfig,
    ReflexGuard,
    ReflexSignals,
    SafetyEvent,
    may_cancel_running_action,
)
from tests.fixtures import (
    make_action_state,
    make_game,
    make_observation,
    make_player,
    make_safety,
)
from tests.fixtures.safety_builders import NOW_MS, in_flight, make_nearby, make_zombie

NOW = NOW_MS
GUARD = ReflexGuard()


def _obs(**overrides: Any) -> Observation:
    base: dict[str, Any] = {"seq": 1, "timestamp_ms": NOW}
    base.update(overrides)
    return make_observation(**base)


def _signals(**overrides: Any) -> ReflexSignals:
    base: dict[str, Any] = {"now_ms": NOW}
    base.update(overrides)
    return ReflexSignals(**base)


def _codes(events: list[SafetyEvent]) -> list[ReasonCode]:
    return [event.reason_code for event in events]


def _one(events: list[SafetyEvent], code: ReasonCode) -> SafetyEvent:
    matching = [event for event in events if event.reason_code is code]
    assert len(matching) == 1, f"expected exactly one {code.value}, got {_codes(events)}"
    return matching[0]


# --------------------------------------------------------------------------
# the quiet case
# --------------------------------------------------------------------------


def test_a_calm_tick_produces_nothing() -> None:
    assert GUARD.evaluate(_obs(), _obs(seq=2)) == []


def test_the_guard_runs_without_signals_or_a_previous_observation() -> None:
    # No planner, no IO, no history: this is the REFLEX_ONLY path.
    assert GUARD.evaluate(None, _obs()) == []


# --------------------------------------------------------------------------
# user input always wins
# --------------------------------------------------------------------------


def test_a_movement_key_cancels_automation() -> None:
    current = _obs(seq=2, safety=make_safety(manual_takeover=True))
    event = _one(GUARD.evaluate(_obs(), current), ReasonCode.USER_TAKEOVER)

    assert event.priority is Priority.MANUAL_INPUT
    assert event.cancels_mod_owned_queue
    assert not event.forces_disarm


def test_a_manual_action_appearing_cancels_automation() -> None:
    current = _obs(
        seq=2, action=make_action_state(busy=True, ownership=ActionOwnership.MANUAL, type="Reading")
    )
    event = _one(GUARD.evaluate(_obs(), current), ReasonCode.USER_TAKEOVER)

    assert event.cancels_mod_owned_queue
    # The player's own action is never touched.
    assert not event.cancels_running_action


def test_an_ongoing_manual_action_reports_busy_rather_than_repeating_the_takeover() -> None:
    manual = _obs(seq=2, action=make_action_state(busy=True, ownership=ActionOwnership.MANUAL))
    events = GUARD.evaluate(manual, _obs(seq=3, action=manual.action))

    assert _codes(events) == [ReasonCode.PLAYER_BUSY_MANUAL_ACTION]
    assert not events[0].cancels_mod_owned_queue
    assert not events[0].cancels_running_action


def test_an_ambiguous_queue_is_treated_as_the_players() -> None:
    ambiguous = make_action_state(busy=True, ownership=ActionOwnership.AMBIGUOUS)
    events = GUARD.evaluate(_obs(), _obs(seq=2, action=ambiguous))

    assert not may_cancel_running_action(ambiguous)
    assert all(not event.cancels_running_action for event in events)


def test_a_mod_owned_action_may_be_cancelled() -> None:
    mine = make_action_state(busy=True, ownership=ActionOwnership.MOD, type="consume.eat")
    assert may_cancel_running_action(mine)

    current = _obs(seq=2, action=mine, safety=make_safety(manual_takeover=True))
    assert _one(GUARD.evaluate(_obs(), current), ReasonCode.USER_TAKEOVER).cancels_running_action


# --------------------------------------------------------------------------
# panic stop
# --------------------------------------------------------------------------


def test_the_panic_hotkey_disarms_and_clears_the_mod_queue() -> None:
    events = GUARD.evaluate(
        _obs(), _obs(seq=2), _signals(panic_requested=True, in_flight=(in_flight("c1"),))
    )
    event = _one(events, ReasonCode.PANIC_STOP)

    assert event.priority is Priority.PANIC_STOP
    assert event.forces_disarm
    assert event.cancels_mod_owned_queue
    assert event.command_ids == ("c1",)


def test_a_panic_stop_still_will_not_touch_an_action_the_mod_does_not_own() -> None:
    current = _obs(seq=2, action=make_action_state(busy=True, ownership=ActionOwnership.MANUAL))
    event = _one(
        GUARD.evaluate(_obs(), current, _signals(panic_requested=True)), ReasonCode.PANIC_STOP
    )

    assert event.forces_disarm
    assert event.cancels_mod_owned_queue
    assert not event.cancels_running_action


def test_a_panic_stop_sorts_above_everything_else() -> None:
    current = _obs(
        seq=2,
        safety=make_safety(manual_takeover=True),
        nearby=make_nearby(make_zombie("z1", 1.0, chasing=True)),
    )
    events = GUARD.evaluate(_obs(), current, _signals(panic_requested=True))

    assert events[0].reason_code is ReasonCode.PANIC_STOP
    assert [e.priority for e in events] == sorted(e.priority for e in events)


# --------------------------------------------------------------------------
# threat
# --------------------------------------------------------------------------


def _eating(**overrides: Any) -> Observation:
    return _obs(
        action=make_action_state(busy=True, ownership=ActionOwnership.MOD, type="consume.eat"),
        **overrides,
    )


def test_a_threat_crossing_the_threshold_interrupts_eating() -> None:
    calm = _eating(nearby=make_nearby(make_zombie("z1", 30.0)))
    threatened = _eating(seq=2, nearby=make_nearby(make_zombie("z1", 10.0, chasing=True)))

    assert GUARD.evaluate(None, calm) == []
    event = _one(GUARD.evaluate(calm, threatened), ReasonCode.THREAT_INTERRUPTED)
    assert event.priority is Priority.LETHAL_THREAT
    assert event.cancels_running_action


def test_a_threat_below_the_threshold_does_not_interrupt_eating() -> None:
    quiet = _eating(seq=2, nearby=make_nearby(make_zombie("z1", 5.0)))
    assert GUARD.evaluate(_eating(), quiet) == []


def test_an_unnoticed_zombie_is_not_an_interrupt_but_a_distant_chaser_is() -> None:
    unaware = _eating(seq=2, nearby=make_nearby(make_zombie("z1", 3.0)))
    chaser = _eating(seq=2, nearby=make_nearby(make_zombie("z2", 12.0, chasing=True)))

    assert GUARD.evaluate(_eating(), unaware) == []
    assert _codes(GUARD.evaluate(_eating(), chaser)) == [ReasonCode.THREAT_INTERRUPTED]


def test_a_critical_threat_interrupts_even_when_nothing_is_running() -> None:
    idle = _obs(seq=2, nearby=make_nearby(make_zombie("z1", 1.0, chasing=True)))
    event = _one(GUARD.evaluate(_obs(), idle), ReasonCode.THREAT_INTERRUPTED)

    assert event.cancels_mod_owned_queue
    assert not event.cancels_running_action


def test_a_threat_does_not_interrupt_an_action_the_player_started() -> None:
    theirs = _obs(
        seq=2,
        action=make_action_state(busy=True, ownership=ActionOwnership.MANUAL, type="consume.eat"),
        nearby=make_nearby(make_zombie("z1", 10.0, chasing=True)),
    )
    assert ReasonCode.THREAT_INTERRUPTED not in _codes(GUARD.evaluate(_obs(), theirs))


def test_without_world_data_the_guard_uses_the_level_the_game_reported() -> None:
    blind = _obs(seq=2, nearby=None, safety=make_safety(danger_level=DangerLevel.CRITICAL))
    event = _one(GUARD.evaluate(_obs(), blind), ReasonCode.THREAT_INTERRUPTED)

    assert "reported by the game" in event.message


def test_the_reported_level_can_only_raise_the_computed_one() -> None:
    # The mod knows things the nearby view does not (fire, a horde off-radius).
    current = _eating(
        seq=2, nearby=make_nearby(), safety=make_safety(danger_level=DangerLevel.HIGH)
    )
    assert _codes(GUARD.evaluate(_eating(), current)) == [ReasonCode.THREAT_INTERRUPTED]


# --------------------------------------------------------------------------
# liveness and lifecycle
# --------------------------------------------------------------------------


def test_a_lost_sidecar_heartbeat_stops_new_work_without_disarming() -> None:
    events = GUARD.evaluate(_obs(), _obs(seq=2), _signals(sidecar_alive=False))
    event = _one(events, ReasonCode.STALE_SESSION)

    assert not event.forces_disarm
    assert not event.cancels_mod_owned_queue
    assert events, "a non-empty result is what tells the caller to start nothing new"


def test_the_mods_own_staleness_flag_has_the_same_effect() -> None:
    current = _obs(seq=2, safety=make_safety(sidecar_stale=True))
    assert ReasonCode.STALE_SESSION in _codes(GUARD.evaluate(_obs(), current))


def test_a_lost_game_heartbeat_closes_in_flight_actions() -> None:
    signals = _signals(game_alive=False, in_flight=(in_flight("c1"), in_flight("c2")))
    event = _one(GUARD.evaluate(_obs(), _obs(seq=2), signals), ReasonCode.GAME_DISCONNECTED)

    assert event.priority is Priority.PANIC_STOP
    assert event.forces_disarm
    assert event.command_ids == ("c1", "c2")
    assert "lost" in event.message


def test_a_dead_character_terminates_the_session() -> None:
    dead = _obs(seq=2, player=make_player(alive=False))
    event = _one(GUARD.evaluate(_obs(), dead), ReasonCode.SESSION_TERMINATED)

    assert event.priority is Priority.PANIC_STOP
    assert event.forces_disarm


def test_a_dead_character_is_caught_without_a_previous_observation() -> None:
    dead = _obs(player=make_player(alive=False))
    assert ReasonCode.SESSION_TERMINATED in _codes(GUARD.evaluate(None, dead))


def test_an_absent_character_terminates_the_session_too() -> None:
    gone = _obs(seq=2, player=make_player(present=False))
    assert ReasonCode.SESSION_TERMINATED in _codes(GUARD.evaluate(_obs(), gone))


def test_a_changed_save_invalidates_every_reference() -> None:
    current = _obs(seq=2, game=make_game(save_id="save-other"))
    event = _one(GUARD.evaluate(_obs(), current), ReasonCode.SAVE_CHANGED)

    assert event.forces_disarm
    assert "invalid" in event.message


def test_a_changed_session_invalidates_every_reference() -> None:
    current = _obs(seq=2, session_id=str(uuid.UUID(int=0xBEEF)))
    event = _one(GUARD.evaluate(_obs(), current), ReasonCode.STALE_SESSION)

    assert event.forces_disarm


def test_a_save_change_needs_a_previous_observation_to_be_visible() -> None:
    assert GUARD.evaluate(None, _obs(game=make_game(save_id="save-other"))) == []


def test_a_replaced_session_still_disarms_when_the_link_is_also_stale() -> None:
    # Both conditions speak through STALE_SESSION. Collapsing them must not lose
    # the disarm the replacement demanded, nor the commands it named.
    current = _obs(
        seq=2,
        session_id=str(uuid.UUID(int=0xBEEF)),
        safety=make_safety(sidecar_stale=True),
    )
    signals = _signals(sidecar_alive=False, in_flight=(in_flight("c1"),))
    event = _one(GUARD.evaluate(_obs(), current, signals), ReasonCode.STALE_SESSION)

    assert event.forces_disarm
    assert event.command_ids == ("c1",)
    assert "session was replaced" in event.message


def test_a_stale_link_alone_still_does_not_disarm() -> None:
    # The guard against over-merging: with no session change there is nothing to
    # union in, so the weaker event keeps its weaker authority.
    current = _obs(seq=2, safety=make_safety(sidecar_stale=True))
    event = _one(
        GUARD.evaluate(_obs(), current, _signals(in_flight=(in_flight("c1"),))),
        ReasonCode.STALE_SESSION,
    )

    assert not event.forces_disarm
    assert event.command_ids == ()


def test_a_save_change_and_a_stale_link_are_reported_separately() -> None:
    current = _obs(
        seq=2, game=make_game(save_id="save-other"), safety=make_safety(sidecar_stale=True)
    )
    events = GUARD.evaluate(_obs(), current)

    assert set(_codes(events)) == {ReasonCode.SAVE_CHANGED, ReasonCode.STALE_SESSION}
    assert _one(events, ReasonCode.SAVE_CHANGED).forces_disarm
    assert not _one(events, ReasonCode.STALE_SESSION).forces_disarm


# --------------------------------------------------------------------------
# command faults
# --------------------------------------------------------------------------


def test_an_expired_lease_is_rejected() -> None:
    signals = _signals(in_flight=(in_flight("c1", deadline_ms=NOW - 1),))
    event = _one(GUARD.evaluate(_obs(), _obs(seq=2), signals), ReasonCode.LEASE_EXPIRED)

    assert event.command_ids == ("c1",)
    assert event.priority is Priority.USER_COMMAND


def test_a_lease_expiring_exactly_now_is_still_valid() -> None:
    signals = _signals(in_flight=(in_flight("c1", deadline_ms=NOW, last_progress_ms=NOW),))
    assert GUARD.evaluate(_obs(), _obs(seq=2), signals) == []


def test_a_stalled_action_is_cancelled_as_no_progress() -> None:
    stalled = in_flight("c1", last_progress_ms=NOW - 60_000)
    events = GUARD.evaluate(_obs(), _obs(seq=2), _signals(in_flight=(stalled,)))
    event = _one(events, ReasonCode.NO_PROGRESS)

    assert event.command_ids == ("c1",)
    assert event.cancels_mod_owned_queue


def test_a_stalled_move_that_did_not_move_is_reported_as_stuck() -> None:
    stalled = in_flight(
        "c1", action="movement.move_to", last_progress_ms=NOW - 60_000, moves_character=True
    )
    events = GUARD.evaluate(_obs(), _obs(seq=2), _signals(in_flight=(stalled,)))
    event = _one(events, ReasonCode.PATH_STUCK)

    assert event.command_ids == ("c1",)
    assert ReasonCode.NO_PROGRESS not in _codes(events)


def test_a_stalled_move_that_is_still_moving_is_only_no_progress() -> None:
    moved = _obs(
        seq=2, player=make_player(position=Position(x=1210.0, y=3400.0, z=0, direction="S"))
    )
    stalled = in_flight(
        "c1", action="movement.move_to", last_progress_ms=NOW - 60_000, moves_character=True
    )
    events = GUARD.evaluate(_obs(), moved, _signals(in_flight=(stalled,)))

    assert _codes(events) == [ReasonCode.NO_PROGRESS]


def test_a_stall_with_no_previous_observation_is_not_diagnosed_as_stuck() -> None:
    # "Stuck" compares two positions. With only one observation there is no
    # second position, so the guard reports what it saw, not what it guessed.
    stalled = in_flight(
        "c1", action="movement.move_to", last_progress_ms=NOW - 60_000, moves_character=True
    )
    events = GUARD.evaluate(None, _obs(), _signals(in_flight=(stalled,)))

    assert _codes(events) == [ReasonCode.NO_PROGRESS]
    assert events[0].command_ids == ("c1",)


def test_a_progressing_action_is_left_alone() -> None:
    healthy = in_flight("c1", last_progress_ms=NOW - 1_000)
    assert GUARD.evaluate(_obs(), _obs(seq=2), _signals(in_flight=(healthy,))) == []


def test_an_expired_command_is_not_also_reported_as_stalled() -> None:
    both = in_flight("c1", deadline_ms=NOW - 1, last_progress_ms=NOW - 600_000)
    events = GUARD.evaluate(_obs(), _obs(seq=2), _signals(in_flight=(both,)))

    assert _codes(events) == [ReasonCode.LEASE_EXPIRED]


# --------------------------------------------------------------------------
# shape of the output
# --------------------------------------------------------------------------


def test_events_are_ordered_by_priority_and_bounded() -> None:
    current = _obs(
        seq=2,
        session_id=str(uuid.UUID(int=0xBEEF)),
        player=make_player(alive=False),
        safety=make_safety(manual_takeover=True, sidecar_stale=True),
        nearby=make_nearby(make_zombie("z1", 1.0, chasing=True)),
    )
    signals = _signals(
        panic_requested=True,
        game_alive=False,
        in_flight=(in_flight("c1", deadline_ms=NOW - 1), in_flight("c2", last_progress_ms=0)),
    )
    events = GUARD.evaluate(_obs(), current, signals)

    assert [e.priority for e in events] == sorted(e.priority for e in events)
    assert len(events) <= MAX_EVENTS
    assert len(_codes(events)) == len(set(_codes(events)))


def test_every_message_is_safe_to_speak_aloud() -> None:
    current = _obs(
        seq=2,
        game=make_game(save_id="C:\\Users\\ivan\\Zomboid"),
        player=make_player(alive=False),
        nearby=make_nearby(make_zombie("z1", 1.0, chasing=True)),
    )
    events = GUARD.evaluate(_obs(), current, _signals(panic_requested=True, game_alive=False))

    assert events
    for event in events:
        assert event.message.strip()
        assert "ivan" not in event.message
        assert ":\\" not in event.message
        assert "zombie:" not in event.message
        assert "item:" not in event.message


def test_a_blank_message_is_refused() -> None:
    with pytest.raises(ValueError, match="spoken aloud"):
        SafetyEvent(priority=Priority.PANIC_STOP, reason_code=ReasonCode.PANIC_STOP, message="   ")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stall_ms": 0},
        {"max_events": 0},
        {"max_events": MAX_EVENTS + 1},
        {"interrupt_at": DangerLevel.CRITICAL, "flee_at": DangerLevel.MEDIUM},
    ],
)
def test_an_incoherent_config_is_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="must"):
        ReflexConfig(**kwargs)
