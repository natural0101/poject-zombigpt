"""The two-slot snapshot exchange, especially when a write is caught mid-flight."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pz_agent_core.ipc.atomic import DocumentError, SharingViolationError, read_json_document
from pz_agent_core.ipc.layout import SnapshotSlot
from pz_agent_core.ipc.snapshot import SnapshotMiss, SnapshotRead, SnapshotReader, SnapshotWriter
from tests.fixtures.ipc_builders import FakeClock, make_layout


def _document(seq: int) -> dict[str, object]:
    return {"seq": seq, "full": True, "player": {"present": True}}


#: Two session ids, in the shape the mod stamps on every observation.
SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"


def _session_document(session_id: str, seq: int) -> dict[str, object]:
    doc = _document(seq)
    doc["session_id"] = session_id
    return doc


def test_publish_alternates_slots_and_moves_the_pointer(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = SnapshotWriter(layout, clock=FakeClock())

    assert writer.publish(_document(1)) is SnapshotSlot.A
    assert writer.publish(_document(2)) is SnapshotSlot.B
    assert writer.publish(_document(3)) is SnapshotSlot.A
    assert writer.current_slot is SnapshotSlot.A
    # Both slots exist: the reader always has a previous snapshot to fall back on.
    assert layout.snapshot_slot(SnapshotSlot.A).exists()
    assert layout.snapshot_slot(SnapshotSlot.B).exists()


def test_reader_follows_the_pointer(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    SnapshotWriter(layout).publish(_document(1))
    SnapshotWriter(layout).publish(_document(2))

    read = SnapshotReader(layout).read()
    assert isinstance(read, SnapshotRead)
    assert read.seq == 2
    assert read.slot is SnapshotSlot.B
    assert read.from_pointer
    assert not read.recovered


def test_a_torn_slot_falls_back_to_the_other_one(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = SnapshotWriter(layout)
    writer.publish(_document(1))
    writer.publish(_document(2))

    # Simulate the mod dying halfway through rewriting the pointed-at slot.
    pointed = layout.snapshot_slot(SnapshotSlot.B)
    pointed.write_text('{"seq": 3, "full": tr', encoding="utf-8")

    read = SnapshotReader(layout).read()
    assert isinstance(read, SnapshotRead)
    assert read.slot is SnapshotSlot.A
    assert read.seq == 1
    assert read.recovered
    assert any("slot b" in diagnostic for diagnostic in read.diagnostics)


def test_a_torn_pointer_probes_both_slots_and_takes_the_newer(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    writer = SnapshotWriter(layout)
    writer.publish(_document(1))
    writer.publish(_document(2))
    layout.snapshot_pointer.write_text("{", encoding="utf-8")

    read = SnapshotReader(layout).read()
    assert isinstance(read, SnapshotRead)
    assert read.seq == 2
    assert not read.from_pointer
    assert any("pointer" in diagnostic for diagnostic in read.diagnostics)


def test_a_pointer_naming_an_unknown_slot_is_not_trusted(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    SnapshotWriter(layout).publish(_document(1))
    layout.snapshot_pointer.write_text('{"slot": "c", "seq": 9}', encoding="utf-8")

    read = SnapshotReader(layout).read()
    assert isinstance(read, SnapshotRead)
    assert read.slot is SnapshotSlot.A
    assert read.seq == 1


def test_no_snapshot_at_all_is_a_miss_not_an_exception(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    read = SnapshotReader(layout).read()
    assert isinstance(read, SnapshotMiss)
    assert read.diagnostics


def test_both_slots_unreadable_is_a_miss(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    SnapshotWriter(layout).publish(_document(1))
    layout.snapshot_slot(SnapshotSlot.A).write_text("garbage", encoding="utf-8")

    read = SnapshotReader(layout).read()
    assert isinstance(read, SnapshotMiss)
    assert len(read.diagnostics) >= 1


def test_a_slot_without_a_sequence_is_refused(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    SnapshotWriter(layout).publish(_document(1))
    layout.snapshot_slot(SnapshotSlot.A).write_text('{"full": true}', encoding="utf-8")

    read = SnapshotReader(layout).read()
    assert isinstance(read, SnapshotMiss)
    assert any("seq" in diagnostic for diagnostic in read.diagnostics)


def test_the_reader_refuses_to_rewind(tmp_path: Path) -> None:
    """A fallback must not hand the world model an older state than it has."""
    layout = make_layout(tmp_path)
    writer = SnapshotWriter(layout)
    writer.publish(_document(1))
    writer.publish(_document(5))
    reader = SnapshotReader(layout)
    assert isinstance(reader.read(), SnapshotRead)
    assert reader.last_seq == 5

    layout.snapshot_slot(SnapshotSlot.B).write_text('{"seq": 6, "trunc', encoding="utf-8")
    read = reader.read()
    assert isinstance(read, SnapshotMiss)
    assert any("older than the accepted 5" in diagnostic for diagnostic in read.diagnostics)


class TestTheRewindGuardIsScopedToOneSession:
    """Sequence numbers restart with the session, so they cannot be compared across one.

    The mod builds fresh counters on ``OnGameStart`` and publishes the first
    snapshot of the new session as seq 0, into an exchange directory still
    holding the previous session's files. Ordering that 0 against the number
    the dead session reached made the reader refuse the live world snapshot
    after snapshot — and this pointer is the *only* observation channel the mod
    publishes on, so the sidecar sat blind, with a diagnostic that read like a
    torn slot, until the new session climbed past the old one's last number.
    """

    def test_the_next_sessions_first_snapshot_is_not_refused_as_a_rewind(
        self, tmp_path: Path
    ) -> None:
        layout = make_layout(tmp_path)
        writer = SnapshotWriter(layout)
        writer.publish(_session_document(SESSION_A, 900))
        reader = SnapshotReader(layout)
        assert isinstance(reader.read(), SnapshotRead)
        assert reader.last_seq == 900

        # The player restarts the game: a new session, numbering from zero.
        writer.publish(_session_document(SESSION_B, 0))
        first = reader.read()

        assert isinstance(first, SnapshotRead)
        assert first.seq == 0
        assert first.document["session_id"] == SESSION_B
        assert any("restart per session" in diagnostic for diagnostic in first.diagnostics), (
            "the session change must be said out loud, not passed over in silence"
        )

        # And the new session's own ordering is picked up from there.
        writer.publish(_session_document(SESSION_B, 1))
        second = reader.read()
        assert isinstance(second, SnapshotRead)
        assert second.seq == 1

    def test_a_rewind_inside_one_session_is_still_refused(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path)
        writer = SnapshotWriter(layout)
        writer.publish(_session_document(SESSION_B, 1))
        writer.publish(_session_document(SESSION_B, 5))
        reader = SnapshotReader(layout)
        assert isinstance(reader.read(), SnapshotRead)

        # The pointed-at slot is caught mid-write; the fallback is this same
        # session's older document, which must not rewind the world model.
        layout.snapshot_slot(SnapshotSlot.B).write_text('{"seq": 6, "trunc', encoding="utf-8")
        read = reader.read()

        assert isinstance(read, SnapshotMiss)
        assert any("older than the accepted 5" in diagnostic for diagnostic in read.diagnostics)
        assert reader.last_seq == 5


class TestABoundReaderServesOnlyItsOwnSession:
    """The exchange directory survives the session that filled it.

    ``last_seq`` cannot help here: a reader that has accepted nothing has
    nothing to order the leftover document against, so the previous session's
    last snapshot is served as the first observation of the next one. Binding
    the reader to the session the sidecar handshook answers *whose* rather than
    *how old*, and a document from any other session is refused by name.
    """

    def test_the_previous_sessions_leftover_document_is_refused(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path)
        SnapshotWriter(layout).publish(_session_document(SESSION_A, 900))
        reader = SnapshotReader(layout, session_id=SESSION_B)

        read = reader.read()

        assert isinstance(read, SnapshotMiss)
        assert any(SESSION_A in diagnostic for diagnostic in read.diagnostics)
        assert any(SESSION_B in diagnostic for diagnostic in read.diagnostics)
        assert reader.last_seq is None, "a refused document must not anchor the rewind guard"

    def test_the_bound_sessions_document_is_served_and_anchors_the_guard(
        self, tmp_path: Path
    ) -> None:
        layout = make_layout(tmp_path)
        writer = SnapshotWriter(layout)
        writer.publish(_session_document(SESSION_A, 900))
        reader = SnapshotReader(layout, session_id=SESSION_B)
        assert isinstance(reader.read(), SnapshotMiss)

        # The mod accepts the handshake and publishes under the live session.
        writer.publish(_session_document(SESSION_B, 0))
        read = reader.read()

        assert isinstance(read, SnapshotRead)
        assert read.seq == 0
        assert read.document["session_id"] == SESSION_B
        assert reader.last_seq == 0

    def test_a_foreign_slot_cannot_shadow_this_sessions_through_a_torn_pointer(
        self, tmp_path: Path
    ) -> None:
        """Probing both slots must not pick the dead session for its higher seq."""
        layout = make_layout(tmp_path)
        writer = SnapshotWriter(layout)
        writer.publish(_session_document(SESSION_A, 900))
        writer.publish(_session_document(SESSION_B, 0))
        layout.snapshot_pointer.write_text("{", encoding="utf-8")

        read = SnapshotReader(layout, session_id=SESSION_B).read()

        assert isinstance(read, SnapshotRead)
        assert read.seq == 0
        assert read.document["session_id"] == SESSION_B

    def test_a_document_naming_no_session_is_refused_by_a_bound_reader(
        self, tmp_path: Path
    ) -> None:
        layout = make_layout(tmp_path)
        SnapshotWriter(layout).publish(_document(3))

        read = SnapshotReader(layout, session_id=SESSION_B).read()

        assert isinstance(read, SnapshotMiss)
        assert any("names no session" in diagnostic for diagnostic in read.diagnostics)

    def test_an_unbound_reader_still_serves_whatever_it_finds(self, tmp_path: Path) -> None:
        """The default is what every existing caller gets: no session filter."""
        layout = make_layout(tmp_path)
        SnapshotWriter(layout).publish(_session_document(SESSION_A, 900))

        read = SnapshotReader(layout).read()

        assert isinstance(read, SnapshotRead)
        assert read.seq == 900


def test_re_reading_the_same_snapshot_is_allowed(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    SnapshotWriter(layout).publish(_document(4))
    reader = SnapshotReader(layout)
    assert isinstance(reader.read(), SnapshotRead)
    again = reader.read()
    assert isinstance(again, SnapshotRead)
    assert again.seq == 4


def test_a_document_without_a_sequence_cannot_be_published(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    with pytest.raises(DocumentError, match="seq"):
        SnapshotWriter(layout).publish({"full": True})


def test_the_pointer_is_written_after_the_slot(tmp_path: Path) -> None:
    """Ordering is the commit rule: a pointer must never outrun its payload."""
    layout = make_layout(tmp_path)
    writer = SnapshotWriter(layout)
    writer.publish(_document(1))
    slot_mtime = layout.snapshot_slot(SnapshotSlot.A).stat().st_mtime_ns
    pointer_mtime = layout.snapshot_pointer.stat().st_mtime_ns
    assert pointer_mtime >= slot_mtime


def test_a_restarted_writer_does_not_overwrite_the_live_slot(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    SnapshotWriter(layout).publish(_document(1))
    # A fresh writer object, as after a sidecar restart: it must read the
    # pointer rather than assume it starts at slot a again.
    assert SnapshotWriter(layout).next_slot() is SnapshotSlot.B


def _lock_paths(monkeypatch: pytest.MonkeyPatch, *locked: Path) -> dict[Path, int]:
    """Make :func:`read_json_document` speak the Windows hold for *locked*.

    Patched at the snapshot module's seam rather than under ``Path``: the
    retry mechanics that produce :class:`SharingViolationError` are pinned in
    ``test_ipc_atomic_patience``; these tests pin what the reader *does* with
    it, so the refusal is injected already-typed and counted per path.
    """
    holds: dict[Path, int] = dict.fromkeys(locked, 0)

    def held(path: Path) -> dict[str, Any]:
        if path in holds:
            holds[path] += 1
            raise SharingViolationError(f"{path.name}: could not be read; another process held it")
        return read_json_document(path)

    monkeypatch.setattr("pz_agent_core.ipc.snapshot.read_json_document", held)
    return holds


class TestALockedPointerIsAMissForThisPollOnly:
    def test_the_miss_names_the_lock_and_is_not_corruption(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Live finding, Build 42.20.2: the game holds the pointer mid-commit.

        Before the read side learnt patience this surfaced as a
        :class:`DocumentError` and was logged as an unreadable pointer — a
        corruption diagnostic for a healthy file. The lock must be named as a
        lock, and nothing about the poll may crash.
        """
        layout = make_layout(tmp_path)
        SnapshotWriter(layout).publish(_document(3))
        holds = _lock_paths(monkeypatch, layout.snapshot_pointer)

        read = SnapshotReader(layout).read()

        assert isinstance(read, SnapshotMiss)
        assert any(
            "pointer locked by another process this poll" in diagnostic
            for diagnostic in read.diagnostics
        )
        assert not any("unreadable" in diagnostic for diagnostic in read.diagnostics)
        assert holds[layout.snapshot_pointer] == 1

    def test_last_seq_survives_the_lock_and_the_next_poll_recovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = make_layout(tmp_path)
        writer = SnapshotWriter(layout)
        writer.publish(_document(4))
        reader = SnapshotReader(layout)
        assert isinstance(reader.read(), SnapshotRead)
        assert reader.last_seq == 4

        with monkeypatch.context() as hold:
            _lock_paths(hold, layout.snapshot_pointer)
            missed = reader.read()

        assert isinstance(missed, SnapshotMiss)
        assert reader.last_seq == 4, "a locked poll must not regress the accepted sequence"

        # The hold has been released; the very next poll serves the world
        # published meanwhile, ordered against the surviving last_seq.
        writer.publish(_document(5))
        recovered = reader.read()
        assert isinstance(recovered, SnapshotRead)
        assert recovered.seq == 5
        assert reader.last_seq == 5


