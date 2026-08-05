"""Bounded diagnostic logging: one JSONL stream and one human-readable stream.

The pair exists because the two readers are different. A support bundle and
``pz-agent replay`` consume the JSONL; a user looking for the last thing that
happened reads the text file. They are written from the same record, so they can
never disagree about what occurred.

Three bounds hold, and each has a test that proves the cap rather than the
happy path:

* **Per file.** A file is rotated once the next line would take it past
  ``max_bytes``; the rotation is counted and the counter is public, so "did it
  rotate?" is an observation rather than an inference from file sizes.
* **Per file count.** At most ``keep`` rotated generations survive; the oldest
  is deleted by the rotation that displaces it. Total bytes on disk are
  therefore bounded by ``(keep + 1) * max_bytes``.
* **Per record.** A record over ``max_record_bytes`` is replaced by a marker
  record naming the event and the size it would have taken. Dropping it in
  silence would make the log lie by omission.

Every record passes through a :class:`~pz_agent_core.diagnostics.redaction.Redactor`
on the way to both files. There is no unredacted write path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from ..ipc.clocks import Clock, system_clock_ms
from ..protocol import JsonDict
from .redaction import Redactor, null_redactor

#: One megabyte of diagnostics is several days of normal operation.
DEFAULT_MAX_BYTES: Final = 1024 * 1024

#: Rotated generations kept besides the current file.
DEFAULT_KEEP: Final = 3

#: Ceiling on a single record. Comfortably larger than an action trace entry,
#: far smaller than an observation snapshot pasted in by accident.
DEFAULT_MAX_RECORD_BYTES: Final = 16 * 1024

#: Fields carried by one structured record. Beyond this the extras are dropped
#: and the count of dropped keys is recorded in their place.
MAX_FIELDS: Final = 32

#: Event names are identifiers, not prose.
MAX_EVENT_LEN: Final = 80

_EVENT_TRUNCATED: Final = "diagnostics.record_truncated"
_FIELDS_DROPPED_KEY: Final = "fields_dropped"


class DiagnosticsError(ValueError):
    """A diagnostic record or writer configuration is unusable."""


class LogLevel(StrEnum):
    """Severity of one record. Ordered by :data:`LEVEL_ORDER`."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


#: Ascending severity, so a threshold comparison is a lookup rather than a guess.
LEVEL_ORDER: Final[Mapping[LogLevel, int]] = {
    LogLevel.DEBUG: 10,
    LogLevel.INFO: 20,
    LogLevel.WARNING: 30,
    LogLevel.ERROR: 40,
}


def iso_from_ms(timestamp_ms: int) -> str:
    """Render a wall-clock millisecond stamp as UTC ISO-8601."""
    moment = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class LogRecord:
    """One diagnostic event, already redacted."""

    timestamp_ms: int
    level: LogLevel
    event: str
    fields: JsonDict

    def __post_init__(self) -> None:
        if not self.event:
            raise DiagnosticsError("a log record must name an event")
        if len(self.event) > MAX_EVENT_LEN:
            raise DiagnosticsError(f"event name longer than {MAX_EVENT_LEN}: {self.event[:40]!r}")

    def to_dict(self) -> JsonDict:
        return {
            "timestamp_ms": self.timestamp_ms,
            "timestamp": iso_from_ms(self.timestamp_ms),
            "level": self.level.value,
            "event": self.event,
            "fields": dict(self.fields),
        }

    def to_line(self) -> str:
        """The human-readable rendering: one line, sorted fields, no wrapping."""
        rendered = " ".join(
            f"{key}={_render_scalar(value)}" for key, value in sorted(self.fields.items())
        )
        head = f"{iso_from_ms(self.timestamp_ms)} {self.level.value.upper():<7} {self.event}"
        return f"{head} {rendered}".rstrip()


