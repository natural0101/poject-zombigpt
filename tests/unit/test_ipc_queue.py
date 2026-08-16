"""Sequencing, idempotency, leases and backpressure.

These are the rules that decide whether a command runs twice, runs late, or
runs while another one is still running — the three ways a file-based queue
hurts a player who cannot undo anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pz_agent_core.ipc.journal import JournalError, JournalReader, JournalWriter, read_header
from pz_agent_core.ipc.queue import (
    QUEUE_OCCUPYING_ACTIONS,
    RECOVERY_TAIL_BYTES,
    CommandQueue,
    IdempotencyCache,
    LeaseCheckpoint,
    SequenceEvent,
    SequenceTracker,
    Stream,
)
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    Command,
    ReasonCode,
)
from tests.fixtures.ipc_builders import (
    IPC_SESSION_ID,
    FakeClock,
    make_command,
    make_failure,
    make_layout,
    make_started,
    make_success,
    publish_acks,
)

# --------------------------------------------------------------------------
# sequence tracking
# --------------------------------------------------------------------------


def test_allocation_is_monotonic_and_per_stream() -> None:
    tracker = SequenceTracker()
    assert [tracker.allocate(Stream.COMMAND) for _ in range(3)] == [0, 1, 2]
    assert tracker.allocate(Stream.OBSERVATION) == 0
    assert tracker.peek(Stream.COMMAND) == 3


def test_in_order_observations_report_no_gap() -> None:
    tracker = SequenceTracker()
    for seq in range(3):
        check = tracker.observe(Stream.ACK, seq)
        assert check.event is SequenceEvent.IN_ORDER
        assert not check.needs_full_snapshot
    assert tracker.gaps == ()


def test_a_gap_is_reported_and_never_interpolated() -> None:
    tracker = SequenceTracker()
    tracker.observe(Stream.OBSERVATION, 0)
    check = tracker.observe(Stream.OBSERVATION, 4)
    assert check.event is SequenceEvent.GAP
    assert check.expected == 1
    assert check.missing == 3
    assert check.needs_full_snapshot
    assert tracker.gaps == (check,)


def test_a_gap_is_reported_once_not_forever() -> None:
    tracker = SequenceTracker()
    tracker.observe(Stream.EVENT, 0)
    tracker.observe(Stream.EVENT, 5)
    assert tracker.observe(Stream.EVENT, 6).event is SequenceEvent.IN_ORDER


def test_a_replayed_sequence_number_is_a_duplicate_not_a_gap() -> None:
    tracker = SequenceTracker()
    tracker.observe(Stream.ACK, 0)
    tracker.observe(Stream.ACK, 1)
    check = tracker.observe(Stream.ACK, 1)
    assert check.event is SequenceEvent.DUPLICATE
    assert check.missing == 0
    assert tracker.last_seen(Stream.ACK) == 1


def test_gap_history_is_bounded() -> None:
    tracker = SequenceTracker(gap_history=2)
    for seq in (2, 5, 9, 20):
        tracker.observe(Stream.OBSERVATION, seq)
    assert len(tracker.gaps) == 2
    assert tracker.gaps[-1].seq == 20


def test_reset_forgets_a_stream() -> None:
    tracker = SequenceTracker()
    tracker.observe(Stream.COMMAND, 7)
    tracker.reset(Stream.COMMAND)
    assert tracker.last_seen(Stream.COMMAND) is None
    assert tracker.observe(Stream.COMMAND, 0).event is SequenceEvent.IN_ORDER


def test_negative_sequence_numbers_are_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        SequenceTracker().observe(Stream.ACK, -1)


def test_a_gap_history_that_keeps_nothing_is_refused() -> None:
    """A cap of zero is not a bound, it is evidence thrown away."""
    with pytest.raises(ValueError, match="gap_history"):
        SequenceTracker(gap_history=0)


# --------------------------------------------------------------------------
# idempotency cache
# --------------------------------------------------------------------------


def test_terminal_results_are_replayed() -> None:
    cache = IdempotencyCache()
    command = make_command()
    result = make_success(command)
    cache.remember("key", result)
    assert cache.replay("key") is result
    assert "key" in cache


def test_non_terminal_results_are_never_cached() -> None:
    cache = IdempotencyCache()
    cache.remember("key", make_started(make_command()))
    assert cache.replay("key") is None
    assert len(cache) == 0


def test_the_first_terminal_result_wins() -> None:
    cache = IdempotencyCache()
    command = make_command()
    first = make_success(command)
    cache.remember("key", first)
    cache.remember("key", make_failure(command))
    assert cache.replay("key") is first


def test_the_cache_is_bounded_by_insertion_order() -> None:
    cache = IdempotencyCache(capacity=2)
    command = make_command()
    for index in range(3):
        cache.remember(f"key-{index}", make_success(command, seq=index))
    assert len(cache) == 2
    assert cache.replay("key-0") is None
    assert cache.replay("key-2") is not None
    # Replaying must not extend a key's life, or one hot key pins the rest.
    cache.replay("key-1")
    cache.remember("key-3", make_success(command, seq=3))
    assert cache.replay("key-1") is None


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        IdempotencyCache(capacity=0)


# --------------------------------------------------------------------------
# the queue itself
# --------------------------------------------------------------------------


def _queue(tmp_path: Path, clock: FakeClock) -> CommandQueue:
    return CommandQueue(make_layout(tmp_path), session_id=IPC_SESSION_ID, clock=clock)


def _written_commands(queue: CommandQueue) -> list[Command]:
    reader = JournalReader(queue.layout, queue.layout.command_queue)
    return [Command.from_dict(record.payload) for record in reader.read().records]


def test_backpressure_only_applies_to_world_touching_actions() -> None:
    assert ActionName.MOVEMENT_MOVE_TO in QUEUE_OCCUPYING_ACTIONS
    assert ActionName.CONSUME_EAT in QUEUE_OCCUPYING_ACTIONS
    assert ActionName.SAFETY_STOP not in QUEUE_OCCUPYING_ACTIONS
    assert ActionName.WORLD_INSPECT not in QUEUE_OCCUPYING_ACTIONS
    assert ActionName.PLAN_CANCEL not in QUEUE_OCCUPYING_ACTIONS


def test_an_accepted_command_reaches_the_journal(tmp_path: Path) -> None:
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="k1", lease_ms=10_000)
    outcome = queue.submit(command)

    assert outcome.accepted
    assert outcome.terminal_result is None
    written = _written_commands(queue)
    assert [c.command_id for c in written] == [command.command_id]
    assert queue.in_flight == outcome.command
    queue.close()


def test_sequence_numbers_are_allocated_monotonically(tmp_path: Path) -> None:
    queue = _queue(tmp_path, FakeClock())
    first = queue.submit(
        queue.build(ActionName.WORLD_INSPECT, idempotency_key="k1", lease_ms=1_000)
    )
    second = queue.submit(
        queue.build(ActionName.WORLD_INSPECT, idempotency_key="k2", lease_ms=1_000)
    )
    assert (first.command.seq, second.command.seq) == (0, 1)
    assert [c.seq for c in _written_commands(queue)] == [0, 1]
    queue.close()


def test_a_command_that_is_never_written_does_not_burn_a_sequence_number(
    tmp_path: Path,
) -> None:
    """§3.4 makes a hole in a stream mean "a record was lost". A command that a
    gate refused was never sent, so it must not leave one behind: the mod would
    otherwise report a lost command every time backpressure did its job."""
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    queue.submit(queue.build(ActionName.MOVEMENT_MOVE_TO, idempotency_key="m", lease_ms=10_000))

    blocked = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    assert not queue.submit(blocked).accepted  # backpressure
    expired = queue.build(ActionName.WORLD_INSPECT, idempotency_key="look", lease_ms=1_000)
    clock.advance(2_000)
    assert not queue.submit(expired).accepted  # lease
    later = queue.build(ActionName.WORLD_INSPECT, idempotency_key="look-2", lease_ms=10_000)
    assert queue.submit(later).accepted

    seqs = [c.seq for c in _written_commands(queue)]
    assert seqs == [0, 1]
    tracker = SequenceTracker()
    assert all(tracker.observe(Stream.COMMAND, seq).event is SequenceEvent.IN_ORDER for seq in seqs)
    queue.close()


def test_a_duplicate_command_replays_the_original_result(tmp_path: Path) -> None:
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    queue.submit(command)
    original = make_success(command, seq=0, timestamp_ms=clock.now)
    queue.record_ack(original)

    retry = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    outcome = queue.submit(retry)

    assert not outcome.accepted
    assert outcome.duplicate
    assert outcome.replayed is original
    assert outcome.terminal_result is original
    # The decisive assertion: nothing new was queued, so nothing re-executes.
    assert len(_written_commands(queue)) == 1
    queue.close()


def test_a_duplicate_of_a_finished_command_replays_even_after_the_lease_ran_out(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=1_000)
    queue.submit(command)
    queue.record_ack(make_success(command, timestamp_ms=clock.now))

    clock.advance(60_000)
    retry = Command(
        session_id=IPC_SESSION_ID,
        seq=99,
        command_id=command.command_id,
        idempotency_key="eat-1",
        issued_at_ms=command.issued_at_ms,
        lease_ms=1_000,
        action=ActionName.CONSUME_EAT,
    )
    outcome = queue.submit(retry)
    assert outcome.replayed is not None
    assert outcome.replayed.status is ActionStatus.SUCCEEDED
    assert outcome.rejection is None
    queue.close()


def test_a_non_terminal_ack_does_not_make_a_command_replayable(tmp_path: Path) -> None:
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    queue.submit(command)
    queue.record_ack(make_started(command))

    assert queue.cache.replay("eat-1") is None
    assert queue.in_flight is not None
    assert queue.in_flight.command_id == command.command_id
    queue.close()


def test_an_expired_lease_is_rejected_on_receipt(tmp_path: Path) -> None:
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=1_000)
    clock.advance(1_001)

    outcome = queue.submit(command)
    assert not outcome.accepted
    rejection = outcome.rejection
    assert rejection is not None
    assert rejection.reason_code is ReasonCode.LEASE_EXPIRED
    assert rejection.status is ActionStatus.REJECTED
    assert LeaseCheckpoint.ON_RECEIPT.value in rejection.message
    assert _written_commands(queue) == []
    assert queue.in_flight is None
    queue.close()


def test_an_expired_lease_is_rejected_again_before_execution(tmp_path: Path) -> None:
    """The second gate is the point: a command can wait behind a long action."""
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=1_000)
    assert queue.submit(command).accepted
    assert queue.check_lease(command) is None

    clock.advance(5_000)
    rejection = queue.check_lease(command)
    assert rejection is not None
    assert rejection.reason_code is ReasonCode.LEASE_EXPIRED
    assert LeaseCheckpoint.BEFORE_EXECUTION.value in rejection.message
    assert "4000 ms ago" in rejection.message
    queue.close()


def test_only_one_world_touching_command_is_in_flight(tmp_path: Path) -> None:
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    first = queue.build(ActionName.MOVEMENT_MOVE_TO, idempotency_key="move-1", lease_ms=10_000)
    assert queue.submit(first).accepted

    second = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    outcome = queue.submit(second)
    assert not outcome.accepted
    assert outcome.rejection is not None
    assert outcome.rejection.reason_code is ReasonCode.QUEUE_REJECTED
    assert len(_written_commands(queue)) == 1
    queue.close()


def test_a_backpressure_rejection_is_not_cached_so_the_retry_can_succeed(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    first = queue.build(ActionName.MOVEMENT_MOVE_TO, idempotency_key="move-1", lease_ms=10_000)
    queue.submit(first)
    blocked = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    queue.submit(blocked)
    assert queue.cache.replay("eat-1") is None

    queue.record_ack(make_success(first, timestamp_ms=clock.now))
    assert queue.in_flight is None
    retry = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    assert queue.submit(retry).accepted
    queue.close()


def test_read_only_commands_are_not_blocked_by_an_in_flight_action(tmp_path: Path) -> None:
    queue = _queue(tmp_path, FakeClock())
    queue.submit(queue.build(ActionName.MOVEMENT_MOVE_TO, idempotency_key="m", lease_ms=10_000))
    look = queue.build(ActionName.WORLD_INSPECT, idempotency_key="look", lease_ms=1_000)
    assert queue.submit(look).accepted
    queue.close()


def test_safety_stop_bypasses_the_queue(tmp_path: Path) -> None:
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    moving = queue.build(ActionName.MOVEMENT_MOVE_TO, idempotency_key="move-1", lease_ms=10_000)
    queue.submit(moving)

    stop = queue.send_stop(idempotency_key="stop-1")
    written = _written_commands(queue)
    assert [c.action for c in written] == [ActionName.MOVEMENT_MOVE_TO, ActionName.SAFETY_STOP]
    # The stop does not evict the in-flight command: the mod's cancellation ack
    # is what closes it, and pretending otherwise would fabricate an outcome.
    assert queue.in_flight is not None
    assert queue.in_flight.command_id == moving.command_id
    assert stop.action is ActionName.SAFETY_STOP
    queue.close()


def test_a_stop_is_sent_even_when_an_earlier_stop_is_cached(tmp_path: Path) -> None:
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    first = queue.send_stop(idempotency_key="stop-1")
    queue.record_ack(make_failure(first, reason_code=ReasonCode.PANIC_STOP))
    queue.send_stop(idempotency_key="stop-1")
    assert len(_written_commands(queue)) == 2
    queue.close()


def test_a_command_from_another_session_is_rejected(tmp_path: Path) -> None:
    queue = _queue(tmp_path, FakeClock())
    foreign = make_command(session_id="00000000-0000-0000-0000-0000000000ff")
    outcome = queue.submit(foreign)
    assert outcome.rejection is not None
    assert outcome.rejection.reason_code is ReasonCode.STALE_SESSION
    assert _written_commands(queue) == []
    queue.close()


def test_polling_acks_applies_them_to_the_queue_state(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    queue = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    queue.submit(command)

    publish_acks(layout, [make_started(command, seq=0), make_success(command, seq=1)])
    poll = queue.poll_acks()

    assert [r.status for r in poll.results] == [ActionStatus.STARTED, ActionStatus.SUCCEEDED]
    assert not poll.needs_full_snapshot
    assert queue.in_flight is None
    assert queue.cache.replay("eat-1") is not None
    queue.close()


def test_a_gap_in_the_ack_stream_demands_a_full_snapshot(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    queue = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    queue.submit(command)

    publish_acks(layout, [make_started(command, seq=0), make_success(command, seq=4)])
    poll = queue.poll_acks()

    assert poll.needs_full_snapshot
    assert [gap.missing for gap in poll.gaps] == [3]
    queue.close()


def test_a_duplicate_ack_is_reported_but_applied_once(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    queue = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    queue.submit(command)

    publish_acks(layout, [make_success(command, seq=0), make_failure(command, seq=0)])
    poll = queue.poll_acks()

    assert len(poll.results) == 1
    assert poll.results[0].status is ActionStatus.SUCCEEDED
    assert any(check.event is SequenceEvent.DUPLICATE for check in poll.checks)
    replayed = queue.cache.replay("eat-1")
    assert replayed is not None and replayed.status is ActionStatus.SUCCEEDED
    queue.close()


def test_unusable_and_foreign_acks_become_diagnostics(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    queue = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    queue.submit(command)

    foreign = make_command(session_id="00000000-0000-0000-0000-0000000000ff")
    publish_acks(layout, [make_success(foreign, seq=0)])
    with layout.command_ack.open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version": "1.0", "status": "who knows"}\n')

    poll = queue.poll_acks()
    assert poll.results == ()
    details = " ".join(diagnostic.detail for diagnostic in poll.diagnostics)
    assert "foreign session" in details
    assert "unusable ack" in details
    queue.close()


def test_closing_an_in_flight_command_reports_it_as_lost(tmp_path: Path) -> None:
    clock = FakeClock()
    queue = _queue(tmp_path, clock)
    command = queue.build(ActionName.MOVEMENT_MOVE_TO, idempotency_key="move-1", lease_ms=10_000)
    queue.submit(command)

    lost = queue.close_in_flight(ReasonCode.GAME_DISCONNECTED)
    assert lost is not None
    assert lost.status is ActionStatus.LOST
    assert lost.is_terminal
    assert lost.evidence == {}
    assert queue.in_flight is None
    # Lost is terminal, so a redelivery replays it rather than starting again.
    assert queue.cache.replay("move-1") is lost
    assert queue.close_in_flight(ReasonCode.GAME_DISCONNECTED) is None
    queue.close()


def test_pending_commands_are_bounded(tmp_path: Path) -> None:
    queue = CommandQueue(
        make_layout(tmp_path), session_id=IPC_SESSION_ID, clock=FakeClock(), pending_limit=2
    )
    for index in range(4):
        queue.submit(
            queue.build(ActionName.WORLD_INSPECT, idempotency_key=f"k{index}", lease_ms=1_000)
        )
    assert len(queue.pending) == 2
    # The bound sheds the oldest, not a random one.
    assert [c.idempotency_key for c in queue.pending] == ["k2", "k3"]
    queue.close()


def test_the_bound_never_sheds_the_in_flight_command(tmp_path: Path) -> None:
    """Forgetting it would lose the key its terminal ack is filed under, and a
    redelivered ``consume.eat`` would then be executed a second time."""
    clock = FakeClock()
    queue = CommandQueue(
        make_layout(tmp_path), session_id=IPC_SESSION_ID, clock=clock, pending_limit=2
    )
    eating = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    assert queue.submit(eating).accepted
    for index in range(4):
        queue.submit(
            queue.build(ActionName.WORLD_INSPECT, idempotency_key=f"k{index}", lease_ms=1_000)
        )

    assert len(queue.pending) == 2
    assert [c.command_id for c in queue.pending].count(eating.command_id) == 1
    assert queue.in_flight is not None
    assert queue.in_flight.command_id == eating.command_id

    # Its ack therefore still reaches the idempotency cache, and the retry the
    # planner sends after a hiccup replays instead of eating a second sandwich.
    queue.record_ack(make_success(eating, timestamp_ms=clock.now))
    retry = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    outcome = queue.submit(retry)
    assert outcome.duplicate
    assert len(_written_commands(queue)) == 5
    queue.close()


def test_a_pending_limit_of_zero_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pending_limit"):
        CommandQueue(
            make_layout(tmp_path), session_id=IPC_SESSION_ID, clock=FakeClock(), pending_limit=0
        )


# --------------------------------------------------------------------------
# restart recovery of the outbound sequence
# --------------------------------------------------------------------------


def test_a_restarted_queue_continues_the_sequence_it_left_off(tmp_path: Path) -> None:
    """Live finding (Build 42.20.2): a restarted sidecar started the COMMAND
    stream at 0 while the journal still held records 0..N — a second producer
    of the same stream, accepted silently because the mod dedups by command_id
    rather than seq."""
    clock = FakeClock()
    layout = make_layout(tmp_path)
    died = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=clock)
    for index in range(5):
        outcome = died.submit(
            died.build(ActionName.WORLD_INSPECT, idempotency_key=f"k{index}", lease_ms=10_000)
        )
        assert outcome.accepted
    died.close()

    restarted = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=clock)
    outcome = restarted.submit(
        restarted.build(ActionName.WORLD_INSPECT, idempotency_key="k-new", lease_ms=10_000)
    )
    assert outcome.accepted
    assert outcome.command.seq == 5
    # The decisive assertion is the record on disk: one stream, one producer.
    assert [c.seq for c in _written_commands(restarted)] == [0, 1, 2, 3, 4, 5]
    restarted.close()


def test_a_predecessor_that_died_mid_write_is_refused_not_resumed_from_zero(
    tmp_path: Path,
) -> None:
    """The uncommitted tail is the signal for exactly how a previous process dies.

    Same live finding as above and the other half of it: recovery must not
    answer "nothing here" for a journal it could not read. A process killed
    mid-append leaves bytes after the header with no committed newline — no
    diagnostic, because an unterminated line short of ``MAX_LINE_BYTES`` is
    "the producer is mid-write", which is true and is the wrong reading once
    that producer is gone. ``pending_bytes > 0`` is what tells the two apart.

    Without it recovery returns ``None``, the restarted queue seeds the command
    stream at zero, and it becomes a second producer of numbers the mod will
    accept — it dedups by ``command_id``, not by ``seq`` — with gap detection
    on both sides quietly meaningless. Deleting that conjunct left the whole
    suite green (9471 passed).
    """
    layout = make_layout(tmp_path)
    JournalWriter(layout, layout.command_queue).close()  # a header, nothing more
    with layout.command_queue.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 0, "command_id": "half-writ')  # killed mid-append

    with pytest.raises(JournalError) as caught:
        CommandQueue(layout, session_id=IPC_SESSION_ID, clock=FakeClock())

    assert "no record could be parsed for a seq" in str(caught.value), (
        "recovery accepted a journal whose tail it could not read, so the restarted "
        "queue would seed the command stream at zero beside the numbers already on disk"
    )


def test_recovery_reads_the_newest_rotated_generation_when_the_live_file_is_fresh(
    tmp_path: Path,
) -> None:
    """Rotation leaves the live file holding nothing but its header; the
    previous process's last committed commands are then in generation ``.1``,
    and that is where the sequence has to resume from."""
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_queue)
    for index in range(5):
        writer.append(make_command(seq=index, idempotency_key=f"k{index}").to_dict())
    writer.rotate()
    writer.close()
    # The rotation left a freshly headed live file behind.
    header = read_header(layout.command_queue)
    assert header is not None and header.end_offset == layout.command_queue.stat().st_size

    queue = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=FakeClock())
    outcome = queue.submit(
        queue.build(ActionName.WORLD_INSPECT, idempotency_key="k-new", lease_ms=10_000)
    )
    assert outcome.accepted
    assert outcome.command.seq == 5
    assert [c.seq for c in _written_commands(queue)] == [5]
    queue.close()


def test_an_absent_journal_still_starts_the_sequence_at_zero(tmp_path: Path) -> None:
    """Today's behaviour, pinned: a stream that never existed starts at 0."""
    queue = _queue(tmp_path, FakeClock())
    outcome = queue.submit(
        queue.build(ActionName.WORLD_INSPECT, idempotency_key="k", lease_ms=10_000)
    )
    assert outcome.accepted
    assert outcome.command.seq == 0
    assert [c.seq for c in _written_commands(queue)] == [0]
    queue.close()


