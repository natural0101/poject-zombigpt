"""The traversal guard refused one separator convention and shipped to the other.

``modinstall._check_relative`` says what it is for: *"refusing anything that
could escape the mod directory"*. It split the path on ``/`` only, so on the
platform this product actually ships to the guard was one convention short:

* ``"../../evil.txt"`` — refused, because ``..`` becomes a part.
* ``"..\\..\\evil.txt"`` — **admitted**, because nothing splits it. The whole
  string stays one segment, that segment is not ``".."``, the depth is 1, and
  every check passes. ``destination.joinpath`` on Windows then reads the
  backslashes as separators and lands outside the mod directory.

Reachable, and worth stating precisely rather than either dramatising or waving
away. It is not a remote attack: the paths come from the install ledger
``pz-agent`` itself writes. But that ledger lives in the user's Zomboid
directory and is read back on the next ``install-mod``, every path in it goes
through this guard, and the ones the audit calls stale are then ``unlink``ed. A
corrupt or hand-edited ledger could therefore delete a file this project never
installed — the exact outcome the surrounding audit exists to prevent, since it
raises ``ForeignFileError`` on the first file pz-agent did not write.

Everything here is measured with ``PureWindowsPath``, which has no filesystem
behind it and produces backslash semantics on a machine that has none. That is
the repository's established way of testing this class — ``test_portable_paths``
uses it for the same reason — and it is what D-004 asks for: the Windows shape
constructed explicitly, so the defect fails on any platform rather than waiting
for the next Windows run to go red.

``platform/backup.py`` already normalised both separators. The fix is that
idiom applied where it was missing, not a new one.
"""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Final

import pytest

from pz_agent_cli.modinstall import MAX_DEPTH, InstallError, _check_relative

#: A destination shaped like the real one on the machine that has the game.
DESTINATION: Final = PureWindowsPath(r"C:\Users\Иван\Zomboid\mods\pz_agent_bridge")

#: Traversals, in both conventions and mixed. Each of these escapes, and each
#: must be refused. Kept separate from the dot segments below because only these
#: support the consequence test: an escape is a different claim from untidiness.
TRAVERSALS: Final = (
    "../../evil.txt",
    r"..\..\evil.txt",
    r"..\../evil.txt",
    "media/../../../evil.txt",
    r"media\..\..\..\evil.txt",
    "..",
    r"media\..",
)

#: Refused too, but for tidiness rather than because they escape.
DOT_SEGMENTS: Final = ("./x", r".\x", "a//b", r"a\\b")

#: Paths the installer must keep accepting. A guard that refused these would be
#: worse than the hole it closes: nothing would install at all.
LEGITIMATE: Final = (
    "mod.info",
    "media/lua/client/PZAgent/Runtime.lua",
    "media/lua/shared/Json.lua",
)


@pytest.mark.parametrize("relative", TRAVERSALS, ids=lambda s: s)
def test_a_traversal_is_refused_in_either_convention(relative: str) -> None:
    """The load-bearing one. The backslash half of these used to pass."""
    with pytest.raises(InstallError):
        _check_relative(relative)


@pytest.mark.parametrize("relative", DOT_SEGMENTS, ids=lambda s: s)
def test_a_dot_or_empty_segment_is_refused_in_either_convention(relative: str) -> None:
    """Not escapes; refused so a manifest path means exactly one filesystem path."""
    with pytest.raises(InstallError):
        _check_relative(relative)


@pytest.mark.parametrize("relative", LEGITIMATE, ids=lambda s: s)
def test_a_real_install_path_is_still_accepted(relative: str) -> None:
    """The control: without it, a guard that refused everything would pass above."""
    parts = _check_relative(relative)

    assert parts == tuple(relative.split("/"))
    assert DESTINATION.joinpath(*parts).is_relative_to(DESTINATION)


