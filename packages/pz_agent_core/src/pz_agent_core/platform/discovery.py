"""Locate the Project Zomboid install, the user's ``Zomboid`` directory and the
installed build.

Every input from the outside world — the filesystem root standing in for the
system drive, the environment block, an explicit ``-cachedir`` — arrives inside
a :class:`DiscoveryContext`. Nothing in this module reads ``os.environ`` or
``Path.home()`` at call time. That is not stylistic: the cases that actually
break path detection are a Cyrillic username, a relocated user profile, a
second Steam library on another drive and a portable install, and none of them
are reproducible by running the real thing on one developer's machine. They are
all reproducible against a temporary tree.

Discovery never copies or bulk-reads game content. It stats directories and
reads at most :data:`MAX_METADATA_BYTES` from a small number of named metadata
files (docs/blueprint/12_COMPATIBILITY_AND_RESEARCH.md §12.7).

The build is reported as *unknown* when no metadata file yields one. It is
never inferred from :data:`~pz_agent_core.version.TARGET_BUILD`: a guessed build
would let mutating actions run against an install whose API surface was never
probed, which §12.6 forbids.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .vdf import VdfError, VdfObject, as_object, as_text, get_ci, parse_vdf

#: Metadata reads are capped so that pointing discovery at a huge file — a save,
#: a log that grew for months — can never pull it into memory.
MAX_METADATA_BYTES: Final = 64 * 1024

#: ``libraryfolders.vdf`` is a few kilobytes even with a dozen libraries.
MAX_VDF_BYTES: Final = 1 * 1024 * 1024

#: A machine with more Steam libraries than this is far more likely to be a
#: corrupt file loop than a real setup.
MAX_LIBRARIES: Final = 32

#: Steam's ``common`` directory name for the game. The spaced variant appears on
#: installs restored from older backups and manual copies.
GAME_DIR_NAMES: Final = ("ProjectZomboid", "Project Zomboid")

USER_DIR_NAME: Final = "Zomboid"

#: Relative to the injected filesystem root, tried when the environment block
#: does not name a Program Files location.
DEFAULT_STEAM_SUBPATHS: Final = (
    ("Program Files (x86)", "Steam"),
    ("Program Files", "Steam"),
    ("Steam",),
)

_PROGRAM_FILES_VARS: Final = ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432")

_LIBRARY_VDF_SUBPATHS: Final = (
    ("steamapps", "libraryfolders.vdf"),
    ("config", "libraryfolders.vdf"),
)

_ROOT_KEY_NAMES: Final = ("libraryfolders", "library_folders")

#: ``versionNumber=42.20.0 demo=false`` is written near the top of the game's
#: ``console.txt`` on every launch.
_CONSOLE_VERSION_RE: Final = re.compile(r"versionNumber\s*=\s*([0-9]+(?:\.[0-9]+)+)")

_VERSION_RE: Final = re.compile(r"(?<![0-9.])([0-9]{1,3})\.([0-9]{1,3})(?:\.[0-9]{1,6})*")

#: Plain-text version files that a Project Zomboid install may carry. Which of
#: them exists differs between distribution channels, so all are tried and the
#: absence of all of them is reported honestly rather than filled in.
_INSTALL_VERSION_FILES: Final = (
    ("version.txt",),
    ("version",),
    ("media", "version.txt"),
)


class DiscoveryError(Exception):
    """A discovery result was required but the thing was never found."""


@dataclass(frozen=True, slots=True)
class DiscoveryContext:
    """Everything path discovery is permitted to read from the outside world.

    ``root`` stands in for the drive that holds the system: on Windows it is
    typically ``C:\\``, in tests it is a temporary directory. It is consulted
    only for defaults; anything named by ``env`` or by an explicit override is
    used verbatim, so a library on another drive still resolves.
    """

    root: Path
    env: Mapping[str, str] = field(default_factory=dict)
    steam_root_hints: tuple[Path, ...] = ()
    install_override: Path | None = None
    home_override: Path | None = None
    zomboid_dir_override: Path | None = None

    @classmethod
    def from_process(
        cls,
        *,
        steam_root_hints: Sequence[Path] = (),
        install_override: Path | None = None,
        home_override: Path | None = None,
        zomboid_dir_override: Path | None = None,
    ) -> DiscoveryContext:
        """Build a context from this process's environment.

        This is the single place the real environment is read, so that every
        function below stays a pure function of its arguments.
        """
        env = dict(os.environ)
        system_drive = _env_value(env, "SystemDrive")
        root = Path(system_drive + os.sep) if system_drive else Path(os.sep)
        return cls(
            root=root,
            env=env,
            steam_root_hints=tuple(steam_root_hints),
            install_override=install_override,
            home_override=home_override,
            zomboid_dir_override=zomboid_dir_override,
        )


@dataclass(frozen=True, slots=True)
class SteamLibrary:
    """One Steam library folder — the directory that contains ``steamapps``."""

    path: Path
    source: Path
    label: str = ""

    @property
    def steamapps(self) -> Path:
        return self.path / "steamapps"


@dataclass(frozen=True, slots=True)
class SteamLibraries:
    """Result of enumerating Steam libraries.

    ``searched`` and ``problems`` exist so ``pz-agent doctor`` can tell the user
    exactly where it looked and what it could not read, rather than reporting a
    bare "not found".
    """

    libraries: tuple[SteamLibrary, ...] = ()
    searched: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return bool(self.libraries)

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(library.path for library in self.libraries)


@dataclass(frozen=True, slots=True)
class InstallLocation:
    """Where the game is installed, or an account of where it is not."""

    found: bool
    path: Path | None = None
    searched: tuple[str, ...] = ()
    libraries: tuple[SteamLibrary, ...] = ()
    problems: tuple[str, ...] = ()

    def require(self) -> Path:
        """Return the install path.

        Raises:
            DiscoveryError: when the install was not found.
        """
        if not self.found or self.path is None:
            raise DiscoveryError(
                f"Project Zomboid installation not found; searched {len(self.searched)} location(s)"
            )
        return self.path


@dataclass(frozen=True, slots=True)
class UserDirectory:
    """The ``Zomboid`` directory holding saves, mods and ``Lua``."""

    found: bool
    path: Path | None = None
    source: str = "none"
    searched: tuple[str, ...] = ()

    def require(self) -> Path:
        """Return the user directory path.

        Raises:
            DiscoveryError: when the directory was not found.
        """
        if not self.found or self.path is None:
            raise DiscoveryError(
                f"Zomboid user directory not found; searched {len(self.searched)} location(s)"
            )
        return self.path

    @property
    def saves_dir(self) -> Path | None:
        return None if self.path is None else self.path / "Saves"

    @property
    def mods_dir(self) -> Path | None:
        return None if self.path is None else self.path / "mods"

    @property
    def lua_dir(self) -> Path | None:
        return None if self.path is None else self.path / "Lua"


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """The build of the installed game, or an explicit statement of ignorance.

    ``known is False`` means exactly that — no caller may substitute a default.
    The mod reports the authoritative build on the observation stream once a
    session exists; this type covers the pre-session case only.
    """

    known: bool
    raw: str | None = None
    version: tuple[int, int] | None = None
    source: Path | None = None
    searched: tuple[str, ...] = ()
    note: str = ""

    @property
    def display(self) -> str:
        return self.raw if self.raw is not None else "unknown"

    def matches(self, candidates: Sequence[str]) -> bool:
        """True when the detected ``(major, minor)`` equals one of *candidates*.

        An unknown build matches nothing, so a caller that treats a False here
        as "unsupported" must check :attr:`known` first to tell the two apart.
        """
        if self.version is None:
            return False
        return any(_parse_version(candidate) == self.version for candidate in candidates)


@dataclass(frozen=True, slots=True)
class Discovery:
    """Everything ``pz-agent doctor`` needs about the local installation."""

    libraries: SteamLibraries
    install: InstallLocation
    user_dir: UserDirectory
    build: BuildInfo

    @property
    def complete(self) -> bool:
        """True when install, user directory and build were all determined."""
        return self.install.found and self.user_dir.found and self.build.known


# ---------------------------------------------------------------------------
# small filesystem helpers — every read is bounded
# ---------------------------------------------------------------------------


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive environment lookup; empty values count as absent.

    Windows environment names are case-insensitive and the exact casing of
    ``ProgramFiles(x86)`` differs between shells, so an exact-match lookup
    silently loses libraries.
    """
    direct = env.get(name)
    if direct:
        return direct
    wanted = name.lower()
    for key, value in env.items():
        if key.lower() == wanted and value:
            return value
    return None


