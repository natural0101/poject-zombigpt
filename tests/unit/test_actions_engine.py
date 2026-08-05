"""The action lifecycle, including every way it is allowed to end badly.

The happy path is one test. The rest of the file is the honesty guarantee:
what the engine reports when the mod is optimistic, when the world stops
moving, when the player takes the controls back, and when the save underneath
it changes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

import pytest

from pz_agent_core.actions import (
    ActionEngine,
    ActionRequest,
    AdapterRegistry,
    PreconditionFailed,
    deny_capability,
    no_panic,
)
from pz_agent_core.protocol import (
    RETRYABLE_CODES,
    ActionName,
    ActionOwnership,
    ActionStatus,
    CommandPolicy,
    DangerLevel,
    Observation,
    ReasonCode,
)
from pz_agent_core.protocol.messages import MAX_IDEMPOTENCY_KEY_LEN
from tests.fixtures import (
    DEFAULT_SESSION,
    make_action_state,
    make_game,
    make_observation,
    make_player,
    make_safety,
)
from tests.fixtures.action_doubles import (
    DEFAULT_START_MS,
    AckPlan,
    FakeClock,
    FakeCommandSink,
    FakeObservationSource,
    StubAdapter,
)


class Lever:
    """A panic switch that engages after *engage_after* reads."""

    def __init__(self, engage_after: int) -> None:
        self.engage_after = engage_after
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self.engage_after


@dataclass
class Harness:
    engine: ActionEngine
    sink: FakeCommandSink
    source: FakeObservationSource
    adapter: StubAdapter
    clock: FakeClock


def make_harness(
    adapter: StubAdapter | None = None,
    *,
    acks: Sequence[AckPlan] = (),
    rejections: Sequence[ReasonCode] = (),
    latest: Observation | None = None,
    registered: bool = True,
    panic_stop: Callable[[], bool] | None = None,
    capability_check: Callable[[str], bool] | None = None,
    no_progress_ms: int = 5_000,
    post_ack_grace_ms: int = 500,
    observation_timeout_ms: int = 2_000,
) -> Harness:
    adapter = adapter or StubAdapter()
    clock = FakeClock()
    sink = FakeCommandSink(clock, acks=acks, rejections=rejections)
    source = FakeObservationSource(clock, latest=latest)
    registry = AdapterRegistry()
    if registered:
        registry.register(adapter)
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        panic_stop=no_panic if panic_stop is None else panic_stop,
        capability_check=deny_capability if capability_check is None else capability_check,
        no_progress_ms=no_progress_ms,
        post_ack_grace_ms=post_ack_grace_ms,
        observation_timeout_ms=observation_timeout_ms,
    )
    return Harness(engine=engine, sink=sink, source=source, adapter=adapter, clock=clock)


def a_request(
    action: ActionName = ActionName.MOVEMENT_MOVE_TO,
    *,
    key: str = "move-1",
    retries: int = 0,
    allow_interrupt: bool = True,
) -> ActionRequest:
    return ActionRequest(
        action=action,
        session_id=DEFAULT_SESSION,
        idempotency_key=key,
        args={"target": {"x": 1, "y": 2, "z": 0}},
        policy=CommandPolicy(allow_interrupt=allow_interrupt, max_retries=retries),
    )


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_observed_postcondition_is_the_only_success() -> None:
    harness = make_harness(StubAdapter(verify_after=1))
    harness.source.push(make_observation(seq=1), make_observation(seq=2))

    result = harness.engine.execute(a_request())

    assert result.status is ActionStatus.SUCCEEDED
    assert result.reason_code is ReasonCode.POSTCONDITION_MET
    assert result.evidence["kind"] == "stub_postcondition"
    assert result.evidence["observation_seq"] == 2
    assert len(harness.sink.sent) == 1
    assert harness.sink.cancelled == []


def test_prepared_arguments_are_what_the_mod_receives() -> None:
    harness = make_harness(StubAdapter(verify_after=1))
    harness.source.push(make_observation(seq=4), make_observation(seq=5))

    harness.engine.execute(a_request())

    shipped = harness.sink.last
    assert shipped.args["observed_seq"] == 4
    assert shipped.expected_observation_seq == 4
    assert shipped.idempotency_key == "move-1"


def test_preconditions_are_checked_against_a_fresh_observation() -> None:
    """A cached snapshot describes the world the decision was made in, not this one."""
    stale = make_observation(seq=7)
    harness = make_harness(StubAdapter(verify_after=1), latest=stale)
    harness.source.push(make_observation(seq=8), make_observation(seq=9))

    harness.engine.execute(a_request())

    assert harness.adapter.validate_calls == [8]
    assert harness.source.waits[0][0] == 7  # waited for something newer than the cache


# --------------------------------------------------------------------------
# the mod's opinion never wins
# --------------------------------------------------------------------------


def test_mod_acked_success_without_an_observed_postcondition_fails() -> None:
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=5_000),
        acks=[AckPlan(status=ActionStatus.SUCCEEDED, after_polls=1)],
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.POSTCONDITION_FAILED
    assert "never observed" in result.message
    # The mod finished, so there is nothing left to cancel.
    assert harness.sink.cancelled == []


def test_observed_postcondition_outranks_a_failure_ack() -> None:
    harness = make_harness(
        StubAdapter(verify_after=2, timeout_ms=5_000),
        acks=[AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.PATH_NOT_FOUND, after_polls=1)],
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.status is ActionStatus.SUCCEEDED
    assert harness.adapter.verify_calls == 2


def test_failure_ack_without_evidence_reports_the_mod_reason() -> None:
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=5_000),
        acks=[AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.PATH_NOT_FOUND, after_polls=1)],
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.PATH_NOT_FOUND
    assert result.status is ActionStatus.FAILED


# --------------------------------------------------------------------------
# budgets
# --------------------------------------------------------------------------


def test_timeout_never_reports_success() -> None:
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=1_000, poll_interval_ms=250))
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.ACTION_TIMEOUT
    assert result.status is ActionStatus.FAILED
    assert harness.sink.cancelled == [(harness.sink.last.command_id, ReasonCode.ACTION_TIMEOUT)]
    assert harness.clock.now - DEFAULT_START_MS >= 1_000


def test_stuck_action_fails_before_the_timeout_expires() -> None:
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=100_000, poll_interval_ms=250),
        no_progress_ms=300,
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.NO_PROGRESS
    assert harness.clock.now - DEFAULT_START_MS < 100_000
    assert harness.sink.cancelled == [(harness.sink.last.command_id, ReasonCode.NO_PROGRESS)]


def test_visible_progress_keeps_the_stuck_window_open() -> None:
    harness = make_harness(
        StubAdapter(verify_after=6, timeout_ms=100_000, poll_interval_ms=250),
        no_progress_ms=300,
    )
    moving = [
        make_observation(
            seq=seq,
            action=make_action_state(ownership=ActionOwnership.MOD, busy=True, progress=seq / 10),
        )
        for seq in range(1, 8)
    ]
    harness.source.push(*moving)

    result = harness.engine.execute(a_request())

    assert result.status is ActionStatus.SUCCEEDED


def test_paused_game_suspends_the_action_timeout() -> None:
    """§ 4.14: time the player spent in a menu is not time the character failed in."""
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=1_000, poll_interval_ms=250))
    harness.source.repeat(make_observation(seq=1, game=make_game(paused=True)))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.ACTION_TIMEOUT
    # 1000/250 = 4 polls would have been the unpaused budget; the loop only
    # stopped because its iteration ceiling did.
    assert len(harness.source.waits) == 1 + (1_000 // 250 + 16)


# --------------------------------------------------------------------------
# interruptions
# --------------------------------------------------------------------------


def test_manual_takeover_mid_flight_cancels() -> None:
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=5_000))
    harness.source.push(make_observation(seq=1))
    harness.source.repeat(make_observation(seq=2, safety=make_safety(manual_takeover=True)))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.USER_TAKEOVER
    assert result.status is ActionStatus.CANCELLED
    assert harness.sink.cancelled == [(harness.sink.last.command_id, ReasonCode.USER_TAKEOVER)]


def test_panic_stop_mid_flight_cancels() -> None:
    lever = Lever(engage_after=1)
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=5_000), panic_stop=lever)
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.PANIC_STOP
    assert result.status is ActionStatus.CANCELLED
    assert len(harness.sink.sent) == 1


def test_threat_interrupts_an_interruptible_command() -> None:
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=5_000))
    harness.source.push(make_observation(seq=1))
    harness.source.repeat(
        make_observation(seq=2, safety=make_safety(danger_level=DangerLevel.CRITICAL))
    )

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.THREAT_INTERRUPTED
    assert result.status is ActionStatus.CANCELLED


def test_threat_does_not_interrupt_when_the_caller_forbade_it() -> None:
    harness = make_harness(StubAdapter(verify_after=2, timeout_ms=5_000))
    harness.source.push(make_observation(seq=1))
    harness.source.repeat(
        make_observation(seq=2, safety=make_safety(danger_level=DangerLevel.CRITICAL))
    )

    result = harness.engine.execute(a_request(allow_interrupt=False))

    assert result.status is ActionStatus.SUCCEEDED


def test_save_change_mid_flight_aborts() -> None:
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=5_000))
    harness.source.push(make_observation(seq=1))
    harness.source.repeat(make_observation(seq=2, game=make_game(save_id="another-save")))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.SAVE_CHANGED
    assert harness.sink.cancelled[-1][1] is ReasonCode.SAVE_CHANGED


def test_player_death_terminates_the_session() -> None:
    harness = make_harness(StubAdapter(verify_after=None))
    harness.source.push(make_observation(seq=1, player=make_player(alive=False)))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.SESSION_TERMINATED
    assert harness.sink.sent == []


def test_no_observation_at_all_is_a_disconnect() -> None:
    harness = make_harness(StubAdapter(verify_after=1))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.GAME_DISCONNECTED
    assert harness.sink.sent == []


def test_manual_action_in_the_queue_blocks_the_send() -> None:
    harness = make_harness(StubAdapter(verify_after=1))
    harness.source.push(
        make_observation(
            seq=1, action=make_action_state(ownership=ActionOwnership.MANUAL, busy=True)
        )
    )

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.PLAYER_BUSY_MANUAL_ACTION
    assert harness.sink.sent == []


def test_disarmed_session_refuses_a_mutating_command() -> None:
    harness = make_harness(StubAdapter(verify_after=1))
    harness.source.push(make_observation(seq=1, safety=make_safety(armed=False)))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.NOT_ARMED
    assert harness.sink.sent == []


def test_cancel_still_runs_while_the_player_has_taken_over() -> None:
    """Gating the stop mechanism on "nobody is stopping us" makes it useless."""
    lever = Lever(engage_after=0)
    harness = make_harness(
        StubAdapter(name=ActionName.PLAN_CANCEL, verify_after=1),
        panic_stop=lever,
    )
    hostile = make_safety(armed=False, manual_takeover=True, danger_level=DangerLevel.CRITICAL)
    harness.source.push(
        make_observation(seq=1, safety=hostile), make_observation(seq=2, safety=hostile)
    )

    result = harness.engine.execute(a_request(ActionName.PLAN_CANCEL, key="cancel-1"))

    assert result.status is ActionStatus.SUCCEEDED


# --------------------------------------------------------------------------
# retries
# --------------------------------------------------------------------------


def test_retryable_failure_is_retried_up_to_the_budget_then_terminal() -> None:
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=5_000),
        acks=[AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.QUEUE_REJECTED, after_polls=1)],
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request(retries=2))

    assert result.reason_code is ReasonCode.QUEUE_REJECTED
    assert result.reason_code in RETRYABLE_CODES
    assert result.attempt == 3
    assert len(harness.sink.sent) == 3
    keys = [command.idempotency_key for command in harness.sink.sent]
    assert keys == ["move-1", "move-1#a2", "move-1#a3"]
    ids = {command.command_id for command in harness.sink.sent}
    assert len(ids) == 3


def test_non_retryable_failure_is_not_retried() -> None:
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=5_000),
        acks=[AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.INVALID_REF, after_polls=1)],
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request(retries=2))

    assert result.reason_code is ReasonCode.INVALID_REF
    assert len(harness.sink.sent) == 1


def test_a_queue_rejection_from_the_sink_is_retried() -> None:
    harness = make_harness(
        StubAdapter(verify_after=1, timeout_ms=5_000),
        rejections=[ReasonCode.QUEUE_REJECTED],
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request(retries=1))

    assert result.status is ActionStatus.SUCCEEDED
    assert len(harness.sink.sent) == 2


def test_retry_budget_of_zero_means_one_attempt() -> None:
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=1_000),
        acks=[AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.QUEUE_REJECTED, after_polls=1)],
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request(retries=0))

    assert result.attempt == 1
    assert len(harness.sink.sent) == 1


# --------------------------------------------------------------------------
# refusals before anything is sent
# --------------------------------------------------------------------------


def test_precondition_failure_sends_nothing() -> None:
    adapter = StubAdapter(
        refuse=PreconditionFailed(
            "item is gone",
            reason_code=ReasonCode.INVALID_REF,
            evidence={"item_ref": "item:x"},
        )
    )
    harness = make_harness(adapter, no_progress_ms=5_000)
    harness.source.push(make_observation(seq=1))

    result = harness.engine.execute(a_request(retries=2))

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code is ReasonCode.INVALID_REF
    assert result.evidence == {"item_ref": "item:x"}
    assert harness.sink.sent == []
    assert harness.sink.cancelled == []


def test_unregistered_action_is_a_missing_capability() -> None:
    harness = make_harness(StubAdapter(verify_after=1), registered=False)
    harness.source.push(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert result.status is ActionStatus.REJECTED
    assert harness.sink.sent == []
    assert harness.source.waits == []


def test_required_capability_fails_closed_by_default() -> None:
    harness = make_harness(StubAdapter(verify_after=1, required_capability="eat_percentage"))
    harness.source.push(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert harness.sink.sent == []


def test_a_probed_capability_lets_the_command_through() -> None:
    harness = make_harness(
        StubAdapter(verify_after=1, required_capability="eat_percentage"),
        capability_check=lambda name: name == "eat_percentage",
    )
    harness.source.push(make_observation(seq=1), make_observation(seq=2))

    result = harness.engine.execute(a_request())

    assert result.status is ActionStatus.SUCCEEDED


def test_an_adapter_that_crashes_produces_a_result_not_an_exception() -> None:
    harness = make_harness(StubAdapter(crash=RuntimeError("adapter blew up")))
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.INTERNAL_ERROR
    assert "RuntimeError" in result.message
    assert harness.sink.sent == []


def test_an_observation_from_another_session_is_stale() -> None:
    harness = make_harness(StubAdapter(verify_after=1))
    foreign = replace(make_observation(seq=1), session_id="00000000-0000-0000-0000-00000000beef")
    harness.source.push(foreign)

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.STALE_SESSION
    assert harness.sink.sent == []


def test_every_terminal_result_carries_bounded_diagnostics() -> None:
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=1_000))
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request(retries=1))

    assert result.diagnostics
    assert len(result.diagnostics) <= 10
    assert result.diagnostics[0] == "attempt=2"
    assert result.to_dict()["reason_code"] == ReasonCode.ACTION_TIMEOUT.value


# --------------------------------------------------------------------------
# request shape
# --------------------------------------------------------------------------


def test_attempt_keys_stay_within_the_protocol_limit() -> None:
    """A retry is new work and needs its own key; the prefix keeps them traceable."""
    request = a_request()
    assert request.attempt_key(1) == "move-1"
    assert request.attempt_key(2) == "move-1#a2"
    assert len(a_request(key="x" * 120).attempt_key(3)) <= MAX_IDEMPOTENCY_KEY_LEN


def test_request_rejects_a_key_with_no_room_for_the_suffix() -> None:
    with pytest.raises(ValueError, match="idempotency_key"):
        a_request(key="x" * 121)


def test_request_rejects_a_lease_outside_the_protocol_bounds() -> None:
    with pytest.raises(ValueError, match="lease_ms"):
        ActionRequest(
            action=ActionName.MOVEMENT_MOVE_TO,
            session_id=DEFAULT_SESSION,
            idempotency_key="k",
            lease_ms=10,
        )
