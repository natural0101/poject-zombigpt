"""Places the bridge mod, the configuration and the launcher on a Windows machine.

**Not the shipped install path, and nothing in the product runs this.** The
Windows archive carries ``install.bat``, which calls ``pz-agent install-mod``; a
checkout follows ``docs/QUICKSTART.md``. This module is the third case — a
machine with neither the archive nor pip — and ``installer/INSTALL.md`` opens by
saying so. It is kept because it is the only path that works before anything is
installed, and it is complete and tested rather than a sketch; it is *not* kept
as an alternative anyone should be steered to, and a reader who arrives at it by
opening this directory needs that said before the first heading.

This module deliberately imports nothing from ``packages/``. An installer runs
*before* the thing it installs exists, so it has to work on a bare CPython with
nothing on ``sys.path`` but the standard library — which is also why the payload
it copies is read off disk rather than resolved through a package.

Four rules, each enforced rather than described:

* **No administrator rights, ever.** Everything is written under the user's own
  ``Zomboid`` directory. The game installation is never opened, not even for
  reading: the sidecar's ``pz-agent doctor`` is what inspects it, and it is a
  separate program the user can decline to run.
* **Never overwrite a file it did not write.** An audit pass hashes every
  destination the install would touch and stops the whole install before writing
  anything if one of them is unaccounted for. A partial install over somebody
  else's mod is worse than a refused one.
* **Uninstall consults the manifest, never the directory.** It removes the exact
  files it recorded placing, leaves any whose hash has since changed, and never
  looks at ``Saves``, ``backups`` or ``config.toml`` at all — the configuration
  is recorded as placed *and* marked preserved, so the ledger stays complete
  without becoming a licence to delete the user's settings.
* **Bounded.** File count, total bytes, per-file bytes, path depth and the
  extensions that may be copied are all capped, so the payload can only ever be
  the Lua, metadata and poster a Project Zomboid mod is made of.

The path discovery here takes an injected filesystem root and environment
mapping for the same reason ``pz_agent_core.platform.discovery`` does: a
Cyrillic account name, a OneDrive-relocated profile and a portable
``-cachedir`` are the cases that actually break Windows path handling, and none
of them is reproducible by running the real thing on one developer's machine.
They are all reproducible against a temporary tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO

#: Format tag on the manifest. An uninstaller that meets a manifest it does not
#: understand must refuse rather than guess at the fields it recognises.
MANIFEST_FORMAT: Final = "pz-agent-installer/1"

MANIFEST_NAME: Final = "installer-manifest.json"

#: Mirrors ``pz_agent_cli.context.STATE_DIR_NAME``. Restated rather than imported
#: because this module has no access to that package; the round-trip test asserts
#: the two still agree.
STATE_DIR_NAME: Final = "pz-agent"

CONFIG_NAME: Final = "config.toml"

LAUNCHER_NAME: Final = "Start-PZ-Agent.cmd"

MODS_DIR_NAME: Final = "mods"

USER_DIR_NAME: Final = "Zomboid"

MOD_INFO_NAME: Final = "mod.info"

#: Where the payload lives in a checkout or an unpacked release, relative to the
#: directory this file sits in or any of its parents.
PAYLOAD_SUBPATH: Final = ("pz-mod", "42")

#: Steam's app id for Project Zomboid. Used only to compose the ``steam://`` URI
#: the launcher hands to the shell.
STEAM_APP_ID: Final = "108600"

ALLOWED_SUFFIXES: Final = frozenset({".lua", ".info", ".txt", ".md", ".png", ".json"})

MAX_FILES: Final = 500

MAX_TOTAL_BYTES: Final = 16 * 1024 * 1024

MAX_FILE_BYTES: Final = 4 * 1024 * 1024

MAX_DEPTH: Final = 12

_MOD_INFO_MAX_BYTES: Final = 64 * 1024

#: Roles a placed file can have. The role decides what uninstall does with it.
ROLE_MOD: Final = "mod"
ROLE_LAUNCHER: Final = "launcher"
ROLE_CONFIG: Final = "config"

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1
EXIT_USAGE: Final = 2


class InstallerError(Exception):
    """The install or uninstall could not be completed."""


class ForeignFileError(InstallerError):
    """A destination holds a file this installer did not write.

    Carries the path so the message can name it exactly instead of telling the
    user that "something" is in the way.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlacedFile:
    """One file the installer put down, addressed relative to ``Zomboid``."""

    path: str
    role: str
    size: int
    sha256: str
    preserved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "size": self.size,
            "sha256": self.sha256,
            "preserved": self.preserved,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PlacedFile:
        path = payload.get("path")
        digest = payload.get("sha256")
        size = payload.get("size")
        role = payload.get("role")
        preserved = payload.get("preserved", False)
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(role, str):
            raise InstallerError("manifest entry has a non-string path, digest or role")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise InstallerError(f"manifest entry {path!r} has no usable size")
        if not isinstance(preserved, bool):
            raise InstallerError(f"manifest entry {path!r} has a non-boolean 'preserved'")
        split_relative(path)
        return cls(path=path, role=role, size=size, sha256=digest, preserved=preserved)