def test_another_sessions_records_seed_nothing(tmp_path: Path) -> None:
    """A journal full of a *different* session's commands is not this stream's
    past: that stream ended with its session, and this one honestly begins at
    zero rather than continuing someone else's numbering."""
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_queue)
    foreign = "00000000-0000-0000-0000-0000000000ff"
    for index in range(3):
        writer.append(
            make_command(seq=index, session_id=foreign, idempotency_key=f"f{index}").to_dict()
        )
    writer.close()

    queue = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=FakeClock())
    outcome = queue.submit(
        queue.build(ActionName.WORLD_INSPECT, idempotency_key="k", lease_ms=10_000)
    )
    assert outcome.accepted
    assert outcome.command.seq == 0
    queue.close()


def test_an_unreadable_tail_refuses_construction_instead_of_inventing_zero(
    tmp_path: Path,
) -> None:
    """Bytes are on disk but not one record yields a seq. Seeding 0 over that
    would be the restarted-second-producer lie again, wearing a diagnostic."""
    layout = make_layout(tmp_path)
    JournalWriter(layout, layout.command_queue).close()  # a valid header line
    with layout.command_queue.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("this is not a record\n")
        handle.write('{"type": "command", "no_seq_here": true}\n')

    with pytest.raises(JournalError, match=r"command\.queue\.0001\.jsonl"):
        CommandQueue(layout, session_id=IPC_SESSION_ID, clock=FakeClock())


