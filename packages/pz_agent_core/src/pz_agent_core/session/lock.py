"""Mutual exclusion between sidecars attached to one exchange directory.

Two sidecars writing the same command stream would interleave sequence numbers
and both believe they own the single in-flight slot, so attachment is guarded
by a lock file created with ``O_EXCL``.

The hard part is not taking the lock but *releasing* one that was never
released: a sidecar killed with the task manager leaves its file behind, and a
lock that outlives its owner forever is a worse outcome than no lock at all —
the user is left with a program that refuses to start and no way to fix it.
Staleness is therefore decided by the heartbeat the holder keeps refreshing,
not by asking the operating system whether the pid still exists: on Windows
``os.kill(pid, 0)`` does not probe, it calls ``TerminateProcess``, and a pid
that has been recycled would be killed by the check itself.

``O_EXCL`` alone is not the whole guarantee. The file exists, and is empty, for
as long as it takes to write the record into it, so two starting sidecars can
still collide: one sees an unusable lock and breaks it. What closes that is the
read-back in :meth:`SidecarLock._claim` — a claim counts only once our own
record is what is on disk — together with :meth:`SidecarLock._break_stale`
refusing to delete a file that became readable while it was being judged. The
loser of such a race reports a failed acquisition; it never proceeds as a
second sidecar.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final

from ..ipc.atomic import DocumentError, read_json_document, write_json_atomic
from ..ipc.clocks import Clock, system_clock_ms
from ..ipc.layout import IpcLayout
from ..protocol import JsonDict

#: A lock whose holder has not refreshed it for this long may be taken over.
#: Three heartbeat periods: long enough that a stalled frame does not hand the
#: session to a second sidecar, short enough that a crash does not strand the
#: user for a noticeable time.
DEFAULT_STALE_AFTER_MS: Final = 15_000

#: Attempts to claim the file when a stale lock has to be removed first. Two
#: contenders can each remove and re-create; the retry is bounded so the loser
#: reports a failure instead of spinning.
MAX_ACQUIRE_ATTEMPTS: Final = 3


class LockError(ValueError):
    """A lock file is malformed or an operation was made out of order."""


@dataclass(frozen=True, slots=True)
class LockInfo:
    """Who holds the lock, and when they last said so."""

    owner_id: str
    pid: int
    session_id: str
    acquired_at_ms: int
    refreshed_at_ms: int

    def age_ms(self, now_ms: int) -> int:
        return max(0, now_ms - self.refreshed_at_ms)

    def is_stale(self, now_ms: int, stale_after_ms: int = DEFAULT_STALE_AFTER_MS) -> bool:
        if stale_after_ms <= 0:
            raise ValueError(f"stale_after_ms must be positive, got {stale_after_ms}")
        return self.age_ms(now_ms) > stale_after_ms

    def to_dict(self) -> JsonDict:
        return {
            "owner_id": self.owner_id,
            "pid": self.pid,
            "session_id": self.session_id,
            "acquired_at_ms": self.acquired_at_ms,
            "refreshed_at_ms": self.refreshed_at_ms,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LockInfo:
        def integer(key: str) -> int:
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise LockError(f"lock field {key!r} must be an integer")
            return value

        def text(key: str) -> str:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise LockError(f"lock field {key!r} must be a non-empty string")
            return value

        return cls(
            owner_id=text("owner_id"),
            pid=integer("pid"),
            session_id=text("session_id"),
            acquired_at_ms=integer("acquired_at_ms"),
            refreshed_at_ms=integer("refreshed_at_ms"),
        )


@dataclass(frozen=True, slots=True)
class LockOutcome:
    """The result of one acquisition attempt."""

    acquired: bool
    detail: str
    info: LockInfo | None = None
    blocked_by: LockInfo | None = None
    recovered_stale: bool = False


@dataclass
class SidecarLock:
    """The exchange directory's single-attachment guard."""

    layout: IpcLayout
    session_id: str
    clock: Clock = system_clock_ms
    stale_after_ms: int = DEFAULT_STALE_AFTER_MS
    owner_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _held: LockInfo | None = field(default=None, init=False)

    @property
    def held(self) -> bool:
        return self._held is not None

    @property
    def info(self) -> LockInfo | None:
        """Our own lock record while we hold it."""
        return self._held

    def read(self) -> LockInfo | None:
        """The holder recorded on disk, or None if there is no usable record.

        An unparseable lock file returns None as well: it names nobody, so it
        cannot be refreshed by anybody, and treating it as a live holder would
        wedge the directory permanently.
        """
        try:
            payload = read_json_document(self.layout.sidecar_lock)
        except DocumentError:
            return None
        try:
            return LockInfo.from_dict(payload)
        except LockError:
            return None

    def acquire(self) -> LockOutcome:
        """Claim the lock, taking over a stale one if necessary."""
        self.layout.ensure()
        recovered = False
        obstacle: str | None = None
        for _ in range(MAX_ACQUIRE_ATTEMPTS):
            now = self.clock()
            claimed = self._claim(now)
            if claimed is not None:
                self._held = claimed
                return LockOutcome(
                    acquired=True,
                    detail="lock acquired",
                    info=claimed,
                    recovered_stale=recovered,
                )
            holder = self.read()
            if holder is not None and holder.owner_id == self.owner_id:
                # Our own record from a previous run of this same process
                # object; adopt it rather than fighting ourselves for it.
                self._held = holder
                return LockOutcome(acquired=True, detail="existing lock re-adopted", info=holder)
            if holder is not None and not holder.is_stale(now, self.stale_after_ms):
                return LockOutcome(
                    acquired=False,
                    detail=(
                        f"held by {holder.owner_id} (pid {holder.pid}), "
                        f"refreshed {holder.age_ms(now)} ms ago"
                    ),
                    blocked_by=holder,
                )
            recovered = True
            obstacle = self._break_stale(holder)
        detail = f"could not claim the lock in {MAX_ACQUIRE_ATTEMPTS} attempts"
        return LockOutcome(
            acquired=False,
            detail=detail if obstacle is None else f"{detail}: {obstacle}",
            blocked_by=self.read(),
            recovered_stale=recovered,
        )

    def refresh(self) -> LockInfo:
        """Prove we are still alive. Raises if we do not hold the lock."""
        current = self._held
        if current is None:
            raise LockError("refresh called without holding the lock")
        on_disk = self.read()
        if on_disk is not None and on_disk.owner_id != self.owner_id:
            # Somebody took the lock from us — almost certainly because we were
            # stalled long enough to look dead. Report it instead of stamping
            # our own name back over theirs.
            self._held = None
            raise LockError(f"lock was taken over by {on_disk.owner_id}")
        refreshed = LockInfo(
            owner_id=current.owner_id,
            pid=current.pid,
            session_id=current.session_id,
            acquired_at_ms=current.acquired_at_ms,
            refreshed_at_ms=self.clock(),
        )
        write_json_atomic(self.layout, self.layout.sidecar_lock, refreshed.to_dict())
        self._held = refreshed
        return refreshed

    def release(self) -> bool:
        """Remove our lock. Returns False when it was not ours to remove.

        The held flag is only cleared once the file is actually gone: if the
        unlink fails we still hold the lock, and saying otherwise would leave
        the directory guarded by a record this process no longer refreshes.
        """
        current = self._held
        if current is None:
            return False
        on_disk = self.read()
        if on_disk is not None and on_disk.owner_id != current.owner_id:
            self._held = None
            return False
        self.layout.sidecar_lock.unlink(missing_ok=True)
        self._held = None
        return True

    def _claim(self, now_ms: int) -> LockInfo | None:
        """Create the lock file exclusively and verify we still own it.

        ``O_EXCL`` makes the *creation* exclusive but not the whole claim: the
        file is visible, and empty, for as long as it takes to write the record
        into it. A contender that reads it during that window sees an unusable
        lock and may break it (see :meth:`_break_stale`). Reading our own record
        back is what turns "we wrote a lock file" into "we hold the lock" — if
        somebody else's name is in there, we lost the race and say so rather
        than proceeding as a second sidecar.
        """
        info = LockInfo(
            owner_id=self.owner_id,
            pid=os.getpid(),
            session_id=self.session_id,
            acquired_at_ms=now_ms,
            refreshed_at_ms=now_ms,
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            handle = os.open(self.layout.sidecar_lock, flags, 0o600)
        except FileExistsError:
            return None
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(info.to_dict(), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        confirmed = self.read()
        if confirmed is None or confirmed.owner_id != self.owner_id:
            return None
        return confirmed

    def _break_stale(self, holder: LockInfo | None) -> str | None:
        """Remove a lock we judged stale. Returns why it was left in place.

        Re-reading before unlinking closes the common race: the holder woke up
        and refreshed between our read and our removal. It also covers the
        narrower one where *holder* was None because the file was mid-creation —
        by the time we get here it may carry a perfectly good record, and
        deleting that would hand the directory to two sidecars at once.

        What is deliberately still removed is a lock that stays unreadable: it
        names nobody, so nobody can refresh or release it, and leaving it would
        wedge the directory with no way out for the user.
        """
        current = self.read()
        if current is not None:
            if holder is None:
                return f"lock became readable while being broken (owner {current.owner_id})"
            if current.refreshed_at_ms != holder.refreshed_at_ms:
                return f"lock was refreshed by {current.owner_id} while being broken"
            if current.owner_id != holder.owner_id:
                return f"lock was taken over by {current.owner_id} while being broken"
        try:
            self.layout.sidecar_lock.unlink(missing_ok=True)
        except OSError as exc:
            # Windows refuses to unlink a file another process still has open,
            # which is exactly the case where the "stale" holder is alive after
            # all. Report it; the caller reports a failed acquisition.
            return f"stale lock could not be removed: {exc}"
        return None

    def __enter__(self) -> SidecarLock:
        outcome = self.acquire()
        if not outcome.acquired:
            raise LockError(outcome.detail)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
