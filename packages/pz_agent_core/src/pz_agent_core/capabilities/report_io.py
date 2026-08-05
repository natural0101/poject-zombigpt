"""Persistence for the generated compatibility report (``compat/generated_api_report.json``).

The file is machine-generated from the user's own installation and is
gitignored: it describes their build, and a report committed by one person would
be a claim about everyone else's machine.

Loading is where the honesty rule earns its keep. A report is only ever loaded
*for* a build, and if the build on disk differs from the build the caller is
running against, every ``verified`` capability is downgraded before the report
is handed back. A probe that passed on 42.19 is not evidence about 42.20 (§12.6),
and the sidecar must fall back to OBSERVE-plus-reprobe rather than trusting it.

Writing is atomic and refuses to place the report inside the game directory: the
scanner only ever reads there and nothing in this project may change that.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .model import (
    REASON_BUILD_CHANGED,
    CapabilityError,
    CapabilityReport,
)
from .scanner import refuse_write

#: Repository-relative default location, as named in §12.5.
DEFAULT_REPORT_PATH: Final = Path("compat") / "generated_api_report.json"

#: A capability report is a few kilobytes. Anything far larger is corrupt or
#: hostile, and is refused before it is parsed rather than after.
MAX_REPORT_BYTES: Final = 2 * 1024 * 1024

_TEMP_SUFFIX: Final = ".tmp"


class ReportIOError(Exception):
    """The report could not be read or written as a complete document."""


@dataclass(frozen=True, slots=True)
class LoadedReport:
    """A report as it applies *now*, plus what had to be given up to say so."""

    report: CapabilityReport
    stored_build: str
    downgraded: bool
    notes: tuple[str, ...] = ()

    @property
    def build_matches(self) -> bool:
        return not self.downgraded


def save_report(
    path: Path,
    report: CapabilityReport,
    *,
    protected_roots: Sequence[Path] = (),
) -> int:
    """Write *report* to *path* atomically. Returns the number of bytes written.

    ``protected_roots`` are directories this project only reads — the game
    install above all. Passing the scanned root here is what turns "the scanner
    never writes to the game directory" from a convention into a check.

    Raises:
        ReportIOError: when the document is over :data:`MAX_REPORT_BYTES` or the
            filesystem refuses the write.
        WriteRefused: when *path* falls inside a protected root.
    """
    refuse_write(path, protected_roots)
    body = json.dumps(report.to_dict(), ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    encoded = body.encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise ReportIOError(f"report of {len(encoded)} bytes exceeds {MAX_REPORT_BYTES}")
    temp = path.with_name(path.name + _TEMP_SUFFIX)
    refuse_write(temp, protected_roots)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        raise ReportIOError(f"{path}: cannot write report ({exc.strerror or exc})") from exc
    return len(encoded)


def load_report(path: Path, *, expected_build: str) -> LoadedReport:
    """Read the report at *path* and re-state it against *expected_build*.

    Raises:
        ReportIOError: when the file is missing, oversized, not JSON, or holds a
            capability claim that fails validation — a ``verified`` entry with no
            probe evidence, for instance. A malformed report is never partially
            trusted.
    """
    if not expected_build:
        raise ReportIOError("expected_build must be given; a report is only valid for one build")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReportIOError(f"{path}: cannot stat report ({exc.strerror or exc})") from exc
    if size > MAX_REPORT_BYTES:
        raise ReportIOError(f"{path}: {size} bytes exceeds {MAX_REPORT_BYTES}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReportIOError(f"{path}: cannot read report ({exc.strerror or exc})") from exc
    try:
        document: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportIOError(f"{path}: not a valid JSON document ({exc})") from exc
    if not isinstance(document, dict):
        raise ReportIOError(f"{path}: expected a JSON object at the top level")
    try:
        stored = CapabilityReport.from_dict(document)
    except CapabilityError as exc:
        raise ReportIOError(f"{path}: {exc}") from exc

    if stored.build == expected_build:
        return LoadedReport(report=stored, stored_build=stored.build, downgraded=False)

    rebased = stored.for_build(expected_build, reason=REASON_BUILD_CHANGED)
    note = (
        f"report was generated against build {stored.build}; running {expected_build}. "
        "Every verified capability was downgraded — re-run the probes."
    )
    return LoadedReport(
        report=rebased,
        stored_build=stored.build,
        downgraded=True,
        notes=(note,),
    )


def load_or_empty(path: Path, *, expected_build: str) -> LoadedReport:
    """Load the report, or return an empty one for *expected_build*.

    A missing file is the normal first-run state and must not be an error; a
    file that exists but is unreadable stays an error, because silently
    substituting an empty report would hide a corrupted ledger.
    """
    if not path.exists():
        empty = CapabilityReport(build=expected_build)
        return LoadedReport(
            report=empty,
            stored_build="",
            downgraded=False,
            notes=(f"no report at {path}; nothing has been probed yet",),
        )
    return load_report(path, expected_build=expected_build)
