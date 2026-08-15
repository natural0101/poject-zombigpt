"""Installing and removing the bridge mod, reversibly and without administrator rights.

Three rules, and each is enforced rather than documented:

* **Never touch the game installation.** The destination is always under the
  user's ``Zomboid/mods`` directory. The game directory is read by the
  capability scanner and by nothing else in this project.
* **Never overwrite a file it did not write.** Install records a manifest with a
  SHA-256 per file. A destination file that is absent from the manifest, or
  present but changed since install, stops the whole install *before* anything
  is written — a partially replaced mod is worse than a refused one.
* **Uninstall removes exactly what install added.** It walks the manifest, not
  the directory. A file whose hash no longer matches is left in place and
  reported, because a user who edited it meant to.

Everything is bounded: the file count and the byte total are capped, and the
extension allowlist means an install can only ever place the kinds of file a
Project Zomboid mod is made of.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pz_agent_core.protocol import JsonDict
from pz_agent_core.version import MOD_VERSION, PRODUCT_VERSION

#: Directory name under ``Zomboid/mods``. Matches ``id=`` in ``mod.info``.
MOD_ID: Final = "pz_agent_bridge"

MOD_INFO_NAME: Final = "mod.info"

#: The install ledger, written last and removed last.
MANIFEST_NAME: Final = "pz-agent-install.json"

#: A Project Zomboid mod is Lua, metadata and a poster. Anything else in the
#: source tree is not copied, so an installer can never place an executable.
ALLOWED_SUFFIXES: Final = frozenset({".lua", ".info", ".txt", ".md", ".png", ".json"})

MAX_INSTALL_FILES: Final = 500

MAX_INSTALL_BYTES: Final = 16 * 1024 * 1024

MAX_FILE_BYTES: Final = 4 * 1024 * 1024

#: Depth of the source tree that is copied. ``media/lua/client/PZAgent/X.lua``
#: is five, so this leaves room without allowing an unbounded walk.
MAX_DEPTH: Final = 10

#: Where the mod's source lives in a checkout, relative to the repository root.
SOURCE_SUBPATH: Final = ("pz-mod", "42")

_MOD_INFO_MAX_BYTES: Final = 64 * 1024


class InstallError(Exception):
    """An install or uninstall could not be completed."""


class ForeignFileError(InstallError):
    """A destination file was not written by pz-agent, so nothing was written.

    Carries the offending path so the caller can name it exactly rather than
    telling the user that "something" was in the way.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True, slots=True)
class InstalledFile:
    """One file the installer wrote, addressed relative to the mod directory."""

    path: str
    size: int
    sha256: str

    def to_dict(self) -> JsonDict:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InstalledFile:
        path = payload.get("path")
        size = payload.get("size")
        digest = payload.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise InstallError("install manifest entry has a non-string path or digest")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise InstallError(f"install manifest entry {path!r} has no usable size")
        _check_relative(path)
        return cls(path=path, size=size, sha256=digest)


