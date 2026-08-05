"""Append-only JSONL streams with resumable, loss-reporting reads.

§3.5 fixes the rules this module implements: one record per line, the newline
is what commits the record, an incomplete trailing line is ignored until it is
finished, the reader keeps a byte offset, files are size-bounded, and a reader
that crosses a rotation is *told* rather than silently losing records.

Two invariants carry the design:

* **A record exists only once its newline is on disk.** The writer emits the
  whole line in one write; the reader never parses past the last newline it can
  see. A half-written line is therefore re-read next time, not half-parsed.
* **Rotation is discoverable from the file alone.** Every file starts with a
  header record carrying a serial number, so a reader that wakes up after one
  or more rotations recognises the situation from the header rather than from a
  guess about file sizes, and can drain the rotated predecessors in order.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Final

from .atomic import encode_json_line, guard_managed
from .clocks import Clock, system_clock_ms
from .layout import IpcLayout

#: Record ``type`` values the journal reserves for itself. A caller that tries
#: to append one is rejected: forging a header would let a producer make a
#: consumer believe a rotation happened.
HEADER_TYPE: Final = "journal.header"
ROTATED_TYPE: Final = "journal.rotated"
RESERVED_TYPES: Final = frozenset({HEADER_TYPE, ROTATED_TYPE})

DEFAULT_MAX_BYTES: Final = 1024 * 1024
DEFAULT_KEEP: Final = 3
DEFAULT_MAX_RECORDS_PER_READ: Final = 512

#: A single line longer than this cannot be a legitimate record. It is skipped
#: with a diagnostic so a producer that wedged mid-line cannot stall the reader
#: forever waiting for a newline that will never arrive.
MAX_LINE_BYTES: Final = 256 * 1024

#: Upper bound on the bytes one read syscall pulls into memory.
MAX_READ_BYTES: Final = 4 * MAX_LINE_BYTES


class JournalError(ValueError):
    """A journal could not be opened or appended to."""


@dataclass(frozen=True, slots=True)
class JournalHeader:
    """The first line of every journal file."""

    serial: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """One decoded line, with the byte offset it started at."""

    offset: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class JournalDiagnostic:
    """A line that was complete but unusable. Reported, skipped, never fatal."""

    offset: int
    detail: str
    excerpt: str = ""


@dataclass(frozen=True, slots=True)
class JournalRead:
    """The outcome of one poll of a journal."""

    records: tuple[JournalRecord, ...] = ()
    diagnostics: tuple[JournalDiagnostic, ...] = ()
    offset: int = 0
    serial: int | None = None
    rotations: int = 0
    lost_serials: tuple[int, ...] = ()
    pending_bytes: int = 0

    @property
    def rotated(self) -> bool:
        """True when the reader crossed at least one rotation this poll."""
        return self.rotations > 0

    @property
    def lost_records(self) -> bool:
        """True when a rotated file the reader still needed had been pruned."""
        return bool(self.lost_serials)

    @property
    def payloads(self) -> tuple[dict[str, Any], ...]:
        return tuple(record.payload for record in self.records)


@dataclass(frozen=True, slots=True)
class _Segment:
    """A journal file the reader must consume, and where to start in it."""

    path: Path
    start: int
    serial: int
    live: bool


def rotated_path(path: Path, index: int) -> Path:
    """Path of the *index*-th rotated generation (``…jsonl.1`` is the newest)."""
    if index < 1:
        raise ValueError(f"rotation index must be >= 1, got {index}")
    return path.with_name(f"{path.name}.{index}")


def probe_header(path: Path) -> tuple[JournalHeader | None, str | None]:
    """Read a journal file's header, and say why when there is not one.

    The second element separates the two very different reasons a header can be
    missing. ``None`` means "nothing to report": the file does not exist yet, or
    its first line is still being written — both resolve by themselves on the
    next poll. A string means the file is there and *cannot* be used: an I/O
    error, or a first line that is complete but is not a header. That case must
    reach the caller, because the reader will otherwise return an empty,
    healthy-looking poll for a stream it is in fact unable to read at all.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.readline(MAX_LINE_BYTES)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"journal is unreadable: {exc}"
    if not raw.endswith(b"\n"):
        if len(raw) >= MAX_LINE_BYTES:
            return None, f"journal header exceeds {MAX_LINE_BYTES} bytes"
        # An incomplete first line: the producer is mid-write, or the file is
        # empty because it was only just created.
        return None, None
    try:
        parsed: Any = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None, "journal does not start with a header record"
    if not isinstance(parsed, dict) or parsed.get("type") != HEADER_TYPE:
        return None, "journal does not start with a header record"
    serial = parsed.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 0:
        return None, "journal header carries no usable serial"
    return JournalHeader(serial=serial, end_offset=len(raw)), None