def test_a_journal_without_a_readable_header_is_refused_not_restarted(tmp_path: Path) -> None:
    """The writer's own resume logic would rotate such a file aside and begin a
    fresh serial — with the sequence back at 0. Recovery runs first and refuses,
    because a file whose header never committed hides how far the stream got."""
    layout = make_layout(tmp_path)
    layout.command_queue.write_text('{"seq": 3}\n', encoding="utf-8")

    with pytest.raises(JournalError, match=r"command\.queue\.0001\.jsonl"):
        CommandQueue(layout, session_id=IPC_SESSION_ID, clock=FakeClock())


def test_recovery_reads_a_bounded_tail_not_the_whole_journal(tmp_path: Path) -> None:
    """The proof is behavioural, not timed: a rogue high seq buried at the
    *top* of an oversized journal is never seen, because only the tail — where
    a monotonic producer's highest number genuinely lives — is read."""
    layout = make_layout(tmp_path)
    budget = 8 * RECOVERY_TAIL_BYTES
    writer = JournalWriter(layout, layout.command_queue, max_bytes=budget)
    writer.append(make_command(seq=10_000_000, idempotency_key="rogue").to_dict())
    seq = 0
    while writer.size <= RECOVERY_TAIL_BYTES + 4_096:
        writer.append(make_command(seq=seq, idempotency_key=f"k{seq}").to_dict())
        seq += 1
    writer.close()
    highest = seq - 1

    queue = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=FakeClock(), max_bytes=budget)
    outcome = queue.submit(
        queue.build(ActionName.WORLD_INSPECT, idempotency_key="k-new", lease_ms=10_000)
    )
    assert outcome.accepted
    assert outcome.command.seq == highest + 1

    # And the record on disk carries the same number: read the whole file the
    # slow way, which a test may afford and the recovery may not.
    reader = JournalReader(queue.layout, queue.layout.command_queue)
    last_seq: int | None = None
    while True:
        read = reader.read()
        if not read.records:
            break
        last_seq = read.records[-1].payload["seq"]
    assert last_seq == highest + 1
    queue.close()


