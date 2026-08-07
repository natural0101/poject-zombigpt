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
    Dispatch,
    PreconditionFailed,
    deny_capability,
    no_panic,
)
from pz_agent_core.actions.engine import MAX_POLLS
from pz_agent_core.capabilities import (
    DRINK_WORLD_SOURCE,
    EAT_PERCENTAGE,
    REASON_NO_VERIFIED_API,
    Capability,
    CapabilityReport,
    utc_now_iso,
)
from pz_agent_core.capabilities import Evidence as CapabilityEvidence
from pz_agent_core.protocol import (
    RETRYABLE_CODES,
    ActionName,
    ActionOwnership,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    DangerLevel,
    Observation,
    ReasonCode,
)
from pz_agent_core.protocol.messages import MAX_IDEMPOTENCY_KEY_LEN, MAX_RETRIES
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


class RogueSink(FakeCommandSink):
    """A sink that answers a ``send`` with a result it was never entitled to.

    The IPC layer is another subsystem's code; the engine's success invariant
    only means something if a port cannot hand back a ready-made verdict.
    """

    def __init__(self, clock: FakeClock, status: ActionStatus) -> None:
        super().__init__(clock)
        self._status = status

    def send(self, command: Command) -> Dispatch:
        dispatch = super().send(command)
        shipped = dispatch.command
        if self._status is ActionStatus.SUCCEEDED:
            rejection = ActionResult.succeeded(
                session_id=shipped.session_id,
                seq=0,
                command_id=shipped.command_id,
                action=shipped.action.value,
                timestamp_ms=self._clock(),
                evidence={"sink_says": "already done"},
            )
        else:
            rejection = ActionResult.failure(
                session_id=shipped.session_id,
                seq=0,
                command_id=shipped.command_id,
                action=shipped.action.value,
                timestamp_ms=self._clock(),
                reason_code=ReasonCode.QUEUE_REJECTED,
                status=self._status,
            )
        return Dispatch(command=shipped, rejection=rejection)


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
    sink_factory: Callable[[FakeClock], FakeCommandSink] | None = None,
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
    sink = (
        sink_factory(clock)
        if sink_factory is not None
        else FakeCommandSink(clock, acks=acks, rejections=rejections)
    )
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


def test_a_progress_ack_in_the_same_batch_cannot_erase_a_terminal_one() -> None:
    """A stray frame after the verdict must not downgrade a failure to a timeout."""
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=5_000),
        acks=[
            AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.PATH_NOT_FOUND, after_polls=1),
            AckPlan(status=ActionStatus.PROGRESS, after_polls=1, progress=0.5),
        ],
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    # Not the ACTION_TIMEOUT it would have reported had the failure been lost.
    assert result.reason_code is ReasonCode.PATH_NOT_FOUND
    assert result.message == "scripted ack"


def test_a_failed_ack_claiming_the_success_code_is_reported_as_a_postcondition_failure() -> None:
    """POSTCONDITION_MET is reserved for results this engine verified itself."""
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=5_000),
        acks=[
            AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.POSTCONDITION_MET, after_polls=1)
        ],
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.POSTCONDITION_FAILED


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


def test_progress_arriving_on_the_stuck_deadline_is_not_a_stall() -> None:
    """The window is judged after the observation is folded in, not before.

    With ``no_progress_ms`` equal to one poll, every observation lands exactly
    on the deadline it refreshes. Ruling first would report "nothing changed"
    about the very tick that changed something.
    """
    harness = make_harness(
        StubAdapter(verify_after=2, timeout_ms=100_000, poll_interval_ms=250),
        no_progress_ms=250,
    )
    harness.source.push(
        *[
            make_observation(
                seq=seq,
                action=make_action_state(
                    ownership=ActionOwnership.MOD, busy=True, progress=seq / 10
                ),
            )
            for seq in range(1, 4)
        ]
    )

    result = harness.engine.execute(a_request())

    assert result.status is ActionStatus.SUCCEEDED
    assert harness.sink.cancelled == []