@dataclass(frozen=True, slots=True)
class InstallManifest:
    """The complete record of one install. The only thing uninstall consults."""

    mod_id: str
    mod_version: str
    pz_version: str
    installed_at: str
    files: tuple[PlacedFile, ...]
    directories: tuple[str, ...]
    format: str = MANIFEST_FORMAT

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "mod_id": self.mod_id,
            "mod_version": self.mod_version,
            "pz_version": self.pz_version,
            "installed_at": self.installed_at,
            "files": [entry.to_dict() for entry in self.files],
            "directories": list(self.directories),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InstallManifest:
        declared = payload.get("format")
        if declared != MANIFEST_FORMAT:
            raise InstallerError(
                f"{MANIFEST_NAME} is in format {declared!r}, not {MANIFEST_FORMAT!r}; "
                "this installer will not remove files it cannot account for"
            )
        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            raise InstallerError(f"{MANIFEST_NAME} has no 'files' array")
        raw_dirs = payload.get("directories", [])
        if not isinstance(raw_dirs, list):
            raise InstallerError(f"{MANIFEST_NAME} 'directories' is not an array")
        return cls(
            mod_id=_text(payload, "mod_id"),
            mod_version=_text(payload, "mod_version"),
            pz_version=_text(payload, "pz_version"),
            installed_at=_text(payload, "installed_at"),
            files=tuple(
                PlacedFile.from_dict(entry) for entry in raw_files if isinstance(entry, Mapping)
            ),
            directories=tuple(str(entry) for entry in raw_dirs),
        )

    def by_path(self) -> dict[str, PlacedFile]:
        return {entry.path: entry for entry in self.files}


@dataclass(frozen=True, slots=True)
class InstallResult:
    """What one install placed."""

    target: Path
    manifest: InstallManifest
    files_written: int
    bytes_written: int
    replaced: tuple[str, ...] = ()
    config_created: bool = False
    launcher: Path | None = None


@dataclass(frozen=True, slots=True)
class UninstallResult:
    """What one uninstall removed, and what it deliberately left."""

    target: Path
    removed: tuple[str, ...] = ()
    kept_modified: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    directories_removed: tuple[str, ...] = ()


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InstallerError(f"{MANIFEST_NAME} field {key!r} is missing or not a string")
    return value


