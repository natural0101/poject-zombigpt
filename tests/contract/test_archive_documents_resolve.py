"""Every link a shipped document makes must land inside the archive that ships it.

Defect 13 was exactly this: ``LOCAL_AGENT_PROMPT.md`` told the local agent to
read ``PROGRESS.md`` and ``LIMITATIONS.md`` sent a reader to ``RELEASE.md``, and
neither was in ``DOC_NAMES``. It was found by opening the ZIP for an unrelated
reason, and the fix was two names in a tuple — which left the general case
exactly where it was. This file is the general case.

An operator on Windows unzips the archive and reads it there. They have no
repository, so a relative link is either a file beside the one they are reading
or nothing at all. Seven of the archive's own README links were nothing at all:
``CONTRIBUTING.md``, ``AGENTS.md``, ``docs/ARCHITECTURE.md``,
``docs/PROTOCOL.md``, ``docs/TESTING.md``, ``docs/DEVELOPMENT.md`` and the
blueprint directory. ``PROGRESS.md`` pointed at the task graph, also absent.

Two ways to make a link resolve, and the choice is about who the document is
for. A document an operator needs is added to the archive — ``PROTOCOL.md``,
which ``LOCAL_DEBUG_MAP.md`` and ``LIVE_TEST_PLAYBOOK.md`` both assume when they
talk about journals and refs, and ``ARCHITECTURE.md``, which is what a reader
needs before either. A document about *building* the project is linked
absolutely, so both readers get the same thing: a GitHub reader follows it, and
an operator gets a URL rather than a dead path.

The archive is built here rather than read from ``dist/``: a checked-in artefact
would make this test pass against whatever was last built by hand.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BUILDER: Final = REPO_ROOT / "packaging" / "windows" / "build_rc.py"

#: ``[text](target)`` — the target only, stopping at a fragment or whitespace.
_LINK: Final = re.compile(r"\]\(([^)\s#]+)")

#: Schemes a reader's browser handles and this test has no opinion about.
_EXTERNAL: Final = ("http://", "https://", "mailto:")


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> zipfile.ZipFile:
    """The archive as ``build_rc.py`` produces it from this working tree."""
    out = tmp_path_factory.mktemp("rc")
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--output-dir", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    built = sorted(out.glob("*.zip"))
    # The builder exits non-zero for the two Windows executables it cannot make
    # on Linux, and writes the archive anyway. That is the documented behaviour
    # and is not what this file is about, so the archive is used and the exit
    # code is not asserted — but a build that produced nothing is a failure.
    assert built, f"build_rc.py wrote no archive (exit {result.returncode}):\n{result.stderr}"
    return zipfile.ZipFile(built[0])


def _documents(archive: zipfile.ZipFile) -> list[str]:
    return sorted(name for name in archive.namelist() if name.endswith(".md"))


def _unresolved(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Every ``(document, link)`` whose target is not in the archive."""
    names = set(archive.namelist())
    # A link may name a directory; the archive stores no directory entries, so a
    # prefix match is what "this directory is present" means here.
    directories = {posixpath.dirname(name) for name in names if posixpath.dirname(name)}
    broken: list[tuple[str, str]] = []
    for document in _documents(archive):
        body = archive.read(document).decode("utf-8", errors="replace")
        for target in _LINK.findall(body):
            if target.startswith(_EXTERNAL):
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(document), target))
            if resolved not in names and resolved not in directories:
                broken.append((document, target))
    return broken


def test_the_archive_carries_documents_worth_checking(archive: zipfile.ZipFile) -> None:
    """A build that shipped no documents would make the test below vacuous."""
    documents = _documents(archive)

    assert len(documents) >= 15, documents
    assert "README.md" in documents
    assert "docs/QUICKSTART.md" in documents


def test_every_relative_link_lands_inside_the_archive(archive: zipfile.ZipFile) -> None:
    """The whole point. An operator has no repository to fall back to."""
    broken = _unresolved(archive)

    assert broken == [], (
        "these shipped documents link to files the archive does not contain; add the "
        "file to build_rc.py's DOC_NAMES if an operator needs it, or make the link "
        "absolute if it is about building the project"
    )


def test_the_check_would_notice_a_link_that_does_not_resolve(
    archive: zipfile.ZipFile, tmp_path: Path
) -> None:
    """The mutation, run rather than reasoned about.

    Every assertion above is "a set is empty", which is what a scanner that
    silently stopped matching also produces. So a document with a deliberately
    dead link is planted into a copy of the archive, and the scan has to find it.
    """
    planted = tmp_path / "planted.zip"
    with zipfile.ZipFile(planted, "w") as out:
        for item in archive.infolist():
            out.writestr(item, archive.read(item.filename))
        out.writestr("docs/PLANTED.md", "See [the missing one](NOT_SHIPPED.md).\n")

    with zipfile.ZipFile(planted) as reopened:
        broken = _unresolved(reopened)

    assert ("docs/PLANTED.md", "NOT_SHIPPED.md") in broken