def _read_whole(path: Path, max_bytes: int) -> tuple[str | None, str]:
    """Read a small file completely, or explain why not.

    Returns ``(text, problem)``; exactly one of them is meaningful. The file is
    refused rather than truncated when it is over the cap, because a truncated
    VDF parses into a half tree.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        return None, f"{path}: cannot read ({exc.strerror or exc})"
    if len(raw) > max_bytes:
        return None, f"{path}: larger than {max_bytes} bytes; refusing to parse"
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, f"{path}: not valid UTF-8"


def _read_head(path: Path, max_bytes: int) -> str | None:
    """Read the first *max_bytes* of a file, tolerating a split character.

    Used for append-only logs where only the header matters; the cut can land
    inside a multi-byte sequence, so decoding is lenient here on purpose.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes)
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


def _normalise_vdf_path(raw: str) -> str:
    """Turn a path as written by Windows Steam into one this process can use.

    ``libraryfolders.vdf`` is written with escaped backslashes. A backslash is
    not a legal character inside a Windows path component and Steam on Linux
    writes forward slashes, so rewriting is unambiguous — and it is what makes
    a Windows-shaped file testable on a POSIX CI box.
    """
    return raw.strip().replace("\\", "/")


def _parse_version(raw: str) -> tuple[int, int] | None:
    match = _VERSION_RE.search(raw)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _dedupe(paths: Iterator[Path]) -> tuple[Path, ...]:
    """Preserve order while dropping paths that differ only in case/separator."""
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in paths:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return tuple(ordered)