def read_header(path: Path) -> JournalHeader | None:
    """Read a journal file's header line, or None if it has no usable one."""
    return probe_header(path)[0]


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _highest_rotated_serial(path: Path, keep: int) -> int:
    highest = -1
    for index in range(1, keep + 1):
        header = read_header(rotated_path(path, index))
        if header is not None:
            highest = max(highest, header.serial)
    return highest


class JournalWriter:
    """Appends records to one JSONL stream, rotating on a size cap.

    The handle stays open: reopening per record would multiply the syscall cost
    of an observation tick, and the append itself is a single write of a
    complete line, which is what keeps a concurrent reader from seeing a torn
    record.
    """

    def __init__(
        self,
        layout: IpcLayout,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep: int = DEFAULT_KEEP,
        clock: Clock = system_clock_ms,
        fsync: bool = False,
    ) -> None:
        if max_bytes < MAX_LINE_BYTES:
            raise ValueError(f"max_bytes must be at least {MAX_LINE_BYTES}, got {max_bytes}")
        if keep < 0:
            raise ValueError(f"keep must be non-negative, got {keep}")
        self._path = guard_managed(layout, path)
        self._max_bytes = max_bytes
        self._keep = keep
        self._clock = clock
        self._fsync = fsync
        self._serial = 0
        self._appended = 0
        self._size = 0
        self._header_bytes = 0
        self._handle = self._open()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def serial(self) -> int:
        """Serial of the file currently being appended to."""
        return self._serial

    @property
    def size(self) -> int:
        return self._size

    @property
    def appended(self) -> int:
        """Records this writer instance has appended to the current file."""
        return self._appended

    def _open(self) -> IO[str]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        header = read_header(self._path)
        if header is not None:
            self._serial = header.serial
            self._size = _file_size(self._path)
            self._header_bytes = header.end_offset
            return self._path.open("a", encoding="utf-8", newline="\n")
        if _file_size(self._path) > 0:
            # A file without a readable header cannot be resumed: a reader has
            # no way to tell its records apart from a previous generation's.
            # Keep it as a rotated generation and start a fresh serial above
            # anything already on disk.
            self._serial = _highest_rotated_serial(self._path, self._keep) + 1
            self._retire_current()
        return self._start_file()

    def _start_file(self) -> IO[str]:
        handle = self._path.open("a", encoding="utf-8", newline="\n")
        self._size = 0
        self._appended = 0
        self._header_bytes = self._write_line(
            handle,
            {"type": HEADER_TYPE, "serial": self._serial, "created_at_ms": self._clock()},
        )
        return handle

    def _write_line(self, handle: IO[str], payload: Mapping[str, Any]) -> int:
        """Write one complete line; returns the file size after the write."""
        line = encode_json_line(payload)
        encoded_length = len(line.encode("utf-8"))
        if encoded_length > MAX_LINE_BYTES:
            raise JournalError(f"record of {encoded_length} bytes exceeds {MAX_LINE_BYTES}")
        handle.write(line)
        handle.flush()
        if self._fsync:
            os.fsync(handle.fileno())
        self._size += encoded_length
        return self._size

    def append(self, payload: Mapping[str, Any]) -> int:
        """Append one record and return the byte offset its line starts at."""
        record_type = payload.get("type")
        if isinstance(record_type, str) and record_type in RESERVED_TYPES:
            raise JournalError(f"record type {record_type!r} is reserved for the journal itself")
        line_length = len(encode_json_line(payload).encode("utf-8"))
        has_records = self._size > self._header_bytes
        if has_records and self._size + line_length > self._max_bytes:
            self.rotate()
        offset = self._size
        self._write_line(self._handle, payload)
        self._appended += 1
        return offset

    def rotate(self) -> int:
        """Close the current file and start the next one. Returns the new serial.

        The outgoing file gets a trailing rotation marker so a reader sitting
        exactly at its end learns the stream continued elsewhere even before it
        looks at the live file's header.
        """
        self._write_line(
            self._handle,
            {
                "type": ROTATED_TYPE,
                "serial": self._serial,
                "next_serial": self._serial + 1,
                "records_appended": self._appended,
                "rotated_at_ms": self._clock(),
            },
        )
        self._handle.close()
        self._retire_current()
        self._serial += 1
        self._handle = self._start_file()
        return self._serial

    def _retire_current(self) -> None:
        """Move the live file into the rotated set, or drop it when keep is 0."""
        if not self._keep:
            self._path.unlink(missing_ok=True)
            return
        oldest = rotated_path(self._path, self._keep)
        oldest.unlink(missing_ok=True)
        for index in range(self._keep - 1, 0, -1):
            source = rotated_path(self._path, index)
            if source.exists():
                source.replace(rotated_path(self._path, index + 1))
        self._path.replace(rotated_path(self._path, 1))

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()

    def __enter__(self) -> JournalWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


