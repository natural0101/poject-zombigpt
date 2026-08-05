"""One sidecar per exchange directory — and never a lock nobody can clear."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.session.lock import LockError, LockInfo, SidecarLock
from tests.fixtures.ipc_builders import IPC_SESSION_ID, FakeClock, make_layout


def _lock(layout: IpcLayout, clock: FakeClock, **kwargs: object) -> SidecarLock:
    return SidecarLock(layout, session_id=IPC_SESSION_ID, clock=clock, **kwargs)  # type: ignore[arg-type]


def test_acquiring_writes_the_lock_file(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    lock = _lock(layout, clock)
    outcome = lock.acquire()

    assert outcome.acquired
    assert not outcome.recovered_stale
    assert lock.held
    assert layout.sidecar_lock.exists()
    stored = lock.read()
    assert stored is not None
    assert stored.session_id == IPC_SESSION_ID
    assert stored.acquired_at_ms == clock.now


def test_a_second_sidecar_is_refused_while_the_lock_is_fresh(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    first = _lock(layout, clock)
    first.acquire()

    second = _lock(layout, clock)
    outcome = second.acquire()
    assert not outcome.acquired
    assert not second.held
    assert outcome.blocked_by is not None
    assert outcome.blocked_by.owner_id == first.owner_id
    assert "held by" in outcome.detail


def test_a_stale_lock_is_recovered_rather_than_deadlocking_the_user(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    abandoned = _lock(layout, clock, stale_after_ms=1_000)
    abandoned.acquire()

    clock.advance(5_000)
    fresh = _lock(layout, clock, stale_after_ms=1_000)
    outcome = fresh.acquire()

    assert outcome.acquired
    assert outcome.recovered_stale
    stored = fresh.read()
    assert stored is not None
    assert stored.owner_id == fresh.owner_id


def test_an_unparseable_lock_file_never_wedges_the_directory(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.sidecar_lock.write_text("{ this is not a lock", encoding="utf-8")
    outcome = _lock(layout, FakeClock()).acquire()
    assert outcome.acquired
    assert outcome.recovered_stale


def test_a_lock_file_missing_fields_is_treated_as_stale(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.sidecar_lock.write_text(json.dumps({"owner_id": "x"}), encoding="utf-8")
    assert _lock(layout, FakeClock()).read() is None
    assert _lock(layout, FakeClock()).acquire().acquired


def test_refresh_extends_the_lock(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    lock = _lock(layout, clock, stale_after_ms=1_000)
    lock.acquire()
    clock.advance(900)
    refreshed = lock.refresh()

    assert refreshed.refreshed_at_ms == clock.now
    assert refreshed.acquired_at_ms < refreshed.refreshed_at_ms
    clock.advance(900)
    stored = lock.read()
    assert stored is not None
    assert not stored.is_stale(clock.now, 1_000)


def test_refresh_without_holding_the_lock_is_an_error(tmp_path: Path) -> None:
    lock = _lock(make_layout(tmp_path), FakeClock())
    with pytest.raises(LockError, match="without holding"):
        lock.refresh()


def test_refresh_after_a_takeover_reports_instead_of_overwriting(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    original = _lock(layout, clock, stale_after_ms=1_000)
    original.acquire()
    clock.advance(5_000)
    successor = _lock(layout, clock, stale_after_ms=1_000)
    assert successor.acquire().acquired

    with pytest.raises(LockError, match="taken over"):
        original.refresh()
    assert not original.held
    stored = layout.sidecar_lock.read_text(encoding="utf-8")
    assert successor.owner_id in stored


def test_release_removes_only_our_own_lock(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    lock = _lock(layout, clock)
    lock.acquire()
    assert lock.release()
    assert not layout.sidecar_lock.exists()
    assert not lock.release()


def test_release_leaves_a_successor_s_lock_alone(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    original = _lock(layout, clock, stale_after_ms=1_000)
    original.acquire()
    clock.advance(5_000)
    successor = _lock(layout, clock, stale_after_ms=1_000)
    successor.acquire()

    assert not original.release()
    assert layout.sidecar_lock.exists()
    assert successor.read() is not None


def test_the_same_owner_readopts_its_own_lock(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    lock = _lock(layout, clock)
    lock.acquire()
    again = SidecarLock(layout, session_id=IPC_SESSION_ID, clock=clock, owner_id=lock.owner_id)
    outcome = again.acquire()
    assert outcome.acquired
    assert "re-adopted" in outcome.detail


def test_the_context_manager_releases(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    with _lock(layout, clock) as lock:
        assert lock.held
        assert layout.sidecar_lock.exists()
    assert not layout.sidecar_lock.exists()


def test_the_context_manager_refuses_to_start_when_blocked(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    _lock(layout, clock).acquire()
    with pytest.raises(LockError, match="held by"), _lock(layout, clock):
        pass  # pragma: no cover - the context manager raises on entry


def _foreign(now_ms: int) -> LockInfo:
    return LockInfo(
        owner_id="somebody-else",
        pid=4321,
        session_id=IPC_SESSION_ID,
        acquired_at_ms=now_ms,
        refreshed_at_ms=now_ms,
    )


def test_a_claim_that_lost_the_race_is_not_reported_as_acquired(tmp_path: Path) -> None:
    """``O_EXCL`` makes the creation exclusive, not the whole claim: the file is
    empty until the record is written, and a contender may break it in between.
    The claim is only real once our own record is what is on disk."""
    layout = make_layout(tmp_path)
    clock = FakeClock()

    class _Overtaken(SidecarLock):
        def read(self) -> LockInfo | None:
            return _foreign(clock.now)

    lock = _Overtaken(layout, session_id=IPC_SESSION_ID, clock=clock)
    outcome = lock.acquire()

    assert not outcome.acquired
    assert not lock.held
    assert outcome.blocked_by is not None
    assert outcome.blocked_by.owner_id == "somebody-else"


def test_a_lock_that_becomes_readable_while_being_broken_is_left_alone(tmp_path: Path) -> None:
    """An unreadable lock file may simply be one that is still being written."""
    layout = make_layout(tmp_path)
    clock = FakeClock()
    layout.sidecar_lock.write_bytes(b"")  # a claim caught mid-creation

    class _RaceLoser(SidecarLock):
        calls = 0

        def read(self) -> LockInfo | None:
            _RaceLoser.calls += 1
            # The first look finds the empty file; by the second the owner has
            # finished writing its record.
            return None if _RaceLoser.calls == 1 else _foreign(clock.now)

    outcome = _RaceLoser(layout, session_id=IPC_SESSION_ID, clock=clock).acquire()

    assert not outcome.acquired
    assert layout.sidecar_lock.exists()
    assert outcome.blocked_by is not None
    assert outcome.blocked_by.owner_id == "somebody-else"


def test_a_permanently_unreadable_lock_is_still_broken(tmp_path: Path) -> None:
    """The other half of the rule: a lock nobody can refresh must not wedge the
    directory forever."""
    layout = make_layout(tmp_path)
    layout.sidecar_lock.write_bytes(b"")
    lock = _lock(layout, FakeClock())
    outcome = lock.acquire()

    assert outcome.acquired
    assert outcome.recovered_stale
    stored = lock.read()
    assert stored is not None and stored.owner_id == lock.owner_id


def test_lock_staleness_is_measured_from_the_refresh(tmp_path: Path) -> None:
    info = LockInfo(
        owner_id="o",
        pid=1,
        session_id=IPC_SESSION_ID,
        acquired_at_ms=0,
        refreshed_at_ms=1_000,
    )
    assert not info.is_stale(6_000, 5_000)
    assert info.is_stale(6_001, 5_000)
    with pytest.raises(ValueError, match="positive"):
        info.is_stale(1, 0)
