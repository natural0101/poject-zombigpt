"""Read-only symbol index of the installed game's Lua, built on the user's machine.

The compatibility ledger (§12.5) has to describe the API surface of the *actual*
installation, not of a stubs repository whose licence is unclear (§12.7). That
forces a scanner, and a scanner that touches game files is a liability, so this
module is constrained hard:

* Every filesystem operation is a stat or a binary read. Nothing here opens a
  file for writing, creates a directory, or copies a file. :func:`refuse_write`
  exists so the modules that *do* write — the report writer, the backup tool —
  can be handed the scanned root and made to prove they are staying out of it.
* The index records a symbol name, the path relative to the scan root, the line,
  a **reconstructed** signature and the sha256 of the file. It never records a
  line of the file. Vanilla Lua may not be redistributed, and a report carrying
  source excerpts would be a redistribution in a trench coat.
* The scan is bounded by file count, per-file size, total bytes, symbol count and
  directory depth, and the two diagnostic lists it returns are bounded too. When
  a bound bites, the result says so in :attr:`ScanResult.truncation_reasons`; a
  silently short index would read as "the API is missing" further downstream.

The extractor is deliberately line-based. A real Lua parser would be a
dependency (forbidden in core) or a large hand-written one, and the declaration
forms that matter — ``function X:y()``, ``function X.y()``, ``X = X or {}``,
``X = Base:derive("X")`` — are all single-line in the vanilla codebase. Anything
it cannot recognise is simply absent from the index, which downgrades a
capability rather than inventing one.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

JsonDict = dict[str, Any]

#: Where the game keeps its Lua, relative to the install root.
LUA_SUBPATH: Final = ("media", "lua")

#: Vanilla ships a few thousand Lua files; a tree far larger than that is a
#: modded install or a symlink loop, and either way the scan should stop.
DEFAULT_MAX_FILES: Final = 8000

#: The largest vanilla Lua file is well under this. A bigger one is refused
#: whole rather than read in part: a partial file yields a partial signature and
#: a sha256 that does not describe the file on disk.
DEFAULT_MAX_FILE_BYTES: Final = 1024 * 1024

DEFAULT_MAX_TOTAL_BYTES: Final = 96 * 1024 * 1024

DEFAULT_MAX_SYMBOLS: Final = 40_000

#: Directory recursion is bounded too; the vanilla tree is about six deep.
DEFAULT_MAX_DEPTH: Final = 12

_IDENT: Final = r"[A-Za-z_][A-Za-z0-9_]*"
_QUALIFIED: Final = r"[A-Za-z_][A-Za-z0-9_.]*"

_METHOD_RE: Final = re.compile(rf"^function\s+({_QUALIFIED})\s*([.:])\s*({_IDENT})\s*\(([^)]*)\)")
_FUNCTION_RE: Final = re.compile(rf"^function\s+({_IDENT})\s*\(([^)]*)\)")
_ASSIGNED_FUNCTION_RE: Final = re.compile(
    rf"^({_QUALIFIED})\s*[.:]\s*({_IDENT})\s*=\s*function\s*\(([^)]*)\)"
)
_DERIVE_RE: Final = re.compile(
    rf"^({_QUALIFIED})\s*=\s*({_QUALIFIED})\s*[.:]derive\s*\(\s*[\"']([^\"']*)[\"']\s*\)"
)
_TABLE_RE: Final = re.compile(rf"^({_QUALIFIED})\s*=\s*(?:({_QUALIFIED})\s+or\s+)?\{{\s*\}}\s*;?$")

#: ``--[[`` and ``--[==[`` open a block comment; ``]]`` / ``]==]`` close it.
_BLOCK_OPEN_RE: Final = re.compile(r"--\[(=*)\[")
_LINE_COMMENT_RE: Final = re.compile(r"^--")

#: Symbols and paths are bounded so a hostile filename cannot bloat the report.
MAX_SIGNATURE_LEN: Final = 300
MAX_PARAMS: Final = 16

#: A tree can produce one problem per file and one truncation reason per
#: directory. Both lists end up in a generated report, so both are capped and the
#: cap itself is reported rather than applied silently.
MAX_PROBLEMS: Final = 50
MAX_TRUNCATION_REASONS: Final = 20


class ScanError(Exception):
    """The scan could not be performed as requested."""


class WriteRefused(ScanError):
    """Something tried to write inside a directory the scanner only reads."""


class SymbolKind(StrEnum):
    """Which declaration form produced a record."""

    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    TABLE = "table"


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """Bounds for one scan. Every one of them is reported when it bites."""

    max_files: int = DEFAULT_MAX_FILES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_symbols: int = DEFAULT_MAX_SYMBOLS
    max_depth: int = DEFAULT_MAX_DEPTH

    def __post_init__(self) -> None:
        for name in ("max_files", "max_file_bytes", "max_total_bytes", "max_symbols", "max_depth"):
            value: int = getattr(self, name)
            if value <= 0:
                raise ScanError(f"{name} must be positive, got {value}")


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    """One declaration found on disk.

    ``signature`` is rebuilt from the parsed parts (``ISEatFoodAction:new(character,
    item, percentage)``), so it can be shown to a user and diffed between builds
    without ever holding a line of the game's source.
    """

    symbol: str
    kind: SymbolKind
    relative_path: str
    line: int
    signature: str
    params: tuple[str, ...] = ()
    file_sha256: str = ""

    @property
    def key(self) -> str:
        """Lookup key: ``Owner.name``, with the method colon normalised away."""
        return normalise_symbol(self.symbol)

    def to_dict(self) -> JsonDict:
        return {
            "symbol": self.symbol,
            "kind": self.kind.value,
            "path": self.relative_path,
            "line": self.line,
            "signature": self.signature,
            "params": list(self.params),
            "sha256": self.file_sha256,
            "source": "local_game",
        }


def normalise_symbol(symbol: str) -> str:
    """``ISEatFoodAction:new`` and ``ISEatFoodAction.new`` name the same thing.

    Lua's colon is sugar for a dot plus an implicit ``self``; probes are written
    with dots (§12.5) while the source uses whichever form the author preferred.
    """
    return symbol.replace(":", ".")


@dataclass(frozen=True, slots=True)
class SymbolIndex:
    """Symbol key → the records that declared it.

    A key can have several records: the same method is defined in more than one
    file across client/server/shared, and knowing all of them is what makes a
    later "which file changed" question answerable.
    """

    entries: dict[str, tuple[SymbolRecord, ...]]

    @classmethod
    def from_records(cls, records: Sequence[SymbolRecord]) -> SymbolIndex:
        grouped: dict[str, list[SymbolRecord]] = {}
        for record in records:
            grouped.setdefault(record.key, []).append(record)
        return cls(entries={key: tuple(value) for key, value in sorted(grouped.items())})

    def __len__(self) -> int:
        return len(self.entries)

    def has(self, symbol: str) -> bool:
        return normalise_symbol(symbol) in self.entries

    def find(self, symbol: str) -> SymbolRecord | None:
        """First record for *symbol*, or None. Never raises on an unknown name."""
        records = self.entries.get(normalise_symbol(symbol))
        return records[0] if records else None

    def missing(self, symbols: Sequence[str]) -> tuple[str, ...]:
        """Which of *symbols* the local install does not declare."""
        return tuple(s for s in symbols if not self.has(s))


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Everything one scan learned, plus everything it deliberately did not read.

    ``root`` is kept for logs and for the doctor's "here is where I looked" line,
    but it is not serialised: on Windows it embeds the account name, and the
    generated report is a file users attach to bug reports.
    """

    root: str
    symbols: tuple[SymbolRecord, ...] = ()
    files_scanned: int = 0
    files_seen: int = 0
    bytes_read: int = 0
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    def index(self) -> SymbolIndex:
        return SymbolIndex.from_records(self.symbols)

    def to_dict(self) -> JsonDict:
        return {
            "root_name": Path(self.root).name,
            "files_scanned": self.files_scanned,
            "files_seen": self.files_seen,
            "bytes_read": self.bytes_read,
            "truncated": self.truncated,
            "truncation_reasons": list(self.truncation_reasons),
            "problems": list(self.problems),
            "symbols": [record.to_dict() for record in self.symbols],
        }


