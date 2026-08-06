"""Traces: what is recorded, what is refused, and what replays."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from pz_agent_core.diagnostics.log import LogLimits
from pz_agent_core.diagnostics.redaction import USER_DIR_PLACEHOLDER, build_redactor
from pz_agent_core.diagnostics.trace import (
    TraceEntry,
    TraceError,
    TraceKind,
    TraceWriter,
    read_trace,
    replay_observations,
)
from pz_agent_core.observation.diff import diff_observations
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    Command,
    InventoryView,
    ReasonCode,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    backpack_container_ref,
    item_ref,
    main_container_ref,
    make_container,
    make_item,
    make_observation,
    make_player,
)
from tests.fixtures.cli_worlds import StepClock
from tests.fixtures.platform_trees import CYRILLIC_USER

COMMAND_ID = str(uuid.UUID(int=0xC0FFEE))


def _writer(tmp_path: Path, limits: LogLimits | None = None) -> TraceWriter:
    home = tmp_path / "Users" / CYRILLIC_USER
    return TraceWriter(
        tmp_path / "traces" / "session.jsonl",
        redactor=build_redactor(user_dir=home / "Zomboid", home_dir=home),
        clock=StepClock(step_ms=100),
        limits=limits or LogLimits(max_bytes=256 * 1024, keep=2, max_record_bytes=64 * 1024),
    )


def _command(action: ActionName = ActionName.ACTION_WAIT) -> Command:
    return Command(
        session_id=DEFAULT_SESSION,
        seq=4,
        command_id=COMMAND_ID,
        idempotency_key="goal-1-step-1",
        issued_at_ms=1_700_000_000_000,
        lease_ms=15_000,
        action=action,
        args={"duration_ms": 500},
    )


def _result(command: Command) -> ActionResult:
    return ActionResult.succeeded(
        session_id=command.session_id,
        seq=5,
        command_id=command.command_id,
        action=command.action.value,
        timestamp_ms=1_700_000_001_000,
        evidence={"waited_ms": 500},
    )


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def test_an_action_entry_carries_the_command_and_the_terminal_result(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    command = _command()

    entry = writer.record_action(command, _result(command))

    assert entry.kind is TraceKind.ACTION
    assert entry.payload["action"] == ActionName.ACTION_WAIT.value
    assert entry.payload["status"] == "succeeded"
    assert entry.payload["reason_code"] == ReasonCode.POSTCONDITION_MET.value
    assert entry.summary().startswith("action action.wait -> succeeded")


def test_an_action_without_a_result_records_the_command_alone(tmp_path: Path) -> None:
    writer = _writer(tmp_path)

    entry = writer.record_action(_command())

    assert "result" not in entry.payload
    assert entry.summary().endswith("no-result")


def test_entries_are_numbered_from_zero_without_gaps(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    command = _command()

    seqs = [writer.record_action(command).seq for _ in range(4)]

    assert seqs == [0, 1, 2, 3]
    assert writer.entries_written == 4


def test_an_oversize_entry_becomes_a_dropped_marker_keeping_the_sequence(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path, LogLimits(max_bytes=8192, keep=1, max_record_bytes=512))

    first = writer.record_action(_command())
    oversize = writer.record_observation(make_observation())

    assert first.kind is TraceKind.ACTION
    assert oversize.kind is TraceKind.DROPPED
    assert oversize.seq == 1
    assert oversize.payload["reason"] == "oversize"
    assert oversize.payload["original_kind"] == TraceKind.OBSERVATION.value


def test_a_path_in_a_recorded_command_is_redacted_on_the_way_in(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    home = tmp_path / "Users" / CYRILLIC_USER
    command = Command(
        session_id=DEFAULT_SESSION,
        seq=4,
        command_id=COMMAND_ID,
        idempotency_key="k",
        issued_at_ms=1,
        lease_ms=15_000,
        action=ActionName.ACTION_WAIT,
        args={"note": str(home / "Zomboid" / "Lua")},
    )

    writer.record_action(command)

    text = writer.path.read_text(encoding="utf-8")
    assert CYRILLIC_USER not in text
    assert USER_DIR_PLACEHOLDER in text


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def test_reading_skips_a_malformed_line_and_reports_it(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.record_action(_command())
    with writer.path.open("a", encoding="utf-8") as handle:
        handle.write("{oops\n")
        handle.write(json.dumps({"kind": "nonsense", "seq": 9, "timestamp_ms": 1}) + "\n")
    writer.record_action(_command())

    read = read_trace(writer.path)

    assert len(read.entries) == 2
    assert len(read.problems) == 2
    assert "not JSON" in read.problems[0]
    assert "unknown trace kind" in read.problems[1]


def test_reading_stops_at_the_entry_bound_and_says_so(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    for _ in range(5):
        writer.record_action(_command())

    read = read_trace(writer.path, max_entries=2)

    assert len(read.entries) == 2
    assert read.truncated


def test_reading_a_missing_trace_raises_with_the_path(tmp_path: Path) -> None:
    with pytest.raises(TraceError, match="cannot read trace"):
        read_trace(tmp_path / "absent.jsonl")


def test_an_entry_missing_a_field_is_rejected_whole() -> None:
    with pytest.raises(TraceError, match="'seq' must be an integer"):
        TraceEntry.from_dict({"kind": "action", "timestamp_ms": 1, "payload": {}})


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def test_a_snapshot_and_its_diffs_replay_to_the_observed_series(tmp_path: Path) -> None:
    writer = _writer(
        tmp_path, LogLimits(max_bytes=1024 * 1024, keep=1, max_record_bytes=512 * 1024)
    )
    first = make_observation(seq=1)
    second = make_observation(seq=2, player=make_player(stats={"hunger": 0.55}))
    writer.record_observation(first)
    writer.record_observation_diff(from_seq=1, to_seq=2, diff=diff_observations(first, second))

    steps = replay_observations(read_trace(writer.path).entries)

    assert [step.observation.seq for step in steps] == [1, 2]
    assert steps[1].observation.player.stats["hunger"] == 0.55


def test_a_nested_inventory_diff_survives_the_redaction_depth_cap(tmp_path: Path) -> None:
    """The deepest document written here, and the one the depth cap is sized for.

    A diff wraps every level of the snapshot in a ``nested`` object, so an
    inventory change is roughly twice as deep as the inventory itself. If the
    cap elided the changed value, replay would fail rather than lie — but it
    would still be a trace that cannot be replayed, so the bound is asserted
    against the real shape.
    """
    writer = _writer(
        tmp_path, LogLimits(max_bytes=1024 * 1024, keep=1, max_record_bytes=512 * 1024)
    )
    main = main_container_ref()
    bag = backpack_container_ref()
    before = make_observation(
        seq=1,
        inventory=InventoryView(
            containers=[make_container(main), make_container(bag, parent_ref=main)],
            items=[make_item(item_ref("9002", "worn:Back:99001"), bag, weight=0.2)],
        ),
    )
    after = make_observation(
        seq=2,
        inventory=InventoryView(
            containers=[make_container(main), make_container(bag, parent_ref=main)],
            items=[make_item(item_ref("9002", "worn:Back:99001"), bag, weight=0.9)],
        ),
    )
    writer.record_observation(before)
    writer.record_observation_diff(from_seq=1, to_seq=2, diff=diff_observations(before, after))

    steps = replay_observations(read_trace(writer.path).entries)

    inventory = steps[1].observation.inventory
    assert inventory is not None, "the replayed observation lost its inventory"
    assert inventory.items[0].weight == 0.9


def test_a_diff_with_no_preceding_snapshot_refuses_rather_than_guessing(
    tmp_path: Path,
) -> None:
    writer = _writer(
        tmp_path, LogLimits(max_bytes=1024 * 1024, keep=1, max_record_bytes=512 * 1024)
    )
    first = make_observation(seq=1)
    second = make_observation(seq=2, player=make_player(stats={"hunger": 0.55}))
    writer.record_observation_diff(from_seq=1, to_seq=2, diff=diff_observations(first, second))

    with pytest.raises(TraceError, match="no preceding full snapshot"):
        replay_observations(read_trace(writer.path).entries)


def test_a_corrupt_snapshot_payload_is_named_by_its_entry(tmp_path: Path) -> None:
    entry = TraceEntry(
        seq=3,
        timestamp_ms=1,
        kind=TraceKind.OBSERVATION,
        payload={"observation": {"session_id": "not-a-uuid"}},
    )

    with pytest.raises(TraceError, match="trace entry 3: unusable observation"):
        replay_observations([entry])


def test_action_entries_do_not_take_part_in_the_observation_replay(tmp_path: Path) -> None:
    writer = _writer(
        tmp_path, LogLimits(max_bytes=1024 * 1024, keep=1, max_record_bytes=512 * 1024)
    )
    writer.record_observation(make_observation(seq=1))
    writer.record_action(_command())

    read = read_trace(writer.path)

    assert len(replay_observations(read.entries)) == 1
    assert len(read.of_kind(TraceKind.ACTION)) == 1
