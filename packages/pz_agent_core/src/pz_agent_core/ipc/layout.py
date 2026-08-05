"""The fixed on-disk layout of the mod/sidecar exchange directory.

Every filename in ``docs/blueprint/03_GAME_STATE_AND_IPC.md`` §3.2 is a constant
here. The invariant this module holds is that *no value derived from a command,
an LLM plan or an observation ever contributes to a path*: a caller asks the
layout for a role (``command_queue``, ``panic_stop``) and receives a path the
layout composed itself. :func:`IpcLayout.is_managed_path` exists so writers can
assert that before opening a file, which turns "the mod named a file" from a
silent path traversal into a failed assertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

#: Journal names carry a four-digit series number. The series only changes when
#: the protocol changes shape (a new series lets an old reader stop cleanly
#: instead of misparsing), so it is a constant rather than a rotation counter —
#: rotation is expressed by the ``.1``/``.2`` suffixes :mod:`.journal` appends.
JOURNAL_SERIES: Final = "0001"

SESSION_FILE: Final = "session.json"
CAPABILITIES_FILE: Final = "capabilities.json"
SNAPSHOT_SLOT_A_FILE: Final = "observation.snapshot.a.json"
SNAPSHOT_SLOT_B_FILE: Final = "observation.snapshot.b.json"
SNAPSHOT_POINTER_FILE: Final = "observation.snapshot.pointer"
OBSERVATION_EVENTS_FILE: Final = f"observation.events.{JOURNAL_SERIES}.jsonl"
COMMAND_QUEUE_FILE: Final = f"command.queue.{JOURNAL_SERIES}.jsonl"
COMMAND_ACK_FILE: Final = f"command.ack.{JOURNAL_SERIES}.jsonl"
GAME_HEARTBEAT_FILE: Final = "heartbeat.game.json"
SIDECAR_HEARTBEAT_FILE: Final = "heartbeat.sidecar.json"
PANIC_STOP_FILE: Final = "panic.stop"
LOGS_DIR: Final = "logs"

#: Not part of §3.2: the mod never reads it. It is the sidecar's own mutual
#: exclusion file (see :mod:`pz_agent_core.session.lock`), kept in the same
#: directory so that removing the exchange directory removes the lock with it.
SIDECAR_LOCK_FILE: Final = "sidecar.lock"

#: Suffix used by the atomic-replace helper and by snapshot writes. Temporary
#: files are managed paths too, otherwise the write guard would reject them.
TEMP_SUFFIX: Final = ".tmp"


class SnapshotSlot(StrEnum):
    """Which of the two alternating full-snapshot files is meant.

    §3.5: an atomic rename is not confirmed to be available from inside
    Kahlua, so the writer alternates between two whole files and publishes the
    switch by rewriting a tiny pointer last.
    """

    A = "a"
    B = "b"

    @property
    def other(self) -> SnapshotSlot:
        return SnapshotSlot.B if self is SnapshotSlot.A else SnapshotSlot.A


@dataclass(frozen=True, slots=True)
class IpcLayout:
    """Resolved paths for one exchange directory (``~/Zomboid/Lua/pz_agent``)."""

    root: Path

    @property
    def session(self) -> Path:
        return self.root / SESSION_FILE

    @property
    def capabilities(self) -> Path:
        return self.root / CAPABILITIES_FILE

    @property
    def snapshot_pointer(self) -> Path:
        return self.root / SNAPSHOT_POINTER_FILE

    @property
    def observation_events(self) -> Path:
        return self.root / OBSERVATION_EVENTS_FILE

    @property
    def command_queue(self) -> Path:
        return self.root / COMMAND_QUEUE_FILE

    @property
    def command_ack(self) -> Path:
        return self.root / COMMAND_ACK_FILE

    @property
    def game_heartbeat(self) -> Path:
        return self.root / GAME_HEARTBEAT_FILE

    @property
    def sidecar_heartbeat(self) -> Path:
        return self.root / SIDECAR_HEARTBEAT_FILE

    @property
    def panic_stop(self) -> Path:
        return self.root / PANIC_STOP_FILE

    @property
    def sidecar_lock(self) -> Path:
        return self.root / SIDECAR_LOCK_FILE

    @property
    def logs_dir(self) -> Path:
        return self.root / LOGS_DIR

    def snapshot_slot(self, slot: SnapshotSlot) -> Path:
        name = SNAPSHOT_SLOT_A_FILE if slot is SnapshotSlot.A else SNAPSHOT_SLOT_B_FILE
        return self.root / name

    def journals(self) -> tuple[Path, ...]:
        """The three append-only streams, in no particular order."""
        return (self.observation_events, self.command_queue, self.command_ack)

    @staticmethod
    def managed_names() -> tuple[str, ...]:
        """Every filename this layout owns, excluding rotated journals."""
        return (
            SESSION_FILE,
            CAPABILITIES_FILE,
            SNAPSHOT_SLOT_A_FILE,
            SNAPSHOT_SLOT_B_FILE,
            SNAPSHOT_POINTER_FILE,
            OBSERVATION_EVENTS_FILE,
            COMMAND_QUEUE_FILE,
            COMMAND_ACK_FILE,
            GAME_HEARTBEAT_FILE,
            SIDECAR_HEARTBEAT_FILE,
            PANIC_STOP_FILE,
            SIDECAR_LOCK_FILE,
            LOGS_DIR,
        )

    def managed_paths(self) -> tuple[Path, ...]:
        """Every path this layout may itself create, excluding rotated journals."""
        return tuple(self.root / name for name in self.managed_names())

    def is_managed_path(self, path: Path) -> bool:
        """True when *path* is one this layout owns and may be written.

        Accepts the fixed names, their temporary twins, rotated journal files
        (``command.ack.0001.jsonl.2``) and anything under ``logs/``. Everything
        else — including a path that merely resolves back inside the root after
        traversal — is rejected, because the only legitimate way to obtain a
        path in this directory is to ask the layout for it.
        """
        candidate = _normalise(path)
        root = _normalise(self.root)
        if candidate == root:
            return False
        # The root is resolved, the names are not: a symlink standing in for a
        # managed name resolves somewhere else and stops matching, which is the
        # whole point of comparing a fully resolved candidate.
        managed = {root / name for name in self.managed_names()}
        if candidate in managed:
            return True
        if (root / LOGS_DIR) in candidate.parents:
            return True
        if candidate.parent != root:
            return False
        stem = candidate.name
        if stem.endswith(TEMP_SUFFIX):
            stem = stem[: -len(TEMP_SUFFIX)]
        base, _, rotation = stem.rpartition(".")
        if base and rotation.isdigit():
            stem = base
        return (root / stem) in managed

    def ensure(self) -> None:
        """Create the exchange directory and ``logs/`` if they are missing."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)


def _normalise(path: Path) -> Path:
    """Resolve without requiring the file to exist.

    ``Path.resolve()`` on a missing file is fine on 3.11, but it also follows
    symlinks; that is deliberate here — a symlink pointing out of the exchange
    directory must not pass :meth:`IpcLayout.is_managed_path`.
    """
    return Path(path).expanduser().resolve()