class TestALockedSlotIsAPerSlotMissNotCorruption:
    def test_the_other_slot_still_serves_under_the_anti_rewind_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh reader falls back past the locked slot to the older one."""
        layout = make_layout(tmp_path)
        writer = SnapshotWriter(layout)
        writer.publish(_document(1))
        writer.publish(_document(2))
        _lock_paths(monkeypatch, layout.snapshot_slot(SnapshotSlot.B))

        read = SnapshotReader(layout).read()

        assert isinstance(read, SnapshotRead)
        assert read.slot is SnapshotSlot.A
        assert read.seq == 1
        assert read.recovered
        assert any(
            "slot b locked by another process this poll" in diagnostic
            for diagnostic in read.diagnostics
        )

    def test_a_reader_that_has_seen_newer_refuses_the_stale_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lock must not smuggle a rewind in through the fallback."""
        layout = make_layout(tmp_path)
        writer = SnapshotWriter(layout)
        writer.publish(_document(1))
        writer.publish(_document(2))
        reader = SnapshotReader(layout)
        assert isinstance(reader.read(), SnapshotRead)
        assert reader.last_seq == 2

        with monkeypatch.context() as hold:
            _lock_paths(hold, layout.snapshot_slot(SnapshotSlot.B))
            missed = reader.read()

        assert isinstance(missed, SnapshotMiss)
        assert reader.last_seq == 2
        assert any("slot b locked" in diagnostic for diagnostic in missed.diagnostics)

        recovered = reader.read()
        assert isinstance(recovered, SnapshotRead)
        assert recovered.seq == 2

    def test_both_slots_locked_is_a_miss_naming_both(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = make_layout(tmp_path)
        writer = SnapshotWriter(layout)
        writer.publish(_document(1))
        writer.publish(_document(2))
        _lock_paths(
            monkeypatch,
            layout.snapshot_slot(SnapshotSlot.A),
            layout.snapshot_slot(SnapshotSlot.B),
        )

        read = SnapshotReader(layout).read()

        assert isinstance(read, SnapshotMiss)
        assert any("slot a locked" in diagnostic for diagnostic in read.diagnostics)
        assert any("slot b locked" in diagnostic for diagnostic in read.diagnostics)