@dataclass(frozen=True, slots=True)
class InstallManifest:
    """What one install placed. The only thing uninstall is allowed to consult."""

    mod_id: str
    mod_version: str
    product_version: str
    installed_at: str
    files: tuple[InstalledFile, ...]
    directories: tuple[str, ...]

    def to_dict(self) -> JsonDict:
        return {
            "mod_id": self.mod_id,
            "mod_version": self.mod_version,
            "product_version": self.product_version,
            "installed_at": self.installed_at,
            "files": [entry.to_dict() for entry in self.files],
            "directories": list(self.directories),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InstallManifest:
        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            raise InstallError("install manifest has no 'files' array")
        raw_dirs = payload.get("directories", [])
        if not isinstance(raw_dirs, list):
            raise InstallError("install manifest 'directories' is not an array")
        return cls(
            mod_id=_text(payload, "mod_id"),
            mod_version=_text(payload, "mod_version"),
            product_version=_text(payload, "product_version"),
            installed_at=_text(payload, "installed_at"),
            files=tuple(
                InstalledFile.from_dict(entry) for entry in raw_files if isinstance(entry, Mapping)
            ),
            directories=tuple(str(entry) for entry in raw_dirs),
        )

    def by_path(self) -> dict[str, InstalledFile]:
        return {entry.path: entry for entry in self.files}


@dataclass(frozen=True, slots=True)
class InstallResult:
    """What an install did."""

    destination: Path
    manifest: InstallManifest
    files_written: int
    bytes_written: int
    replaced: tuple[str, ...] = ()
    removed_stale: tuple[str, ...] = ()
    kept_stale: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UninstallResult:
    """What an uninstall removed, and what it deliberately did not."""

    destination: Path
    removed: tuple[str, ...] = ()
    kept_modified: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    directory_removed: bool = False


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InstallError(f"install manifest field {key!r} is missing or not a string")
    return value


def _check_relative(relative: str) -> tuple[str, ...]:
    """Split a manifest path, refusing anything that could escape the mod directory.

    **Both separator conventions, because this runs on Windows.** The first
    version split on ``/`` alone, so ``../../evil`` was refused and
    ``..\\..\\evil`` was not: the traversal parts never became parts at all, the
    whole string stayed one segment that is not ``..``, and every check below
    passed it. ``destination.joinpath("..\\..\\evil")`` on Windows then resolves
    the backslashes as separators and lands outside the mod directory —
    measured, not reasoned about.

    That is reachable through a manifest, not only through the shipped one.
    ``install-mod`` reads the ledger it wrote into the user's Zomboid directory
    on a previous run, and every path recorded there is passed through here;
    the ones it calls stale are then ``unlink``ed. A corrupt or hand-edited
    ledger could therefore delete a file this project never installed, which is
    the exact outcome the surrounding audit — ``ForeignFileError`` on the first
    file pz-agent did not write — exists to make impossible.

    ``platform/backup.py`` already normalises both separators this way; this is
    that idiom, applied where it was missing rather than invented here.
    """
    if not relative or relative.startswith("/") or relative.startswith("\\"):
        raise InstallError(f"install path must be relative, got {relative!r}")
    parts = tuple(relative.replace("\\", "/").split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise InstallError(f"install path must not traverse: {relative!r}")
    if len(parts) > MAX_DEPTH:
        raise InstallError(f"install path is deeper than {MAX_DEPTH}: {relative!r}")
    return parts


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def find_source(start: Path | None = None) -> Path | None:
    """Locate ``pz-mod/42`` by walking up from this module.

    An editable checkout is the supported layout, so the mod source sits beside
    the packages rather than inside the wheel. Returning ``None`` rather than
    guessing lets the caller print a remediation naming ``--source``.
    """
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        candidate = parent.joinpath(*SOURCE_SUBPATH)
        if (candidate / MOD_INFO_NAME).is_file():
            return candidate
    return None


def read_mod_info(directory: Path) -> tuple[dict[str, str] | None, str]:
    """Parse ``mod.info`` as the ``key=value`` file the game reads.

    Returns ``(fields, problem)``; exactly one is meaningful. Not a general INI
    parser: ``mod.info`` has no sections and a value may contain ``=``.
    """
    path = directory / MOD_INFO_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, f"no {MOD_INFO_NAME} in {path.parent.name}"
    except OSError as exc:
        return None, f"{MOD_INFO_NAME} cannot be read ({exc.strerror or exc})"
    if len(raw) > _MOD_INFO_MAX_BYTES:
        return None, f"{MOD_INFO_NAME} is {len(raw)} bytes; that is not a mod descriptor"
    fields: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator:
            continue
        fields[key.strip()] = value.strip()
    if "id" not in fields:
        return None, f"{MOD_INFO_NAME} has no id= line"
    return fields, ""


def _plan_source(source: Path) -> tuple[tuple[str, Path, int], ...]:
    """Every file to copy, as ``(relative posix path, absolute path, size)``.

    Raises:
        InstallError: when the source is not a mod, or breaches a bound.
    """
    if not (source / MOD_INFO_NAME).is_file():
        raise InstallError(f"{source}: no {MOD_INFO_NAME}; this is not a mod directory")
    planned: list[tuple[str, Path, int]] = []
    total = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        relative = path.relative_to(source).as_posix()
        _check_relative(relative)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise InstallError(f"{relative}: {size} bytes exceeds the {MAX_FILE_BYTES} cap")
        total += size
        if len(planned) >= MAX_INSTALL_FILES:
            raise InstallError(f"the source holds more than {MAX_INSTALL_FILES} files")
        if total > MAX_INSTALL_BYTES:
            raise InstallError(f"the source is larger than {MAX_INSTALL_BYTES} bytes")
        planned.append((relative, path, size))
    if not planned:
        raise InstallError(f"{source}: nothing to install")
    return tuple(planned)


def read_manifest(destination: Path) -> InstallManifest | None:
    """Load the install ledger, or ``None`` when there is none.

    Raises:
        InstallError: when a manifest exists but does not parse. A destination
            with an unreadable ledger is not treated as a fresh install, because
            that would authorise overwriting whatever is already there.
    """
    path = destination / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"{MANIFEST_NAME} is present but unreadable ({exc})") from exc
    if not isinstance(document, dict):
        raise InstallError(f"{MANIFEST_NAME} does not hold a JSON object")
    return InstallManifest.from_dict(document)


def _audit_destination(
    destination: Path,
    planned: Iterable[tuple[str, Path, int]],
    manifest: InstallManifest | None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Check every file the install would touch.

    Returns ``(replaced, stale, kept_stale)``: what this install will overwrite,
    what an earlier version left behind that it will delete, and what it will
    leave alone because the hash says the user edited it.

    Raises:
        ForeignFileError: on the first file pz-agent did not write. Nothing has
            been written at this point, which is the whole reason the audit is a
            separate pass.
    """
    known = {} if manifest is None else manifest.by_path()
    replaced: list[str] = []
    for relative, _, _ in planned:
        # Through the guard rather than around it. This was the one place that
        # split a manifest path itself, so a path the guard would refuse still
        # reached the filesystem here — for a read, but the audit's answer
        # decides what the write pass then does.
        target = destination.joinpath(*_check_relative(relative))
        if not target.exists():
            continue
        recorded = known.get(relative)
        if recorded is None:
            raise ForeignFileError(target, "a file is already here that pz-agent did not install")
        digest, size = _sha256(target)
        if digest != recorded.sha256 or size != recorded.size:
            raise ForeignFileError(target, "the installed file has been modified since install")
        replaced.append(relative)

    planned_paths = {relative for relative, _, _ in planned}
    stale: list[str] = []
    kept: list[str] = []
    for relative in sorted(path for path in known if path not in planned_paths):
        target = destination / Path(*_check_relative(relative))
        if not target.is_file():
            # Already gone. Listing it as removed would report a deletion this
            # install did not perform.
            continue
        digest, size = _sha256(target)
        recorded = known[relative]
        # The same rule uninstall follows: a file whose hash no longer matches
        # was edited by the user, and dropping a version is not authority to
        # delete their work.
        (kept if digest != recorded.sha256 or size != recorded.size else stale).append(relative)
    return tuple(replaced), tuple(stale), tuple(kept)


def install_mod(
    source: Path,
    mods_dir: Path,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> InstallResult:
    """Copy the mod into ``<mods_dir>/pz_agent_bridge``.

    Raises:
        InstallError: when the source is unusable, a bound is breached, or a
            copy fails part way. In that last case the ledger is written for the
            files that did land, so the next run recognises them as its own
            instead of refusing them as foreign.
        ForeignFileError: when the destination holds a file pz-agent did not
            write. Nothing is copied in that case.
    """
    destination = mods_dir / MOD_ID
    planned = _plan_source(source)
    manifest = read_manifest(destination)
    replaced, stale, kept_stale = _audit_destination(destination, planned, manifest)

    written: list[InstalledFile] = []
    directories: set[str] = set()
    destination.mkdir(parents=True, exist_ok=True)
    for relative, path, _ in planned:
        parts = _check_relative(relative)
        target = destination.joinpath(*parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            for depth in range(1, len(parts)):
                directories.add("/".join(parts[:depth]))
            shutil.copyfile(path, target)
            digest, size = _sha256(target)
        except OSError as exc:
            # The ledger goes down before the error goes up. Without it the
            # files already copied have no record, and the audit above would
            # refuse the retry as somebody else's — an install that failed once
            # could never be repeated.
            _write_ledger(destination, written, directories, clock)
            raise InstallError(
                f"{relative}: copy failed after {len(written)} file(s) "
                f"({exc.strerror or exc}); what was written is recorded in "
                f"{MANIFEST_NAME}, so install-mod can be run again"
            ) from exc
        written.append(InstalledFile(path=relative, size=size, sha256=digest))

    for relative in stale:
        stale_path = destination.joinpath(*_check_relative(relative))
        stale_path.unlink(missing_ok=True)

    ledger = _write_ledger(destination, written, directories, clock)
    return InstallResult(
        destination=destination,
        manifest=ledger,
        files_written=len(written),
        bytes_written=sum(entry.size for entry in written),
        replaced=replaced,
        removed_stale=stale,
        kept_stale=kept_stale,
    )


def _write_ledger(
    destination: Path,
    written: Iterable[InstalledFile],
    directories: Iterable[str],
    clock: Callable[[], datetime],
) -> InstallManifest:
    """Record exactly the files that are on disk. The only writer of the ledger."""
    ledger = InstallManifest(
        mod_id=MOD_ID,
        mod_version=MOD_VERSION,
        product_version=PRODUCT_VERSION,
        installed_at=clock().astimezone(UTC).isoformat(),
        files=tuple(written),
        directories=tuple(sorted(set(directories), key=lambda item: (item.count("/"), item))),
    )
    (destination / MANIFEST_NAME).write_text(
        json.dumps(ledger.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ledger


def uninstall_mod(mods_dir: Path) -> UninstallResult:
    """Remove exactly the files the manifest records, and nothing else.

    Raises:
        InstallError: when there is no manifest. Deleting a directory this tool
            has no record of installing is how an uninstaller takes a user's
            files with it.
    """
    destination = mods_dir / MOD_ID
    if not destination.exists():
        raise InstallError(f"{MOD_ID} is not installed in {mods_dir}")
    manifest = read_manifest(destination)
    if manifest is None:
        raise InstallError(
            f"{destination} has no {MANIFEST_NAME}: pz-agent has no record of installing "
            "this directory and will not delete it. Remove it by hand if it is yours."
        )

    removed: list[str] = []
    kept: list[str] = []
    missing: list[str] = []
    for entry in manifest.files:
        target = destination.joinpath(*_check_relative(entry.path))
        if not target.is_file():
            missing.append(entry.path)
            continue
        digest, size = _sha256(target)
        if digest != entry.sha256 or size != entry.size:
            kept.append(entry.path)
            continue
        target.unlink()
        removed.append(entry.path)

    (destination / MANIFEST_NAME).unlink(missing_ok=True)
    for relative in sorted(manifest.directories, key=lambda item: -item.count("/")):
        directory = destination.joinpath(*_check_relative(relative))
        _remove_if_empty(directory)
    directory_removed = _remove_if_empty(destination)
    return UninstallResult(
        destination=destination,
        removed=tuple(removed),
        kept_modified=tuple(kept),
        missing=tuple(missing),
        directory_removed=directory_removed,
    )


def _remove_if_empty(directory: Path) -> bool:
    """Remove *directory* only when nothing is left in it. Returns whether it went."""
    if not directory.is_dir():
        return False
    try:
        directory.rmdir()
    except OSError:
        # Not empty: something the user put there, or a file kept because it was
        # modified. Either way it stays, and so does its parent.
        return False
    return True
