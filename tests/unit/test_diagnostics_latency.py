"""The latency instrument: exact percentiles over synthetic journals, no invention.

Every expected number in this file is computed by hand from the offsets the
test wrote, because the instrument's one promise is that its numbers are the
journals' numbers — nearest-rank percentiles over stamps that exist, an
"unmeasured" for everything else, and a typed refusal for a journal it cannot
read whole.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from pz_agent_core.diagnostics import (
    MAX_TRACES,
    LatencyError,
    LatencyReport,
    TargetVerdict,
    collect_latency,
    evaluate_targets,
    nearest_rank,
)
from pz_agent_core.ipc.journal import JournalWriter
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.ipc.snapshot import SnapshotWriter
from pz_agent_core.protocol import ActionName, ActionResult, ActionStatus, Command, ReasonCode
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from tests.fixtures.ipc_builders import (
    BASE_TIME_MS,
    IPC_SESSION_ID,
    FakeClock,
    make_command,
    make_layout,
    publish_acks,
)

# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def _write_commands(layout: IpcLayout, commands: list[Command]) -> None:
    """Append commands to the queue journal the way the sidecar would."""
    writer = JournalWriter(layout, layout.command_queue)
    try:
        for command in commands:
            writer.append(command.to_dict())
    finally:
        writer.close()


def _accepted(command: Command, *, timestamp_ms: int, seq: int = 0) -> ActionResult:
    """The first game-side stamp a command ever gets."""
    return ActionResult(
        session_id=command.session_id,
        seq=seq,
        command_id=command.command_id,
        action=command.action.value,
        status=ActionStatus.ACCEPTED,
        reason_code=ReasonCode.POSTCONDITION_MET,
        timestamp_ms=timestamp_ms,
    )


def _started(
    command: Command, *, timestamp_ms: int, started_at_ms: int, seq: int = 0
) -> ActionResult:
    return ActionResult(
        session_id=command.session_id,
        seq=seq,
        command_id=command.command_id,
        action=command.action.value,
        status=ActionStatus.STARTED,
        reason_code=ReasonCode.POSTCONDITION_MET,
        timestamp_ms=timestamp_ms,
        started_at_ms=started_at_ms,
    )


def _terminal(
    command: Command,
    *,
    timestamp_ms: int,
    status: ActionStatus = ActionStatus.SUCCEEDED,
    seq: int = 0,
) -> ActionResult:
    if status is ActionStatus.SUCCEEDED:
        return ActionResult.succeeded(
            session_id=command.session_id,
            seq=seq,
            command_id=command.command_id,
            action=command.action.value,
            timestamp_ms=timestamp_ms,
            evidence={"observed": True},
        )
    return ActionResult.failure(
        session_id=command.session_id,
        seq=seq,
        command_id=command.command_id,
        action=command.action.value,
        timestamp_ms=timestamp_ms,
        reason_code=ReasonCode.POSTCONDITION_FAILED,
        status=status,
    )


def _scripted_run(layout: IpcLayout) -> None:
    """Twenty commands with acks at known offsets, for exact percentiles.

    Command *i* is issued at BASE + i·1000, accepted 100+i ms later, started
    40 ms after that, terminal 200 ms after that. Every distribution below is
    computable by hand from those four lines.
    """
    commands = []
    acks = []
    for index in range(20):
        issued = BASE_TIME_MS + index * 1000
        command = make_command(issued_at_ms=issued, idempotency_key=f"key-{index}")
        accepted = issued + 100 + index
        started = accepted + 40
        terminal = started + 200
        commands.append(command)
        acks.append(_accepted(command, timestamp_ms=accepted, seq=index * 3))
        acks.append(
            _started(command, timestamp_ms=started, started_at_ms=started, seq=index * 3 + 1)
        )
        acks.append(_terminal(command, timestamp_ms=terminal, seq=index * 3 + 2))
    _write_commands(layout, commands)
    publish_acks(layout, acks)


# --------------------------------------------------------------------------
# nearest-rank percentiles
# --------------------------------------------------------------------------


def test_nearest_rank_returns_a_sample_that_actually_happened() -> None:
    samples = [30, 10, 20, 40, 50]
    assert nearest_rank(samples, 50) == 30
    assert nearest_rank(samples, 95) == 50
    assert nearest_rank(samples, 100) == 50
    assert nearest_rank([7], 50) == 7


def test_nearest_rank_refuses_an_empty_sample_list_and_a_bad_percent() -> None:
    with pytest.raises(ValueError, match="no samples"):
        nearest_rank([], 50)
    with pytest.raises(ValueError, match="percent"):
        nearest_rank([1], 0)


# --------------------------------------------------------------------------
# the joined distributions
# --------------------------------------------------------------------------


def test_scripted_acks_produce_the_exact_expected_percentiles(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    _scripted_run(layout)

    report = collect_latency(layout.root)

    # submit → accepted samples are 100..119: rank ⌈20·0.5⌉ = 10th is 109,
    # rank ⌈20·0.95⌉ = 19th is 118.
    accepted = report.submit_to_accepted
    assert (accepted.count, accepted.minimum, accepted.maximum) == (20, 100, 119)
    assert (accepted.p50, accepted.p95) == (109, 118)
    # The single-clock legs are constants by construction.
    assert (report.accepted_to_started.p50, report.accepted_to_started.p95) == (40, 40)
    assert (report.started_to_terminal.p50, report.started_to_terminal.p95) == (200, 200)
    # submit → terminal samples are 340..359.
    assert (report.submit_to_terminal.p50, report.submit_to_terminal.p95) == (349, 358)
    assert report.pending == 0
    assert len(report.traces) == 20
    assert all(trace.status == "succeeded" for trace in report.traces)


def test_a_command_with_no_ack_is_pending_and_outside_terminal_distributions(
    tmp_path: Path,
) -> None:
    layout = make_layout(tmp_path)
    answered = make_command(issued_at_ms=BASE_TIME_MS, idempotency_key="answered")
    silent = make_command(issued_at_ms=BASE_TIME_MS, idempotency_key="silent")
    _write_commands(layout, [answered, silent])
    publish_acks(
        layout,
        [
            _accepted(answered, timestamp_ms=BASE_TIME_MS + 50),
            _terminal(answered, timestamp_ms=BASE_TIME_MS + 250, seq=1),
        ],
    )

    report = collect_latency(layout.root)

    assert report.pending == 1
    assert report.submit_to_terminal.count == 1
    assert report.submit_to_accepted.count == 1
    unterminated = [trace for trace in report.traces if not trace.terminal]
    assert [trace.command_id for trace in unterminated] == [silent.command_id]
    assert unterminated[0].status == "pending"


def test_the_first_terminal_ack_wins_over_a_redelivered_one(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    command = make_command(issued_at_ms=BASE_TIME_MS)
    _write_commands(layout, [command])
    publish_acks(
        layout,
        [
            _terminal(command, timestamp_ms=BASE_TIME_MS + 150),
            _terminal(command, timestamp_ms=BASE_TIME_MS + 900, seq=1),
        ],
    )

    report = collect_latency(layout.root)

    assert report.submit_to_terminal.count == 1
    assert report.submit_to_terminal.maximum == 150


def test_rotated_command_journal_generations_are_included(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    early = [make_command(issued_at_ms=BASE_TIME_MS, idempotency_key=f"a{i}") for i in range(2)]
    late = [make_command(issued_at_ms=BASE_TIME_MS, idempotency_key=f"b{i}") for i in range(2)]
    writer = JournalWriter(layout, layout.command_queue)
    try:
        for command in early:
            writer.append(command.to_dict())
        writer.rotate()
        for command in late:
            writer.append(command.to_dict())
    finally:
        writer.close()

    report = collect_latency(layout.root)

    traced = {trace.command_id for trace in report.traces}
    assert traced == {command.command_id for command in early + late}


def test_an_ack_for_an_untraced_command_is_counted_not_guessed_about(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    orphan = make_command(issued_at_ms=BASE_TIME_MS)
    publish_acks(layout, [_terminal(orphan, timestamp_ms=BASE_TIME_MS + 100)])

    report = collect_latency(layout.root)

    assert report.unmatched_acks == 1
    assert report.traces == ()


def test_the_trace_cap_keeps_the_newest_commands_and_counts_the_dropped(
    tmp_path: Path,
) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_queue)
    try:
        for index in range(MAX_TRACES + 1):
            writer.append(
                {"command_id": f"cmd-{index}", "action": "consume.eat", "issued_at_ms": index}
            )
    finally:
        writer.close()

    report = collect_latency(layout.root)

    assert len(report.traces) == MAX_TRACES
    assert report.dropped_commands == 1
    traced = {trace.command_id for trace in report.traces}
    assert "cmd-0" not in traced
    assert f"cmd-{MAX_TRACES}" in traced


def test_a_truncated_journal_tail_is_a_typed_refusal(tmp_path: Path) -> None:
    """A journal that provably lost its last record is refused, never averaged."""
    layout = make_layout(tmp_path)
    _write_commands(layout, [make_command(issued_at_ms=BASE_TIME_MS)])
    with layout.command_queue.open("ab") as handle:
        handle.write(b'{"cut": ')

    with pytest.raises(LatencyError, match="cannot measure from"):
        collect_latency(layout.root)


def test_an_empty_and_a_missing_directory_measure_nothing(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    empty = collect_latency(layout.root)
    missing = collect_latency(tmp_path / "never-created")

    for report in (empty, missing):
        assert report.traces == ()
        assert report.submit_to_accepted.count == 0
        assert report.observation_intervals.count == 0
        assert report.implied_observation_hz is None
        assert all(check.verdict is TargetVerdict.UNMEASURED for check in evaluate_targets(report))


# --------------------------------------------------------------------------
# observations, heartbeats, pointer
# --------------------------------------------------------------------------


def _publish_observations(layout: IpcLayout, points: list[tuple[int, int]]) -> None:
    writer = JournalWriter(layout, layout.observation_events)
    try:
        for seq, timestamp_ms in points:
            writer.append({"seq": seq, "timestamp_ms": timestamp_ms})
    finally:
        writer.close()


def test_observation_intervals_count_only_adjacent_seqs(tmp_path: Path) -> None:
    """A seq gap spans an unknown number of publications; no delta is invented."""
    layout = make_layout(tmp_path)
    t0 = BASE_TIME_MS
    _publish_observations(
        layout,
        [(0, t0), (1, t0 + 250), (2, t0 + 500), (3, t0 + 750), (4, t0 + 1000), (6, t0 + 1600)],
    )
    # A snapshot slot with the next seq joins the same stream.
    SnapshotWriter(layout, clock=FakeClock(t0 + 1850)).publish(
        {"seq": 7, "timestamp_ms": t0 + 1850}
    )

    report = collect_latency(layout.root)

    # Four adjacent journal pairs plus the 6→7 slot pair; 4→6 yields nothing.
    assert report.observation_intervals.count == 5
    assert (report.observation_intervals.p50, report.observation_intervals.p95) == (250, 250)
    assert report.observation_points == 7
    assert report.implied_observation_hz == 4.0


def test_heartbeats_are_facts_and_their_cadence_is_honestly_unmeasured(
    tmp_path: Path,
) -> None:
    """One overwritten file per peer holds one beat; a rate needs a live watcher."""
    layout = make_layout(tmp_path)
    clock = FakeClock()
    monitor = HeartbeatMonitor(layout, clock=clock)
    monitor.publish(Peer.GAME, session_id=IPC_SESSION_ID, nonce="a" * 32, version="0.1.0")

    report = collect_latency(layout.root)

    assert [fact.peer for fact in report.heartbeats] == ["game"]
    assert report.heartbeats[0].timestamp_ms == BASE_TIME_MS
    assert report.game_heartbeat_intervals.count == 0
    document = report.to_dict()
    game = document["distributions"]["heartbeat_intervals"]["game"]
    assert "one beat per peer" in game["note"]


def test_the_snapshot_pointer_written_at_is_surfaced(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    SnapshotWriter(layout, clock=FakeClock(BASE_TIME_MS + 5)).publish(
        {"seq": 0, "timestamp_ms": BASE_TIME_MS}
    )

    report = collect_latency(layout.root)

    assert report.pointer_written_at_ms == BASE_TIME_MS + 5


# --------------------------------------------------------------------------
# the document and its cross-clock labels
# --------------------------------------------------------------------------


def test_cross_clock_intervals_are_labelled_in_the_json_document(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    _scripted_run(layout)

    document: dict[str, Any] = collect_latency(layout.root).to_dict()

    distributions = document["distributions"]
    for cross in ("submit_to_accepted", "submit_to_terminal", "safety_submit_to_terminal"):
        assert distributions[cross]["cross_clock"] is True
        assert "two clocks" in distributions[cross]["cross_clock_note"]
    for same in ("accepted_to_started", "started_to_terminal", "observation_intervals"):
        assert distributions[same]["cross_clock"] is False
        assert "cross_clock_note" not in distributions[same]


def test_the_empty_report_serialises_with_every_statistic_absent() -> None:
    document = LatencyReport.empty().to_dict()
    accepted = document["distributions"]["submit_to_accepted"]
    assert accepted["count"] == 0
    assert accepted["p95_ms"] is None
    assert document["observation"]["implied_hz"] is None


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------


def _by_name(report: LatencyReport) -> dict[str, TargetVerdict]:
    return {check.name: check.verdict for check in evaluate_targets(report)}


def test_targets_are_met_when_the_measured_p95_is_inside_them(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    stop = Command(
        session_id=IPC_SESSION_ID,
        seq=0,
        command_id=str(uuid.uuid4()),
        idempotency_key="stop-1",
        issued_at_ms=BASE_TIME_MS,
        lease_ms=5_000,
        action=ActionName.SAFETY_STOP,
    )
    _write_commands(layout, [stop])
    publish_acks(
        layout,
        [
            _accepted(stop, timestamp_ms=BASE_TIME_MS + 60),
            _terminal(stop, timestamp_ms=BASE_TIME_MS + 150, status=ActionStatus.CANCELLED, seq=1),
        ],
    )
    _publish_observations(layout, [(0, BASE_TIME_MS), (1, BASE_TIME_MS + 200)])

    verdicts = _by_name(collect_latency(layout.root))

    assert verdicts["submit_to_accepted"] is TargetVerdict.MET
    assert verdicts["safety_reaction"] is TargetVerdict.MET
    assert verdicts["observation_rate"] is TargetVerdict.MET
    # Visibility has no on-disk stamp for when the sidecar read the ack, so it
    # stays unmeasured even on a directory full of data.
    assert verdicts["terminal_ack_visibility"] is TargetVerdict.UNMEASURED


def test_a_slow_accepted_ack_is_a_measured_miss_not_a_shrug(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    command = make_command(issued_at_ms=BASE_TIME_MS)
    _write_commands(layout, [command])
    publish_acks(layout, [_accepted(command, timestamp_ms=BASE_TIME_MS + 400)])

    checks = {check.name: check for check in evaluate_targets(collect_latency(layout.root))}

    assert checks["submit_to_accepted"].verdict is TargetVerdict.MISSED
    assert checks["submit_to_accepted"].measured_p95_ms == 400
    assert checks["safety_reaction"].verdict is TargetVerdict.UNMEASURED
