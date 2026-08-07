"""`portable_relative_path`, checked against the Windows shape on any platform.

This function is the single place a system path becomes a recorded one, so it
carries the claim that a manifest written on Windows and read on Linux compares
equal. That claim cannot be tested through anything that touches the filesystem:
on Linux `str(relative_to(...))` and `as_posix()` return the same string, so
dropping `as_posix()` from a real install leaves every Linux test green and
breaks Windows only.

`PureWindowsPath` has no filesystem behind it, which is exactly why it works
here: it produces backslashes on a machine that has none, so the defect is
visible where the suite normally runs.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Final

import pytest

from pz_agent_core.platform.paths import (
    PathNotUnderRoot,
    portable_posix,
    portable_relative_path,
)

ZOMBOID: Final = PureWindowsPath(r"C:\Users\Иван\Zomboid")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (ZOMBOID / "pz-agent" / "config.toml", "pz-agent/config.toml"),
        (ZOMBOID / "mods" / "pz_agent_bridge" / "mod.info", "mods/pz_agent_bridge/mod.info"),
        (ZOMBOID / "pz-agent", "pz-agent"),
        (
            ZOMBOID / "mods" / "pz_agent_bridge" / "media" / "lua" / "shared" / "Json.lua",
            "mods/pz_agent_bridge/media/lua/shared/Json.lua",
        ),
    ],
)
def test_a_windows_path_is_recorded_with_posix_separators(
    path: PureWindowsPath, expected: str
) -> None:
    """The one assertion the whole portable/system distinction rests on."""
    recorded = portable_relative_path(path, ZOMBOID)

    assert recorded == expected
    assert "\\" not in recorded


def test_the_same_file_records_identically_from_either_platform() -> None:
    """A manifest written on Windows has to match one written on Linux."""
    windows = portable_relative_path(
        PureWindowsPath(r"C:\Users\Иван\Zomboid\pz-agent\config.toml"),
        PureWindowsPath(r"C:\Users\Иван\Zomboid"),
    )
    posix = portable_relative_path(
        PurePosixPath("/home/ivan/Zomboid/pz-agent/config.toml"),
        PurePosixPath("/home/ivan/Zomboid"),
    )

    assert windows == posix == "pz-agent/config.toml"


def test_a_recorded_path_never_carries_the_account_name() -> None:
    """Which file it is, is diagnostic. Where the user keeps their files is not."""
    recorded = portable_relative_path(ZOMBOID / "pz-agent" / "config.toml", ZOMBOID)

    assert "Иван" not in recorded
    assert "C:" not in recorded


@pytest.mark.parametrize(
    "outsider",
    [
        PureWindowsPath(r"C:\Users\Иван\Documents\notes.txt"),
        PureWindowsPath(r"D:\Games\ProjectZomboid\ProjectZomboid64.exe"),
        PureWindowsPath(r"C:\Users\Иван"),
    ],
)
def test_a_path_outside_the_root_is_refused_rather_than_recorded_absolute(
    outsider: PureWindowsPath,
) -> None:
    """Falling back to the absolute path would leak the profile into a manifest.

    It would also record an entry no other machine can match, so the uninstaller
    reading it would find nothing and remove nothing — the failure would be
    silent on both counts.
    """
    with pytest.raises(PathNotUnderRoot):
        portable_relative_path(outsider, ZOMBOID)


def test_portable_posix_keeps_the_drive_because_it_is_a_rendering() -> None:
    """The no-root case: a full path that still has to read the same everywhere."""
    assert portable_posix(ZOMBOID / "logs") == "C:/Users/Иван/Zomboid/logs"
