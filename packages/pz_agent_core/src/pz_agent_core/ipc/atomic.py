"""Whole-file writes that a reader never observes half-finished.

§3.5: write a temporary file, flush and fsync it, then replace the target. The
invariant is that a reader either sees the previous complete document or the
new complete document, never a prefix of either. Every writer in this package
goes through :func:`write_json_atomic`, and every one of them asserts the
target belongs to :class:`~pz_agent_core.ipc.layout.IpcLayout` first, so a
filename can never originate from the wire.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .layout import TEMP_SUFFIX, IpcLayout

#: A document larger than this is a bug in the producer, not a big world. The
#: cap keeps a corrupt or hostile file from being pulled entirely into memory.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


class IpcPathError(ValueError):
    """A path is outside the layout, so nothing will be written to it."""


class DocumentError(ValueError):
    """A file did not contain a single complete JSON object."""


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


def write_json_atomic(layout: IpcLayout, path: Path, document: Mapping[str, Any]) -> int:
    """Write *document* to *path* atomically. Returns the byte count written."""
    guard_managed(layout, path)
    body = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    encoded = body.encode("utf-8")
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise DocumentError(f"document of {len(encoded)} bytes exceeds {MAX_DOCUMENT_BYTES}")
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    with tmp.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    # os.replace is atomic on both POSIX and Win32 (MoveFileEx with
    # MOVEFILE_REPLACE_EXISTING); the Lua side, which cannot rely on that, uses
    # the two-slot scheme in .snapshot instead.
    os.replace(tmp, path)
    return len(encoded)


def read_json_document(path: Path) -> dict[str, Any]:
    """Read one whole JSON object, or raise :class:`DocumentError`.

    A truncated file, a file holding a JSON array or scalar, and a file larger
    than :data:`MAX_DOCUMENT_BYTES` all fail the same way: callers must treat a
    document as usable only once it has been parsed in full.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DocumentError(f"{path.name}: {exc}") from exc
    if size > MAX_DOCUMENT_BYTES:
        raise DocumentError(f"{path.name}: {size} bytes exceeds {MAX_DOCUMENT_BYTES}")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DocumentError(f"{path.name}: {exc}") from exc
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DocumentError(f"{path.name}: malformed JSON at byte {exc.pos}") from exc
    if not isinstance(parsed, dict):
        raise DocumentError(f"{path.name}: expected a JSON object, got {type(parsed).__name__}")
    return parsed