# ---------------------------------------------------------------------------
# Steam libraries
# ---------------------------------------------------------------------------


def steam_root_candidates(ctx: DiscoveryContext) -> tuple[Path, ...]:
    """Every directory that might be a Steam installation root, in priority order."""

    def _generate() -> Iterator[Path]:
        yield from ctx.steam_root_hints
        steam_path = _env_value(ctx.env, "SteamPath")
        if steam_path:
            yield Path(_normalise_vdf_path(steam_path))
        for var in _PROGRAM_FILES_VARS:
            value = _env_value(ctx.env, var)
            if value:
                yield Path(value) / "Steam"
        for parts in DEFAULT_STEAM_SUBPATHS:
            yield ctx.root.joinpath(*parts)

    return _dedupe(_generate())


def _library_entries(document: VdfObject) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, label)`` for every library entry in a parsed document.

    Handles both shapes Steam has shipped: the legacy ``"1" "D:\\Games"`` leaf
    and the modern ``"1" { "path" "D:\\Games" }`` object. Non-numeric keys are
    bookkeeping (``TimeNextStatsReport``, ``ContentStatsID``) and are skipped,
    which is what keeps the two shapes distinguishable without a version check.
    """
    root: VdfObject = document
    for name in _ROOT_KEY_NAMES:
        nested = as_object(get_ci(document, name))
        if nested is not None:
            root = nested
            break

    for key, value in root.items():
        if not key.isdigit():
            continue
        leaf = as_text(value)
        if leaf is not None:
            if leaf.strip():
                yield Path(_normalise_vdf_path(leaf)), ""
            continue
        entry = as_object(value)
        if entry is None:
            continue
        path_text = as_text(get_ci(entry, "path"))
        if not path_text or not path_text.strip():
            continue
        yield Path(_normalise_vdf_path(path_text)), as_text(get_ci(entry, "label")) or ""


def find_steam_libraries(ctx: DiscoveryContext) -> SteamLibraries:
    """Enumerate every Steam library folder reachable from *ctx*.

    A Steam root whose ``steamapps`` directory exists is itself a library even
    when no ``libraryfolders.vdf`` is present — that is the shape of a fresh
    install with a single library.
    """
    searched: list[str] = []
    problems: list[str] = []
    libraries: list[SteamLibrary] = []
    seen: set[str] = set()

    def _add(path: Path, source: Path, label: str) -> None:
        key = os.path.normcase(str(path))
        if key in seen:
            return
        if len(libraries) >= MAX_LIBRARIES:
            problem = f"stopped after {MAX_LIBRARIES} Steam libraries"
            if problem not in problems:
                problems.append(problem)
            return
        if not (path / "steamapps").is_dir():
            # Recorded as seen as well: several libraryfolders.vdf copies list
            # the same dead entry, and repeating the diagnostic per copy would
            # bury the readable libraries in doctor output.
            seen.add(key)
            problems.append(f"{path}: listed as a Steam library but has no steamapps/")
            return
        seen.add(key)
        libraries.append(SteamLibrary(path=path, source=source, label=label))

    for steam_root in steam_root_candidates(ctx):
        steamapps = steam_root / "steamapps"
        searched.append(str(steamapps))
        if steamapps.is_dir():
            _add(steam_root, steamapps, "")
        for parts in _LIBRARY_VDF_SUBPATHS:
            vdf_path = steam_root.joinpath(*parts)
            searched.append(str(vdf_path))
            if not vdf_path.is_file():
                continue
            text, problem = _read_whole(vdf_path, MAX_VDF_BYTES)
            if text is None:
                problems.append(problem)
                continue
            try:
                document = parse_vdf(text)
            except VdfError as exc:
                problems.append(f"{vdf_path}: malformed VDF ({exc})")
                continue
            for path, label in _library_entries(document):
                _add(path, vdf_path, label)

    return SteamLibraries(
        libraries=tuple(libraries),
        searched=tuple(searched),
        problems=tuple(problems),
    )


# ---------------------------------------------------------------------------
# game install
# ---------------------------------------------------------------------------


def _looks_like_install(path: Path) -> bool:
    """True when *path* has the one directory every Project Zomboid build ships.

    Requiring ``media/`` distinguishes a real install from an empty directory
    Steam left behind after an uninstall, which otherwise reports as found and
    then fails much later during the API scan.
    """
    return (path / "media").is_dir()


def find_install(
    ctx: DiscoveryContext,
    libraries: SteamLibraries | None = None,
) -> InstallLocation:
    """Find the game install across every Steam library.

    An explicit ``install_override`` wins, so a manual or non-Steam copy is
    usable without Steam being installed at all.
    """
    scan = find_steam_libraries(ctx) if libraries is None else libraries
    searched: list[str] = []
    problems: list[str] = list(scan.problems)

    if ctx.install_override is not None:
        override = ctx.install_override
        searched.append(str(override))
        if _looks_like_install(override):
            return InstallLocation(
                found=True,
                path=override,
                searched=tuple(searched),
                libraries=scan.libraries,
                problems=tuple(problems),
            )
        problems.append(f"{override}: install override does not contain media/")

    for library in scan.libraries:
        for name in GAME_DIR_NAMES:
            candidate = library.steamapps / "common" / name
            searched.append(str(candidate))
            if not candidate.is_dir():
                continue
            if _looks_like_install(candidate):
                return InstallLocation(
                    found=True,
                    path=candidate,
                    searched=tuple(searched),
                    libraries=scan.libraries,
                    problems=tuple(problems),
                )
            problems.append(f"{candidate}: directory exists but has no media/; not an install")

    if not scan.libraries:
        problems.append("no Steam library folder was found")

    return InstallLocation(
        found=False,
        path=None,
        searched=tuple(searched),
        libraries=scan.libraries,
        problems=tuple(problems),
    )


# ---------------------------------------------------------------------------
# user directory
# ---------------------------------------------------------------------------


def _user_dir_candidates(ctx: DiscoveryContext) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, source)`` candidates for the ``Zomboid`` directory."""
    if ctx.zomboid_dir_override is not None:
        # ``-cachedir`` names the data directory itself, not its parent.
        yield ctx.zomboid_dir_override, "cachedir_override"
    if ctx.home_override is not None:
        yield ctx.home_override / USER_DIR_NAME, "home_override"

    user_profile = _env_value(ctx.env, "USERPROFILE")
    if user_profile:
        profile = Path(_normalise_vdf_path(user_profile))
        yield profile / USER_DIR_NAME, "USERPROFILE"
        # Folder redirection can move the profile's contents under OneDrive
        # while %USERPROFILE% keeps pointing at the local profile root.
        yield profile / "OneDrive" / USER_DIR_NAME, "USERPROFILE_onedrive"

    one_drive = _env_value(ctx.env, "OneDrive")
    if one_drive:
        yield Path(_normalise_vdf_path(one_drive)) / USER_DIR_NAME, "OneDrive"

    home_drive = _env_value(ctx.env, "HOMEDRIVE")
    home_path = _env_value(ctx.env, "HOMEPATH")
    if home_drive and home_path:
        combined = _normalise_vdf_path(home_drive) + _normalise_vdf_path(home_path)
        yield Path(combined) / USER_DIR_NAME, "HOMEDRIVE+HOMEPATH"

    username = _env_value(ctx.env, "USERNAME") or _env_value(ctx.env, "USER")
    if username:
        yield ctx.root / "Users" / username / USER_DIR_NAME, "root_users"