def test_the_poll_loop_stops_on_its_iteration_ceiling() -> None:
    """A deadline is not a bound when the clock barely moves; the ceiling is."""
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=10_000_000, poll_interval_ms=1),
        no_progress_ms=10_000_000,
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.ACTION_TIMEOUT
    # One wait for the fresh pre-flight observation, then exactly the ceiling.
    assert len(harness.source.waits) == 1 + MAX_POLLS
    assert harness.clock.now - DEFAULT_START_MS < 10_000_000


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
    # The lever is useless if the command it interrupted keeps running.
    assert harness.sink.cancelled == [(harness.sink.last.command_id, ReasonCode.PANIC_STOP)]


def test_threat_interrupts_an_interruptible_command() -> None:
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=5_000))
    harness.source.push(make_observation(seq=1))
    harness.source.repeat(
        make_observation(seq=2, safety=make_safety(danger_level=DangerLevel.CRITICAL))
    )

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.THREAT_INTERRUPTED
    assert result.status is ActionStatus.CANCELLED
    assert harness.sink.cancelled == [(harness.sink.last.command_id, ReasonCode.THREAT_INTERRUPTED)]
    assert result.evidence["danger_level"] == DangerLevel.CRITICAL.value


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


def test_a_stale_sidecar_sends_nothing() -> None:
    """The mod stopped trusting this sidecar; issuing commands anyway is worse."""
    harness = make_harness(StubAdapter(verify_after=1))
    harness.source.push(make_observation(seq=1, safety=make_safety(sidecar_stale=True)))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.STALE_SESSION
    assert result.status is ActionStatus.REJECTED
    assert harness.sink.sent == []


def test_an_absent_character_sends_nothing() -> None:
    harness = make_harness(StubAdapter(verify_after=1))
    harness.source.push(make_observation(seq=1, player=make_player(present=False)))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.PRECONDITION_FAILED
    assert harness.sink.sent == []


def test_safety_stop_is_exempt_from_every_interrupt_gate() -> None:
    """§ 8.1: the stop paths must not be gated on the conditions they exist to end."""
    lever = Lever(engage_after=0)
    harness = make_harness(
        StubAdapter(name=ActionName.SAFETY_STOP, verify_after=1),
        panic_stop=lever,
    )
    hostile = make_safety(armed=False, manual_takeover=True, danger_level=DangerLevel.CRITICAL)
    harness.source.push(
        make_observation(
            seq=1,
            safety=hostile,
            action=make_action_state(ownership=ActionOwnership.MANUAL, busy=True),
        ),
        make_observation(seq=2, safety=hostile),
    )

    result = harness.engine.execute(a_request(ActionName.SAFETY_STOP, key="stop-1"))

    assert result.status is ActionStatus.SUCCEEDED
    assert len(harness.sink.sent) == 1


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


def test_manual_takeover_before_the_send_refuses_rather_than_cancels() -> None:
    """The pre-flight half of the takeover check, which nothing else covered.

    `test_manual_takeover_mid_flight_cancels` pushes a clean observation first,
    so it exercises `_in_flight_abort` alone. Deleting the identical check from
    `_interrupt_abort` left the whole suite green while the engine dispatched a
    command into a character the player had already taken control of — the
    command goes out, then gets cancelled, which is not the same thing as never
    going out. The assertion that matters is the second one.
    """
    harness = make_harness(StubAdapter(verify_after=1))
    harness.source.repeat(make_observation(seq=1, safety=make_safety(manual_takeover=True)))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.USER_TAKEOVER
    assert result.status is ActionStatus.REJECTED
    assert harness.sink.sent == [], "a command reached the mod after the player took over"


def test_a_blocking_action_arriving_mid_flight_cancels_the_command() -> None:
    """The mid-flight half of `blocks_automation`, the pre-flight one being above.

    Both halves of both interrupt conditions now have a test; before this, each
    condition had exactly one, and which one differed between them.
    """
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=5_000))
    harness.source.push(make_observation(seq=1))
    harness.source.repeat(
        make_observation(
            seq=2, action=make_action_state(ownership=ActionOwnership.MANUAL, busy=True)
        )
    )

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.PLAYER_BUSY_MANUAL_ACTION
    assert result.status is ActionStatus.CANCELLED
    assert harness.sink.cancelled == [
        (harness.sink.last.command_id, ReasonCode.PLAYER_BUSY_MANUAL_ACTION)
    ]


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


