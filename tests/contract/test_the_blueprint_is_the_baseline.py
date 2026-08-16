"""The blueprint is the yardstick, so it must not move.

``docs/blueprint/`` is marked **read-only** in the repository map: it is the
requirement baseline every "we implemented the spec" claim is measured against.
A yardstick that can be adjusted to match the thing being measured is not a
yardstick, and the adjustment is the kind nobody notices — a clarified sentence,
a corrected number, a scope line softened to match what was built. Every such
edit makes a claim of conformance unfalsifiable in retrospect.

Measured: 22 files, introduced by exactly one commit, with zero modifications,
deletions or renames since. The rule has never been broken and nothing enforced
it, so this is a guard over a discipline rather than a fix for a defect.

Two halves, because they catch different moments. History answers "has it ever
been edited" and is what CI judges. The working tree answers "is it being edited
right now", which is when the edit can still be undone with nothing lost — and
``git log`` cannot see an uncommitted change at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BLUEPRINT: Final = REPO_ROOT / "docs" / "blueprint"
BLUEPRINT_PATH: Final = "docs/blueprint"

#: A file with an ordinary history, used to prove the history query works.
#: Without it a broken query would report "one commit" for everything and the
#: guard would pass over nothing.
A_FILE_THAT_CHANGES: Final = "AGENTS.md"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _shallow() -> bool:
    """Whether history was truncated at clone time.

    Same test ``scripts/audit_pass.py`` uses. A shallow clone cannot answer a
    historical question, and answering it anyway would be a green that means
    nothing — the failure mode this repository keeps removing.
    """
    return (REPO_ROOT / ".git" / "shallow").exists()


def _commits_touching(path: str, *, filters: str = "") -> list[str]:
    args = ["log", "--format=%H %s"]
    if filters:
        args.append(f"--diff-filter={filters}")
    args += ["--", path]
    return [line for line in _git(*args).splitlines() if line.strip()]


def test_the_blueprint_has_only_the_commit_that_created_it() -> None:
    if _shallow():
        pytest.skip("shallow clone: no historical question can be answered")

    commits = _commits_touching(BLUEPRINT_PATH)
    created = _commits_touching(BLUEPRINT_PATH, filters="A")

    assert len(created) == 1, (
        "the blueprint should have been introduced by exactly one commit; found "
        f"{len(created)}:\n" + "\n".join(f"  {row}" for row in created)
    )
    assert commits == created, (
        "docs/blueprint/ is the requirement baseline and is marked read-only, but "
        "commits beyond the one that created it have touched it:\n"
        + "\n".join(f"  {row}" for row in commits if row not in created)
        + "\nA baseline that moves cannot show whether the implementation met it. "
        "Record the disagreement in docs/PROGRESS.md under the deviations table "
        "instead — that is what the table is for."
    )


def test_no_blueprint_file_is_being_edited_right_now() -> None:
    """The half ``git log`` cannot see, caught while it is still cheap to undo."""
    dirty = [line for line in _git("status", "--porcelain", "--", BLUEPRINT_PATH).splitlines()]

    assert not dirty, (
        "docs/blueprint/ has uncommitted changes, and it is the requirement "
        "baseline:\n" + "\n".join(f"  {row}" for row in dirty)
    )


def test_the_blueprint_is_the_document_set_it_claims_to_be() -> None:
    """A guard over an empty directory would pass forever.

    Not a count for its own sake: if the baseline were emptied, both checks
    above would go on passing — no commits beyond the first, no dirty files —
    while the thing they protect had ceased to exist.
    """
    documents = sorted(p.name for p in BLUEPRINT.iterdir() if p.is_file())

    assert len(documents) == 22, f"the blueprint holds {len(documents)} files, not 22"
    assert documents[0] == "01_PRODUCT_AND_SCOPE.md"
    # The task graph is not prose and is the file the rest of the control plane
    # is derived from, so its presence is asserted by name rather than counted.
    assert "task_graph.yaml" in documents


def test_the_history_query_can_see_an_ordinary_file_changing() -> None:
    """The control: the query must report more than one commit for a live file.

    A query that silently matched nothing — a wrong path, a swallowed error —
    would report a clean history for the blueprint and for everything else, and
    the guard above would be an assertion about the empty set.
    """
    if _shallow():
        pytest.skip("shallow clone: no historical question can be answered")

    commits = _commits_touching(A_FILE_THAT_CHANGES)

    assert len(commits) > 1, (
        f"{A_FILE_THAT_CHANGES} has {len(commits)} commit(s); the query is not reading "
        "history the way this file assumes"
    )