def find_user_directory(ctx: DiscoveryContext) -> UserDirectory:
    """Find ``%USERPROFILE%/Zomboid``, honouring a custom home or cache directory."""
    searched: list[str] = []
    seen: set[str] = set()
    for candidate, source in _user_dir_candidates(ctx):
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        searched.append(str(candidate))
        if candidate.is_dir():
            return UserDirectory(
                found=True,
                path=candidate,
                source=source,
                searched=tuple(searched),
            )
    return UserDirectory(found=False, path=None, source="none", searched=tuple(searched))


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def detect_build(
    *,
    install_dir: Path | None,
    user_dir: Path | None,
    max_bytes: int = MAX_METADATA_BYTES,
) -> BuildInfo:
    """Read the installed build from local metadata.

    Install-side version files win over ``console.txt``: the log records what
    last *ran*, which can predate an update, while the install directory
    describes what is on disk right now.

    Returns a :class:`BuildInfo` with ``known=False`` when nothing yields a
    version. No default is substituted.
    """
    searched: list[str] = []

    if install_dir is not None:
        for parts in _INSTALL_VERSION_FILES:
            path = install_dir.joinpath(*parts)
            searched.append(str(path))
            if not path.is_file():
                continue
            text, _ = _read_whole(path, max_bytes)
            if text is None:
                continue
            version = _parse_version(text)
            if version is not None:
                match = _VERSION_RE.search(text)
                raw = text.strip() if match is None else match.group(0)
                return BuildInfo(
                    known=True,
                    raw=raw,
                    version=version,
                    source=path,
                    searched=tuple(searched),
                )

    if user_dir is not None:
        console = user_dir / "console.txt"
        searched.append(str(console))
        head = _read_head(console, max_bytes)
        if head is not None:
            match = _CONSOLE_VERSION_RE.search(head)
            if match is not None:
                raw = match.group(1)
                version = _parse_version(raw)
                if version is not None:
                    return BuildInfo(
                        known=True,
                        raw=raw,
                        version=version,
                        source=console,
                        searched=tuple(searched),
                    )

    return BuildInfo(
        known=False,
        raw=None,
        version=None,
        source=None,
        searched=tuple(searched),
        note=(
            "no local metadata reported a build; launch the game once so it writes "
            "console.txt, or read the build from the mod's observation stream"
        ),
    )


def discover(ctx: DiscoveryContext) -> Discovery:
    """Run the whole detection pass in one call, for ``pz-agent doctor``."""
    libraries = find_steam_libraries(ctx)
    install = find_install(ctx, libraries)
    user_dir = find_user_directory(ctx)
    build = detect_build(install_dir=install.path, user_dir=user_dir.path)
    return Discovery(libraries=libraries, install=install, user_dir=user_dir, build=build)
