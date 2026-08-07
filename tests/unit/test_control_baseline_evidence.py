"""The baseline evidence describes a state that existed, not one that was typed.

`docs/control/evidence/step-01-10/` records where the release-candidate work
started: which branches there were, what the Linux suite reported, what the
Windows suite reported, and which workflows had a route to the branch. Every
later claim about progress is measured against those numbers, so if they are
wrong nothing above them means anything.

They are also the kind of file that rots silently. A SHA typed by hand, a count
copied from the wrong run, a branch that never existed — none of that fails
anything, because a text file has no behaviour. These tests give it one.

The load-bearing assertion is `test_every_recorded_sha_resolves`: a SHA in this
file is a claim that a commit existed, and `git cat-file` is the only thing that
can check it. That is what makes this a regression test rather than a spelling
check, and it is why the E01 tasks can be `PASS` at all — the plan refuses a
`PASS` whose evidence nothing verifies.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BASELINE: Final = REPO_ROOT / "docs" / "control" / "evidence" / "step-01-10"

BRANCHES: Final = BASELINE / "branches.txt"
LINUX: Final = BASELINE / "linux-baseline.txt"
WINDOWS: Final = BASELINE / "windows-failures.txt"
WORKFLOWS: Final = BASELINE / "windows-workflow-runs.txt"

#: A full object name. Deliberately not a short prefix: an abbreviated SHA is
#: ambiguous in principle and unresolvable once the repository grows.
_SHA = re.compile(r"\b[0-9a-f]{40}\b")

#: The branch the work started from, named in the plan and here so the two
#: cannot drift apart without something failing.
_BRANCH_POINT: Final = "873037c081800cf4f4373b9307fc1cdff3140e99"


def _resolves(sha: str) -> bool:
    return (
        subprocess.run(  # noqa: S603
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            cwd=REPO_ROOT,
            check=False,
        ).returncode
        == 0
    )


@pytest.mark.parametrize("path", [BRANCHES, LINUX, WINDOWS, WORKFLOWS], ids=lambda p: p.name)
def test_the_baseline_file_exists_and_says_something(path: Path) -> None:
    """An empty evidence file satisfies "the path exists" and proves nothing."""
    assert path.is_file(), f"{path.name} is missing"
    assert path.read_text(encoding="utf-8").strip(), f"{path.name} is empty"


def test_every_recorded_sha_resolves() -> None:
    """The assertion that makes this evidence rather than prose.

    A SHA here is a claim that a commit existed. Nothing else in the repository
    checks it, and a mistyped one would silently invalidate every measurement
    taken against the baseline.
    """
    text = "\n".join(path.read_text(encoding="utf-8") for path in (BRANCHES, LINUX))
    found = set(_SHA.findall(text))

    assert found, "no full SHA was recorded, so nothing was pinned"
    unresolvable = sorted(sha for sha in found if not _resolves(sha))
    assert unresolvable == [], f"recorded SHAs that no longer resolve: {unresolvable}"


def test_the_branch_point_is_recorded_and_reachable() -> None:
    """Every progress figure is measured from here."""
    recorded = BRANCHES.read_text(encoding="utf-8")

    assert _BRANCH_POINT in recorded, "the branch point is not in the branch list"
    assert _resolves(_BRANCH_POINT)


def test_each_branch_line_names_a_branch_and_a_full_sha() -> None:
    for number, line in enumerate(BRANCHES.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        name, _, sha = line.partition(" ")
        assert name, f"line {number} has no branch name"
        assert _SHA.fullmatch(sha.strip()), f"line {number} has no full SHA: {line!r}"


def test_the_linux_baseline_records_a_count_and_the_command_that_produced_it() -> None:
    """A count with no command is a number nobody can reproduce."""
    text = LINUX.read_text(encoding="utf-8")

    assert re.search(r"\d+ passed", text), "no pytest summary"
    assert "command:" in text, "the command that produced the count is not recorded"
    assert _SHA.search(text), "the commit the baseline was taken at is not recorded"


def test_the_windows_baseline_records_a_run_id_and_a_failure_count() -> None:
    """A Linux result cannot stand in for this one, so the run has to be named."""
    text = WINDOWS.read_text(encoding="utf-8")

    assert re.search(r"run \d{6,}", text), "no workflow run id"
    assert re.search(r"\d+ failed", text), "no failure count"


def test_the_windows_baseline_lists_the_failures_it_counted() -> None:
    """The count and the list must agree, or one of them is wrong.

    This is the assertion that caught nothing at the time and would have caught
    a miscount: 24 failures were claimed, and 24 node ids are listed.
    """
    text = WINDOWS.read_text(encoding="utf-8")
    claimed = re.search(r"(\d+) failed", text)
    assert claimed is not None

    listed = [line for line in text.splitlines() if "::" in line and line.startswith("tests/")]

    assert len(listed) == int(claimed.group(1)), (
        f"the file claims {claimed.group(1)} failures and lists {len(listed)}"
    )


def test_the_workflow_evidence_names_both_workflows() -> None:
    """Both had to have a route to the branch, and both were checked."""
    text = WORKFLOWS.read_text(encoding="utf-8").lower()

    assert "windows" in text
    assert "ci" in text
