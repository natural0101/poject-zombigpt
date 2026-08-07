"""Windows path shapes, exercised on whatever platform runs the suite.

Four of the twenty-four Windows failures were here, and none of them could be
seen on Linux because every one needed a path with backslashes in it. Rather
than wait for CI on another operating system, these build the Windows shapes
explicitly with :class:`~pathlib.PureWindowsPath` and assert on the result — so
the same test that fails on Windows fails here.

Two separate defects lived in this area.

**The separator survived into the placeholder.** A redacted path came out as
``<ZOMBOID>\\logs`` on Windows and ``<ZOMBOID>/logs`` everywhere else. The
placeholder exists so two machines produce the same line for the same file, and
the separator after it carries nothing about the machine — so every document,
comparison and test had to know which platform wrote it. It is normalised now.

**The more specific directory has to win.** ``user_dir`` is a child of
``home_dir`` (``C:\\Users\\Иван\\Zomboid`` inside ``C:\\Users\\Иван``). The
rules are ordered longest-literal-first for exactly this reason, and a path
inside the Zomboid directory must report ``user_dir`` — the support bundle's
verifier prints these labels, and one that said only ``home_dir`` would describe
the redaction that happened incorrectly.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Final
from urllib.parse import quote

import pytest

from pz_agent_core.diagnostics.redaction import (
    PATH_PLACEHOLDER,
    USER_DIR_PLACEHOLDER,
    USER_HOME_PLACEHOLDER,
    Redactor,
    build_redactor,
)

#: A Cyrillic account name, because that is the case the project was built for
#: and the one where NFC/NFD and case folding both apply.
HOME: Final = PureWindowsPath(r"C:\Users\Иван")
ZOMBOID: Final = HOME / "Zomboid"

#: A profile under ``Program Files``-style spacing, which §14.8 requires to work.
SPACED_HOME: Final = PureWindowsPath(r"C:\Users\John Smith")


@pytest.fixture
def windows_redactor() -> Redactor:
    return build_redactor(user_dir=ZOMBOID, home_dir=HOME, usernames=["Иван"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"C:\Users\Иван\Zomboid", USER_DIR_PLACEHOLDER),
        (r"C:\Users\Иван\Zomboid\logs", f"{USER_DIR_PLACEHOLDER}/logs"),
        (
            r"C:\Users\Иван\Zomboid\Lua\pz_agent\command.queue.0001.jsonl",
            f"{USER_DIR_PLACEHOLDER}/Lua/pz_agent/command.queue.0001.jsonl",
        ),
        # The form a Windows path takes after json.dumps, which is how it
        # reaches a support bundle.
        (r"C:\\Users\\Иван\\Zomboid\\logs", f"{USER_DIR_PLACEHOLDER}/logs"),
        # A forward-slash spelling of the same directory: Python hands these
        # back from some APIs even on Windows.
        ("C:/Users/Иван/Zomboid/logs", f"{USER_DIR_PLACEHOLDER}/logs"),
        (r"C:\Users\Иван\Desktop\notes.txt", f"{USER_HOME_PLACEHOLDER}/Desktop/notes.txt"),
    ],
)
def test_a_redacted_windows_path_reads_the_same_as_a_posix_one(
    windows_redactor: Redactor, raw: str, expected: str
) -> None:
    assert windows_redactor.text(raw) == expected


def test_the_placeholder_never_carries_a_backslash(windows_redactor: Redactor) -> None:
    """Stated separately because it is the property, not one example of it."""
    rendered = windows_redactor.text(
        r"sidecar wrote C:\Users\Иван\Zomboid\logs\pz-agent.log just now"
    )

    assert "\\" not in rendered, rendered
    assert f"{USER_DIR_PLACEHOLDER}/logs/pz-agent.log" in rendered


def test_the_zomboid_directory_wins_over_the_profile_that_contains_it(
    windows_redactor: Redactor,
) -> None:
    """Longest literal first. The bundle verifier prints these labels."""
    findings = windows_redactor.findings(r"C:\Users\Иван\Zomboid\logs")

    assert "user_dir" in findings, (
        "a path inside the Zomboid directory reported only the profile that contains it"
    )


def test_a_profile_with_a_space_in_it_is_struck_out_whole() -> None:
    """`C:\\Users\\John Smith` — a surname left in the output is the leak."""
    redactor = build_redactor(home_dir=SPACED_HOME, usernames=["John Smith"])

    rendered = redactor.text(r"C:\Users\John Smith\Zomboid\logs")

    assert "Smith" not in rendered, rendered
    assert rendered == f"{USER_HOME_PLACEHOLDER}/Zomboid/logs"


def test_an_unknown_absolute_windows_path_still_loses_its_directories() -> None:
    """The last line of defence, for a path this process never learnt about."""
    redactor = build_redactor()

    rendered = redactor.text(r"D:\Games\SteamLibrary\steamapps\common\ProjectZomboid\x.txt")

    assert "SteamLibrary" not in rendered
    assert PATH_PLACEHOLDER in rendered


def test_a_path_that_is_not_under_a_known_directory_is_left_recognisable() -> None:
    """Redaction is not obliteration: which file it was stays diagnostic."""
    redactor = build_redactor()

    rendered = redactor.text(r"C:\Windows\Temp\pz-agent.log")

    assert "pz-agent.log" in rendered


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # `quote` leaves `/` alone but escapes `\`, so a Windows path that has
        # been through a URL, a crash-reporter field or a browser log arrives
        # with %5C where a POSIX one would still read `/`. The prefix matched
        # and the tail did not, so the placeholder came out welded to the rest
        # of the path: `<ZOMBOID>%5Clogs`.
        (f"{quote(str(ZOMBOID), safe=':/')}%5Clogs", f"{USER_DIR_PLACEHOLDER}/logs"),
        (f"{quote(str(ZOMBOID), safe=':/')}%5clogs", f"{USER_DIR_PLACEHOLDER}/logs"),
        (
            f"{quote(str(ZOMBOID), safe=':/')}%5CLua%5Cpz_agent",
            f"{USER_DIR_PLACEHOLDER}/Lua/pz_agent",
        ),
        (f"{quote(str(ZOMBOID), safe=':/')}%2Flogs", f"{USER_DIR_PLACEHOLDER}/logs"),
    ],
)
def test_a_percent_encoded_separator_is_normalised_like_any_other(
    windows_redactor: Redactor, raw: str, expected: str
) -> None:
    """The fourth spelling of a separator, and the only one POSIX never shows.

    On Linux `quote("/home/Иван/Zomboid/x")` keeps its slashes, so the tail
    matched by accident and this hole stayed invisible until the Windows suite
    ran. Building the path with `PureWindowsPath` makes the same defect visible
    here.
    """
    assert windows_redactor.text(raw) == expected


def test_a_percent_encoded_separator_leaves_no_backslash_behind(
    windows_redactor: Redactor,
) -> None:
    """`%5C` decoded later is still a separator, so it may not survive either."""
    rendered = windows_redactor.text(f"{quote(str(ZOMBOID), safe=':/')}%5Clogs%5Cconsole.txt")

    assert "%5C" not in rendered and "%5c" not in rendered, rendered
    assert "\\" not in rendered, rendered


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A native prefix joined to a POSIX suffix: `f"{path}/Zomboid"`, or a
        # log line assembled from `str(dir)` and a slash-separated name. Real,
        # and the shape that made the Windows suite report `home_dir` where
        # `user_dir` applies.
        (f"{HOME}/Zomboid", USER_DIR_PLACEHOLDER),
        (f"{HOME}/Zomboid/logs", f"{USER_DIR_PLACEHOLDER}/logs"),
        (f"{HOME}/Zomboid\\logs", f"{USER_DIR_PLACEHOLDER}/logs"),
        ("C:/Users\\Иван/Zomboid/logs", f"{USER_DIR_PLACEHOLDER}/logs"),
        ("C:\\Users/Иван\\Zomboid", USER_DIR_PLACEHOLDER),
    ],
)
def test_a_path_that_mixes_separators_still_finds_the_longest_directory(
    windows_redactor: Redactor, raw: str, expected: str
) -> None:
    """The more specific directory has to win whatever the separators look like.

    Spellings used to be enumerated whole — all-`/`, all-`\\`, all-doubled — so
    a path that mixed them matched none of them, and the shorter `home_dir`
    literal (which happened to be spelled consistently up to where it ended)
    matched instead. Not a leak: the path was still struck out. But under
    `<USER_HOME>` rather than `<ZOMBOID>`, so the same file produced a different
    line on Windows than on Linux, which is the one thing the placeholder is
    for.
    """
    assert windows_redactor.text(raw) == expected


def test_the_finding_names_the_directory_that_actually_applies(
    windows_redactor: Redactor,
) -> None:
    """`verify_bundle` prints these labels; a wrong one misdescribes the redaction."""
    assert "user_dir" in windows_redactor.findings(f"{HOME}/Zomboid/logs")
