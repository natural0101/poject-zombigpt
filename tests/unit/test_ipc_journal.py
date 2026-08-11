"""Journal behaviour under the conditions §3.5 actually cares about.

Every test here is a partial-failure case: a line that is half written, a line
that is complete but corrupt, a file that rotated while nobody was reading. The
happy path is the easy part; these are the reasons the reader is stateful.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pz_agent_core.ipc.atomic import IpcPathError
from pz_agent_core.ipc.journal import (
    MAX_LINE_BYTES,
    JournalError,
    JournalReader,
    JournalWriter,
    probe_truncation,
    read_header,
    rotated_path,
)
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import SessionMode
from tests.fixtures.ipc_builders import FakeClock, make_layout
from tests.fixtures.sidecar_worlds import ScriptedMod, arm_for_real, attached_world


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


def test_a_far_future_header_serial_costs_bounded_work(tmp_path: Path) -> None:
    """The serial gap the reader walks is bounded by `keep`, not by the header.

    ``probe_header`` accepts any non-negative integer serial, and the file it
    reads is written by the mod, not by this process. Walking every serial
    between the reader's own and the header's turned a ~100-byte file into one
    ``lost`` entry per missing generation — measured at 4.7 GiB and minutes of
    CPU for a header claiming serial 20,000,000. Only ``keep`` generations can
    still exist on disk, so anything older is reported as pruned without being
    enumerated.
    """
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack, keep=3)
    reader = JournalReader(layout, layout.command_ack, keep=3)
    writer.append({"n": 0})
    reader.read()
    writer.close()

    header = json.dumps({"type": "journal.header", "serial": 20_000_000}) + "\n"
    layout.command_ack.write_text(header + json.dumps({"n": 1}) + "\n", encoding="utf-8")

    read = reader.read()

    assert read.lost_records
    assert len(read.lost_serials) <= reader.keep + 1
    assert len(read.diagnostics) <= reader.keep + 1
    # The live generation is still delivered: the bound drops enumeration of
    # generations that cannot exist, not records that do.
    assert [r.payload for r in read.records] == [{"n": 1}]
    assert read.serial == 20_000_000


def test_a_journal_recreated_at_an_earlier_serial_reports_the_loss(tmp_path: Path) -> None:
    """A serial that goes *backwards* is a recreated stream, not a quiet poll.

    Serials only climb, so a live file numbered below the generation the reader
    is on cannot be the same stream: it was truncated and started again. The
    mod does exactly that when it cannot read back the header a previous game
    session left — ``journalState`` falls back to serial 0 and the next append
    rewrites the file from byte zero — and everything this reader had not yet
    consumed goes with it. Before this was recognised the poll delivered the new
    file's records with ``rotations == 0`` and no lost serials at all, which is
    the silent loss the rotation machinery exists to prevent, one branch over.
    """
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack, keep=0)
    reader = JournalReader(layout, layout.command_ack, keep=0)
    writer.append({"n": 0})
    writer.rotate()
    writer.append({"n": 1})
    assert [r.payload for r in reader.read().records] == [{"n": 1}]
    assert reader.serial == 1
    writer.append({"never_read": True})
    writer.close()

    header = json.dumps({"type": "journal.header", "serial": 0, "created_at_ms": 1}) + "\n"
    layout.command_ack.write_text(header + json.dumps({"n": 2}) + "\n", encoding="utf-8")

    read = reader.read()

    assert read.lost_records, "a recreated stream loses whatever was not read yet"
    assert read.lost_serials == (1,)
    assert any("backwards" in diagnostic.detail for diagnostic in read.diagnostics)
    # The new generation is still delivered whole: the loss is reported, not
    # turned into a refusal to read the file that is actually there.
    assert [r.payload for r in read.records] == [{"n": 2}]
    assert read.serial == 0


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


def test_one_poll_delivers_at_most_max_records(tmp_path: Path) -> None:
    """The per-poll budget is what keeps a backlog from arriving as one heap."""
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack)
    for index in range(10):
        writer.append({"n": index})
    writer.close()

    reader = JournalReader(layout, layout.command_ack, max_records=3)
    delivered: list[int] = []
    polls = 0
    while polls < 10:
        read = reader.read()
        assert len(read.records) <= 3
        if not read.records:
            break
        delivered.extend(int(record.payload["n"]) for record in read.records)
        polls += 1
    # Bounded per poll, and nothing is lost or repeated across the polls.
    assert delivered == list(range(10))
    assert polls == 4


def test_a_record_budget_of_zero_is_refused(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    with pytest.raises(ValueError, match="max_records"):
        JournalReader(layout, layout.command_ack, max_records=0)


def test_an_unreadable_journal_is_reported_not_answered_with_silence(tmp_path: Path) -> None:
    """An I/O failure must not look like "the stream is quiet"."""
    layout = make_layout(tmp_path)
    layout.command_ack.mkdir()  # something is occupying the name; opening it fails
    read = JournalReader(layout, layout.command_ack).read()

    assert read.records == ()
    assert len(read.diagnostics) == 1
    assert "unreadable" in read.diagnostics[0].detail


def test_a_file_that_never_had_a_header_is_reported(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.command_ack.write_text('{"n": 1}\n', encoding="utf-8")
    read = JournalReader(layout, layout.command_ack).read()

    assert read.records == ()
    assert any("header" in diagnostic.detail for diagnostic in read.diagnostics)


def test_a_half_written_first_line_is_still_silent(tmp_path: Path) -> None:
    """The transient case stays quiet: it resolves itself on the next poll."""
    layout = make_layout(tmp_path)
    layout.command_ack.write_text('{"type": "journal.hea', encoding="utf-8")
    assert JournalReader(layout, layout.command_ack).read().diagnostics == ()


def test_seek_to_end_refuses_to_promise_a_skip_it_cannot_make(tmp_path: Path) -> None:
    """Silently starting at zero would replay the previous run's commands."""
    layout = make_layout(tmp_path)
    layout.command_queue.mkdir()
    reader = JournalReader(layout, layout.command_queue)
    with pytest.raises(JournalError, match="cannot skip to the end"):
        reader.seek_to_end()
    assert reader.offset == 0