# --------------------------------------------------------------------------
# acks for commands this process never shipped
# --------------------------------------------------------------------------


def test_an_ack_for_a_command_this_process_never_sent_is_noted_and_ignored(
    tmp_path: Path,
) -> None:
    """§3.12: after a restart the mod may still be answering the *previous*
    process's in-flight command. That is honest work finishing — it must land
    as a diagnostic, not a crash, and must not be filed under anyone's key."""
    clock = FakeClock()
    layout = make_layout(tmp_path)
    queue = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=clock)
    mine = queue.build(ActionName.CONSUME_EAT, idempotency_key="mine", lease_ms=10_000)
    assert queue.submit(mine).accepted

    ghost = make_command(idempotency_key="ghost")  # the dead process's command
    publish_acks(layout, [make_success(ghost, seq=0)])
    poll = queue.poll_acks()

    assert [r.command_id for r in poll.results] == [ghost.command_id]
    assert any("not tracking" in diagnostic.detail for diagnostic in poll.diagnostics)
    # Nothing this process owns moved: the in-flight slot is still claimed by
    # our command, and neither key was invented into the cache.
    assert queue.in_flight is not None
    assert queue.in_flight.command_id == mine.command_id
    assert queue.cache.replay("ghost") is None
    assert queue.cache.replay("mine") is None
    queue.close()


def test_record_ack_for_an_unknown_command_changes_nothing(tmp_path: Path) -> None:
    queue = _queue(tmp_path, FakeClock())
    queue.record_ack(make_success(make_command(idempotency_key="ghost")))
    assert queue.in_flight is None
    assert queue.pending == ()
    assert len(queue.cache) == 0
    queue.close()
