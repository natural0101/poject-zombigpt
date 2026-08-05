"""File-based IPC between the Lua mod and the sidecar.

Everything the two processes say to each other passes through this package:
a fixed directory layout (:mod:`.layout`), whole-file writes that are never
observed half-finished (:mod:`.atomic`, :mod:`.snapshot`), append-only journals
with resumable reads (:mod:`.journal`), and the command stream with its
sequencing, idempotency, lease and backpressure rules (:mod:`.queue`).

Nothing here interprets game semantics. It moves bytes correctly, reports what
it could not read, and refuses to invent anything it did not observe.
"""

from __future__ import annotations

from .atomic import (
    DocumentError,
    IpcPathError,
    encode_json_line,
    guard_managed,
    read_json_document,
    write_json_atomic,
)
from .clocks import Clock, system_clock_ms
from .journal import (
    JournalDiagnostic,
    JournalError,
    JournalHeader,
    JournalRead,
    JournalReader,
    JournalRecord,
    JournalWriter,
    read_header,
    rotated_path,
)
from .layout import IpcLayout, SnapshotSlot
from .queue import (
    QUEUE_OCCUPYING_ACTIONS,
    AckPoll,
    CommandQueue,
    IdempotencyCache,
    LeaseCheckpoint,
    SequenceCheck,
    SequenceEvent,
    SequenceTracker,
    Stream,
    SubmitOutcome,
)
from .snapshot import SnapshotMiss, SnapshotRead, SnapshotReader, SnapshotWriter

__all__ = [
    "QUEUE_OCCUPYING_ACTIONS",
    "AckPoll",
    "Clock",
    "CommandQueue",
    "DocumentError",
    "IdempotencyCache",
    "IpcLayout",
    "IpcPathError",
    "JournalDiagnostic",
    "JournalError",
    "JournalHeader",
    "JournalRead",
    "JournalReader",
    "JournalRecord",
    "JournalWriter",
    "LeaseCheckpoint",
    "SequenceCheck",
    "SequenceEvent",
    "SequenceTracker",
    "SnapshotMiss",
    "SnapshotRead",
    "SnapshotReader",
    "SnapshotSlot",
    "SnapshotWriter",
    "Stream",
    "SubmitOutcome",
    "encode_json_line",
    "guard_managed",
    "read_header",
    "read_json_document",
    "rotated_path",
    "system_clock_ms",
    "write_json_atomic",
]