def test_retry_budget_is_clamped_to_the_protocol_ceiling() -> None:
    """``CommandPolicy`` is only bounds-checked on the wire; the engine clamps."""
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=1_000),
        acks=[AckPlan(status=ActionStatus.FAILED, reason=ReasonCode.QUEUE_REJECTED, after_polls=1)],
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request(retries=9_999))

    assert len(harness.sink.sent) == 1 + MAX_RETRIES
    assert result.attempt == 1 + MAX_RETRIES
    assert result.reason_code is ReasonCode.QUEUE_REJECTED


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


def test_a_real_capability_report_plugs_straight_into_the_gate() -> None:
    """Fail-closed is a seam, not a dead end: ``CapabilityReport.usable`` fits it.

    Pinning this keeps the default honest in both directions — the engine
    refuses until a probe says otherwise, and a probe that *has* run is all it
    takes to let the command through.
    """
    report = CapabilityReport(
        build="42.20",
        capabilities=(
            Capability.available_unverified(
                name=EAT_PERCENTAGE,
                build="42.20",
                evidence=(
                    CapabilityEvidence.from_scan(
                        symbol="ISEatFoodAction:new",
                        file="media/lua/client/TimedActions/ISEatFoodAction.lua",
                        file_sha256="0" * 64,
                        signature="ISEatFoodAction:new(character, item, percentage)",
                        observed_at=utc_now_iso(),
                    ),
                ),
            ),
            Capability.unsupported(name=DRINK_WORLD_SOURCE, reason=REASON_NO_VERIFIED_API),
        ),
    )

    allowed = make_harness(
        StubAdapter(verify_after=1, required_capability=EAT_PERCENTAGE),
        capability_check=report.usable,
    )
    allowed.source.push(make_observation(seq=1), make_observation(seq=2))
    assert allowed.engine.execute(a_request()).status is ActionStatus.SUCCEEDED

    refused = make_harness(
        StubAdapter(verify_after=1, required_capability=DRINK_WORLD_SOURCE),
        capability_check=report.usable,
    )
    refused.source.push(make_observation(seq=1), make_observation(seq=2))
    denial = refused.engine.execute(a_request())
    assert denial.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert refused.sink.sent == []


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


def test_a_crash_while_watching_cancels_the_command_it_started() -> None:
    """The mod is already working; an internal error is no reason to abandon it."""
    harness = make_harness(
        StubAdapter(verify_crash=RuntimeError("verify blew up"), timeout_ms=5_000)
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.INTERNAL_ERROR
    assert "RuntimeError" in result.message
    assert len(harness.sink.sent) == 1
    assert harness.sink.cancelled == [(harness.sink.last.command_id, ReasonCode.INTERNAL_ERROR)]


@pytest.mark.parametrize("status", [ActionStatus.SUCCEEDED, ActionStatus.ACCEPTED])
def test_a_sink_cannot_deliver_a_verdict_the_engine_did_not_reach(
    status: ActionStatus,
) -> None:
    """Only ``_success`` may report success, and only a terminal result may end a call."""
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=1_000),
        sink_factory=lambda clock: RogueSink(clock, status),
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.INTERNAL_ERROR
    assert status.value in result.message


def test_failure_evidence_carries_both_observation_sequences() -> None:
    """§ 4.15: a failed result names the observations it was judged against."""
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=1_000))
    harness.source.push(make_observation(seq=11))
    harness.source.repeat(make_observation(seq=12))

    result = harness.engine.execute(a_request())

    assert result.reason_code is ReasonCode.ACTION_TIMEOUT
    assert result.evidence["observation_seq_before"] == 11
    assert result.evidence["observation_seq"] > 11
    assert result.evidence["save_id"]


def test_every_terminal_result_carries_bounded_diagnostics() -> None:
    harness = make_harness(StubAdapter(verify_after=None, timeout_ms=1_000))
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request(retries=1))

    assert len(result.diagnostics) <= 10
    assert result.diagnostics[0] == "attempt=2"
    # § 4.15 asks for the retry count, how long it ran and which save it ran on.
    joined = " ".join(result.diagnostics)
    assert "elapsed_ms=" in joined
    assert "save_id=" in joined
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
