"""The two adapters that need no game-specific API.

``action.wait`` is where "verified by observation" is easiest to get wrong:
real seconds always pass, so a wall-clock wait always "succeeds". These tests
pin it to the world clock instead.
"""

from __future__ import annotations

import uuid

import pytest

from pz_agent_core.actions import (
    ActionEngine,
    ActionRequest,
    AdapterRegistry,
    CancelAdapter,
    PreconditionFailed,
    WaitAdapter,
    register_builtins,
)
from pz_agent_core.actions.builtin import DEFAULT_WAIT_TIMEOUT_MS, MAX_WAIT_GAME_SECONDS
from pz_agent_core.protocol import (
    ActionName,
    ActionOwnership,
    ActionStatus,
    Command,
    CommandPolicy,
    JsonDict,
    Observation,
    ReasonCode,
)
from tests.fixtures import DEFAULT_SESSION, make_action_state, make_game, make_observation
from tests.fixtures.action_doubles import (
    DEFAULT_START_MS,
    FakeClock,
    FakeCommandSink,
    FakeObservationSource,
)

WORLD_START = "1993-07-09T14:20:00"


def a_command(action: ActionName, args: JsonDict) -> Command:
    return Command(
        session_id=DEFAULT_SESSION,
        seq=0,
        command_id=str(uuid.uuid4()),
        idempotency_key="k",
        issued_at_ms=1_700_000_000_000,
        lease_ms=15_000,
        action=action,
        args=args,
    )


def at_world_time(seq: int, world_time: str | None) -> Observation:
    return make_observation(seq=seq, game=make_game(world_time=world_time))


# --------------------------------------------------------------------------
# action.wait
# --------------------------------------------------------------------------


def test_wait_rejects_arguments_it_does_not_understand() -> None:
    adapter = WaitAdapter()
    command = a_command(ActionName.ACTION_WAIT, {"game_seconds": 10, "until": "dawn"})
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(command, at_world_time(1, WORLD_START))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "value",
    [None, "10", True, 0, -5, MAX_WAIT_GAME_SECONDS + 1],
)
def test_wait_rejects_an_unusable_duration(value: object) -> None:
    adapter = WaitAdapter()
    args: JsonDict = {} if value is None else {"game_seconds": value}
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(a_command(ActionName.ACTION_WAIT, args), at_world_time(1, WORLD_START))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_wait_refuses_when_the_world_clock_is_not_reported() -> None:
    """Without a world clock there is nothing to verify against, so it does not run."""
    adapter = WaitAdapter()
    command = a_command(ActionName.ACTION_WAIT, {"game_seconds": 30})
    with pytest.raises(PreconditionFailed) as caught:
        adapter.validate(command, at_world_time(1, None))
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE

    with pytest.raises(PreconditionFailed):
        adapter.validate(command, at_world_time(1, "not-a-timestamp"))


def test_wait_normalises_its_argument() -> None:
    adapter = WaitAdapter()
    command = a_command(ActionName.ACTION_WAIT, {"game_seconds": 30})
    assert adapter.build_args(command, at_world_time(1, WORLD_START)) == {"game_seconds": 30.0}


def test_wait_is_verified_by_game_time_not_wall_time() -> None:
    adapter = WaitAdapter()
    command = a_command(ActionName.ACTION_WAIT, {"game_seconds": 60})
    before = at_world_time(1, WORLD_START)

    assert adapter.verify(command, before, at_world_time(2, "1993-07-09T14:20:59")) is None

    evidence = adapter.verify(command, before, at_world_time(3, "1993-07-09T14:21:30"))
    assert evidence is not None
    assert evidence.kind == "world_time_advanced"
    assert evidence.observation_seq == 3
    assert evidence.observed["elapsed_game_seconds"] == 90.0