# ---------------------------------------------------------------------------
# write refusal
# ---------------------------------------------------------------------------


def _normalised(path: Path) -> str:
    # normcase matters on Windows, where the same directory is reachable as
    # C:\Program Files\... and c:\program files\...
    return os.path.normcase(str(_resolve(path)))


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        # A path on a disconnected drive cannot be resolved; the unresolved form
        # is still the right thing to compare, and refusing on the safe side is
        # what matters here.
        return path.absolute()


def is_inside(candidate: Path, protected: Path) -> bool:
    """True when *candidate* is *protected* itself or lives under it."""
    normalised_candidate = _normalised(candidate)
    normalised_protected = _normalised(protected)
    if normalised_candidate == normalised_protected:
        return True
    return normalised_candidate.startswith(normalised_protected.rstrip(os.sep) + os.sep)


def refuse_write(destination: Path, protected_roots: Sequence[Path]) -> Path:
    """Return *destination*, or raise if it falls inside a read-only root.

    The game directory is read-only for this project as a rule, not as a
    convention: writing there risks the install's integrity and would put agent
    state inside something Steam may overwrite or verify.
    """
    for root in protected_roots:
        if is_inside(destination, root):
            raise WriteRefused(f"refusing to write inside the game directory: {destination}")
    return destination


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def _split_params(raw: str) -> tuple[str, ...]:
    params = [part.strip() for part in raw.split(",")]
    kept = [p for p in params if p]
    return tuple(kept[:MAX_PARAMS])