def test_seek_to_end_on_a_journal_that_does_not_exist_is_fine(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    reader = JournalReader(layout, layout.command_queue)
    reader.seek_to_end()
    assert reader.offset == 0
    assert reader.serial is None


def test_header_carries_the_writer_clock(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    writer = JournalWriter(layout, layout.command_ack, clock=clock)
    writer.close()
    first_line = layout.command_ack.read_text(encoding="utf-8").splitlines()[0]
    assert str(clock.now) in first_line


# ---------------------------------------------------------------------------
# a truncated journal is detected and the session refuses to arm (E12-M03-T005)
# ---------------------------------------------------------------------------


def _truncate_last_record(layout: IpcLayout) -> bytes:
    """Cut the ack journal mid-record, the way a producer dying mid-write does.

    Returns the whole file as it was before the cut, so a test can also show
    the condition healing.
    """
    writer = JournalWriter(layout, layout.command_ack)
    try:
        writer.append({"type": "ack.first", "n": 1})
        writer.append({"type": "ack.torn", "n": 2})
    finally:
        writer.close()
    whole = layout.command_ack.read_bytes()
    assert whole.endswith(b"\n")
    layout.command_ack.write_bytes(whole[: -len(b'"n":2}\n')])
    return whole


class TestATruncatedJournalRefusesToArm:
    def test_a_torn_tail_is_detected(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path)
        _truncate_last_record(layout)

        problem = probe_truncation(layout.command_ack)

        assert problem is not None
        assert "mid-record" in problem
        assert layout.command_ack.name in problem

    def test_a_healthy_journal_probes_clean(self, tmp_path: Path) -> None:
        """The control: without it, a probe that always accuses would pass above."""
        layout = make_layout(tmp_path)
        writer = JournalWriter(layout, layout.command_ack)
        try:
            writer.append({"type": "ack.first", "n": 1})
        finally:
            writer.close()

        assert probe_truncation(layout.command_ack) is None

    def test_a_journal_that_does_not_exist_is_not_an_accusation(self, tmp_path: Path) -> None:
        """A stream that has not started is a different fact from a damaged one."""
        layout = make_layout(tmp_path)

        assert probe_truncation(layout.command_ack) is None

    def test_a_complete_first_line_that_is_not_a_header_is_also_detected(
        self, tmp_path: Path
    ) -> None:
        """The other unusable shape: committed lines the reader can never consume."""
        layout = make_layout(tmp_path)
        layout.command_ack.write_text('{"n": 1}\n', encoding="utf-8")

        problem = probe_truncation(layout.command_ack)

        assert problem is not None
        assert "header" in problem

    def test_the_session_refuses_to_arm_over_a_truncated_journal(self, tmp_path: Path) -> None:
        """The criterion itself, on the real loop over a real exchange directory.

        The same world arms cleanly before the tear and again once the stream
        is whole — so the refusal observed in the middle is the truncation
        being detected, not an arm gate that always refuses.
        """
        with attached_world(tmp_path) as world:
            mod = ScriptedMod(world)
            world.beat_game()
            arm_for_real(world, mod, mode=SessionMode.ASSISTED)
            world.loop.disarm()

            whole = _truncate_last_record(world.layout)
            world.beat_game()

            refused = world.loop.arm(SessionMode.ASSISTED)

            assert refused.armed is False
            assert world.loop.armed is False
            assert "truncated" in refused.detail
            assert "mid-record" in refused.detail, (
                f"the refusal must name what was detected: {refused.detail}"
            )

            # Recovery: the journal healed (here, rewritten whole), so the
            # refusal lifts — it was about the tear, not about the file.
            world.layout.command_ack.write_bytes(whole)
            world.beat_game()
            arm_for_real(world, mod, mode=SessionMode.ASSISTED)

    def test_disarming_is_never_gated_on_a_damaged_journal(self, tmp_path: Path) -> None:
        """Taking authority away must work exactly when things are broken."""
        with attached_world(tmp_path) as world:
            world.beat_game()
            arm_for_real(world, mode=SessionMode.ASSISTED)
            _truncate_last_record(world.layout)

            outcome = world.loop.disarm()

            assert outcome.armed is False
            assert world.loop.armed is False


class TestRotationWaitsOutAWindowsReader:
    """Rotation survives a reader holding the file open, briefly, and no longer.

    On POSIX a rename succeeds under any open handle, so the seam cannot be
    driven by a real reader here. On Windows it cannot be *avoided*: the reader
    opens the journal for every poll, and ``os.replace`` on a file with an open
    handle raises ``PermissionError`` (WinError 32) — which took the CI soak
    down when a rotation raced a poll. The writer now retries each move with a
    bounded budget, so a reader mid-poll is a brief wait; a reader that never
    lets go is the writer's own :class:`JournalError`, not a bare crash. The
    seam is pinned directly by making ``Path.replace`` speak the Windows
    refusal for a controlled number of attempts.
    """

    def test_a_reader_mid_poll_is_waited_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writer, reader = _writer(tmp_path)
        writer.append({"type": "probe", "n": 1})

        real_replace = Path.replace
        refusals = {"left": 3}

        def windows_shaped(self: Path, target: object) -> object:
            if refusals["left"] > 0:
                refusals["left"] -= 1
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(self, target)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "replace", windows_shaped)
        monkeypatch.setattr("pz_agent_core.ipc.journal._REPLACE_PAUSE_SECONDS", 0.001)

        serial = writer.rotate()

        assert serial == 1, "the rotation did not complete once the reader let go"
        assert refusals["left"] == 0, "the Windows refusals were never consumed"
        # The first record moved into the rotated generation; the new live file
        # takes the next append and reads back cleanly — the rotation completed
        # rather than crashing under the reader's hold.
        assert rotated_path(writer._path, 1).exists(), "the old generation was not retired"
        writer.append({"type": "probe", "n": 2})
        writer.close()
        read = reader.read()
        assert [r.payload["n"] for r in read.records] == [2], (
            "the new live file did not survive the waited-out rotation"
        )

    def test_a_reader_that_never_lets_go_is_a_named_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writer, _ = _writer(tmp_path)
        writer.append({"type": "probe", "n": 1})

        def always_held(self: Path, target: object) -> object:
            raise PermissionError(32, "The process cannot access the file")

        monkeypatch.setattr(Path, "replace", always_held)
        monkeypatch.setattr("pz_agent_core.ipc.journal._REPLACE_PAUSE_SECONDS", 0.001)

        with pytest.raises(JournalError, match="held it open past the"):
            writer.rotate()
