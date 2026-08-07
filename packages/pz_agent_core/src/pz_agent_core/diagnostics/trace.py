"""The action trace and the observation-diff trace, and the reader that replays them.

A trace is a JSONL stream of numbered entries in one file, written by the same
bounded, redacting machinery as the diagnostic log. Two kinds of entry carry the
history worth replaying:

* an **action** entry — the command as issued and the terminal result as
  observed, so a support report shows what was asked for next to what actually
  happened;
* an **observation** entry, either a full snapshot or a tier-1 diff against the
  entry before it, so ``pz-agent replay`` can walk the world forward exactly the
  way the sidecar saw it.

:func:`replay_observations` is what makes the second kind more than decoration:
it rebuilds the snapshot series with
:func:`~pz_agent_core.observation.diff.apply_diff` and refuses — rather than
guessing — when a diff arrives with no snapshot to apply it to.

One honesty note the reader must carry: a trace is bounded and redacted, so it
holds the *recorded* view. An entry that was too large for the record cap is
present as a marker naming what was dropped, never absent.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from ..ipc.clocks import Clock, system_clock_ms
from ..observation.diff import DiffError, ObservationDiff, apply_diff, diff_observations
from ..protocol import ActionResult, Command, JsonDict, Observation
from ..protocol.messages import ProtocolError
from .log import DiagnosticsError, LogLimits, RotatingWriter
from .redaction import Redactor, null_redactor

#: An observation snapshot is an order of magnitude larger than a log record, so
#: the trace gets its own ceiling rather than borrowing the log's.
DEFAULT_TRACE_RECORD_BYTES: Final = 128 * 1024

DEFAULT_TRACE_BYTES: Final = 4 * 1024 * 1024

DEFAULT_TRACE_KEEP: Final = 2

#: Entries one :func:`read_trace` call returns. A longer trace is reported as
#: truncated rather than pulled entirely into memory.
MAX_TRACE_ENTRIES: Final = 5_000

DEFAULT_TRACE_LIMITS: Final = LogLimits(
    max_bytes=DEFAULT_TRACE_BYTES,
    keep=DEFAULT_TRACE_KEEP,
    max_record_bytes=DEFAULT_TRACE_RECORD_BYTES,
)

_EVENT_DROPPED: Final = "trace.entry_dropped"


class TraceError(ValueError):
    """A trace could not be written, read or replayed."""


class TraceKind(StrEnum):
    """What one entry records."""

    ACTION = "action"
    OBSERVATION = "observation"
    OBSERVATION_DIFF = "observation_diff"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One numbered entry. ``seq`` is the trace's own counter, not the wire seq."""

    seq: int
    timestamp_ms: int
    kind: TraceKind
    payload: JsonDict

    def __post_init__(self) -> None:
        if self.seq < 0:
            raise TraceError(f"trace seq must be non-negative, got {self.seq}")
        if self.timestamp_ms < 0:
            raise TraceError(f"trace timestamp must be non-negative, got {self.timestamp_ms}")

    def to_dict(self) -> JsonDict:
        return {
            "seq": self.seq,
            "timestamp_ms": self.timestamp_ms,
            "kind": self.kind.value,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> TraceEntry:
        """Parse one entry.

        Raises:
            TraceError: when the document is not a complete entry. A partial
                entry is never returned half-built.
        """
        if not isinstance(payload, dict):
            raise TraceError("a trace entry must be a JSON object")
        try:
            kind = TraceKind(payload["kind"])
        except (KeyError, ValueError) as exc:
            raise TraceError(f"unknown trace kind: {payload.get('kind')!r}") from exc
        seq = payload.get("seq")
        stamp = payload.get("timestamp_ms")
        body = payload.get("payload")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise TraceError("trace entry 'seq' must be an integer")
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            raise TraceError("trace entry 'timestamp_ms' must be an integer")
        if not isinstance(body, dict):
            raise TraceError("trace entry 'payload' must be an object")
        return cls(seq=seq, timestamp_ms=stamp, kind=kind, payload=body)

    def summary(self) -> str:
        """One line describing the entry, for ``pz-agent replay``."""
        if self.kind is TraceKind.ACTION:
            action = self.payload.get("action", "?")
            status = self.payload.get("status", "no-result")
            reason = self.payload.get("reason_code", "")
            tail = f" {reason}" if reason else ""
            return f"action {action} -> {status}{tail}"
        if self.kind is TraceKind.OBSERVATION:
            return f"observation full seq={self.payload.get('observation_seq', '?')}"
        if self.kind is TraceKind.OBSERVATION_DIFF:
            return (
                f"observation diff {self.payload.get('from_seq', '?')}"
                f"->{self.payload.get('to_seq', '?')}"
            )
        return f"dropped {self.payload.get('reason', 'unknown')}"


@dataclass(frozen=True, slots=True)
class TraceRead:
    """The outcome of reading a trace file."""

    entries: tuple[TraceEntry, ...] = ()
    problems: tuple[str, ...] = ()
    truncated: bool = False

    def of_kind(self, kind: TraceKind) -> tuple[TraceEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind is kind)


class TraceWriter:
    """Appends entries to one bounded, redacted trace file."""

    def __init__(
        self,
        path: Path,
        *,
        redactor: Redactor | None = None,
        clock: Clock = system_clock_ms,
        limits: LogLimits = DEFAULT_TRACE_LIMITS,
    ) -> None:
        self._redactor = null_redactor() if redactor is None else redactor
        self._clock = clock
        self._writer = RotatingWriter(path, limits)
        self._seq = 0

    @property
    def path(self) -> Path:
        return self._writer.path

    @property
    def writer(self) -> RotatingWriter:
        return self._writer

    @property
    def rotations(self) -> int:
        return self._writer.rotations

    @property
    def entries_written(self) -> int:
        return self._writer.records_written

    def record_action(self, command: Command, result: ActionResult | None = None) -> TraceEntry:
        """Record a command and, when it has finished, the result that closed it."""
        payload: JsonDict = {
            "action": command.action.value,
            "command_id": command.command_id,
            "command": command.to_dict(),
        }
        if result is not None:
            payload["status"] = result.status.value
            payload["reason_code"] = result.reason_code.value
            payload["result"] = result.to_dict()
        return self._append(TraceKind.ACTION, payload)

    def record_refusal(self, result: ActionResult) -> TraceEntry:
        """Record a terminal result for a command that never reached the mod.

        A gate that fires before dispatch — an unusable capability, a denied
        policy, an unknown action — produces a result and no command. Recording
        only the pairs would make the trace lie by omission in exactly the case
        an operator is most likely to be reading it for: they asked for
        something and nothing happened in the world.
        """
        return self._append(
            TraceKind.ACTION,
            {
                "action": result.action,
                "command_id": result.command_id,
                "status": result.status.value,
                "reason_code": result.reason_code.value,
                "result": result.to_dict(),
            },
        )

    def record_observation(self, observation: Observation) -> TraceEntry:
        """Record a full snapshot. A replay needs one of these before any diff."""
        return self._append(
            TraceKind.OBSERVATION,
            {"observation_seq": observation.seq, "observation": observation.to_dict()},
        )

    def record_observation_diff(
        self, *, from_seq: int, to_seq: int, diff: ObservationDiff
    ) -> TraceEntry:
        """Record a tier-1 diff between two snapshots."""
        return self._append(
            TraceKind.OBSERVATION_DIFF,
            {"from_seq": from_seq, "to_seq": to_seq, "diff": diff.to_dict()},
        )

    def record_world(self, observation: Observation, baseline: Observation | None) -> TraceEntry:
        """Record the world as one entry, keeping every file replayable.

        A diff whenever one applies, because a snapshot is an order of magnitude
        larger and a long run would otherwise rotate its own history away within
        minutes. A full snapshot in three cases: there is no baseline, the
        baseline belongs to another session or a later moment, or **writing the
        diff would rotate the file**.

        That last one is not an optimisation. :func:`replay_observations`
        refuses a diff it has no baseline for — rightly, since applying one to
        the wrong snapshot is the only way this module can produce a plausible
        lie — so a rotation that put a diff at the top of the new file would
        make every long run's trace unreplayable from its first line. Letting
        the *snapshot* trigger the rotation instead means the fresh file opens
        with exactly what a replay needs.
        """
        divergent = (
            baseline is None
            or baseline.session_id != observation.session_id
            or observation.seq < baseline.seq
        )
        if divergent or baseline is None:
            return self.record_observation(observation)
        entry, line = self._render(
            TraceKind.OBSERVATION_DIFF,
            {
                "from_seq": baseline.seq,
                "to_seq": observation.seq,
                "diff": diff_observations(baseline, observation).to_dict(),
            },
        )
        if self._writer.would_rotate(line):
            return self.record_observation(observation)
        return self._commit(entry, line)

    def _append(self, kind: TraceKind, payload: JsonDict) -> TraceEntry:
        entry, line = self._render(kind, payload)
        return self._commit(entry, line)

    def _render(self, kind: TraceKind, payload: JsonDict) -> tuple[TraceEntry, str]:
        """Build the entry and the line it will be written as, without writing.

        Split from the write so :meth:`record_world` can ask what a line would
        cost before committing to it.
        """
        redacted = self._redactor.value(payload)
        entry = TraceEntry(
            seq=self._seq,
            timestamp_ms=self._clock(),
            kind=kind,
            payload=redacted if isinstance(redacted, dict) else {"value": redacted},
        )
        line = json.dumps(entry.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        limit = self._writer.limits.max_record_bytes
        if len(line.encode("utf-8")) + 1 > limit:
            # The entry is replaced, never skipped: a gap in the sequence would
            # read as a lost record, which is a different and more alarming fact
            # than an entry that was too large to keep.
            entry = TraceEntry(
                seq=entry.seq,
                timestamp_ms=entry.timestamp_ms,
                kind=TraceKind.DROPPED,
                payload={
                    "reason": "oversize",
                    "original_kind": kind.value,
                    "bytes": len(line.encode("utf-8")),
                    "limit_bytes": limit,
                    "event": _EVENT_DROPPED,
                },
            )
            line = json.dumps(
                entry.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
            )
        return entry, line

    def _commit(self, entry: TraceEntry, line: str) -> TraceEntry:
        self._writer.write(line)
        self._seq += 1
        return entry


def read_trace(
    path: Path,
    *,
    max_entries: int = MAX_TRACE_ENTRIES,
    max_bytes: int = DEFAULT_TRACE_BYTES,
) -> TraceRead:
    """Read a trace file. A malformed line is reported and skipped, never fatal.

    Raises:
        TraceError: when *max_entries* is not positive, or the file cannot be
            opened at all.
    """
    if max_entries < 1:
        raise TraceError(f"max_entries must be positive, got {max_entries}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TraceError(f"{path}: cannot read trace ({exc.strerror or exc})") from exc
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    lines = raw.decode("utf-8", errors="replace").splitlines()
    entries: list[TraceEntry] = []
    problems: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if len(entries) >= max_entries:
            truncated = True
            break
        try:
            document: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}:{number}: not JSON ({exc.msg})")
            continue
        try:
            entries.append(TraceEntry.from_dict(document))
        except TraceError as exc:
            problems.append(f"{path.name}:{number}: {exc}")
    return TraceRead(entries=tuple(entries), problems=tuple(problems), truncated=truncated)


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """One reconstructed snapshot and the entry that produced it."""

    entry: TraceEntry
    observation: Observation


def replay_observations(entries: Sequence[TraceEntry]) -> tuple[ReplayStep, ...]:
    """Rebuild the observation series a trace recorded.

    Raises:
        TraceError: when a diff arrives before any full snapshot, when a
            recorded snapshot does not parse, or when a diff does not apply.
            Each of those means the trace cannot be replayed faithfully, and a
            partially reconstructed world is worse than a refusal — every layer
            above treats a snapshot as current.
    """
    steps: list[ReplayStep] = []
    current: Observation | None = None
    for entry in entries:
        if entry.kind is TraceKind.OBSERVATION:
            current = _parse_observation(entry)
        elif entry.kind is TraceKind.OBSERVATION_DIFF:
            if current is None:
                raise TraceError(
                    f"trace entry {entry.seq}: a diff with no preceding full snapshot; "
                    "the trace cannot be replayed from here"
                )
            current = _apply(entry, current)
        else:
            continue
        steps.append(ReplayStep(entry=entry, observation=current))
    return tuple(steps)


def _parse_observation(entry: TraceEntry) -> Observation:
    document = entry.payload.get("observation")
    if not isinstance(document, dict):
        raise TraceError(f"trace entry {entry.seq}: no observation document")
    try:
        return Observation.from_dict(document)
    except ProtocolError as exc:
        raise TraceError(f"trace entry {entry.seq}: unusable observation ({exc})") from exc


def _apply(entry: TraceEntry, current: Observation) -> Observation:
    document = entry.payload.get("diff")
    if not isinstance(document, dict):
        raise TraceError(f"trace entry {entry.seq}: no diff document")
    try:
        return apply_diff(current, ObservationDiff.from_dict(document))
    except (DiffError, ProtocolError, DiagnosticsError) as exc:
        raise TraceError(f"trace entry {entry.seq}: diff does not apply ({exc})") from exc