def _signature(owner: str, separator: str, name: str, params: Sequence[str]) -> str:
    joined = ", ".join(params)
    prefix = f"{owner}{separator}" if owner else ""
    return f"{prefix}{name}({joined})"[:MAX_SIGNATURE_LEN]


def _strip_block_comments(lines: Sequence[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(lineno, code)`` for lines that are not inside a block comment.

    Only whole-line accuracy is needed: a declaration that shares a line with the
    end of a block comment is vanishingly rare in the vanilla codebase, and the
    cost of missing it is a capability that reports as unavailable — the safe
    direction.
    """
    depth_marker: str | None = None
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if depth_marker is not None:
            if depth_marker in line:
                depth_marker = None
            continue
        opener = _BLOCK_OPEN_RE.search(line)
        if _LINE_COMMENT_RE.match(line) and (opener is None or opener.start() != 0):
            # ``-- talks about --[[ something`` is a line comment that merely
            # mentions the block syntax. Treating it as an opener would swallow
            # every declaration up to the next ``]]`` in the file.
            continue
        if opener is not None:
            closer = f"]{opener.group(1)}]"
            if closer not in line[opener.end() :]:
                depth_marker = closer
            continue
        yield lineno, line


def extract_symbols(
    text: str,
    *,
    relative_path: str,
    file_sha256: str,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
) -> tuple[SymbolRecord, ...]:
    """Extract declarations from one Lua file's text.

    Pure function, so the recognised forms can be tested without a filesystem.
    ``local`` declarations are skipped on purpose: a file-local function is not
    part of the API surface another mod — or this agent — can call.
    """
    records: list[SymbolRecord] = []

    def _add(
        symbol: str,
        kind: SymbolKind,
        line: int,
        signature: str,
        params: Sequence[str] = (),
    ) -> bool:
        if len(records) >= max_symbols:
            return False
        records.append(
            SymbolRecord(
                symbol=symbol,
                kind=kind,
                relative_path=relative_path,
                line=line,
                signature=signature,
                params=tuple(params),
                file_sha256=file_sha256,
            )
        )
        return True

    for lineno, line in _strip_block_comments(text.splitlines()):
        if line.startswith("local "):
            continue

        method = _METHOD_RE.match(line)
        if method is not None:
            owner, separator, name, raw_params = method.groups()
            params = _split_params(raw_params)
            kind = SymbolKind.METHOD if separator == ":" else SymbolKind.FUNCTION
            if not _add(
                f"{owner}{separator}{name}",
                kind,
                lineno,
                _signature(owner, separator, name, params),
                params,
            ):
                break
            continue

        plain = _FUNCTION_RE.match(line)
        if plain is not None:
            name, raw_params = plain.groups()
            params = _split_params(raw_params)
            if not _add(
                name, SymbolKind.FUNCTION, lineno, _signature("", "", name, params), params
            ):
                break
            continue

        assigned = _ASSIGNED_FUNCTION_RE.match(line)
        if assigned is not None:
            owner, name, raw_params = assigned.groups()
            params = _split_params(raw_params)
            if not _add(
                f"{owner}.{name}",
                SymbolKind.FUNCTION,
                lineno,
                _signature(owner, ".", name, params),
                params,
            ):
                break
            continue

        derived = _DERIVE_RE.match(line)
        if derived is not None:
            name, base, _declared = derived.groups()
            if not _add(name, SymbolKind.CLASS, lineno, f"{name} <- {base}"):
                break
            continue

        table = _TABLE_RE.match(line)
        if table is not None:
            name, guard = table.groups()
            # ``X = Y or {}`` with Y different from X re-exports another table;
            # recording it under X would claim a declaration this file does not
            # make.
            declares_itself = guard is None or guard == name
            if declares_itself and not _add(name, SymbolKind.TABLE, lineno, f"{name} (table)"):
                break

    return tuple(records)


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def _append_bounded(items: list[str], message: str, *, cap: int, label: str) -> None:
    """Append *message* once, keeping *items* at or below *cap* entries.

    The last slot is reserved for a marker saying the list itself was cut, so a
    reader of the report can tell "no more problems" from "more problems than
    fit". Duplicates are dropped: one message per distinct cause, not one per
    directory that hit it.
    """
    if message in items:
        return
    if len(items) >= cap - 1:
        marker = f"more than {cap - 1} {label}; the rest are not listed"
        if marker not in items:
            items.append(marker)
        return
    items.append(message)


def lua_root(install_dir: Path) -> Path:
    """The Lua directory of an install. Existence is the caller's to check."""
    return install_dir.joinpath(*LUA_SUBPATH)


def _iter_lua_files(root: Path, limits: ScanLimits) -> tuple[list[Path], list[str]]:
    """Collect ``*.lua`` paths under *root*, deterministically and bounded."""
    found: list[Path] = []
    reasons: list[str] = []
    root_depth = len(_resolve(root).parts)

    # followlinks stays off: a symlinked directory can point back into the tree
    # and turn a bounded walk into an unbounded one.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        depth = len(_resolve(current).parts) - root_depth
        if depth >= limits.max_depth:
            dirnames[:] = []
            _append_bounded(
                reasons,
                f"stopped descending below depth {limits.max_depth}",
                cap=MAX_TRUNCATION_REASONS,
                label="truncation reasons",
            )
            continue
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".lua"):
                continue
            if len(found) >= limits.max_files:
                _append_bounded(
                    reasons,
                    f"stopped after {limits.max_files} files",
                    cap=MAX_TRUNCATION_REASONS,
                    label="truncation reasons",
                )
                return found, reasons
            found.append(current / name)
    return found, reasons


def scan_lua_tree(root: Path, limits: ScanLimits | None = None) -> ScanResult:
    """Build a symbol index from the Lua tree at *root*, reading only.

    Raises:
        ScanError: when *root* is not a directory. An empty or unreadable tree is
            reported through :attr:`ScanResult.problems` instead, because
            ``doctor`` needs to show the user where it looked.
    """
    bounds = limits or ScanLimits()
    if not root.is_dir():
        raise ScanError(f"not a directory: {root}")

    resolved_root = _resolve(root)
    files, reasons = _iter_lua_files(root, bounds)
    problems: list[str] = []
    records: list[SymbolRecord] = []
    bytes_read = 0
    files_scanned = 0

    def _problem(message: str) -> None:
        _append_bounded(problems, message, cap=MAX_PROBLEMS, label="unreadable files")

    def _reason(message: str) -> None:
        _append_bounded(reasons, message, cap=MAX_TRUNCATION_REASONS, label="truncation reasons")

    for path in files:
        if bytes_read >= bounds.max_total_bytes:
            _reason(f"stopped after reading {bounds.max_total_bytes} bytes")
            break
        if len(records) >= bounds.max_symbols:
            _reason(f"stopped after {bounds.max_symbols} symbols")
            break
        try:
            size = path.stat().st_size
        except OSError as exc:
            _problem(f"{path.name}: cannot stat ({exc.strerror or exc})")
            continue
        if size > bounds.max_file_bytes:
            _problem(f"{_relative(path, resolved_root)}: {size} bytes, over the per-file cap")
            _reason(f"skipped a file larger than {bounds.max_file_bytes} bytes")
            continue
        try:
            with path.open("rb") as handle:
                # One byte past the cap: if that byte exists the file grew between
                # the stat above and this read, and hashing what we got would
                # record a sha256 that describes no file on disk.
                raw = handle.read(bounds.max_file_bytes + 1)
        except OSError as exc:
            _problem(f"{path.name}: cannot read ({exc.strerror or exc})")
            continue
        if len(raw) > bounds.max_file_bytes:
            _problem(
                f"{_relative(path, resolved_root)}: grew past the per-file cap while being read"
            )
            _reason(f"skipped a file larger than {bounds.max_file_bytes} bytes")
            continue

        bytes_read += len(raw)
        files_scanned += 1
        digest = hashlib.sha256(raw).hexdigest()
        # errors="replace": vanilla files carry the odd non-UTF-8 byte inside a
        # translated string literal, and identifiers are ASCII regardless.
        text = raw.decode("utf-8", errors="replace")
        remaining = bounds.max_symbols - len(records)
        records.extend(
            extract_symbols(
                text,
                relative_path=_relative(path, resolved_root),
                file_sha256=digest,
                max_symbols=remaining,
            )
        )

    if len(records) >= bounds.max_symbols:
        _reason(f"symbol index capped at {bounds.max_symbols} entries")

    return ScanResult(
        root=str(root),
        symbols=tuple(records),
        files_scanned=files_scanned,
        files_seen=len(files),
        bytes_read=bytes_read,
        truncated=bool(reasons),
        truncation_reasons=tuple(reasons),
        problems=tuple(problems),
    )


def _relative(path: Path, root: Path) -> str:
    """Path relative to the scan root, with forward slashes.

    Only the relative form is ever recorded: the absolute path leaks the user's
    Windows account name into a file they may attach to a bug report.
    """
    try:
        relative = _resolve(path).relative_to(root)
    except ValueError:
        return path.name
    return relative.as_posix()
