"""Journal behaviour under the conditions §3.5 actually cares about.

Every test here is a partial-failure case: a line that is half written, a line
that is complete but corrupt, a file that rotated while nobody was reading. The
happy path is the easy part; these are the reasons the reader is stateful.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pz_agent_core.ipc.atomic import IpcPathError
from pz_agent_core.ipc.journal import (
    MAX_LINE_BYTES,
    JournalError,
    JournalReader,
    JournalWriter,
    read_header,
    rotated_path,
)
from tests.fixtures.ipc_builders import FakeClock, make_layout


def _writer(tmp_path: Path, **kwargs: object) -> tuple[JournalWriter, JournalReader]:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack, **kwargs)  # type: ignore[arg-type]
    reader = JournalReader(layout, layout.command_ack)
    return writer, reader


def test_records_round_trip_in_order(tmp_path: Path) -> None:
    writer, reader = _writer(tmp_path)
    for index in range(3):
        writer.append({"n": index})
    writer.close()

    read = reader.read()
    assert [record.payload["n"] for record in read.records] == [0, 1, 2]
    assert read.offset == writer.size
    assert read.pending_bytes == 0
    assert not read.rotated


def test_second_read_returns_only_new_records(tmp_path: Path) -> None:
    writer, reader = _writer(tmp_path)
    writer.append({"n": 0})
    first = reader.read()
    writer.append({"n": 1})
    second = reader.read()
    writer.close()

    assert [r.payload["n"] for r in first.records] == [0]
    assert [r.payload["n"] for r in second.records] == [1]
    assert second.offset > first.offset


def test_partial_trailing_line_is_ignored_until_it_is_finished(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack)
    writer.append({"n": 0})
    writer.close()
    reader = JournalReader(layout, layout.command_ack)
    assert len(reader.read().records) == 1
    offset_before = reader.offset

    with layout.command_ack.open("a", encoding="utf-8") as handle:
        handle.write('{"n": 1, "half": ')

    torn = reader.read()
    assert torn.records == ()
    assert reader.offset == offset_before
    assert torn.pending_bytes > 0

    with layout.command_ack.open("a", encoding="utf-8") as handle:
        handle.write('"written"}\n')

    completed = reader.read()
    assert [r.payload["n"] for r in completed.records] == [1]
    assert completed.records[0].payload["half"] == "written"
    assert completed.pending_bytes == 0


def test_corrupt_line_is_reported_and_skipped(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack)
    writer.append({"n": 0})
    writer.close()
    with layout.command_ack.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all}\n")
        handle.write('{"n": 2}\n')

    read = JournalReader(layout, layout.command_ack).read()
    assert [r.payload["n"] for r in read.records] == [0, 2]
    assert len(read.diagnostics) == 1
    assert "corrupt record" in read.diagnostics[0].detail
    assert read.diagnostics[0].excerpt.startswith("{not json")


def test_a_json_scalar_line_is_corrupt_not_a_record(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    JournalWriter(layout, layout.command_ack).close()
    with layout.command_ack.open("a", encoding="utf-8") as handle:
        handle.write("42\n")

    read = JournalReader(layout, layout.command_ack).read()
    assert read.records == ()
    assert "expected an object" in read.diagnostics[0].detail


def test_a_corrupt_line_does_not_stall_the_reader(tmp_path: Path) -> None:
    """The offset must move past a bad line, or every later record is hostage."""
    layout = make_layout(tmp_path)
    JournalWriter(layout, layout.command_ack).close()
    reader = JournalReader(layout, layout.command_ack)
    with layout.command_ack.open("a", encoding="utf-8") as handle:
        handle.write("]]not json\n")
    first = reader.read()
    assert first.diagnostics and first.records == ()

    with layout.command_ack.open("a", encoding="utf-8") as handle:
        handle.write('{"n": 7}\n')
    second = reader.read()
    assert [r.payload["n"] for r in second.records] == [7]
    assert second.diagnostics == ()


def test_unterminated_oversized_line_is_skipped_rather_than_awaited(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    JournalWriter(layout, layout.command_ack).close()
    reader = JournalReader(layout, layout.command_ack)
    with layout.command_ack.open("a", encoding="utf-8") as handle:
        handle.write("x" * (MAX_LINE_BYTES + 10))

    read = reader.read()
    assert read.records == ()
    assert "unterminated line" in read.diagnostics[0].detail
    assert reader.offset == MAX_LINE_BYTES + 10 + read.diagnostics[0].offset


def test_byte_offset_can_be_persisted_and_resumed(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack)
    for index in range(4):
        writer.append({"n": index})
    reader = JournalReader(layout, layout.command_ack)
    reader.max_records = 2
    first = reader.read()
    assert [r.payload["n"] for r in first.records] == [0, 1]

    resumed = JournalReader(layout, layout.command_ack)
    resumed.resume(offset=first.offset, serial=first.serial)
    assert [r.payload["n"] for r in resumed.read().records] == [2, 3]
    writer.close()


def test_seek_to_end_skips_everything_already_written(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_queue)
    writer.append({"n": 0})
    reader = JournalReader(layout, layout.command_queue)
    reader.seek_to_end()
    assert reader.read().records == ()
    writer.append({"n": 1})
    assert [r.payload["n"] for r in reader.read().records] == [1]
    writer.close()


def test_rotation_is_detected_and_no_record_is_lost(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack, keep=2)
    writer.append({"n": 0})
    reader = JournalReader(layout, layout.command_ack, keep=2)
    assert [r.payload["n"] for r in reader.read().records] == [0]

    writer.append({"n": 1})
    writer.rotate()
    writer.append({"n": 2})

    read = reader.read()
    assert read.rotated
    assert read.rotations == 1
    assert not read.lost_records
    # The record written before the rotation is still delivered, in order.
    assert [r.payload["n"] for r in read.records] == [1, 2]
    assert read.serial == writer.serial
    writer.close()


def test_two_rotations_between_polls_are_drained_in_order(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack, keep=3)
    reader = JournalReader(layout, layout.command_ack, keep=3)
    writer.append({"n": 0})
    reader.read()

    writer.append({"n": 1})
    writer.rotate()
    writer.append({"n": 2})
    writer.rotate()
    writer.append({"n": 3})

    read = reader.read()
    assert read.rotations == 2
    assert [r.payload["n"] for r in read.records] == [1, 2, 3]
    assert not read.lost_records
    writer.close()


def test_pruned_generations_are_reported_rather_than_hidden(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack, keep=1)
    reader = JournalReader(layout, layout.command_ack, keep=1)
    writer.append({"n": 0})
    reader.read()

    writer.append({"lost": True})
    writer.rotate()
    writer.append({"also_lost": True})
    writer.rotate()
    writer.append({"n": 3})

    read = reader.read()
    assert read.lost_records
    assert read.lost_serials == (0,)
    assert any("pruned" in diagnostic.detail for diagnostic in read.diagnostics)
    # What survived is still delivered; only the pruned generation is missing.
    assert [r.payload for r in read.records] == [{"also_lost": True}, {"n": 3}]
    writer.close()


def test_rotation_keeps_a_bounded_number_of_files(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack, keep=2)
    for index in range(5):
        writer.append({"n": index})
        writer.rotate()
    writer.close()

    assert rotated_path(layout.command_ack, 1).exists()
    assert rotated_path(layout.command_ack, 2).exists()
    assert not rotated_path(layout.command_ack, 3).exists()


def test_size_cap_triggers_rotation(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack, max_bytes=MAX_LINE_BYTES, keep=1)
    reader = JournalReader(layout, layout.command_ack, keep=1)
    reader.read()
    blob = "x" * (MAX_LINE_BYTES // 2)
    writer.append({"blob": blob})
    assert writer.serial == 0
    writer.append({"blob": blob})
    assert writer.serial == 1
    writer.close()

    read = reader.read()
    assert read.rotated
    assert len(read.records) == 2


def test_a_rotation_marker_is_written_into_the_outgoing_file(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack, keep=1)
    writer.append({"n": 0})
    writer.rotate()
    writer.close()

    retired = rotated_path(layout.command_ack, 1).read_text(encoding="utf-8").splitlines()
    assert '"journal.rotated"' in retired[-1]
    assert read_header(layout.command_ack) is not None


def test_structural_markers_are_never_delivered_as_records(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack, keep=1)
    writer.append({"n": 0})
    writer.rotate()
    writer.append({"n": 1})
    writer.close()

    read = JournalReader(layout, layout.command_ack, keep=1).read()
    assert all("journal." not in str(record.payload.get("type")) for record in read.records)


def test_reserved_record_types_are_refused(tmp_path: Path) -> None:
    writer, _ = _writer(tmp_path)
    with pytest.raises(JournalError, match="reserved"):
        writer.append({"type": "journal.header", "serial": 99})
    writer.close()


def test_oversized_record_is_refused_by_the_writer(tmp_path: Path) -> None:
    writer, _ = _writer(tmp_path)
    with pytest.raises(JournalError, match="exceeds"):
        writer.append({"blob": "x" * (MAX_LINE_BYTES + 1)})
    writer.close()


def test_a_headerless_file_is_retired_instead_of_being_appended_to(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.command_ack.write_text('{"n": "written by something else"}\n', encoding="utf-8")
    writer = JournalWriter(layout, layout.command_ack, keep=1)
    writer.append({"n": 0})
    writer.close()

    assert rotated_path(layout.command_ack, 1).exists()
    read = JournalReader(layout, layout.command_ack, keep=1).read()
    assert [r.payload["n"] for r in read.records] == [0]


def test_a_writer_resumes_an_existing_journal(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    first = JournalWriter(layout, layout.command_ack)
    first.append({"n": 0})
    first.close()

    second = JournalWriter(layout, layout.command_ack)
    assert second.serial == 0
    second.append({"n": 1})
    second.close()

    read = JournalReader(layout, layout.command_ack).read()
    assert [r.payload["n"] for r in read.records] == [0, 1]


def test_paths_outside_the_layout_are_refused(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    with pytest.raises(IpcPathError):
        JournalWriter(layout, tmp_path / "somewhere.jsonl")
    with pytest.raises(IpcPathError):
        JournalReader(layout, tmp_path / "somewhere.jsonl")


def test_reading_a_journal_that_does_not_exist_yet_is_not_an_error(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    read = JournalReader(layout, layout.observation_events).read()
    assert read.records == ()
    assert read.serial is None
    assert read.offset == 0


def test_header_carries_the_writer_clock(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    writer = JournalWriter(layout, layout.command_ack, clock=clock)
    writer.close()
    first_line = layout.command_ack.read_text(encoding="utf-8").splitlines()[0]
    assert str(clock.now) in first_line
