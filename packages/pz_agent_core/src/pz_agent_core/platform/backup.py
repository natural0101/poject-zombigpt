"""Versioned, hash-verified backups of a Project Zomboid save
(docs/blueprint/08_SAFETY.md §8.3).

Three invariants hold here, and each of them exists because the failure it
prevents destroys a save the user cannot get back:

* **Restore only while the game is closed.** ``restore`` takes ``game_running``
  as a required keyword and refuses when it is true. There is no override
  argument, so the check cannot be bypassed by accident — a caller that does not
  know whether the game is running cannot call the method at all.
* **Verify before writing.** Every file's SHA-256 is checked against the
  manifest before a single byte of the target save is touched, and the restore
  is staged into a sibling directory and swapped in, so an interrupted restore
  leaves either the old save or the new one, never half of each. Precisely: the
  swap is two renames, and a process killed between them leaves the save
  directory absent and the previous save whole under
  ``.pz-agent-replaced-<id>``. Re-running the restore completes it.
* **Prune is the only deletion path**, it keeps the newest N, and it will not
  accept ``keep < 1``. Nothing else in this module removes a backup.

A backup also carries, when there was one to carry, **the save id the mod itself
reported** while it was being taken (``observed_save_id``). It is copied from the
mod's own observation and never derived here: the id the autonomy gate compares
against is a digest computed inside the game over a save key that never crosses
the boundary, so the only honest way to know a backup covers the save being
played is to have been told so at the moment it was made. A backup taken with no
session attached carries none and keeps none — "the newest backup is probably
this save" is precisely the reassurance
:class:`~pz_agent_core.policy.autonomy.BackupEvidence` exists to refuse — and a
manifest written before the field existed reads back the same way, as a backup
that names no save rather than as a corrupt one.

Everything is bounded: the size and file count of a save are measured and
enforced *while copying*, not only before it, so a save that grows underneath
the copy still cannot fill the disk.

The two copy passes — plan then copy on the way in, verify then copy on the way
out — are each two reads of a tree something else may be writing. Every way the
second read can disagree with the first (the file grew, shrank, changed, or went
away) surfaces as a :class:`BackupError`, never as a raw :class:`OSError`: the
CLI renders refusals from this subsystem and would let anything else escape as a
traceback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ..protocol.messages import MAX_SAVE_ID_LEN
from ..version import PRODUCT_VERSION, SCHEMA_VERSION

#: Default ceiling on one backup. Generous for a normal save; small enough that
#: pointing the manager at the wrong directory fails loudly instead of copying a
#: whole Steam library.
DEFAULT_MAX_BACKUP_BYTES: Final = 4 * 1024 * 1024 * 1024

DEFAULT_MAX_BACKUP_FILES: Final = 200_000

#: Directories inside one save. Not configurable: a save's shape is the game's,
#: not the user's, and the only tree with more subdirectories than this is one
#: the manager was pointed at by mistake. It exists so the *traversal* is
#: bounded and not only the file count — a cap that is checked after the whole
#: listing has been materialised protects nothing.
MAX_BACKUP_DIRS: Final = 50_000

#: A manifest for the maximum-size backup is a few tens of megabytes; anything
#: past this is not a manifest this module wrote.
MAX_MANIFEST_BYTES: Final = 64 * 1024 * 1024

#: Copy buffer. Large enough to be fast on spinning disks, small enough that
#: memory use is independent of save size.
COPY_CHUNK_BYTES: Final = 1024 * 1024

MANIFEST_NAME: Final = "manifest.json"
DATA_DIR_NAME: Final = "data"
SAVES_DIR_NAME: Final = "Saves"

_STAGING_PREFIX: Final = ".staging-"
_RESTORE_PREFIX: Final = ".pz-agent-restore-"
_REPLACED_PREFIX: Final = ".pz-agent-replaced-"

#: How many nested components a save id may carry. Project Zomboid uses
#: ``<GameMode>/<SaveName>``; anything deeper is a malformed id.
MAX_SAVE_ID_DEPTH: Final = 4

#: Suffix collisions inside one second are resolved by counting; the loop is
#: bounded so a clock stuck at a fixed instant cannot spin forever.
_MAX_ID_ATTEMPTS: Final = 1000

USER_DIR_PLACEHOLDER: Final = "<ZOMBOID>"
HOME_PLACEHOLDER: Final = "<USER_HOME>"
REDACTED_PLACEHOLDER: Final = "<REDACTED>"

#: Save name characters that are illegal on Windows, plus the separators and
#: NUL that would let an id escape the saves directory.
_SAVE_SEGMENT_RE: Final = re.compile(r'^[^\\/:*?"<>|\x00]+$')

_BACKUP_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: What an observed save id may look like. The mod reports a hex digest today,
#: but the wire type is ``game.save_id`` and this only has to be as strict as
#: that: no whitespace and no control characters, because the value is rendered
#: into ``status`` output and into support bundles, and within the protocol's
#: length bound, because a manifest field is not the place to widen it.
_OBSERVED_SAVE_ID_RE: Final = re.compile(rf"^[^\s\x00-\x1f\x7f]{{1,{MAX_SAVE_ID_LEN}}}$")

_TIMESTAMP_FORMAT: Final = "%Y%m%dT%H%M%SZ"


class BackupError(Exception):
    """Base class for every refusal from the backup subsystem."""


class GameRunningError(BackupError):
    """A restore was attempted while Project Zomboid was running.

    Overwriting the save directory of a live game corrupts it: the game holds
    the chunk files open and rewrites them from its own in-memory state on the
    next autosave.
    """


class BackupTooLargeError(BackupError):
    """The source save exceeds the configured size or file-count cap."""


class BackupNotFoundError(BackupError):
    """No backup with the requested id exists under the backup root."""


class BackupCorruptError(BackupError):
    """A backup's contents do not match its manifest."""


