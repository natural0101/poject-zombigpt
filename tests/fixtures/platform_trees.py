"""Builders for the fake Windows-shaped trees the platform tests run against.

Path detection and save backup are the two subsystems whose bugs only show up
on someone else's machine — a second Steam library, a Cyrillic username, a
legacy ``libraryfolders.vdf``. These helpers make each of those a three-line
setup against ``tmp_path`` so the tests can be about the behaviour instead of
about the scaffolding.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

CYRILLIC_USER = "Пользователь"


def make_steam_root(base: Path, name: str = "Steam") -> Path:
    """Create a Steam installation root with an empty ``steamapps``."""
    steam_root = base / name
    (steam_root / "steamapps" / "common").mkdir(parents=True, exist_ok=True)
    return steam_root


def make_library(base: Path, name: str) -> Path:
    """Create a bare Steam library folder (a directory with ``steamapps``)."""
    library = base / name
    (library / "steamapps" / "common").mkdir(parents=True, exist_ok=True)
    return library


def install_game(
    library: Path,
    *,
    dir_name: str = "ProjectZomboid",
    version: str | None = None,
    with_media: bool = True,
) -> Path:
    """Place a Project Zomboid install inside *library*.

    ``with_media=False`` produces the leftover-directory shape Steam creates
    after an uninstall, which discovery must not report as an install.
    """
    install = library / "steamapps" / "common" / dir_name
    install.mkdir(parents=True, exist_ok=True)
    if with_media:
        (install / "media").mkdir(exist_ok=True)
    if version is not None:
        (install / "version.txt").write_text(version, encoding="utf-8")
    return install


def _vdf_path(path: Path, *, windows_separators: bool) -> str:
    text = str(path)
    if windows_separators:
        text = text.replace("/", "\\")
    return text.replace("\\", "\\\\")


def write_modern_libraryfolders(
    steam_root: Path,
    libraries: Sequence[Path],
    *,
    windows_separators: bool = False,
    subdir: str = "steamapps",
) -> Path:
    """Write the ``"0" { "path" "..." }`` form Steam has used since 2021."""
    lines = ['"libraryfolders"', "{"]
    for index, library in enumerate(libraries):
        lines.extend(
            [
                f'\t"{index}"',
                "\t{",
                f'\t\t"path"\t\t"{_vdf_path(library, windows_separators=windows_separators)}"',
                '\t\t"label"\t\t""',
                '\t\t"contentid"\t\t"1234567890"',
                '\t\t"apps"',
                "\t\t{",
                '\t\t\t"108600"\t\t"9876543"',
                "\t\t}",
                "\t}",
            ]
        )
    lines.append("}")
    return _write_vdf(steam_root / subdir / "libraryfolders.vdf", lines)


def write_legacy_libraryfolders(
    steam_root: Path,
    libraries: Sequence[Path],
    *,
    windows_separators: bool = False,
    subdir: str = "steamapps",
) -> Path:
    """Write the pre-2021 form where a numeric key maps straight to a path."""
    lines = [
        '"LibraryFolders"',
        "{",
        '\t"TimeNextStatsReport"\t\t"1700000000"',
        '\t"ContentStatsID"\t\t"-1234567890123456789"',
    ]
    for index, library in enumerate(libraries, start=1):
        value = _vdf_path(library, windows_separators=windows_separators)
        lines.append(f'\t"{index}"\t\t"{value}"')
    lines.append("}")
    return _write_vdf(steam_root / subdir / "libraryfolders.vdf", lines)


def _write_vdf(path: Path, lines: Sequence[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # CRLF on purpose: the real file is written by Windows Steam.
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    return path


def make_user_dir(home: Path, *, name: str = "Zomboid") -> Path:
    """Create a ``Zomboid`` user directory with the subdirectories the game makes."""
    user_dir = home / name
    for child in ("Saves", "mods", "Lua", "Logs"):
        (user_dir / child).mkdir(parents=True, exist_ok=True)
    return user_dir


def write_console_log(user_dir: Path, version: str, *, preamble_bytes: int = 0) -> Path:
    """Write a ``console.txt`` whose header carries ``versionNumber=``."""
    console = user_dir / "console.txt"
    filler = "" if preamble_bytes <= 0 else ("x" * preamble_bytes + "\n")
    console.write_text(
        f"{filler}versionNumber={version} demo=false\nLOG: General, 0> boot\n",
        encoding="utf-8",
    )
    return console


def make_save(user_dir: Path, save_id: str, files: Mapping[str, bytes]) -> Path:
    """Create a save directory under ``Saves`` populated with *files*."""
    save_dir = user_dir.joinpath("Saves", *save_id.split("/"))
    save_dir.mkdir(parents=True, exist_ok=True)
    for relative, payload in files.items():
        destination = save_dir.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    return save_dir


def read_save(save_dir: Path) -> dict[str, bytes]:
    """Snapshot a save directory as ``{relative posix path: bytes}``."""
    return {
        path.relative_to(save_dir).as_posix(): path.read_bytes()
        for path in sorted(save_dir.rglob("*"))
        if path.is_file()
    }


class FakeClock:
    """A monotone clock the backup manager can be built with.

    Backup ids and retention order are derived from the timestamp, so the tests
    that care about ordering must not depend on how fast the machine runs.
    """

    def __init__(self, start: datetime | None = None, *, step_seconds: int = 60) -> None:
        self._now = start if start is not None else datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        current = self._now
        self._now += self._step
        return current

    def freeze(self) -> None:
        """Stop advancing, so several backups share one timestamp."""
        self._step = timedelta(0)
