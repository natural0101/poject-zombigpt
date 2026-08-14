"""The mod's identity, spelled in six places, checked in none.

``modinstall.py`` carries::

    #: Directory name under ``Zomboid/mods``. Matches ``id=`` in ``mod.info``.
    MOD_ID: Final = "pz_agent_bridge"

The comment states a relationship. Nothing tested it. That is the shape of two
defects this repository has already had: five Lua adapters declaring
``capability = nil`` under comments asserting no probe existed for them, and a
retraction caused by trusting what a file said about the code beside it.

Six files spell the identity independently — the installer's constant, both
``mod.info`` files, and the paths ``installer/INSTALL.md`` and
``docs/QUICKSTART.md`` print for the user. They agree today. Nothing held them
together, and the failure they would cause is one this project has already met:
the docstring on ``test_mod_info_declares_the_same_mod_version`` records that on
2026-08-08, against Build 42.20.2, **the mod simply did not appear in the mod
list** — twice, for two different ``mod.info`` rules. That test now pins
``modversion``, ``pzversion`` and the absence of ``require=`` in both files. It
does not pin ``id``.

A divergence would be quiet in the worst way. The installer would write
``Zomboid/mods/<MOD_ID>/mod.info`` whose ``id=`` said something else; the
verifier, the support bundle and every path in the documents would follow
``MOD_ID`` while the game read the file. Nothing here crashes — the user is
simply told the mod is installed, and does not find it.

Two files are deliberately *not* checked: ``tests/fixtures/cli_worlds.py`` and
the several unit tests that write ``id=pz_agent_bridge`` into a fixture. Those
are inputs to tests rather than declarations of the product, and holding them
here would make this file fail for a reason that is not about the shipped mod.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.modinstall import MOD_ID

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: Both halves of the Build 42 versioned-mod layout. The root declares the mod
#: to the launcher; ``42/`` carries the build-specific content and re-declares
#: it. Either one disagreeing with the directory is the same outage.
MOD_INFO_FILES: Final = (
    REPO_ROOT / "pz-mod" / "mod.info",
    REPO_ROOT / "pz-mod" / "42" / "mod.info",
)

#: Documents that print the installed path to the person running the installer.
#: They are the user's only way to check the install by hand, so a stale one
#: sends them to a directory that does not exist.
OPERATOR_DOCUMENTS: Final = (
    REPO_ROOT / "installer" / "INSTALL.md",
    REPO_ROOT / "docs" / "QUICKSTART.md",
)


def _declarations(path: Path) -> dict[str, str]:
    """``mod.info`` as the key/value file the game reads."""
    text = path.read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


@pytest.mark.parametrize("info", MOD_INFO_FILES, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_each_mod_info_declares_the_id_the_installer_creates(info: Path) -> None:
    """The assertion the comment made and nothing checked."""
    assert info.is_file(), f"{info} is missing"

    assert _declarations(info)["id"] == MOD_ID


def test_the_two_mod_info_files_agree_with_each_other() -> None:
    """Both halves of the layout name one mod.

    Checked directly rather than left to transitivity through ``MOD_ID``: if the
    constant were ever removed, the test above would go with it and this one
    would still hold the pair together.
    """
    root, versioned = (_declarations(path) for path in MOD_INFO_FILES)

    assert root["id"] == versioned["id"]
    assert root["name"] == versioned["name"]


@pytest.mark.parametrize(
    "document", OPERATOR_DOCUMENTS, ids=lambda path: str(path.relative_to(REPO_ROOT))
)
def test_the_documents_send_the_user_to_the_directory_the_installer_writes(
    document: Path,
) -> None:
    """Every ``mods/<something>`` path printed for the user names the real one.

    Matched on the path shape rather than on the id, so a document that renamed
    the directory fails here instead of passing by not mentioning it. Both
    separators are accepted: ``INSTALL.md`` prints Windows paths and
    ``QUICKSTART.md`` prints both.
    """
    assert document.is_file(), f"{document} is missing"
    text = document.read_text(encoding="utf-8")

    named = set(re.findall(r"mods[\\/]([A-Za-z0-9_\-]+)", text))

    assert named, f"{document} prints no mods/<directory> path at all"
    assert named == {MOD_ID}, f"{document} names {sorted(named)} under mods/, not {MOD_ID!r}"