@dataclass
class JournalReader:
    """Resumable reader over one JSONL stream.

    The reader is deliberately stateful about two things only — the byte offset
    and the serial of the file that offset refers to — because those two values
    are exactly what has to be persisted to resume after a sidecar restart
    without re-reading anything.
    """

    layout: IpcLayout
    path: Path
    keep: int = DEFAULT_KEEP
    max_records: int = DEFAULT_MAX_RECORDS_PER_READ
    _offset: int = field(default=0, init=False)
    _serial: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        guard_managed(self.layout, self.path)
        if self.max_records < 1:
            raise ValueError(f"max_records must be positive, got {self.max_records}")

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def serial(self) -> int | None:
        return self._serial

    def resume(self, *, offset: int, serial: int | None) -> None:
        """Restore a previously persisted position."""
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        self._offset = offset
        self._serial = serial

    def seek_to_end(self) -> None:
        """Skip everything already written.

        A restarting sidecar uses this on the *command* stream: §3.12 says a
        restart must not re-execute commands, and the cheapest way to guarantee
        that is never to hand the old ones to an executor at all.

        Raises :class:`JournalError` when the end cannot be established. Leaving
        the position at zero instead would silently promise a skip that did not
        happen, and the records that would then be replayed are commands.
        """
        header, problem = probe_header(self.path)
        if problem is not None:
            raise JournalError(f"cannot skip to the end of {self.path.name}: {problem}")
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        except OSError as exc:
            raise JournalError(f"cannot skip to the end of {self.path.name}: {exc}") from exc
        self._offset = size
        self._serial = None if header is None else header.serial

    def read(self) -> JournalRead:
        """Consume every complete record that is new since the last call."""
        header, problem = probe_header(self.path)
        if header is None:
            # A missing file or a half-written first line is "nothing readable
            # yet" and says so silently; anything else is a stream this reader
            # cannot consume, and reporting that as a clean empty poll would
            # hide a broken exchange directory for as long as it stays broken.
            return JournalRead(
                offset=self._offset,
                serial=self._serial,
                diagnostics=(
                    ()
                    if problem is None
                    else (JournalDiagnostic(offset=self._offset, detail=problem),)
                ),
            )

        segments, lost = self._plan(header)
        records: list[JournalRecord] = []
        diagnostics: list[JournalDiagnostic] = [
            JournalDiagnostic(
                offset=self._offset,
                detail=f"records from journal serial {serial} were pruned before being read",
            )
            for serial in lost
        ]

        rotations = 0
        final_offset = self._offset
        for segment in segments:
            # A rotated segment is drained without a record budget: it is
            # already size-bounded, and the alternative — stopping partway — is
            # exactly the silent loss this reader exists to prevent.
            budget = self.max_records if segment.live else None
            consumed, seg_diags, new_offset = _consume(segment.path, segment.start, budget)
            records.extend(consumed)
            diagnostics.extend(seg_diags)
            if segment.live:
                final_offset = new_offset
            else:
                rotations += 1

        self._offset = final_offset
        self._serial = header.serial
        return JournalRead(
            records=tuple(records),
            diagnostics=tuple(diagnostics),
            offset=final_offset,
            serial=header.serial,
            rotations=rotations,
            lost_serials=tuple(lost),
            pending_bytes=max(0, _file_size(self.path) - final_offset),
        )

    def _plan(self, header: JournalHeader) -> tuple[list[_Segment], list[int]]:
        """Decide which files to drain, oldest first, and what was lost."""
        if self._serial is None or self._serial == header.serial:
            start = max(self._offset, header.end_offset)
            return [_Segment(self.path, start, header.serial, live=True)], []

        # The live file is a later generation than the one we were reading, so
        # one or more rotations happened while we were away.
        rotated: dict[int, tuple[Path, JournalHeader]] = {}
        for index in range(1, self.keep + 1):
            candidate = rotated_path(self.path, index)
            candidate_header = read_header(candidate)
            if candidate_header is not None:
                rotated[candidate_header.serial] = (candidate, candidate_header)

        segments: list[_Segment] = []
        lost: list[int] = []
        for serial in range(self._serial, header.serial):
            entry = rotated.get(serial)
            if entry is None:
                lost.append(serial)
                continue
            path, rotated_header = entry
            start = self._offset if serial == self._serial else rotated_header.end_offset
            segments.append(
                _Segment(path, max(start, rotated_header.end_offset), serial, live=False)
            )
        segments.append(_Segment(self.path, header.end_offset, header.serial, live=True))
        return segments, lost


