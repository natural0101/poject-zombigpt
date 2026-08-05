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
  leaves either the old save or the new one, never half of each.
* **Prune is the only deletion path**, it keeps the newest N, and it will not
  accept ``keep < 1``. Nothing else in this module removes a backup.

Everything is bounded: the size and file count of a save are measured and
enforced *while copying*, not only before it, so a save that grows underneath
the copy still cannot fill the disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping
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

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def data_dir(self) -> Path:
        return self.directory / DATA_DIR_NAME

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "product_version": self.product_version,
            "backup_id": self.backup_id,
            "save_id": self.save_id,
            "created_at": self.created_at,
            "source_dir": self.source_dir,
            "total_bytes": self.total_bytes,
            "file_count": self.file_count,
            "files": [entry.to_dict() for entry in self.files],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, directory: Path) -> BackupRecord:
        """Rebuild a record from a manifest document.

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

    def create(self, save_id: str) -> BackupRecord:
        """Copy the save named by *save_id* into a new, hash-manifested backup.

        Raises:
            BackupError: if the save id is unusable.
            SaveNotFoundError: if the save directory does not exist.
            BackupTooLargeError: if the save exceeds the size or file-count cap.
        """
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
            os.replace(staging, final)
        except BaseException:
            # A half-written backup that survived would be listed and could be
            # restored; the staging directory is removed on every failure path.
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return record

    def _plan(self, source: Path) -> tuple[tuple[Path, int], ...]:
        """Enumerate the files to copy, enforcing both caps before any I/O.

        Symbolic links are refused rather than followed: a link inside a save
        directory would otherwise pull arbitrary files from the user's disk into
        a backup that later gets attached to a support bundle.
        """
        planned: list[tuple[Path, int]] = []
        total = 0
        for entry in sorted(source.rglob("*")):
            if entry.is_symlink():
                raise BackupError(f"{entry}: symbolic links inside a save are not backed up")
            if entry.is_dir():
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
            digest, written = _copy_and_hash(entry, destination, limit=self._max_bytes - total)
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

    def _iter_records(self) -> Iterator[BackupRecord]:
        if not self._backup_root.is_dir():
            return
        for directory in sorted(self._backup_root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            try:
                yield self._load(directory)
            except (BackupCorruptError, BackupNotFoundError):
                continue

    def get(self, backup_id: str) -> BackupRecord:
        """Load one backup's record.

        Raises:
            BackupNotFoundError: if no such backup exists.
            BackupCorruptError: if its manifest cannot be read.
        """
        directory = self._backup_root / _validate_backup_id(backup_id)
        if not directory.is_dir():
            raise BackupNotFoundError(f"no backup {backup_id!r} under {self._backup_root}")
        return self._load(directory)

    def _load(self, directory: Path) -> BackupRecord:
        manifest = directory / MANIFEST_NAME
        if not manifest.is_file():
            raise BackupNotFoundError(f"{directory}: no {MANIFEST_NAME}")
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupCorruptError(f"{manifest}: unreadable ({exc})") from exc
        if not isinstance(payload, dict):
            raise BackupCorruptError(f"{manifest}: manifest is not a JSON object")
        return BackupRecord.from_dict(payload, directory=directory)

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
            expected["/".join(segments)] = entry

        present: set[str] = set()
        if data_dir.is_dir():
            for path in sorted(data_dir.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
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
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(replaced, ignore_errors=True)

        total = 0
        try:
            staging.mkdir(parents=True, exist_ok=False)
            for entry in record.files:
                destination = staging.joinpath(*_validate_relative_member(entry.path))
                destination.parent.mkdir(parents=True, exist_ok=True)
                _, written = _copy_and_hash(
                    record.data_dir / entry.path,
                    destination,
                    limit=self._max_bytes - total,
                )
                total += written
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        self._swap(staging=staging, target=target, replaced=replaced)
        return RestoreResult(
            backup_id=record.backup_id,
            save_id=record.save_id,
            target_dir=target,
            file_count=record.file_count,
            total_bytes=total,
        )

    def _swap(self, *, staging: Path, target: Path, replaced: Path) -> None:
        """Move *staging* onto *target*, keeping the old save until it lands."""
        had_previous = target.exists()
        if had_previous:
            os.replace(target, replaced)
        try:
            os.replace(staging, target)
        except OSError:
            if had_previous:
                os.replace(replaced, target)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(replaced, ignore_errors=True)

    # -- retention --------------------------------------------------------

    def prune(self, keep: int) -> tuple[str, ...]:
        """Delete all but the *keep* most recent backups; return the ids removed.

        This is the only code path in the subsystem that deletes a backup, and
        ``keep`` below 1 is rejected so the newest one can never be pruned away.
        """
        if keep < 1:
            raise BackupError("prune must keep at least one backup")
        doomed = self.list_backups()[keep:]
        removed: list[str] = []
        for record in doomed:
            shutil.rmtree(record.directory)
            removed.append(record.backup_id)
        return tuple(removed)