def _render_scalar(value: Any) -> str:
    """Render one field for the text log without ever emitting a newline."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class LogLimits:
    """The three caps. Validated here so no writer can be built past them."""

    max_bytes: int = DEFAULT_MAX_BYTES
    keep: int = DEFAULT_KEEP
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes < 1:
            raise DiagnosticsError(f"max_bytes must be positive, got {self.max_bytes}")
        if self.keep < 1:
            raise DiagnosticsError(f"keep must be at least 1, got {self.keep}")
        if self.max_record_bytes < 1:
            raise DiagnosticsError(
                f"max_record_bytes must be positive, got {self.max_record_bytes}"
            )
        if self.max_record_bytes > self.max_bytes:
            # Otherwise a legal record could never be written: rotation would
            # produce an empty file that still had no room for it.
            raise DiagnosticsError(
                f"max_record_bytes ({self.max_record_bytes}) cannot exceed "
                f"max_bytes ({self.max_bytes})"
            )

    @property
    def max_total_bytes(self) -> int:
        """Ceiling on everything one writer can leave on disk."""
        return self.max_bytes * (self.keep + 1)


DEFAULT_LIMITS: Final = LogLimits()


class RotatingWriter:
    """Append-only text file with size-triggered rotation and a bounded history.

    The handle is not held open between writes. Diagnostics are low-rate, and a
    writer that owns no descriptor cannot leave a half-flushed tail behind when
    the process dies, nor block a rotation on Windows.
    """

    def __init__(self, path: Path, limits: LogLimits = DEFAULT_LIMITS) -> None:
        self._path = path
        self._limits = limits
        self._rotations = 0
        self._written = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self._size = path.stat().st_size if path.is_file() else 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def limits(self) -> LogLimits:
        return self._limits

    @property
    def rotations(self) -> int:
        """How many times this writer has rotated. Rotation is observable."""
        return self._rotations

    @property
    def size(self) -> int:
        """Bytes in the current file."""
        return self._size

    @property
    def records_written(self) -> int:
        return self._written

    def rotated_path(self, index: int) -> Path:
        if index < 1:
            raise DiagnosticsError(f"rotation index must be at least 1, got {index}")
        return self._path.with_name(f"{self._path.name}.{index}")

    def files(self) -> tuple[Path, ...]:
        """Existing files this writer owns, newest first."""
        found = [self._path] if self._path.is_file() else []
        for index in range(1, self._limits.keep + 1):
            candidate = self.rotated_path(index)
            if candidate.is_file():
                found.append(candidate)
        return tuple(found)

    def total_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.files())

    def write(self, line: str) -> int:
        """Append one line, rotating first if it would not fit. Returns bytes written.

        Raises:
            DiagnosticsError: if the line is longer than ``max_record_bytes``.
                Callers hold the policy for oversize records; the writer refuses
                rather than silently truncating a record into a malformed one.
        """
        payload = line if line.endswith("\n") else line + "\n"
        encoded = payload.encode("utf-8")
        if len(encoded) > self._limits.max_record_bytes:
            raise DiagnosticsError(
                f"line of {len(encoded)} bytes exceeds max_record_bytes "
                f"{self._limits.max_record_bytes}"
            )
        if self._size > 0 and self._size + len(encoded) > self._limits.max_bytes:
            self.rotate()
        with self._path.open("ab") as handle:
            handle.write(encoded)
        self._size += len(encoded)
        self._written += 1
        return len(encoded)

    def rotate(self) -> int:
        """Shift the generations down by one and start a new current file.

        Returns the rotation count after the shift. The oldest generation is
        removed by the same call that displaces it, which is what keeps the file
        count — and therefore the bytes on disk — bounded.
        """
        oldest = self.rotated_path(self._limits.keep)
        oldest.unlink(missing_ok=True)
        for index in range(self._limits.keep - 1, 0, -1):
            source = self.rotated_path(index)
            if source.is_file():
                os.replace(source, self.rotated_path(index + 1))
        if self._path.is_file():
            os.replace(self._path, self.rotated_path(1))
        self._size = 0
        self._rotations += 1
        return self._rotations


class DiagnosticLog:
    """The pair of streams, written from one redacted record.

    ``name`` gives the file stems: ``<name>.jsonl`` and ``<name>.log``.
    """

    def __init__(
        self,
        directory: Path,
        *,
        redactor: Redactor | None = None,
        clock: Clock = system_clock_ms,
        limits: LogLimits = DEFAULT_LIMITS,
        name: str = "pz-agent",
        min_level: LogLevel = LogLevel.DEBUG,
    ) -> None:
        if not name or any(separator in name for separator in ("/", "\\", os.sep)):
            raise DiagnosticsError(f"log name must be a bare stem, got {name!r}")
        self._redactor = null_redactor() if redactor is None else redactor
        self._clock = clock
        self._min_level = min_level
        self._structured = RotatingWriter(directory / f"{name}.jsonl", limits)
        self._human = RotatingWriter(directory / f"{name}.log", limits)
        self._suppressed = 0

    @property
    def structured(self) -> RotatingWriter:
        return self._structured

    @property
    def human(self) -> RotatingWriter:
        return self._human

    @property
    def redactor(self) -> Redactor:
        return self._redactor

    @property
    def suppressed(self) -> int:
        """Records dropped by the level threshold. Counted, not forgotten."""
        return self._suppressed

    def files(self) -> tuple[Path, ...]:
        return self._structured.files() + self._human.files()

    def log(self, level: LogLevel, event: str, **fields: Any) -> LogRecord | None:
        """Write one record to both streams. Returns what was written.

        Returns ``None`` when *level* is below the threshold — the record is
        counted in :attr:`suppressed` so a quiet log is distinguishable from an
        idle one.
        """
        if LEVEL_ORDER[level] < LEVEL_ORDER[self._min_level]:
            self._suppressed += 1
            return None
        record = LogRecord(
            timestamp_ms=self._clock(),
            level=level,
            event=event,
            fields=self._bounded_fields(fields),
        )
        return self._emit(record)

    def debug(self, event: str, **fields: Any) -> LogRecord | None:
        return self.log(LogLevel.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> LogRecord | None:
        return self.log(LogLevel.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> LogRecord | None:
        return self.log(LogLevel.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> LogRecord | None:
        return self.log(LogLevel.ERROR, event, **fields)

    def _bounded_fields(self, fields: Mapping[str, Any]) -> JsonDict:
        redacted: JsonDict = {}
        dropped = 0
        for key, value in fields.items():
            if len(redacted) >= MAX_FIELDS:
                dropped += 1
                continue
            redacted[self._redactor.text(str(key))] = self._redactor.value(value)
        if dropped:
            redacted[_FIELDS_DROPPED_KEY] = dropped
        return redacted

    def _emit(self, record: LogRecord) -> LogRecord:
        line = json.dumps(
            record.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        limit = self._structured.limits.max_record_bytes
        if len(line.encode("utf-8")) + 1 > limit:
            record = LogRecord(
                timestamp_ms=record.timestamp_ms,
                level=LogLevel.WARNING,
                event=_EVENT_TRUNCATED,
                fields={
                    "original_event": record.event,
                    "original_level": record.level.value,
                    "bytes": len(line.encode("utf-8")),
                    "limit_bytes": limit,
                },
            )
            line = json.dumps(
                record.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
            )
        self._structured.write(line)
        self._human.write(_bounded_line(record.to_line(), limit))
        return record


def _bounded_line(line: str, limit: int) -> str:
    """Cut a human-readable line to fit the per-record cap.

    The text stream may be truncated where the structured one may not: it is a
    rendering, and the machine-readable record next to it is intact.
    """
    encoded = line.encode("utf-8")
    if len(encoded) + 1 <= limit:
        return line
    return encoded[: limit - 16].decode("utf-8", errors="ignore") + " …<TRUNCATED>"


def read_records(
    path: Path, *, limit: int = 500, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[tuple[JsonDict, ...], tuple[str, ...]]:
    """Read the last *limit* structured records from a JSONL log file.

    Returns ``(records, problems)``. A line that does not parse is reported and
    skipped: one corrupt record must not make a log unreadable, which is the
    same rule :mod:`pz_agent_core.ipc.journal` follows on the wire.
    """
    if limit < 1:
        raise DiagnosticsError(f"limit must be positive, got {limit}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return (), (f"{path.name}: cannot read ({exc.strerror or exc})",)
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
    text = raw.decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    records: list[JsonDict] = []
    problems: list[str] = []
    for number, line in enumerate(lines[-limit:], start=max(1, len(lines) - limit + 1)):
        try:
            parsed: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}:{number}: not JSON ({exc.msg})")
            continue
        if not isinstance(parsed, dict):
            problems.append(f"{path.name}:{number}: expected an object")
            continue
        records.append(parsed)
    return tuple(records), tuple(problems)