def test_wait_treats_a_backwards_world_clock_as_unmeasurable() -> None:
    """A reload rewinds the clock; that is not sixty seconds of waiting."""
    adapter = WaitAdapter()
    command = a_command(ActionName.ACTION_WAIT, {"game_seconds": 60})
    before = at_world_time(1, WORLD_START)
    after = at_world_time(2, "1993-07-09T14:00:00")

    assert adapter.verify(command, before, after) is None
    assert adapter.progress(command, before, after) is None


def test_wait_reports_bounded_progress() -> None:
    adapter = WaitAdapter()
    command = a_command(ActionName.ACTION_WAIT, {"game_seconds": 60})
    before = at_world_time(1, WORLD_START)

    assert adapter.progress(command, before, at_world_time(2, "1993-07-09T14:20:30")) == 0.5
    assert adapter.progress(command, before, at_world_time(3, "1993-07-09T14:25:00")) == 1.0
    assert adapter.progress(command, before, at_world_time(4, None)) is None


def test_wait_runs_end_to_end_through_the_engine() -> None:
    clock = FakeClock()
    sink = FakeCommandSink(clock)
    source = FakeObservationSource(clock)
    source.push(
        at_world_time(1, WORLD_START),
        at_world_time(2, "1993-07-09T14:20:05"),
        at_world_time(3, "1993-07-09T14:20:40"),
    )
    engine = ActionEngine(
        registry=register_builtins(AdapterRegistry()),
        sink=sink,
        observations=source,
        clock=clock,
    )

    result = engine.execute(
        ActionRequest(
            action=ActionName.ACTION_WAIT,
            session_id=DEFAULT_SESSION,
            idempotency_key="wait-1",
            args={"game_seconds": 30},
        )
    )

    assert result.status is ActionStatus.SUCCEEDED
    assert result.evidence["observed"]["elapsed_game_seconds"] == 40.0
    assert sink.last.args == {"game_seconds": 30.0}


def test_a_paused_game_never_satisfies_a_wait() -> None:
    """Wall time runs while the world stands still; the wait must not complete.

    The pause also suspends the action timeout (§ 4.14) and, with it, the stuck
    window — a paused game is not a stuck one — so the attempt ends on the poll
    ceiling instead, which is what keeps the loop bounded.
    """
    clock = FakeClock()
    sink = FakeCommandSink(clock)
    source = FakeObservationSource(clock)
    frozen = make_observation(seq=1, game=make_game(paused=True, world_time=WORLD_START))
    source.repeat(frozen)
    engine = ActionEngine(
        registry=register_builtins(AdapterRegistry()),
        sink=sink,
        observations=source,
        clock=clock,
        no_progress_ms=1_000,
    )

    result = engine.execute(
        ActionRequest(
            action=ActionName.ACTION_WAIT,
            session_id=DEFAULT_SESSION,
            idempotency_key="wait-2",
            args={"game_seconds": 30},
        )
    )

    assert result.status is not ActionStatus.SUCCEEDED
    assert result.reason_code is ReasonCode.ACTION_TIMEOUT
    assert clock.now - DEFAULT_START_MS > DEFAULT_WAIT_TIMEOUT_MS
    assert sink.cancelled


# --------------------------------------------------------------------------
# plan.cancel
# --------------------------------------------------------------------------


def test_cancel_rejects_a_malformed_target() -> None:
    adapter = CancelAdapter()
    observation = make_observation(seq=1)
    for args in ({"command_id": "not-a-uuid"}, {"command_id": 7}, {"everything": True}):
        with pytest.raises(PreconditionFailed) as caught:
            adapter.validate(a_command(ActionName.PLAN_CANCEL, dict(args)), observation)
        assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_cancel_accepts_an_empty_or_targeted_request() -> None:
    adapter = CancelAdapter()
    observation = make_observation(seq=1)
    target = str(uuid.uuid4())

    untargeted = a_command(ActionName.PLAN_CANCEL, {})
    adapter.validate(untargeted, observation)
    assert adapter.build_args(untargeted, observation) == {}

    targeted = a_command(ActionName.PLAN_CANCEL, {"command_id": target})
    adapter.validate(targeted, observation)
    assert adapter.build_args(targeted, observation) == {"command_id": target}


