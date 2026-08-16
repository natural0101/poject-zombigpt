"""The gate's verdict must name the tree it is about.

``scripts/check.sh`` used to end with an unqualified ``All checks passed.`` On
2026-08-16 that line was printed over a working tree with uncommitted work, the
commit made from it was pushed, and CI went red on that commit — the tree the
gate had judged and the tree CI judged were not the same tree, and nothing in
the output said so.

Two halves are covered separately because they can fail separately: the
classification, which decides what a run is a verdict about, and the git
reading, which decides what it is handed. The second is exercised against real
repositories rather than a mocked ``git``: a wrong argument to ``status`` or
``rev-parse`` is exactly the kind of mistake a stubbed subprocess hides.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPTS: Final = REPO_ROOT / "scripts"

if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))

from scripts.check_tree_identity import (  # noqa: E402
    CONTROL_PLANE,
    Subject,
    _porcelain_paths,
    judge,
)

HEAD = "c4b08ef31b1f74ac8f60073dbc967926776b4426"


# --------------------------------------------------------------------------
# what a run is a verdict about
# --------------------------------------------------------------------------


def test_a_clean_tree_is_a_verdict_about_its_commit() -> None:
    verdict = judge(HEAD, [])

    assert verdict.subject is Subject.COMMIT
    assert "c4b08ef3" in verdict.sentence
    assert "CI will judge" in verdict.sentence


def test_a_pending_status_commit_carries_the_verdict() -> None:
    """The prescribed state between the code commit and the STATUS commit.

    ``reconcile_status.py`` has just rewritten the control plane and nothing
    else, so the commit about to be made has exactly this tree. Calling that
    "a verdict about no commit" would accuse the one sequence AGENTS.md
    requires, and a checker that accuses correct work gets switched off.
    """
    verdict = judge(HEAD, [f"{CONTROL_PLANE}STATUS.json", f"{CONTROL_PLANE}EVIDENCE_INDEX.md"])

    assert verdict.subject is Subject.PENDING_STATUS
    assert "carries to it" in verdict.sentence


def test_uncommitted_work_is_a_verdict_about_no_commit() -> None:
    verdict = judge(HEAD, ["packages/pz_agent_core/src/pz_agent_core/protocol/messages.py"])

    assert verdict.subject is Subject.NO_COMMIT
    assert "judged no commit" in verdict.sentence
    assert "messages.py" in verdict.sentence


def test_one_path_outside_the_control_plane_is_enough() -> None:
    """The exemption is per path, not per run — the mixed case is the real one.

    Reconciling STATUS and forgetting to commit a source file produces exactly
    this shape, and treating it as the pending-STATUS state would hand back the
    reassuring sentence for the dangerous tree.
    """
    verdict = judge(HEAD, [f"{CONTROL_PLANE}STATUS.json", "AGENTS.md"])

    assert verdict.subject is Subject.NO_COMMIT
    assert "AGENTS.md" in verdict.sentence


def test_an_unreadable_head_is_said_plainly() -> None:
    verdict = judge("", [])

    assert verdict.subject is Subject.UNKNOWN
    assert "no commit could be read" in verdict.sentence


# --------------------------------------------------------------------------
# what the run is handed
# --------------------------------------------------------------------------


def test_a_rename_names_both_of_its_halves() -> None:
    """A file moved out of the control plane must not read as still inside it."""
    paths = _porcelain_paths(f'R  "{CONTROL_PLANE}STATUS.json" -> docs/STATUS.json\n')

    assert paths == [f"{CONTROL_PLANE}STATUS.json", "docs/STATUS.json"]
    assert judge(HEAD, paths).subject is Subject.NO_COMMIT


def test_an_untracked_file_counts() -> None:
    """`??` is a change the commit will carry, and a new test file arrives this way."""
    assert _porcelain_paths("?? tests/unit/test_new.py\n") == ["tests/unit/test_new.py"]


# --------------------------------------------------------------------------
# against a real repository
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=60)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A clean repository with the script installed where it expects to live.

    ``REPO_ROOT`` is derived from the script's own location, so the copy sits
    in a stand-in ``scripts/`` directory and is committed with everything else:
    a fixture that left its own installation uncommitted would dirty every tree
    it handed out, and both tests would then be reading the fixture's mess
    rather than the state they describe.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "gate@example.invalid")
    _git(repo, "config", "user.name", "gate")
    (repo / "source.py").write_text("x = 1\n", encoding="utf-8")
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "check_tree_identity.py").write_text(
        (SCRIPTS / "check_tree_identity.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "first")
    return repo


def _identity(repo: Path) -> str:
    """Run the script the gate runs, against *repo*.

    A real ``git`` over a real repository: importing the module and stubbing
    the subprocess would leave the arguments to ``rev-parse`` and ``status`` —
    the half most likely to be wrong — unexercised.
    """
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "check_tree_identity.py")],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return result.stdout.strip()


def _head_of(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def test_a_real_clean_repository_is_named_by_its_commit(repository: Path) -> None:
    said = _identity(repository)

    assert _head_of(repository)[:8] in said
    assert "CI will judge" in said


def test_a_real_dirty_repository_is_named_as_no_commit(repository: Path) -> None:
    (repository / "source.py").write_text("x = 2\n", encoding="utf-8")

    said = _identity(repository)

    assert "judged no commit" in said
    assert "source.py" in said


def test_the_gate_asks_who_it_judged_instead_of_claiming_success_bare() -> None:
    """The wiring, read rather than run — the one place that is unavoidable.

    ``scripts/check.sh`` runs the whole test suite, so a test cannot run it
    without running itself. This is therefore a check over the source, with the
    weakness that implies: it proves the gate names the script at both ends and
    no longer carries the bare success line, not that the line printed is the
    one this file's other tests describe. Those cover the script's own answer.
    """
    gate = (SCRIPTS / "check.sh").read_text(encoding="utf-8")

    assert 'echo "All checks passed."' not in gate
    # Invocations, not mentions: the header comment names the script too.
    assert gate.count('"$PY" scripts/check_tree_identity.py') == 2
    # The last thing printed on success, not merely mentioned somewhere above.
    assert gate.rstrip().endswith('--prefix "All checks passed"')


def test_a_real_pending_status_commit_keeps_the_verdict(repository: Path) -> None:
    """The prescribed in-between state, built out of real files.

    The classification test above asserts the same thing over a path list; this
    one proves the paths git actually prints for that state land in the same
    branch — including the leading directory component, which is where a
    prefix comparison goes wrong.
    """
    control = repository / "docs" / "control"
    control.mkdir(parents=True)
    (control / "STATUS.json").write_text("{}\n", encoding="utf-8")

    said = _identity(repository)

    assert "carries to it" in said