def split_relative(relative: str) -> tuple[str, ...]:
    """Split a manifest path, refusing anything that could escape the target.

    Manifest paths are always written with forward slashes, so a backslash in one
    did not come from here. It is refused rather than normalised: on Windows
    ``joinpath("..\\evil.lua")`` is a single component that escapes the directory
    anyway, and ``joinpath("C:", "x")`` resolves drive-relative — both of which
    turn "remove exactly what was placed" into "remove whatever the file says".

    Raises:
        InstallerError: on an absolute path, a backslash, a drive letter, a
            traversal component or a depth past :data:`MAX_DEPTH`. A manifest is
            a file on disk that a user or another tool can edit, so it is parsed
            as untrusted input.
    """
    if not relative or relative.startswith(("/", "\\")):
        raise InstallerError(f"placed path must be relative, got {relative!r}")
    if "\\" in relative:
        raise InstallerError(f"placed path must not use a backslash: {relative!r}")
    parts = tuple(relative.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise InstallerError(f"placed path must not traverse: {relative!r}")
    if any(":" in part for part in parts):
        raise InstallerError(f"placed path must not name a drive: {relative!r}")
    if len(parts) > MAX_DEPTH:
        raise InstallerError(f"placed path is deeper than {MAX_DEPTH}: {relative!r}")
    return parts


def sha256_of(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# ---------------------------------------------------------------------------
# locating things
# ---------------------------------------------------------------------------


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive environment lookup; an empty value counts as absent.

    Windows environment names are case-insensitive and the casing a given shell
    uses for ``OneDrive`` or ``UserProfile`` varies, so an exact-match lookup
    silently loses the profile it was meant to find.
    """
    direct = env.get(name)
    if direct:
        return direct
    wanted = name.lower()
    for key, value in env.items():
        if key.lower() == wanted and value:
            return value
    return None


def zomboid_candidates(*, root: Path, env: Mapping[str, str]) -> tuple[tuple[Path, str], ...]:
    """Every place the ``Zomboid`` directory might be, in priority order."""
    found: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(path: Path, source: str) -> None:
        key = os.path.normcase(str(path))
        if key in seen:
            return
        seen.add(key)
        found.append((path, source))

    profile = _env_value(env, "USERPROFILE")
    if profile:
        add(Path(profile.replace("\\", "/")) / USER_DIR_NAME, "USERPROFILE")
        add(Path(profile.replace("\\", "/")) / "OneDrive" / USER_DIR_NAME, "USERPROFILE_onedrive")
    one_drive = _env_value(env, "OneDrive")
    if one_drive:
        add(Path(one_drive.replace("\\", "/")) / USER_DIR_NAME, "OneDrive")
    home = _env_value(env, "HOME")
    if home:
        add(Path(home) / USER_DIR_NAME, "HOME")
    username = _env_value(env, "USERNAME") or _env_value(env, "USER")
    if username:
        add(root / "Users" / username / USER_DIR_NAME, "root_users")
    return tuple(found)


def find_zomboid_dir(
    *, root: Path, env: Mapping[str, str], override: Path | None = None
) -> tuple[Path | None, tuple[str, ...]]:
    """Locate the ``Zomboid`` directory. Returns ``(path, searched)``.

    The directory is never created. The game creates it on first launch, and an
    empty one this installer made would look to ``pz-agent doctor`` exactly like
    a real profile — which is how a user ends up installing a mod into a
    directory the game will never read.
    """
    if override is not None:
        return (override, (str(override),)) if override.is_dir() else (None, (str(override),))
    searched: list[str] = []
    for candidate, _ in zomboid_candidates(root=root, env=env):
        searched.append(str(candidate))
        if candidate.is_dir():
            return candidate, tuple(searched)
    return None, tuple(searched)


def find_payload(start: Path | None = None) -> Path | None:
    """Locate ``pz-mod/42`` by walking up from this file."""
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        candidate = parent.joinpath(*PAYLOAD_SUBPATH)
        if (candidate / MOD_INFO_NAME).is_file():
            return candidate
    return None


def read_mod_info(directory: Path) -> dict[str, str]:
    """Parse ``mod.info`` as the ``key=value`` file the game reads.

    Not a general INI parser: ``mod.info`` has no sections and a description may
    contain ``=``, so only the first separator splits.

    Raises:
        InstallerError: when the file is absent, oversized or has no ``id=``.
    """
    path = directory / MOD_INFO_NAME
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InstallerError(
            f"{path}: cannot read {MOD_INFO_NAME} ({exc.strerror or exc})"
        ) from exc
    if len(raw) > _MOD_INFO_MAX_BYTES:
        raise InstallerError(f"{path}: {len(raw)} bytes is not a mod descriptor")
    fields: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    if "id" not in fields:
        raise InstallerError(f"{path}: no id= line, so this is not a mod directory")
    return fields


# ---------------------------------------------------------------------------
# generated content
# ---------------------------------------------------------------------------


def render_config(*, expected_build: str) -> str:
    """The starting ``config.toml``.

    Every value is the documented default and ``session.default_mode`` is
    ``observe``, which is the only value that matches what the sidecar actually
    does on attach. The key set is validated against ``pz-agent
    validate-config`` by a test, because an installer that writes a file the
    program then refuses is worse than one that writes none.
    """
    return (
        "# Written by the pz-agent installer. Edit freely; run\n"
        "#   pz-agent validate-config\n"
        "# afterwards — an unknown key is an error, not a warning.\n"
        "\n"
        "[game]\n"
        'channel = "stable"\n'
        f'expected_build = "{expected_build}"\n'
        "\n"
        "[session]\n"
        "# The sidecar always attaches in OBSERVE regardless of this value; it\n"
        "# names the mode 'pz-agent arm' selects when you ask for one.\n"
        'default_mode = "observe"\n'
        "require_backup = true\n"
        "\n"
        "[safety]\n"
        'panic_hotkey = "F12"\n'
        "manual_takeover = true\n"
        "max_autonomous_radius = 30\n"
        "allow_multiplayer = false\n"
        "\n"
        "[planner]\n"
        'provider = "none"\n'
        "max_steps = 8\n"
        "\n"
        "[voice]\n"
        'adapter = "teamon"\n'
        "enabled = false\n"
    )


def render_launcher(*, config_path: Path) -> str:
    """The ``Start-PZ-Agent.cmd`` batch launcher (blueprint §14.4).

    A ``.cmd`` rather than a ``.ps1`` on purpose: an unsigned PowerShell script
    is blocked by the default execution policy, and telling a user to lower that
    policy to run an installer is exactly the habit this project should not be
    teaching. Batch has no equivalent gate, and every line of logic worth testing
    lives in Python rather than in here.
    """
    config = str(config_path)
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        "title PZ Agent\r\n"
        "rem Generated by the pz-agent installer. Safe to delete; re-created on install.\r\n"
        "rem Needs no administrator rights.\r\n"
        f'set "PZ_AGENT_CONFIG={config}"\r\n'
        "\r\n"
        "echo Starting the pz-agent sidecar in OBSERVE.\r\n"
        "echo It will not act until you explicitly run:  pz-agent arm\r\n"
        'pz-agent --config "%PZ_AGENT_CONFIG%" start\r\n'
        "if errorlevel 1 goto failed\r\n"
        "\r\n"
        'if /I "%~1"=="--no-game" goto status\r\n'
        f'start "" "steam://rungameid/{STEAM_APP_ID}"\r\n'
        "\r\n"
        ":status\r\n"
        'pz-agent --config "%PZ_AGENT_CONFIG%" status\r\n'
        "echo.\r\n"
        'echo Stop the sidecar with:  pz-agent --config "%PZ_AGENT_CONFIG%" stop\r\n'
        "goto done\r\n"
        "\r\n"
        ":failed\r\n"
        "echo The sidecar did not start. Run  pz-agent doctor  for the reason.\r\n"
        "endlocal\r\n"
        "exit /b 1\r\n"
        "\r\n"
        ":done\r\n"
        "endlocal\r\n"
    )


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Planned:
    """One file the install intends to place."""

    relative: str
    role: str
    source: Path | None
    body: bytes | None
    preserved: bool = False
    skip_if_present: bool = False


def _plan_payload(payload: Path, mod_id: str) -> tuple[_Planned, ...]:
    """Every mod file to copy, addressed under ``mods/<mod id>/``."""
    planned: list[_Planned] = []
    total = 0
    prefix = f"{MODS_DIR_NAME}/{mod_id}"
    split_relative(prefix)
    for path in sorted(payload.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise InstallerError(
                f"{path.name}: {size} bytes exceeds the {MAX_FILE_BYTES} byte per-file cap"
            )
        total += size
        if len(planned) >= MAX_FILES:
            raise InstallerError(f"the payload holds more than {MAX_FILES} files")
        if total > MAX_TOTAL_BYTES:
            raise InstallerError(f"the payload is larger than {MAX_TOTAL_BYTES} bytes")
        relative = f"{prefix}/{path.relative_to(payload).as_posix()}"
        split_relative(relative)
        planned.append(_Planned(relative=relative, role=ROLE_MOD, source=path, body=None))
    if not planned:
        raise InstallerError(f"{payload}: nothing to install")
    return tuple(planned)


def _audit(
    target: Path, planned: Sequence[_Planned], previous: InstallManifest | None
) -> tuple[str, ...]:
    """Check every destination before a byte is written.

    Returns the relative paths this install will overwrite.

    Raises:
        ForeignFileError: on the first destination that is not accounted for by
            the previous manifest, or whose hash no longer matches it. Nothing
            has been written when this fires, which is the entire reason the
            audit is a separate pass over the plan.
    """
    known = {} if previous is None else previous.by_path()
    replaced: list[str] = []
    for item in planned:
        destination = target.joinpath(*split_relative(item.relative))
        if not destination.exists():
            continue
        if item.skip_if_present:
            # The configuration is the user's file the moment it exists. It is
            # left exactly as it is, so it is not audited and not replaced.
            continue
        recorded = known.get(item.relative)
        if recorded is None:
            raise ForeignFileError(
                destination, "a file is already here that the pz-agent installer did not write"
            )
        digest, size = sha256_of(destination)
        if digest != recorded.sha256 or size != recorded.size:
            raise ForeignFileError(
                destination, "this file has been modified since it was installed"
            )
        replaced.append(item.relative)
    return tuple(replaced)


def read_manifest(target: Path) -> InstallManifest | None:
    """Load the ledger from *target*, or None when there is none.

    Raises:
        InstallerError: when one exists and does not parse. A target with an
            unreadable ledger is not treated as a fresh install, because that
            would authorise overwriting whatever is already there.
    """
    path = target / STATE_DIR_NAME / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallerError(f"{MANIFEST_NAME} is present but unreadable ({exc})") from exc
    if not isinstance(document, dict):
        raise InstallerError(f"{MANIFEST_NAME} does not hold a JSON object")
    return InstallManifest.from_dict(document)


def install(
    target: Path,
    payload: Path,
    *,
    now: datetime | None = None,
) -> InstallResult:
    """Place the mod, the configuration and the launcher under *target*.

    Raises:
        InstallerError: when the payload is unusable or a bound is breached.
        ForeignFileError: when a destination holds a file this installer did not
            write. Nothing is copied in that case.
    """
    if not target.is_dir():
        raise InstallerError(f"{target}: no such Zomboid directory")
    info = read_mod_info(payload)
    mod_id = info["id"]
    split_relative(f"{MODS_DIR_NAME}/{mod_id}")
    state_relative = STATE_DIR_NAME
    config_relative = f"{state_relative}/{CONFIG_NAME}"
    launcher_relative = f"{state_relative}/{LAUNCHER_NAME}"

    planned = [
        *_plan_payload(payload, mod_id),
        _Planned(
            relative=config_relative,
            role=ROLE_CONFIG,
            source=None,
            body=render_config(expected_build=info.get("pzversion", "42.20")).encode("utf-8"),
            preserved=True,
            skip_if_present=True,
        ),
        _Planned(
            relative=launcher_relative,
            role=ROLE_LAUNCHER,
            source=None,
            body=render_launcher(config_path=target / state_relative / CONFIG_NAME).encode("utf-8"),
        ),
    ]

    previous = read_manifest(target)
    replaced = _audit(target, planned, previous)

    placed: list[PlacedFile] = []
    directories: set[str] = set()
    config_created = False
    for item in planned:
        parts = split_relative(item.relative)
        destination = target.joinpath(*parts)
        # Depth 1 is deliberately excluded: ``mods`` and ``pz-agent`` are the
        # user's own directories — the game creates the first and the sidecar
        # keeps logs and backups in the second — and an uninstaller that removed
        # them because they happened to be empty at that moment would be taking
        # something it did not place.
        for depth in range(2, len(parts)):
            directories.add("/".join(parts[:depth]))
        if item.skip_if_present and destination.exists():
            digest, size = sha256_of(destination)
            placed.append(
                PlacedFile(
                    path=item.relative,
                    role=item.role,
                    size=size,
                    sha256=digest,
                    preserved=item.preserved,
                )
            )
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if item.source is not None:
                shutil.copyfile(item.source, destination)
            elif item.body is not None:
                destination.write_bytes(item.body)
            digest, size = sha256_of(destination)
        except OSError as exc:
            # The ledger goes down before the error goes up. Without it the files
            # already copied have no record, and the audit above would refuse the
            # retry as somebody else's — an install that failed once could then
            # never be repeated.
            _write_manifest(target, placed, directories, info, now)
            raise InstallerError(
                f"{item.relative}: could not be written after {len(placed)} file(s) "
                f"({exc.strerror or exc}); what was placed is recorded in {MANIFEST_NAME}, "
                "so the installer can be run again"
            ) from exc
        if item.role == ROLE_CONFIG:
            config_created = True
        placed.append(
            PlacedFile(
                path=item.relative,
                role=item.role,
                size=size,
                sha256=digest,
                preserved=item.preserved,
            )
        )

    manifest = _write_manifest(target, placed, directories, info, now)
    return InstallResult(
        target=target,
        manifest=manifest,
        files_written=len(placed),
        bytes_written=sum(entry.size for entry in placed),
        replaced=replaced,
        config_created=config_created,
        launcher=target.joinpath(*split_relative(launcher_relative)),
    )


def _write_manifest(
    target: Path,
    placed: Sequence[PlacedFile],
    directories: set[str],
    info: Mapping[str, str],
    now: datetime | None,
) -> InstallManifest:
    """Record exactly what is on disk. The only writer of the ledger."""
    moment = now or datetime.now(tz=UTC)
    manifest = InstallManifest(
        mod_id=info["id"],
        mod_version=info.get("modversion", "unknown"),
        pz_version=info.get("pzversion", "unknown"),
        installed_at=moment.astimezone(UTC).isoformat(),
        files=tuple(placed),
        directories=tuple(sorted(directories, key=lambda item: (item.count("/"), item))),
    )
    path = target / STATE_DIR_NAME / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def uninstall(target: Path) -> UninstallResult:
    """Remove exactly the files the manifest records, and nothing else.

    Raises:
        InstallerError: when there is no manifest. Deleting a directory this tool
            has no record of placing is how an uninstaller takes a user's files
            with it.
    """
    manifest = read_manifest(target)
    if manifest is None:
        raise InstallerError(
            f"{target / STATE_DIR_NAME / MANIFEST_NAME} does not exist: this installer has no "
            "record of installing anything here and will not delete files on a guess"
        )
    removed: list[str] = []
    kept: list[str] = []
    preserved: list[str] = []
    missing: list[str] = []
    for entry in manifest.files:
        if entry.preserved:
            preserved.append(entry.path)
            continue
        destination = target.joinpath(*split_relative(entry.path))
        if not destination.is_file():
            missing.append(entry.path)
            continue
        digest, size = sha256_of(destination)
        if digest != entry.sha256 or size != entry.size:
            # Edited since install. A version being dropped is not authority to
            # delete somebody's work.
            kept.append(entry.path)
            continue
        destination.unlink()
        removed.append(entry.path)

    (target / STATE_DIR_NAME / MANIFEST_NAME).unlink(missing_ok=True)
    emptied: list[str] = []
    for relative in sorted(manifest.directories, key=lambda item: -item.count("/")):
        directory = target.joinpath(*split_relative(relative))
        if _remove_if_empty(directory):
            emptied.append(relative)
    return UninstallResult(
        target=target,
        removed=tuple(removed),
        kept_modified=tuple(kept),
        preserved=tuple(preserved),
        missing=tuple(missing),
        directories_removed=tuple(emptied),
    )


def _remove_if_empty(directory: Path) -> bool:
    """Remove *directory* only when nothing is left in it."""
    if not directory.is_dir():
        return False
    try:
        directory.rmdir()
    except OSError:
        # Not empty: the configuration, a log, or something the user put there.
        # It stays, and so does its parent.
        return False
    return True


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pz-agent-installer",
        description=(
            "Install or remove the PZ Agent bridge mod, its configuration and its "
            "launcher. Writes only under your own Zomboid directory; needs no "
            "administrator rights."
        ),
    )
    parser.add_argument("command", choices=("install", "uninstall"), help="what to do")
    parser.add_argument(
        "--zomboid-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="the Zomboid directory, when it is not where the environment says",
    )
    parser.add_argument(
        "--mod-source",
        type=Path,
        default=None,
        metavar="PATH",
        help="the pz-mod/42 payload, when it is not beside this script",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    env: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> int:
    """Entry point. Returns an exit code rather than raising on a refused install.

    A malformed command line still leaves through :mod:`argparse`, which prints
    the usage and exits with status 2 — the same behaviour ``pz-agent`` itself
    has, so the two are not surprising side by side.
    """
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    environment = dict(os.environ) if env is None else env
    base = root or Path(environment.get("SystemDrive", os.sep) + os.sep)
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    target, searched = find_zomboid_dir(root=base, env=environment, override=args.zomboid_dir)
    if target is None:
        _say(err, "No Zomboid directory was found. Looked in:")
        for candidate in searched:
            _say(err, f"  {candidate}")
        _say(
            err,
            "Launch Project Zomboid once so it creates the directory, or pass "
            "--zomboid-dir with the path to it.",
        )
        return EXIT_FAILURE

    if args.command == "uninstall":
        return _run_uninstall(target, out, err)
    return _run_install(target, args.mod_source, out, err)


def _say(stream: TextIO, text: str = "") -> None:
    """Write one line. Not ``print``: the streams are always injected here."""
    stream.write(f"{text}\n")


def _run_install(target: Path, source: Path | None, out: TextIO, err: TextIO) -> int:
    payload = source or find_payload()
    if payload is None:
        _say(
            err,
            "The mod payload (pz-mod/42) was not found beside this script. "
            "Pass --mod-source with the path to it.",
        )
        return EXIT_FAILURE
    try:
        result = install(target, payload)
    except ForeignFileError as exc:
        _say(err, f"Refusing to install: {exc.path} \u2014 {exc.reason}.")
        _say(err, "Nothing was written. Move that file, or run uninstall first.")
        return EXIT_FAILURE
    except InstallerError as exc:
        _say(err, f"Install failed: {exc}")
        return EXIT_FAILURE
    except OSError as exc:
        _say(err, f"Install failed while using the filesystem: {exc.strerror or exc}")
        return EXIT_FAILURE
    config_note = "created" if result.config_created else "yours, left alone"
    _say(out, f"Installed {result.files_written} file(s) into {result.target}")
    _say(
        out,
        f"  mod       {MODS_DIR_NAME}/{result.manifest.mod_id} "
        f"(version {result.manifest.mod_version})",
    )
    _say(out, f"  config    {STATE_DIR_NAME}/{CONFIG_NAME}  ({config_note})")
    _say(out, f"  launcher  {STATE_DIR_NAME}/{LAUNCHER_NAME}")
    _say(out, f"  manifest  {STATE_DIR_NAME}/{MANIFEST_NAME}")
    _say(out)
    _say(out, "Next:")
    _say(out, "  1. Start Project Zomboid and enable 'PZ Agent Bridge' in the Mods menu.")
    _say(out, "  2. Load your save - enabling a mod does not affect a game already loaded.")
    _say(out, "  3. Run  pz-agent doctor  to check the installation.")
    _say(out, "  4. Run the launcher, or  pz-agent start . The sidecar attaches in OBSERVE")
    _say(out, "     and does nothing until you run  pz-agent arm .")
    return EXIT_OK


def _run_uninstall(target: Path, out: TextIO, err: TextIO) -> int:
    try:
        result = uninstall(target)
    except InstallerError as exc:
        _say(err, f"Uninstall failed: {exc}")
        return EXIT_FAILURE
    except OSError as exc:
        _say(
            err,
            f"Uninstall stopped while using the filesystem: {exc.strerror or exc}. "
            "Nothing further was removed.",
        )
        return EXIT_FAILURE
    _say(out, f"Removed {len(result.removed)} file(s) from {result.target}")
    if result.kept_modified:
        _say(out, "Kept, because they were modified after install:")
        for path in result.kept_modified:
            _say(out, f"  {path}")
    if result.preserved:
        _say(out, "Left in place on purpose:")
        for path in result.preserved:
            _say(out, f"  {path}")
    _say(out, "Saves, backups and logs were not touched.")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