def test_cancel_is_unproven_while_a_mod_action_is_still_running() -> None:
    adapter = CancelAdapter()
    command = a_command(ActionName.PLAN_CANCEL, {})
    before = make_observation(seq=1)
    busy = make_observation(
        seq=2, action=make_action_state(ownership=ActionOwnership.MOD, busy=True)
    )
    assert adapter.verify(command, before, busy) is None


def test_cancel_is_unproven_while_ownership_is_ambiguous() -> None:
    """ "Cannot say whose entry this is" is not "the entry is gone"."""
    adapter = CancelAdapter()
    command = a_command(ActionName.PLAN_CANCEL, {})
    before = make_observation(seq=1)
    ambiguous = make_observation(
        seq=2, action=make_action_state(ownership=ActionOwnership.AMBIGUOUS, busy=True)
    )
    assert adapter.verify(command, before, ambiguous) is None


def test_cancel_is_satisfied_by_an_idle_queue() -> None:
    adapter = CancelAdapter()
    command = a_command(ActionName.PLAN_CANCEL, {})
    before = make_observation(seq=1)
    idle = make_observation(seq=5, action=make_action_state(ownership=ActionOwnership.NONE))

    evidence = adapter.verify(command, before, idle)
    assert evidence is not None
    assert evidence.kind == "action_queue_free_of_mod_entries"
    assert evidence.observed["ownership"] == ActionOwnership.NONE.value


def test_cancel_leaves_a_manual_action_alone() -> None:
    """§ 4.3: the postcondition is about mod-owned entries, never the player's."""
    adapter = CancelAdapter()
    command = a_command(ActionName.PLAN_CANCEL, {})
    before = make_observation(seq=1)
    manual = make_observation(
        seq=6,
        action=make_action_state(ownership=ActionOwnership.MANUAL, busy=True, action_id="manual-1"),
    )

    evidence = adapter.verify(command, before, manual)
    assert evidence is not None
    assert evidence.observed["busy"] is True
    assert evidence.observed["ownership"] == ActionOwnership.MANUAL.value


def test_a_targeted_cancel_ignores_a_different_mod_entry() -> None:
    adapter = CancelAdapter()
    target = str(uuid.uuid4())
    command = a_command(ActionName.PLAN_CANCEL, {"command_id": target})
    before = make_observation(seq=1)
    other = make_observation(
        seq=2,
        action=make_action_state(ownership=ActionOwnership.MOD, busy=True, action_id="other"),
    )
    same = make_observation(
        seq=3,
        action=make_action_state(ownership=ActionOwnership.MOD, busy=True, action_id=target),
    )

    assert adapter.verify(command, before, other) is not None
    assert adapter.verify(command, before, same) is None


def test_cancel_runs_end_to_end_while_the_session_is_disarmed() -> None:
    clock = FakeClock()
    sink = FakeCommandSink(clock)
    source = FakeObservationSource(clock)
    source.push(
        make_observation(seq=1, action=make_action_state(ownership=ActionOwnership.MOD, busy=True)),
        make_observation(seq=2, action=make_action_state(ownership=ActionOwnership.NONE)),
    )
    engine = ActionEngine(
        registry=register_builtins(AdapterRegistry()),
        sink=sink,
        observations=source,
        clock=clock,
    )

    result = engine.execute(
        ActionRequest(
            action=ActionName.PLAN_CANCEL,
            session_id=DEFAULT_SESSION,
            idempotency_key="cancel-1",
            policy=CommandPolicy(allow_interrupt=True, max_retries=0),
        )
    )

    assert result.status is ActionStatus.SUCCEEDED
    assert result.reason_code is ReasonCode.POSTCONDITION_MET
    assert len(sink.sent) == 1
