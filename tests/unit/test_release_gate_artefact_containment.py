"""The release gate accepted one statement from the document it exists to doubt.

``scripts/check_release.py`` opens with the rule the whole file is built on:
*"A claim is checked against the artefact, never accepted from it."* The
evidence manifest records a digest per artefact and the gate re-hashes the file
— which is that rule, correctly applied to the *contents*. The **path** was
accepted outright: ``evidence_root / path``, no containment check.

Measured, both of these came back verified with no problem reported:

* ``"../outside.txt"`` — the join walks up and out of the evidence tree.
* ``"/anywhere/outside.txt"`` — pathlib *replaces* the left operand when the
  right one is absolute, so the evidence root disappears from the expression
  entirely. This one needs no traversal at all.

And ``..\\outside.txt`` is the Windows half, invisible on Linux for the same
reason the installer's traversal hole was: a backslash is an ordinary filename
character here.

What that buys an incorrect manifest is a **green** ``evidence.artefacts``
reading *"N required artefact(s), each with a SHA-256; N re-hashed from
<root>"*, over files that are not the evidence — in the bar that certifies
v1.0.0. Not a remote attack; the manifest is written by ``live-test finalize``
and named on the command line. But a gate whose stated purpose is to disbelieve
a document must not take the document's word for where to look, and this one
did.

An escaping path is now **reported**, not skipped. Skipping would record it as
"not re-hashed", which reads as an absent evidence tree rather than as a
manifest pointing outside the one it describes — an understatement in the exact
place understatement is dangerous.

This is the third instance of one class in three days: ``test_unverified_surface``
parsing a Windows path, ``modinstall._check_relative`` splitting on one
separator, and now this. All three are a path from a recorded document reaching
the filesystem without containment. The fix here is deliberately the same idiom
as the installer's, so the two read alike.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))

check_release = importlib.import_module("scripts.check_release")

#: Recorded paths that leave the evidence tree. Each must be refused.
ESCAPES: Final = (
    "../outside.txt",
    "../../outside.txt",
    r"..\outside.txt",
    r"S04_MOVE\..\..\outside.txt",
    "S04_MOVE/../../outside.txt",
    "/etc/passwd",
    r"C:\Windows\win.ini",
)

#: Paths a real manifest carries. Refusing these would break every release.
CONTAINED: Final = (
    "S04_MOVE/logs/console.txt",
    "S04_MOVE/attempts/attempt-0001.json",
    "S11_CONTAINER/screenshots/shot.png",
)


@pytest.mark.parametrize("recorded", ESCAPES, ids=lambda s: s)
def test_a_path_that_leaves_the_tree_is_refused(recorded: str) -> None:
    """The containment rule, on the helper that decides it."""
    assert check_release._inside_the_tree(Path("/tmp/evidence"), recorded) is None


@pytest.mark.parametrize("recorded", CONTAINED, ids=lambda s: s)
def test_a_real_artefact_path_still_resolves(recorded: str) -> None:
    """The control. A helper that refused everything would satisfy the test above."""
    root = Path("/tmp/evidence")
    resolved = check_release._inside_the_tree(root, recorded)

    assert resolved is not None
    assert resolved.is_relative_to(root)


def test_a_windows_spelled_artefact_path_resolves_to_the_same_place() -> None:
    """A manifest written with backslashes lands where the POSIX spelling does."""
    root = Path("/tmp/evidence")

    assert check_release._inside_the_tree(root, r"S04_MOVE\logs\console.txt") == (
        check_release._inside_the_tree(root, "S04_MOVE/logs/console.txt")
    )


def test_the_gate_no_longer_hashes_a_file_outside_the_tree(tmp_path: Path) -> None:
    """The load-bearing one, through the function the gate really calls.

    Not the helper: ``_verify_on_disk`` is what ``_artefact_digests`` uses, and
    the defect was that it returned 1 — *verified* — for a file that was never
    in the evidence tree.
    """
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not evidence at all")
    digest, _ = check_release._sha256_file(outside)

    for recorded in ("../outside.txt", str(outside)):
        problems: list[str] = []
        verified = check_release._verify_on_disk(evidence, recorded, digest, "label", problems)

        assert verified == 0, f"{recorded!r} was counted as a verified artefact"
        assert problems, f"{recorded!r} was refused silently"
        assert "not inside" in problems[0]


def test_an_artefact_really_inside_the_tree_is_still_verified(tmp_path: Path) -> None:
    """The control for the test above: the refusal is about the escape."""
    evidence = tmp_path / "evidence"
    (evidence / "S04_MOVE" / "logs").mkdir(parents=True)
    artefact = evidence / "S04_MOVE" / "logs" / "console.txt"
    artefact.write_bytes(b"console output")
    digest, _ = check_release._sha256_file(artefact)

    problems: list[str] = []
    verified = check_release._verify_on_disk(
        evidence, "S04_MOVE/logs/console.txt", digest, "label", problems
    )

    assert verified == 1
    assert problems == []


def test_a_wrong_digest_inside_the_tree_still_fails_for_the_right_reason(
    tmp_path: Path,
) -> None:
    """Containment must not shadow the check it stands in front of.

    A path that is inside and whose bytes disagree with the manifest has to
    report the digest mismatch, not the containment.
    """
    evidence = tmp_path / "evidence"
    (evidence / "S04_MOVE" / "logs").mkdir(parents=True)
    (evidence / "S04_MOVE" / "logs" / "console.txt").write_bytes(b"console output")

    problems: list[str] = []
    check_release._verify_on_disk(evidence, "S04_MOVE/logs/console.txt", "0" * 64, "l", problems)

    assert problems
    assert "modified after it was written" in problems[0]


def test_the_escape_is_reported_rather_than_skipped(tmp_path: Path) -> None:
    """Silence here would read as an absent tree, which is a different fact.

    ``_verify_on_disk`` returns 0 both when the tree is not on this machine and
    when a path escapes. Only the second appends a problem, and that difference
    is what keeps the summary line honest — it says "not re-hashed, because the
    tree is not here" in one case and refuses in the other.
    """
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    absent_tree: list[str] = []
    check_release._verify_on_disk(tmp_path / "nowhere", "../x", "0" * 64, "l", absent_tree)

    escaping: list[str] = []
    check_release._verify_on_disk(evidence, "../x", "0" * 64, "l", escaping)

    assert absent_tree == [], "an absent tree is not a manifest defect"
    assert escaping, "an escaping path must be a reported problem"


def test_the_defect_reproduces_through_the_naive_join(tmp_path: Path) -> None:
    """Pins the mechanism so a 'simplification' back to ``root / path`` fails.

    Asserts what the old expression did, rather than only that the new one does
    not: the absolute case in particular is not a traversal at all, and a future
    reader who fixes only ``..`` would leave it open.

    **The assertion is containment, not an exact path, and that is the point of
    this note.** The first version asserted ``root / "/etc/passwd" ==
    Path("/etc/passwd")``, which is true on POSIX and false on Windows: pathlib
    keeps the *drive* and replaces only the root, so the join yields
    ``C:/etc/passwd``. The defect reproduces on both — the join leaves the
    evidence tree either way — but the shape of the escape differs, and pinning
    the shape took the Windows release build red. Written, with some
    embarrassment, in a test file about paths from documents reaching the
    filesystem: the fourth instance of this class, and the second I authored.
    """
    root = tmp_path / "evidence"

    # The two halves are visible differently, which is itself worth pinning.
    # A traversal is *lexically* still under the root — ``is_relative_to`` says
    # True for ``…/evidence/../outside.txt`` — and leaves it only once ``..`` is
    # folded, so it is asserted through ``normpath``. An absolute operand is
    # visible immediately, because pathlib discards the root outright.
    traversed = Path(os.path.normpath(root / "../outside.txt"))
    assert not traversed.is_relative_to(root), (
        "the traversal half of this defect no longer reproduces"
    )
    assert not (root / "/etc/passwd").is_relative_to(root), (
        "pathlib no longer discards the root on an absolute operand; the second "
        "half of this defect no longer reproduces"
    )