def test_a_windows_separator_path_lands_inside_the_destination() -> None:
    """A ledger written with backslashes is normalised, not rejected outright.

    ``pz-agent`` writes POSIX paths, so this shape should not occur; refusing it
    would also be defensible. Accepting it *normalised* is chosen because the
    same path then resolves to one place on both platforms, and because the
    thing that must never happen is escaping — not spelling.
    """
    parts = _check_relative(r"media\lua\client\PZAgent\Runtime.lua")

    assert parts == ("media", "lua", "client", "PZAgent", "Runtime.lua")
    assert DESTINATION.joinpath(*parts).is_relative_to(DESTINATION)


@pytest.mark.parametrize("relative", TRAVERSALS, ids=lambda s: s)
def test_no_refused_path_could_have_reached_outside_the_destination(relative: str) -> None:
    """States the consequence the guard exists for, in the Windows shape.

    Asserted through ``PureWindowsPath`` so the escape is visible here: on Linux
    a backslash is an ordinary filename character and the defect is invisible,
    which is precisely why it survived.
    """
    would_be = DESTINATION.joinpath(*PureWindowsPath(relative).parts)

    assert ".." in would_be.parts, (
        f"{relative!r} does not describe an escape, so it does not belong in this list"
    )


def test_the_defect_is_specifically_the_unsplit_backslash() -> None:
    """Pins the mechanism, so a future 'simplification' back to one separator fails.

    Not a restatement of the tests above: this asserts *why* the old code let it
    through — the traversal never became a part — rather than only that it now
    does not.
    """
    # Bound rather than split in place: the point is to *run* the old idiom, and
    # a linter rewrite to a list literal would turn the measurement into a
    # restatement of the answer.
    escaping = r"..\..\evil.txt"
    naive = tuple(escaping.split("/"))

    assert naive == (r"..\..\evil.txt",), "the old idiom no longer reproduces"
    assert not any(part in ("", ".", "..") for part in naive), (
        "the old idiom's own checks would have caught this; the defect was elsewhere"
    )
    assert len(naive) <= MAX_DEPTH

    # And the same string, through the guard as it stands.
    with pytest.raises(InstallError):
        _check_relative(escaping)


def test_a_ledger_naming_an_escape_is_refused_when_it_is_read(tmp_path: Path) -> None:
    """The path the defect actually travels: the ledger, not the shipped manifest.

    ``install-mod`` reads the ledger it wrote on a previous run, and
    ``InstalledFile.from_dict`` puts every recorded path through the guard. So
    with the guard whole, an escaping entry is refused where the file is *read*,
    before the audit can turn it into something to overwrite or delete.
    """
    from pz_agent_cli.modinstall import MANIFEST_NAME, read_manifest  # noqa: PLC0415

    destination = tmp_path / "pz_agent_bridge"
    destination.mkdir()
    ledger = {
        "mod_id": "pz_agent_bridge",
        "mod_version": "0.1.0",
        "product_version": "0.1.0",
        "installed_at": "2026-08-15T00:00:00Z",
        "files": [{"path": r"..\..\evil.txt", "size": 1, "sha256": "0" * 64}],
        "directories": [],
    }
    (destination / MANIFEST_NAME).write_text(json.dumps(ledger), encoding="utf-8")

    with pytest.raises(InstallError):
        read_manifest(destination)


def test_the_same_ledger_with_a_real_path_reads_back(tmp_path: Path) -> None:
    """The control for the test above: the refusal is about the escape, not the shape."""
    from pz_agent_cli.modinstall import MANIFEST_NAME, read_manifest  # noqa: PLC0415

    destination = tmp_path / "pz_agent_bridge"
    destination.mkdir()
    ledger = {
        "mod_id": "pz_agent_bridge",
        "mod_version": "0.1.0",
        "product_version": "0.1.0",
        "installed_at": "2026-08-15T00:00:00Z",
        "files": [{"path": "mod.info", "size": 1, "sha256": "0" * 64}],
        "directories": [],
    }
    (destination / MANIFEST_NAME).write_text(json.dumps(ledger), encoding="utf-8")

    manifest = read_manifest(destination)

    assert manifest is not None
    assert list(manifest.by_path()) == ["mod.info"]
