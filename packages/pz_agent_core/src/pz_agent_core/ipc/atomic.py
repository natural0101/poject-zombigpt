"""Whole-file writes that a reader never observes half-finished.

§3.5: write a temporary file, flush and fsync it, then replace the target. The
invariant is that a reader either sees the previous complete document or the
new complete document, never a prefix of either. Every writer in this package
goes through :func:`write_json_atomic`, and every one of them asserts the
target belongs to :class:`~pz_agent_core.ipc.layout.IpcLayout` first, so a
filename can never originate from the wire.

On Windows the final replace can be *refused* while another process holds the
target open without delete sharing — observed live on Build 42.20.2, where a
reader mid-poll (or an antivirus/indexer sweep drawn to the fresh file) turned
one publish into ``PermissionError``. Such a hold is brief, so the honest
translation is a bounded wait, not a crash: see :data:`_REPLACE_ATTEMPTS`
below. Whatever happens, the scratch file never outlives the attempt silently —
a failure removes it with the same patience, and when even that is refused the
error names the leaked path instead of pretending it was cleaned up.

The read side meets the same contention from the other chair: opening a file
the game holds can be refused too, and folding that refusal into
:class:`DocumentError` — as this module once did — makes a locked file look
corrupt. :func:`read_json_document` therefore waits out ``PermissionError``
with its own, much smaller budget (:data:`_READ_ATTEMPTS`) and then raises
:class:`SharingViolationError`, so "locked this poll" and "corrupt" stay
different answers.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from itertools import count
from pathlib import Path
from typing import Any, Final

from ..jsonbytes import deepest_nesting
from .layout import IpcLayout, temp_name

# `temp_name` is re-exported deliberately: the scratch file it names is part of
# this module's observable behaviour, because a concurrent reader can encounter
# that file mid-write and has to recognise it.
__all__ = [
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_DEPTH",
    "DocumentError",
    "IpcLayout",
    "IpcPathError",
    "SharingViolationError",
    "encode_json_line",
    "guard_managed",
    "read_json_document",
    "temp_name",
    "write_json_atomic",
]

#: A document larger than this is a bug in the producer, not a big world. The
#: cap keeps a corrupt or hostile file from being pulled entirely into memory.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

#: Deepest array/object nesting a document may carry. These documents are
#: session/heartbeat/pointer records and full snapshots; the deepest real
#: structure is a nested inventory, itself bounded well under this by
#: ``MAX_CONTAINER_DEPTH``. The bound exists because ``json.loads`` recurses once
#: per nesting level, so a file of a few kilobytes of ``[`` — under
#: :data:`MAX_DOCUMENT_BYTES` — overflows the interpreter with a
#: ``RecursionError`` the byte cap cannot see. Measured on the raw bytes *before*
#: parsing (see :mod:`pz_agent_core.jsonbytes`), so the parser is never handed a
#: document that could make it recurse past here; catching ``RecursionError``
#: after the fact is not safe, because the stack is already near its limit when
#: it fires. Kept equal to the RPC and journal bounds so every JSON boundary in
#: this package refuses the same depth.
MAX_DOCUMENT_DEPTH = 64

#: Serial for the temporary filename of §3.5. Together with the pid it makes
#: every scratch file unique, so two writers aiming at the same target cannot
#: publish each other's half-written bytes through a shared ``.tmp``.
_temp_serial = count()


#: How a publish waits out a reader holding the target, and for how long. On
#: POSIX ``os.replace`` succeeds under any number of open handles, but Windows
#: refuses to move or delete a file another process holds open without delete
#: sharing — and the readers of these documents open the target for every poll,
#: with antivirus and indexer sweeps drawn to every freshly published file on
#: top. Confirmed live on Build 42.20.2: the replace intermittently drew
#: ``PermissionError`` (WinError 32) mid-publish. Every such hold is itself
#: brief, so the honest translation is a bounded wait, not a crash: each move
#: retries on ``PermissionError`` up to this many times with this pause
#: between, half a second in the worst case, and only then raises
#: :class:`SharingViolationError` naming the file. Zero cost on POSIX, where
#: the first attempt always wins. The budget is kept numerically equal to the
#: one journal rotation uses for the same race (see :mod:`.journal`); the two
#: modules stay independent so neither can widen the other's wait by accident.
_REPLACE_ATTEMPTS: Final = 10
_REPLACE_PAUSE_SECONDS: Final = 0.05


#: The same Windows contention, seen from the reading side. Opening a file the
#: game (or an antivirus sweep) holds without read sharing raises the same
#: ``PermissionError`` a refused replace does — observed live on Build 42.20.2
#: against ``observation.snapshot.pointer`` and the heartbeat files. Before this
#: budget existed the refusal was folded into :class:`DocumentError`, which made
#: a locked file indistinguishable from a corrupt one and silently degraded a
#: poll to a miss. The budget is deliberately much smaller than the write one:
#: a reader sits inside a ~125ms tick loop, so waiting the write side's half
#: second would swallow four ticks for a document that will be polled again in
#: one — 40ms worst case keeps the whole wait inside the current tick, and a
#: hold that outlives it is reported as :class:`SharingViolationError` so the
#: caller can treat "locked right now" as a miss and try next poll. Zero cost
#: on POSIX, where an open handle never refuses a read.
_READ_ATTEMPTS: Final = 4
_READ_PAUSE_SECONDS: Final = 0.01


class IpcPathError(ValueError):
    """A path is outside the layout, so nothing will be written to it."""


class DocumentError(ValueError):
    """A document was not one complete JSON object a file could carry."""


class SharingViolationError(OSError):
    """Another process held a file open past the whole retry budget.

    An ``OSError`` because that is what the write path has always raised — a
    caller catching ``OSError`` around a publish keeps working — while the type
    says the failure is contention on the file, not disk trouble.
    """


def guard_managed(layout: IpcLayout, path: Path) -> Path:
    """Return *path*, or raise if the layout does not own it."""
    if not layout.is_managed_path(path):
        raise IpcPathError(f"refusing to touch unmanaged path: {path}")
    return path


def encode_json_line(payload: Mapping[str, Any]) -> str:
    """Serialise one journal record: compact, single line, newline-terminated.

    ``ensure_ascii`` stays on so a record survives a reader that opens the file
    in the system code page — Windows consoles and Kahlua both do.
    """
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"


def _replace_with_patience(source: Path, target: Path) -> None:
    """``os.replace`` that waits out a reader holding *target*, then refuses.

    Only ``PermissionError`` is retried — it is the Windows spelling of "the
    file is open elsewhere", and the one refusal that resolves by itself. Every
    other ``OSError`` is a real failure and raises on the first attempt.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise SharingViolationError(
                    f"{target.name}: could not be replaced; a reader held it open "
                    f"past the {_REPLACE_ATTEMPTS * _REPLACE_PAUSE_SECONDS:g}s budget"
                ) from exc
            time.sleep(_REPLACE_PAUSE_SECONDS)