class SaveNotFoundError(BackupError):
    """The save directory named by a save id does not exist."""


@dataclass(frozen=True, slots=True)
class BackupFile:
    """One file inside a backup, addressed relative to the save directory."""

    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BackupFile:
        try:
            path = payload["path"]
            size = payload["size"]
            digest = payload["sha256"]
        except KeyError as exc:
            raise BackupCorruptError(f"manifest file entry missing {exc.args[0]!r}") from exc
        if not isinstance(path, str) or not isinstance(digest, str):
            raise BackupCorruptError("manifest file entry has a non-string path or digest")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BackupCorruptError(f"manifest entry {path!r} has a non-numeric size")
        return cls(path=path, size=size, sha256=digest)


@dataclass(frozen=True, slots=True)
class BackupRecord:
    """A backup's manifest, plus where it lives on this machine.

    ``source_dir`` is redacted — the manifest travels inside a support bundle,
    and a Windows profile path carries the user's name.  ``directory`` is the
    real local path and is deliberately not serialised.

    ``save_id`` and ``observed_save_id`` are two different names for the same
    world and neither can be computed from the other. The first is the save
    *directory* this machine copied (``<mode>/<name>``); the second is what the
    mod reported over the exchange directory while the copy was being made, and
    it is the only one the autonomy gate can compare against an observation. A
    backup taken with nothing attached has ``None`` there, which is what keeps it
    unattributed instead of attributed by guesswork.
    """

    backup_id: str
    save_id: str
    created_at: str
    source_dir: str
    total_bytes: int
    files: tuple[BackupFile, ...]
    product_version: str
    schema_version: str
    directory: Path
    #: The mod's own ``observation.game.save_id`` at the moment of the backup,
    #: or None when no session was attached to report one. Never inferred.
    observed_save_id: str | None = None

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def created_at_ms(self) -> int | None:
        """When this backup was taken, in epoch milliseconds, or None.

        None when the timestamp cannot be read as one instant: an unparseable
        string, or one with no offset, which names a different moment in every
        time zone. Callers that need an age report having none rather than
        assuming UTC — a backup whose age is guessed is a backup whose age is
        wrong on exactly the machines where it matters.
        """
        try:
            moment = datetime.fromisoformat(self.created_at)
        except ValueError:
            return None
        if moment.tzinfo is None:
            return None
        return int(moment.timestamp() * 1000)

    @property
    def data_dir(self) -> Path:
        return self.directory / DATA_DIR_NAME

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "product_version": self.product_version,
            "backup_id": self.backup_id,
            "save_id": self.save_id,
            # Written even when it is null, so a reader can tell a backup taken
            # with nothing attached from one written before the field existed.
            # Both are unattributed; only the first one was ever asked.
            "observed_save_id": self.observed_save_id,
            "created_at": self.created_at,
            "source_dir": self.source_dir,
            "total_bytes": self.total_bytes,
            "file_count": self.file_count,
            "files": [entry.to_dict() for entry in self.files],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, directory: Path) -> BackupRecord:
        """Rebuild a record from a manifest document.

        A manifest written before ``observed_save_id`` existed simply has no such
        key, and that is not a defect: it is a backup nothing told which save it
        covers, which is exactly what ``None`` means here.

        Raises:
            BackupCorruptError: if a required field is missing or ill-typed, or
                if a file entry names a path outside the backup.
        """
        files_raw = payload.get("files")
        if not isinstance(files_raw, list):
            raise BackupCorruptError("manifest has no 'files' array")
        files = tuple(
            BackupFile.from_dict(entry)
            for entry in files_raw
            if isinstance(entry, Mapping)  # non-object entries are caught below
        )
        if len(files) != len(files_raw):
            raise BackupCorruptError("manifest 'files' contains a non-object entry")
        for entry in files:
            _validate_relative_member(entry.path)
        total = payload.get("total_bytes")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise BackupCorruptError("manifest 'total_bytes' is not a non-negative integer")
        return cls(
            backup_id=_manifest_str(payload, "backup_id"),
            save_id=_manifest_str(payload, "save_id"),
            observed_save_id=_manifest_observed_save_id(payload),
            created_at=_manifest_str(payload, "created_at"),
            source_dir=_manifest_str(payload, "source_dir"),
            total_bytes=total,
            files=files,
            product_version=_manifest_str(payload, "product_version"),
            schema_version=_manifest_str(payload, "schema_version"),
            directory=directory,
        )


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What a completed restore put back, and where."""

    backup_id: str
    save_id: str
    target_dir: Path
    file_count: int
    total_bytes: int


def _manifest_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise BackupCorruptError(f"manifest field {key!r} is missing or not a string")
    return value


def _manifest_observed_save_id(payload: Mapping[str, Any]) -> str | None:
    """The observed save id in a manifest, or None when it carries none.

    Absent and null are the same answer and both are ordinary: one is an old
    manifest, the other a backup taken with no session attached. A key that is
    *present and unusable* is neither, and it is corruption rather than another
    way of saying None — a manifest this module did not write must not be able to
    put an arbitrary value in front of the attribution check, and reading it as
    "unattributed" would hide the fact that something rewrote it.
    """
    value = payload.get("observed_save_id")
    if value is None:
        return None
    if not isinstance(value, str) or not _OBSERVED_SAVE_ID_RE.match(value):
        raise BackupCorruptError(
            "manifest 'observed_save_id' is present but is not a save id the mod could have "
            "reported"
        )
    return value


def _validate_observed_save_id(observed_save_id: str) -> str:
    """Check a mod-reported save id before it is written into a manifest.

    Raises:
        BackupError: if it is empty, over-long, or carries whitespace or control
            characters. Callers that have nothing to record pass None; an empty
            string is a caller that lost the value on the way here, and silently
            storing it would produce a backup that claims to name a save and
            names nothing.
    """
    if not _OBSERVED_SAVE_ID_RE.match(observed_save_id):
        raise BackupError(
            f"observed save id {observed_save_id!r} is not one the mod could have reported; "
            f"it must be 1..{MAX_SAVE_ID_LEN} characters with no whitespace"
        )
    return observed_save_id


def attributed_to(
    records: Iterable[BackupRecord], observed_save_id: str
) -> tuple[BackupRecord, ...]:
    """Those *records* whose manifest names *observed_save_id*, in the given order.

    Exact string equality against the id the mod itself reported, and nothing
    else. There is deliberately no nearest, newest or only-one-backup fallback:
    the whole point of recording the id is that "this is probably the right
    backup" stops being an answer, and a filter that ever returns a record whose
    ``observed_save_id`` differs would hand the autonomy gate a safety net for
    another world.
    """
    if not observed_save_id:
        return ()
    return tuple(record for record in records if record.observed_save_id == observed_save_id)


def _validate_relative_member(relative: str) -> tuple[str, ...]:
    """Check a manifest path stays inside the backup, returning its segments.

    A hand-edited manifest is the one input to a restore that is not produced by
    this module, so an absolute path or a ``..`` segment must be rejected before
    it is ever joined onto the target directory.
    """
    if not relative or relative.startswith("/") or relative.startswith("\\"):
        raise BackupCorruptError(f"manifest entry {relative!r} is not a relative path")
    segments = tuple(part for part in relative.replace("\\", "/").split("/") if part)
    if not segments:
        raise BackupCorruptError(f"manifest entry {relative!r} names no file")
    for segment in segments:
        if segment in {".", ".."} or ":" in segment:
            raise BackupCorruptError(f"manifest entry {relative!r} escapes the backup")
    return segments


def _validate_save_id(save_id: str) -> tuple[str, ...]:
    """Split a save id into path segments after rejecting anything unsafe.

    Raises:
        BackupError: if the id is empty, too long, too deep, or contains a
            component that could escape the ``Saves`` directory.
    """
    if not save_id or len(save_id) > MAX_SAVE_ID_LEN:
        raise BackupError(f"save id must be 1..{MAX_SAVE_ID_LEN} characters")
    segments = tuple(part for part in save_id.replace("\\", "/").split("/") if part)
    if not segments:
        raise BackupError(f"save id {save_id!r} names no directory")
    if len(segments) > MAX_SAVE_ID_DEPTH:
        raise BackupError(f"save id {save_id!r} is nested deeper than {MAX_SAVE_ID_DEPTH} levels")
    for segment in segments:
        if segment in {".", ".."} or not _SAVE_SEGMENT_RE.match(segment):
            raise BackupError(f"save id {save_id!r} contains an unusable component {segment!r}")
    return segments


def _validate_backup_id(backup_id: str) -> str:
    if not _BACKUP_ID_RE.match(backup_id):
        raise BackupNotFoundError(f"malformed backup id {backup_id!r}")
    return backup_id


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _copy_and_hash(source: Path, destination: Path, *, limit: int) -> tuple[str, int]:
    """Stream *source* to *destination*, returning its digest and byte count.

    Raises:
        BackupTooLargeError: as soon as more than *limit* bytes have been read,
            so a file that grows during the copy cannot exhaust the disk.
    """
    digest = hashlib.sha256()
    written = 0
    with source.open("rb") as reader, destination.open("wb") as writer:
        while True:
            chunk = reader.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise BackupTooLargeError(
                    f"{source}: grew past the remaining {limit} byte budget during copy"
                )
            digest.update(chunk)
            writer.write(chunk)
    return digest.hexdigest(), written


def _remove_tree(path: Path) -> None:
    """Remove *path* whatever it is, tolerating its absence.

    :func:`shutil.rmtree` refuses a symbolic link outright, and ``ignore_errors``
    turns that refusal into silence. That matters here because the paths this is
    asked to clear are the ones :meth:`BackupManager._swap` is about to
    ``os.replace`` onto: a link that survived the clear makes the rename fail
    with ``ENOTDIR``, and it keeps failing on every later attempt, so a save the
    user moved to another drive and linked back becomes permanently
    unrestorable. Links are unlinked; real trees are removed.
    """
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    shutil.rmtree(path, ignore_errors=True)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


class BackupManager:
    """Creates, lists, restores and prunes save backups.

    ``user_dir`` is the ``Zomboid`` directory (saves live under its ``Saves``
    subdirectory); ``backup_root`` is where backups are written and is created
    on demand.
    """

    def __init__(
        self,
        user_dir: Path,
        backup_root: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BACKUP_BYTES,
        max_files: int = DEFAULT_MAX_BACKUP_FILES,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if max_bytes < 1:
            raise BackupError("max_bytes must be positive")
        if max_files < 1:
            raise BackupError("max_files must be positive")
        self._user_dir = user_dir
        self._backup_root = backup_root
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._clock = clock

    @property
    def user_dir(self) -> Path:
        return self._user_dir

    @property
    def backup_root(self) -> Path:
        return self._backup_root

    @property
    def saves_dir(self) -> Path:
        return self._user_dir / SAVES_DIR_NAME

    # -- creation ---------------------------------------------------------

    def create(self, save_id: str, *, observed_save_id: str | None = None) -> BackupRecord:
        """Copy the save named by *save_id* into a new, hash-manifested backup.

        The staged backup is re-read from disk and re-hashed against its own
        manifest *before* it is moved into place (docs/blueprint/08_SAFETY.md
        §8.3, "verify readable"). A record is only returned once that has
        passed, so a returned record means a verified backup exists rather than
        that a copy loop finished without raising.

        *observed_save_id* is the save id the mod reported for the session that
        was attached while this ran, and it is the caller's to establish: this
        module reads save *directories* and cannot compute it. None means no
        session reported one, and it is recorded as none — this is the parameter
        that must never be filled in with a plausible value.

        Raises:
            BackupError: if the save id, or a supplied observed save id, is
                unusable.
            SaveNotFoundError: if the save directory does not exist.
            BackupTooLargeError: if the save exceeds the size or file-count cap.
            BackupCorruptError: if the backup does not read back as written.
        """
        observed = (
            None if observed_save_id is None else _validate_observed_save_id(observed_save_id)
        )
        segments = _validate_save_id(save_id)
        source = self.saves_dir.joinpath(*segments)
        if not source.is_dir():
            raise SaveNotFoundError(f"no save directory at {source}")

        planned = self._plan(source)
        created_at = self._clock()
        self._backup_root.mkdir(parents=True, exist_ok=True)
        backup_id = self._allocate_id(created_at, segments[-1])
        staging = self._backup_root / f"{_STAGING_PREFIX}{backup_id}"
        final = self._backup_root / backup_id

        try:
            files, total = self._copy_into(source, planned, staging / DATA_DIR_NAME)
            record = BackupRecord(
                backup_id=backup_id,
                save_id="/".join(segments),
                observed_save_id=observed,
                created_at=created_at.astimezone(UTC).isoformat(),
                source_dir=self._redact(source),
                total_bytes=total,
                files=files,
                product_version=PRODUCT_VERSION,
                schema_version=SCHEMA_VERSION,
                directory=final,
            )
            manifest = staging / MANIFEST_NAME
            manifest.write_text(
                json.dumps(record.to_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            # Read the manifest back off the disk rather than trusting the
            # in-memory record: this proves the document parses, and re-hashing
            # the staged data proves the copy landed.
            self._verify_record(self._load(staging, expect_id=backup_id))
            os.replace(staging, final)
        except BaseException:
            # A half-written backup that survived would be listed and could be
            # restored; the staging directory is removed on every failure path.
            _remove_tree(staging)
            raise
        if not (final / MANIFEST_NAME).is_file():
            raise BackupCorruptError(f"{final}: backup did not land at its final location")
        return record

    def _plan(self, source: Path) -> tuple[tuple[Path, int], ...]:
        """Enumerate the files to copy, enforcing every cap before any I/O.

        The tree is walked lazily and each cap is checked as the walk proceeds.
        Collecting the whole listing first and checking afterwards would mean a
        directory with ten million entries exhausts memory before the file cap
        is ever consulted, so the caps are applied per entry and only the
        (bounded) planned list is held.

        Symbolic links are refused rather than followed: a link inside a save
        directory would otherwise pull arbitrary files from the user's disk into
        a backup that later gets attached to a support bundle.
        """
        planned: list[tuple[Path, int]] = []
        total = 0
        directories = 0
        for entry in source.rglob("*"):
            if entry.is_symlink():
                raise BackupError(f"{entry}: symbolic links inside a save are not backed up")
            if entry.is_dir():
                directories += 1
                if directories > MAX_BACKUP_DIRS:
                    raise BackupTooLargeError(
                        f"{source}: more than {MAX_BACKUP_DIRS} directories; refusing to back up"
                    )
                continue
            if not entry.is_file():
                raise BackupError(f"{entry}: not a regular file")
            if len(planned) >= self._max_files:
                raise BackupTooLargeError(
                    f"{source}: more than {self._max_files} files; refusing to back up"
                )
            size = entry.stat().st_size
            total += size
            if total > self._max_bytes:
                raise BackupTooLargeError(
                    f"{source}: larger than the {self._max_bytes} byte cap; refusing to back up"
                )
            planned.append((entry, size))
        if not planned:
            raise BackupError(f"{source}: contains no files; refusing to record an empty backup")
        # rglob order is filesystem order; the manifest is sorted so two backups
        # of the same save produce the same document.
        planned.sort()
        return tuple(planned)

    def _copy_into(
        self,
        source: Path,
        planned: tuple[tuple[Path, int], ...],
        data_dir: Path,
    ) -> tuple[tuple[BackupFile, ...], int]:
        data_dir.mkdir(parents=True, exist_ok=False)
        files: list[BackupFile] = []
        total = 0
        for entry, _ in planned:
            relative = entry.relative_to(source)
            destination = data_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                digest, written = _copy_and_hash(entry, destination, limit=self._max_bytes - total)
            except OSError as exc:
                # The plan and the copy are two passes over a directory a live
                # game is still writing, so a file named by the first can be gone
                # by the second. That is a refusal this subsystem owns, not an
                # ``OSError`` for the CLI to let escape as a traceback.
                raise BackupError(
                    f"{entry}: changed while it was being backed up ({exc}); no backup was recorded"
                ) from exc
            total += written
            files.append(BackupFile(path=relative.as_posix(), size=written, sha256=digest))
        return tuple(files), total

    def _allocate_id(self, created_at: datetime, save_name: str) -> str:
        """Mint a backup id that sorts by time and is safe as a directory name."""
        stamp = created_at.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)
        slug = re.sub(r"[^A-Za-z0-9]+", "-", save_name).strip("-").lower() or "save"
        base = f"{stamp}-{slug[:40]}"
        for attempt in range(_MAX_ID_ATTEMPTS):
            candidate = base if attempt == 0 else f"{base}-{attempt}"
            if not (self._backup_root / candidate).exists():
                return candidate
        raise BackupError(f"could not allocate a free backup id starting with {base!r}")

    def _redact(self, path: Path) -> str:
        """Render *path* without the user's home directory in it."""
        for base, placeholder in (
            (self._user_dir, USER_DIR_PLACEHOLDER),
            (self._user_dir.parent, HOME_PLACEHOLDER),
        ):
            if path == base:
                return placeholder
            if path.is_relative_to(base):
                return f"{placeholder}/{path.relative_to(base).as_posix()}"
        return f"{REDACTED_PLACEHOLDER}/{path.name}"

    # -- listing ----------------------------------------------------------

    def list_backups(self) -> tuple[BackupRecord, ...]:
        """Every readable backup, newest first.

        Directories without a parseable manifest are skipped rather than raising:
        an interrupted create or a foreign directory under the backup root must
        not make the whole listing — and therefore ``prune`` — unusable.
        """
        records = list(self._iter_records())
        records.sort(key=lambda record: (record.created_at, record.backup_id), reverse=True)
        return tuple(records)

    def list_unreadable(self) -> tuple[str, ...]:
        """Describe every directory under the backup root that is not a backup.

        :meth:`list_backups` has to skip these — one interrupted create must not
        make the whole listing unusable — but skipping them silently would mean
        a corrupt backup is invisible in ``doctor`` output *and* immune to
        :meth:`prune`, so the reasons are recoverable here.
        """
        problems: list[str] = []
        for _ in self._iter_records(problems):
            continue
        return tuple(problems)

    def _iter_records(self, problems: list[str] | None = None) -> Iterator[BackupRecord]:
        if not self._backup_root.is_dir():
            return
        for directory in sorted(self._backup_root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            try:
                # Held to the same rule ``get`` applies, so every id this
                # yields is one ``get`` can resolve.
                yield self._load(directory, expect_id=_validate_backup_id(directory.name))
            except (BackupCorruptError, BackupNotFoundError) as exc:
                if problems is not None:
                    problems.append(f"{directory.name}: {exc}")
                continue

    def attributed_to(self, observed_save_id: str) -> tuple[BackupRecord, ...]:
        """Readable backups whose manifest names *observed_save_id*, newest first.

        Empty when nothing names it, including when *observed_save_id* is empty:
        "no session reported a save" must not select every backup that reported
        none either. See :func:`attributed_to` for why the match is exact.
        """
        return attributed_to(self.list_backups(), observed_save_id)

    def get(self, backup_id: str) -> BackupRecord:
        """Load one backup's record.

        Raises:
            BackupNotFoundError: if no such backup exists.
            BackupCorruptError: if its manifest cannot be read.
        """
        directory = self._backup_root / _validate_backup_id(backup_id)
        if not directory.is_dir():
            raise BackupNotFoundError(f"no backup {backup_id!r} under {self._backup_root}")
        return self._load(directory, expect_id=backup_id)

    def _load(self, directory: Path, *, expect_id: str) -> BackupRecord:
        """Read and validate one manifest.

        *expect_id* is the id the caller reached this directory by. A manifest
        naming a different backup would make :meth:`list_backups` hand out ids
        that :meth:`get` cannot resolve, so the mismatch is corruption.
        """
        manifest = directory / MANIFEST_NAME
        if not manifest.is_file():
            raise BackupNotFoundError(f"{directory}: no {MANIFEST_NAME}")
        try:
            with manifest.open("rb") as handle:
                raw = handle.read(MAX_MANIFEST_BYTES + 1)
        except OSError as exc:
            raise BackupCorruptError(f"{manifest}: unreadable ({exc})") from exc
        if len(raw) > MAX_MANIFEST_BYTES:
            raise BackupCorruptError(f"{manifest}: larger than {MAX_MANIFEST_BYTES} bytes")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupCorruptError(f"{manifest}: unreadable ({exc})") from exc
        if not isinstance(payload, dict):
            raise BackupCorruptError(f"{manifest}: manifest is not a JSON object")
        record = BackupRecord.from_dict(payload, directory=directory)
        if record.backup_id != expect_id:
            raise BackupCorruptError(
                f"{manifest}: manifest names backup {record.backup_id!r}, not {expect_id!r}"
            )
        return record

    # -- verification and restore ----------------------------------------

    def verify(self, backup_id: str) -> BackupRecord:
        """Re-hash a backup's contents and compare them to its manifest.

        Raises:
            BackupCorruptError: on the first mismatch, a missing file, or a file
                present in the backup but absent from the manifest.
        """
        record = self.get(backup_id)
        self._verify_record(record)
        return record

    def _verify_record(self, record: BackupRecord) -> None:
        data_dir = record.data_dir
        expected: dict[str, BackupFile] = {}
        for entry in record.files:
            segments = _validate_relative_member(entry.path)
            relative = "/".join(segments)
            if relative in expected:
                raise BackupCorruptError(
                    f"backup {record.backup_id}: manifest lists {relative!r} more than once"
                )
            expected[relative] = entry

        declared = sum(entry.size for entry in record.files)
        if declared != record.total_bytes:
            raise BackupCorruptError(
                f"backup {record.backup_id}: manifest totals {declared} bytes across its "
                f"file entries but declares {record.total_bytes}"
            )

        # The walk is bounded the same way the create-side walk is: a backup
        # root someone dropped a tree into must not be able to exhaust memory
        # here either.
        limit = max(self._max_files, len(expected))
        present: set[str] = set()
        if data_dir.is_dir():
            for path in data_dir.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                if len(present) >= limit:
                    raise BackupCorruptError(
                        f"backup {record.backup_id}: contains more than {limit} files"
                    )
                present.add(path.relative_to(data_dir).as_posix())

        missing = sorted(set(expected) - present)
        if missing:
            raise BackupCorruptError(
                f"backup {record.backup_id}: {len(missing)} manifested file(s) missing, "
                f"first is {missing[0]!r}"
            )
        unexpected = sorted(present - set(expected))
        if unexpected:
            raise BackupCorruptError(
                f"backup {record.backup_id}: {len(unexpected)} file(s) not in the manifest, "
                f"first is {unexpected[0]!r}"
            )

        for relative, entry in sorted(expected.items()):
            digest, size = _hash_file(data_dir / relative)
            if size != entry.size:
                raise BackupCorruptError(
                    f"backup {record.backup_id}: {relative!r} is {size} bytes, "
                    f"manifest says {entry.size}"
                )
            if digest != entry.sha256:
                raise BackupCorruptError(
                    f"backup {record.backup_id}: {relative!r} failed its SHA-256 check"
                )

    def restore(self, backup_id: str, *, game_running: bool) -> RestoreResult:
        """Put a verified backup back into the saves directory.

        The save is rebuilt in a sibling staging directory and swapped in, so an
        interruption leaves either the previous save or the restored one intact.

        Raises:
            GameRunningError: if *game_running* is true. Always checked first.
            BackupNotFoundError: if the backup does not exist.
            BackupCorruptError: if any file fails its hash check; nothing is
                written in that case.
        """
        if game_running:
            raise GameRunningError(
                "refusing to restore while Project Zomboid is running; close the game and try again"
            )
        record = self.get(backup_id)
        self._verify_record(record)

        segments = _validate_save_id(record.save_id)
        target = self.saves_dir.joinpath(*segments)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f"{_RESTORE_PREFIX}{record.backup_id}"
        replaced = target.parent / f"{_REPLACED_PREFIX}{record.backup_id}"
        _remove_tree(staging)
        _remove_tree(replaced)
        for path in (staging, replaced):
            # Anything still here would be renamed onto, or renamed over, during
            # the swap. Saying so now is better than an OSError from the middle
            # of a two-step rename, which is the point where "old save or new
            # save, never both" stops being obvious.
            if path.exists() or path.is_symlink():
                raise BackupError(
                    f"{path}: left over from an earlier restore and could not be removed; "
                    "delete it and try again"
                )

        total = 0
        try:
            staging.mkdir(parents=True, exist_ok=False)
            for entry in record.files:
                segments = _validate_relative_member(entry.path)
                destination = staging.joinpath(*segments)
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    digest, written = _copy_and_hash(
                        record.data_dir.joinpath(*segments),
                        destination,
                        limit=self._max_bytes - total,
                    )
                except OSError as exc:
                    # Same window as the digest check below — the backup was
                    # intact at the pre-flight verify and is not now — so it is
                    # reported the same way rather than as a bare OSError.
                    raise BackupCorruptError(
                        f"backup {record.backup_id}: {entry.path!r} became unreadable while "
                        f"it was being restored ({exc}); the save was not touched"
                    ) from exc
                # The pre-flight verify proves the backup was intact a moment
                # ago; this proves the bytes that just went into the staged save
                # are those bytes, so the swap is never a swap of something
                # unobserved.
                if written != entry.size or digest != entry.sha256:
                    raise BackupCorruptError(
                        f"backup {record.backup_id}: {entry.path!r} changed while it was "
                        "being restored; the save was not touched"
                    )
                total += written
        except BaseException:
            _remove_tree(staging)
            raise

        self._swap(staging=staging, target=target, replaced=replaced)
        if not target.is_dir():
            raise BackupError(f"{target}: restore reported no error but the save is not there")
        return RestoreResult(
            backup_id=record.backup_id,
            save_id=record.save_id,
            target_dir=target,
            file_count=record.file_count,
            total_bytes=total,
        )

    def _swap(self, *, staging: Path, target: Path, replaced: Path) -> None:
        """Move *staging* onto *target*, keeping the old save until it lands.

        A *target* that is a symbolic link — a save the user moved to another
        drive and linked back — is displaced like any other previous save and
        ends up a real directory here. The linked-to data is left untouched
        where it is; it simply stops being what the game reads.
        """
        had_previous = target.exists() or target.is_symlink()
        if had_previous:
            try:
                os.replace(target, replaced)
            except OSError:
                # Nothing has moved yet, so the previous save is still whole —
                # but the staged copy must not be left behind to be mistaken
                # for a save later.
                _remove_tree(staging)
                raise
        try:
            os.replace(staging, target)
        except OSError:
            if had_previous:
                os.replace(replaced, target)
            _remove_tree(staging)
            raise
        _remove_tree(replaced)

    # -- retention --------------------------------------------------------

    def prune(self, keep: int) -> tuple[str, ...]:
        """Delete all but the *keep* most recent backups; return the ids removed.

        This is the only code path in the subsystem that deletes a backup, and
        ``keep`` below 1 is rejected so the newest one can never be pruned away.

        Retention only sees readable backups. A directory :meth:`list_backups`
        had to skip is never counted and never deleted — see
        :meth:`list_unreadable`, which is how a caller finds out it is there.

        Raises:
            BackupError: if a deletion fails, naming how many had already been
                removed rather than reporting a clean prune.
        """
        if keep < 1:
            raise BackupError("prune must keep at least one backup")
        doomed = self.list_backups()[keep:]
        removed: list[str] = []
        for record in doomed:
            try:
                shutil.rmtree(record.directory)
            except OSError as exc:
                raise BackupError(
                    f"pruned {len(removed)} backup(s), then could not remove "
                    f"{record.backup_id}: {exc}"
                ) from exc
            removed.append(record.backup_id)
        return tuple(removed)
