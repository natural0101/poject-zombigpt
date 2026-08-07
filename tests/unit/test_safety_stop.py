"""The stop lever, tested under the failures it exists for (E12-M03-T007/T008).

A stop that only works while the rest of the system does is not a stop. The two
claims here are the ones a user is entitled to make about the panic key:

* **It is honoured with nothing else working.** :meth:`ReflexGuard.evaluate` is
  a pure function — no core loop, no planner, no queue read, no file — so a
  panic request produces its event with a dead game, a stale link, no previous
  observation and no world data at all. The engine half is the other direction:
  while the latch is down nothing mutating reaches the mod, and the three
  always-allowed commands still do, because an agent that cannot be stopped by
  the mechanism meant to stop it is the failure this task is about.
* **It clears only what the mod queued.** §8.12 and AGENTS.md: under any
  uncertainty the character stays under manual control. ``ambiguous`` counts as
  the player's, and so does ``none`` — the guard may cancel the *running* action
  only on positive proof that the mod owns it, and the merge in
  :func:`~pz_agent_core.safety.reflex._first_per_reason` may not widen that
  even when four rules fire on one tick.

What is *not* asserted here is the game-side half: cancelling a queue entry
happens in ``PZAgent.Safety.applyStop``, is covered by ``tests/lua/test_safety
.lua``, and refuses to touch a queue holding anything foreign. This file is
about the decisions this process makes.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from pz_agent_core.actions import ActionEngine, ActionRequest, AdapterRegistry
from pz_agent_core.protocol import (
    ALWAYS_ALLOWED_ACTIONS,
    ActionName,
    ActionOwnership,
    ActionStatus,
    CommandPolicy,
    DangerLevel,
    Observation,
    Priority,
    ReasonCode,
)
from pz_agent_core.safety.reflex import (
    ReflexGuard,
    ReflexSignals,
    SafetyEvent,
    may_cancel_running_action,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    make_action_state,
    make_observation,
    make_player,
    make_safety,
)
from tests.fixtures.action_doubles import (
    FakeClock,
    FakeCommandSink,
    FakeObservationSource,
    StubAdapter,
)
from tests.fixtures.safety_builders import NOW_MS, in_flight, make_nearby, make_zombie

GUARD: Final = ReflexGuard()

#: Every mutating action must be refused while the latch is down; this one
#: stands for all of them because the refusal is in ``_session_abort``, which
#: runs before anything action-specific.
MUTATING: Final = ActionName.MOVEMENT_MOVE_TO


def _obs(**overrides: Any) -> Observation:
    base: dict[str, Any] = {"seq": 1, "timestamp_ms": NOW_MS}
    base.update(overrides)
    return make_observation(**base)


def _panic(**overrides: Any) -> ReflexSignals:
    base: dict[str, Any] = {"now_ms": NOW_MS, "panic_requested": True}
    base.update(overrides)
    return ReflexSignals(**base)


def _one(events: list[SafetyEvent], code: ReasonCode) -> SafetyEvent:
    matching = [event for event in events if event.reason_code is code]
    assert len(matching) == 1, (
        f"expected exactly one {code.value}, got {[e.reason_code.value for e in events]}"
    )
    return matching[0]


# ---------------------------------------------------------------------------
# no core, no game, no queue, no link
# ---------------------------------------------------------------------------

#: One degraded world each, named for the subsystem that is gone. The panic
#: verdict must be identical in all of them.
DEGRADED: Final[tuple[tuple[str, dict[str, Any], dict[str, Any]], ...]] = (
    # Nothing else is wrong: the baseline the rest are compared against.
    ("everything_up", {}, {}),
    # No game: the mod's heartbeat stopped and no world data came with the tick.
    ("no_game", {"nearby": None}, {"game_alive": False}),
    # No link: the mod says it has stopped seeing this sidecar.
    ("no_link", {"safety": make_safety(sidecar_stale=True)}, {"sidecar_alive": False}),
    # No queue: nothing is in flight and nothing is running, so there is
    # nothing for the stop to cancel — it must still disarm.
    ("no_queue", {"action": make_action_state()}, {"in_flight": ()}),
    # No character: the session is over, which outranks nothing here.
    ("no_character", {"player": make_player(alive=False)}, {}),
    # A world on fire, so the threat rules fire on the same tick.
    (
        "under_attack",
        {
            "safety": make_safety(danger_level=DangerLevel.CRITICAL),
            "nearby": make_nearby(make_zombie("z1", 1.0, chasing=True)),
        },
        {},
    ),
)


@pytest.mark.parametrize(
    ("world", "signals"),
    [(world, signals) for _, world, signals in DEGRADED],
    ids=[name for name, _, _ in DEGRADED],
)
def test_a_panic_stop_is_honoured_whatever_else_has_failed(
    world: dict[str, Any], signals: dict[str, Any]
) -> None:
    """The guard needs no peer, no history and no world to obey a stop."""
    events = GUARD.evaluate(None, _obs(**world), _panic(**signals))

    event = _one(events, ReasonCode.PANIC_STOP)
    assert event.priority is Priority.PANIC_STOP
    assert event.forces_disarm, "a stop that does not disarm leaves the agent armed"
    assert event.cancels_mod_owned_queue, "a stop must clear what the mod queued"
    assert events[0] is event, (
        "the stop must be reported first; it is what the caller acts on before "
        f"anything else, and it came behind {events[0].reason_code.value}"
    )


def test_the_stop_names_the_commands_it_closes_even_with_the_game_gone() -> None:
    """In-flight work is closed by id, so nothing is left believed-running."""
    events = GUARD.evaluate(
        None,
        _obs(),
        _panic(game_alive=False, in_flight=(in_flight("c1"), in_flight("c2"))),
    )

    assert _one(events, ReasonCode.PANIC_STOP).command_ids == ("c1", "c2")


def test_the_stop_is_a_pure_decision_and_holds_without_signals_or_history() -> None:
    """No signals object at all means no panic; the flag is never assumed.

    The complement of the tests above, and the reason they can fail: a guard
    that returned a PANIC_STOP unconditionally would satisfy every assertion in
    this file up to here.
    """
    assert GUARD.evaluate(None, _obs()) == []
    assert GUARD.evaluate(_obs(), _obs(seq=2)) == []


# ---------------------------------------------------------------------------
# the engine while the latch is down
# ---------------------------------------------------------------------------


def _engine(*, panic: bool, action: ActionName = MUTATING) -> tuple[ActionEngine, FakeCommandSink]:
    """The production engine over doubles, with the stop lever in one position."""
    clock = FakeClock()
    sink = FakeCommandSink(clock)
    source = FakeObservationSource(clock)
    source.repeat(_obs())
    registry = AdapterRegistry()
    registry.register(StubAdapter(name=action, verify_after=1))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        panic_stop=lambda: panic,
        capability_check=lambda _: True,
    )
    return engine, sink


def _request(action: ActionName) -> ActionRequest:
    return ActionRequest(
        action=action,
        session_id=DEFAULT_SESSION,
        idempotency_key=f"{action.value}-1",
        args={"target": {"x": 1, "y": 2, "z": 0}},
        policy=CommandPolicy(allow_interrupt=True, max_retries=0),
    )


def test_nothing_mutating_reaches_the_mod_while_the_latch_is_down() -> None:
    engine, sink = _engine(panic=True)

    result = engine.execute(_request(MUTATING))

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code is ReasonCode.PANIC_STOP
    assert sink.sent == [], "a latched stop must reach the mod before the command does"


#: Sorted so the parametrisation is stable whatever order the frozenset yields.
STOPPING_ACTIONS: Final = tuple(sorted(ALWAYS_ALLOWED_ACTIONS, key=lambda name: name.value))


@pytest.mark.parametrize(
    "action", STOPPING_ACTIONS, ids=[action.value for action in STOPPING_ACTIONS]
)
def test_stopping_disarming_and_cancelling_still_work_while_the_latch_is_down(
    action: ActionName,
) -> None:
    """The lever must not disable the commands that carry the stop out."""
    engine, sink = _engine(panic=True, action=action)

    result = engine.execute(_request(action))

    assert result.reason_code is not ReasonCode.PANIC_STOP
    assert [command.action for command in sink.sent] == [action]


def test_the_same_command_runs_when_the_lever_is_up() -> None:
    """So the refusal above is the lever's doing and not the harness's."""
    engine, sink = _engine(panic=False)

    result = engine.execute(_request(MUTATING))

    assert result.reason_code is not ReasonCode.PANIC_STOP
    assert [command.action for command in sink.sent] == [MUTATING]


# ---------------------------------------------------------------------------
# only the mod's own entries (E12-M03-T008)
# ---------------------------------------------------------------------------

#: Every (ownership, busy) pair, with the answer written out here rather than
#: derived from the production rule. Only a busy action the mod positively owns
#: may be cancelled; ``ambiguous`` and ``none`` are the player's.
OWNERSHIP_CASES: Final[tuple[tuple[ActionOwnership, bool, bool], ...]] = (
    (ActionOwnership.MOD, True, True),
    (ActionOwnership.MOD, False, False),
    (ActionOwnership.MANUAL, True, False),
    (ActionOwnership.MANUAL, False, False),
    (ActionOwnership.AMBIGUOUS, True, False),
    (ActionOwnership.AMBIGUOUS, False, False),
    (ActionOwnership.NONE, True, False),
    (ActionOwnership.NONE, False, False),
)


@pytest.mark.parametrize(
    ("ownership", "busy", "may_cancel"),
    OWNERSHIP_CASES,
    ids=[f"{o.value}-{'busy' if b else 'idle'}" for o, b, _ in OWNERSHIP_CASES],
)
def test_a_stop_cancels_the_running_action_only_on_proof_that_it_is_ours(
    ownership: ActionOwnership, busy: bool, may_cancel: bool
) -> None:
    action = make_action_state(ownership=ownership, busy=busy, type="consume.eat")
    current = _obs(action=action)

    assert may_cancel_running_action(action) is may_cancel
    event = _one(GUARD.evaluate(_obs(), current, _panic()), ReasonCode.PANIC_STOP)
    assert event.cancels_running_action is may_cancel
    assert event.cancels_mod_owned_queue, (
        "clearing the mod's own queue entries never depends on who owns the "
        "action currently running"
    )


def test_four_rules_firing_at_once_cannot_widen_a_cancel_onto_the_players_action() -> None:
    """The union in ``_first_per_reason`` merges authority, never ownership.

    Panic, a takeover, a critical threat and a stalled command all fire on this
    tick, and the character is mid-way through an action nobody can prove is
    the mod's. Not one of the returned events may claim the right to cancel it.
    """
    current = _obs(
        seq=2,
        action=make_action_state(
            ownership=ActionOwnership.AMBIGUOUS, busy=True, type="consume.eat"
        ),
        safety=make_safety(manual_takeover=True, danger_level=DangerLevel.CRITICAL),
        nearby=make_nearby(make_zombie("z1", 1.0, chasing=True)),
    )
    signals = _panic(
        in_flight=(in_flight("c1", last_progress_ms=NOW_MS - 60_000),),
        sidecar_alive=False,
    )

    events = GUARD.evaluate(_obs(), current, signals)

    assert len(events) > 1, "this test is only meaningful when several rules fire"
    offenders = [e.reason_code.value for e in events if e.cancels_running_action]
    assert offenders == [], f"these events would cancel the player's action: {offenders}"


def test_the_stop_does_cancel_a_running_action_the_mod_owns() -> None:
    """The other half, so the rule above is not "never cancel anything"."""
    current = _obs(
        action=make_action_state(ownership=ActionOwnership.MOD, busy=True, type="consume.eat")
    )

    event = _one(GUARD.evaluate(_obs(), current, _panic()), ReasonCode.PANIC_STOP)

    assert event.cancels_running_action