def _remove_with_patience(path: Path) -> None:
    """``unlink`` with the same Windows patience, for the scratch file.

    When even removal is refused for the whole budget the scratch file really
    is left behind, and the error says so — naming the leaked path is what lets
    someone find and delete it, so pretending the cleanup happened would be the
    dishonest failure this module exists to avoid.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError as exc:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise SharingViolationError(
                    f"scratch file {path} was left behind: it could not be removed; "
                    f"another process held it open past the "
                    f"{_REPLACE_ATTEMPTS * _REPLACE_PAUSE_SECONDS:g}s budget"
                ) from exc
            time.sleep(_REPLACE_PAUSE_SECONDS)


def write_json_atomic(layout: IpcLayout, path: Path, document: Mapping[str, Any]) -> int:
    """Write *document* to *path* atomically. Returns the byte count written."""
    guard_managed(layout, path)
    try:
        body = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        # A document the encoder refuses (a non-JSON value, a circular
        # reference) is the producer's bug, reported as this module's own
        # refusal so a caller sees one exception family for "this is not a
        # document". Raised before the scratch file exists, so there is
        # nothing to clean up on this path.
        raise DocumentError(f"{path.name}: document cannot be serialised: {exc}") from exc
    encoded = body.encode("utf-8")
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise DocumentError(f"document of {len(encoded)} bytes exceeds {MAX_DOCUMENT_BYTES}")
    tmp = path.with_name(temp_name(path.name, os.getpid(), next(_temp_serial)))
    # The scratch name is derived, never supplied, but it still has to be a path
    # the layout owns: if the two modules ever drift apart, this fails loudly
    # rather than dropping files the layout does not know how to clean up.
    guard_managed(layout, tmp)
    try:
        with tmp.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # os.replace is atomic on both POSIX and Win32 (MoveFileEx with
        # MOVEFILE_REPLACE_EXISTING); the Lua side, which cannot rely on that,
        # uses the two-slot scheme in .snapshot instead. Win32 additionally
        # refuses the move while another process holds the target open, so the
        # replace goes through the bounded patience rather than one bare call.
        _replace_with_patience(tmp, path)
    except BaseException:
        # A unique scratch name is only bounded if a failed write takes its file
        # with it; otherwise every crashed attempt leaves one behind forever.
        # ``BaseException`` and not ``OSError``, because an interrupt landing
        # mid-write must not leak the scratch file either; the removal is
        # bounded, and the original exception is re-raised right after. When
        # even the removal is refused, the SharingViolationError it raises
        # names the leaked path, chained onto the failure that got us here.
        _remove_with_patience(tmp)
        raise
    return len(encoded)


def _fetch_document_text(path: Path) -> str:
    """One attempt at the stat-and-read pair, keeping the two refusals apart.

    ``PermissionError`` passes through untranslated — it is the Windows
    spelling of "another process holds this file", and the caller retries it.
    Every other ``OSError`` (missing file, disk trouble) and a decode failure
    are genuine document problems and become :class:`DocumentError` here, so
    the retry loop above can never mistake corruption for contention.
    """
    try:
        size = path.stat().st_size
    except PermissionError:
        raise
    except OSError as exc:
        raise DocumentError(f"{path.name}: {exc}") from exc
    if size > MAX_DOCUMENT_BYTES:
        raise DocumentError(f"{path.name}: {size} bytes exceeds {MAX_DOCUMENT_BYTES}")
    try:
        return path.read_text(encoding="utf-8")
    except PermissionError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise DocumentError(f"{path.name}: {exc}") from exc


def _read_text_with_patience(path: Path) -> str:
    """The stat-and-read pair with the bounded read budget applied.

    Only ``PermissionError`` is retried, exactly as on the write side; the
    loop is bounded by :data:`_READ_ATTEMPTS`, and exhausting it raises
    :class:`SharingViolationError` naming the file and the budget — the typed
    "someone held the file" refusal the write path already speaks, so a caller
    can tell a locked document (poll again) from a corrupt one
    (:class:`DocumentError`).
    """
    attempt = 0
    while True:
        try:
            return _fetch_document_text(path)
        except PermissionError as exc:
            attempt += 1
            if attempt >= _READ_ATTEMPTS:
                raise SharingViolationError(
                    f"{path.name}: could not be read; another process held it "
                    f"open past the {_READ_ATTEMPTS * _READ_PAUSE_SECONDS:g}s budget"
                ) from exc
            time.sleep(_READ_PAUSE_SECONDS)


def read_json_document(path: Path) -> dict[str, Any]:
    """Read one whole JSON object, or raise :class:`DocumentError`.

    A truncated file, a file holding a JSON array or scalar, and a file larger
    than :data:`MAX_DOCUMENT_BYTES` all fail the same way: callers must treat a
    document as usable only once it has been parsed in full.

    A file another process holds open is a different failure entirely: the
    read waits it out within the small budget of :data:`_READ_ATTEMPTS`, and a
    hold that outlives even that raises :class:`SharingViolationError` — the
    document on disk may be perfectly good, it just cannot be seen this poll.
    """
    raw = _read_text_with_patience(path)
    # Depth is measured on the raw bytes before parsing: the markers the scan
    # counts are all ASCII, so re-encoding the already-decoded text costs a
    # bounded pass and never miscounts a byte inside a multibyte character.
    if deepest_nesting(raw.encode("utf-8")) > MAX_DOCUMENT_DEPTH:
        raise DocumentError(f"{path.name}: nests deeper than {MAX_DOCUMENT_DEPTH}")
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DocumentError(f"{path.name}: malformed JSON at byte {exc.pos}") from exc
    except ValueError as exc:
        # A ``ValueError`` that is not a ``JSONDecodeError``: the parser's own
        # refusal of a number too extreme to build, which on CPython is the
        # integer-string-conversion ceiling firing on a literal of thousands of
        # digits — inside the byte cap, outside what any real document carries.
        # Named as malformed without echoing the digits.
        raise DocumentError(f"{path.name}: carries a number the parser refuses") from exc
    if not isinstance(parsed, dict):
        raise DocumentError(f"{path.name}: expected a JSON object, got {type(parsed).__name__}")
    return parsed