def _consume(
    path: Path, start: int, budget: int | None
) -> tuple[list[JournalRecord], list[JournalDiagnostic], int]:
    """Parse complete lines from *start*, stopping at the last newline.

    Returns the records, the diagnostics for lines that were complete but
    unusable, and the offset the caller should resume from — which never points
    into the middle of a line.
    """
    records: list[JournalRecord] = []
    diagnostics: list[JournalDiagnostic] = []
    offset = start
    while budget is None or len(records) < budget:
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(MAX_READ_BYTES)
        except OSError as exc:
            diagnostics.append(JournalDiagnostic(offset=offset, detail=str(exc)))
            return records, diagnostics, offset
        if not data:
            return records, diagnostics, offset

        cut = data.rfind(b"\n")
        if cut == -1:
            if len(data) > MAX_LINE_BYTES:
                # An unterminated line this long will never become a valid
                # record; waiting for its newline would stall the stream.
                diagnostics.append(
                    JournalDiagnostic(
                        offset=offset,
                        detail=f"unterminated line exceeds {MAX_LINE_BYTES} bytes; skipped",
                        excerpt=_excerpt(data),
                    )
                )
                offset += len(data)
            return records, diagnostics, offset

        offset, stopped_early = _parse_lines(data[: cut + 1], offset, records, diagnostics, budget)
        if stopped_early:
            return records, diagnostics, offset
    return records, diagnostics, offset


def _parse_lines(
    block: bytes,
    offset: int,
    records: list[JournalRecord],
    diagnostics: list[JournalDiagnostic],
    budget: int | None,
) -> tuple[int, bool]:
    """Decode one newline-terminated block. Returns the offset and whether the
    record budget ran out partway (in which case the offset sits on a line
    boundary and the next poll continues from there)."""
    for raw in block.splitlines(keepends=True):
        if budget is not None and len(records) >= budget:
            return offset, True
        line_offset = offset
        offset += len(raw)
        stripped = raw.strip()
        if not stripped:
            continue
        if len(raw) > MAX_LINE_BYTES:
            diagnostics.append(
                JournalDiagnostic(
                    offset=line_offset,
                    detail=f"line exceeds {MAX_LINE_BYTES} bytes; skipped",
                    excerpt=_excerpt(raw),
                )
            )
            continue
        try:
            parsed: Any = json.loads(stripped.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            diagnostics.append(
                JournalDiagnostic(
                    offset=line_offset,
                    detail=f"corrupt record: {exc}",
                    excerpt=_excerpt(raw),
                )
            )
            continue
        if not isinstance(parsed, dict):
            diagnostics.append(
                JournalDiagnostic(
                    offset=line_offset,
                    detail=f"corrupt record: expected an object, got {type(parsed).__name__}",
                    excerpt=_excerpt(raw),
                )
            )
            continue
        if parsed.get("type") in RESERVED_TYPES:
            # Structural markers belong to the journal, not to the consumer.
            continue
        records.append(JournalRecord(offset=line_offset, payload=parsed))
    return offset, False


def _excerpt(raw: bytes, limit: int = 120) -> str:
    """A short, printable fragment for a diagnostic — never the whole line."""
    text = raw[:limit].decode("utf-8", errors="replace").rstrip("\r\n")
    return text + "…" if len(raw) > limit else text
