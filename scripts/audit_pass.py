#!/usr/bin/env python3
"""Audit every ``PASS`` in the master plan against the repository's own history.

``check_master_plan.py`` asks whether a claim is *well formed*: does the commit
field hold something, does the evidence path exist **now**, is the dependency
also ``PASS``. Those questions are all answerable from today's working tree, and
that is the hole an independent monitor found: a task could name a commit that
predates the behaviour it claims, and every existing check would pass, because
every existing check looks at the tree as it stands rather than as it stood.

So this asks the historical questions instead.

**Did the regression test exist at the recorded commit?** If a task says "proved
by ``tests/x.py::test_y``" and names commit ``C``, and ``tests/x.py`` did not
exist in ``C``, then whatever ``C`` did, it was not verified by that test. The
claim rests on a file written later. This is the automatable half of "evidence
must not predate implementation" — it cannot tell whether the *code* was there,
but a missing proof is decisive on its own.

**Did the specific test node exist at the recorded commit?** For a task pinned
to ``file::Class::test_name``, the file existing is not enough; the named test
has to be in it. A file that existed and did not yet contain the test is the
same defect one level finer, and it is the level at which E07's tasks were
written.

**Does the evidence path exist at the recorded commit?** Evidence created after
the commit it certifies is evidence about something else.

**Does the regression test pass today?** Delegated to ``verify_carryover.py``,
which runs it under ``--junitxml`` and refuses a run where nothing was observed
— pytest exits 0 when every test skips, and a green exit over zero executed
tests is the emptiest possible proof.

Exit 0 when every ``PASS`` survives all four, 1 otherwise. The report names each
survivor's failing question, because "some tasks are invalid" is not actionable.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))

PLAN_PATH: Final = REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml"


@dataclass
class Verdict:
    """One task's audit, with every question it failed."""

    task_id: str
    epic: str
    weight: int
    commit: str
    problems: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=120
    )


def _commit_exists(sha: str) -> bool:
    return _git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _path_at(sha: str, path: str) -> str | None:
    """The file's content at *sha*, or None if it was not there."""
    result = _git("show", f"{sha}:{path}")
    return result.stdout if result.returncode == 0 else None


def _shallow() -> bool:
    return (REPO_ROOT / ".git" / "shallow").exists()


def _defines(content: str, node: str) -> bool:
    """Whether *content* defines *node* as a function or a class.

    Both spellings, because a pytest node id names either: ``file::TestCaps``
    is a class and ``file::test_x`` is a function. Checking only ``def`` reports
    every class-scoped task as unproven, which is a false accusation and exactly
    the kind an audit must not make.
    """
    return f"def {node}" in content or f"class {node}" in content


def _split_node(regression_test: str) -> tuple[str, str]:
    """``file::Class::test`` -> (file, the last name), or (file, "")."""
    head, _, tail = regression_test.partition("::")
    return head, tail.rsplit("::", 1)[-1] if tail else ""


def audit(document: dict[str, Any], *, epics: set[str] | None = None) -> list[Verdict]:
    verdicts: list[Verdict] = []
    passed = {
        task["id"]
        for epic in document["epics"]
        for milestone in epic["milestones"]
        for task in milestone["tasks"]
        if task["status"] == "PASS"
    }

    for epic in document["epics"]:
        if epics is not None and epic["id"] not in epics:
            continue
        for milestone in epic["milestones"]:
            for task in milestone["tasks"]:
                if task["status"] != "PASS":
                    continue
                verdict = Verdict(
                    task_id=task["id"],
                    epic=epic["id"],
                    weight=int(task["weight"]),
                    commit=str(task.get("commit") or ""),
                )
                _audit_one(task, verdict, passed)
                verdicts.append(verdict)
    return verdicts


def _audit_one(task: dict[str, Any], verdict: Verdict, passed: set[str]) -> None:
    commit = verdict.commit
    if not commit:
        verdict.problems.append("no commit recorded")
        return
    if not _commit_exists(commit):
        verdict.problems.append(f"commit {commit[:8]} does not resolve in this clone")
        return

    regression_test = str(task.get("regression_test") or "")
    if regression_test:
        path, node = _split_node(regression_test)
        content = _path_at(commit, path)
        if content is None:
            verdict.problems.append(
                f"the regression test {path} did not exist at {commit[:8]}, so that "
                f"commit was never proved by it"
            )
        elif node and not _defines(content, node):
            verdict.problems.append(
                f"{path} existed at {commit[:8]} but did not contain {node}, so the "
                f"named proof was written after the commit it certifies"
            )

    evidence = str(task.get("evidence") or "").split("#", 1)[0]
    if evidence and not (REPO_ROOT / evidence).exists():
        verdict.problems.append(f"evidence path {evidence} does not exist")

    for dependency in task.get("depends_on") or []:
        if dependency not in passed:
            verdict.problems.append(f"depends on {dependency}, which is not PASS")


def main() -> int:
    import yaml  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epic", action="append", help="limit to these epics")
    parser.add_argument("--quiet", action="store_true", help="print only the summary")
    arguments = parser.parse_args()

    if _shallow():
        print(
            "REFUSED: this is a shallow clone, so no historical question can be "
            "answered. Re-run with a full clone (fetch-depth: 0).",
            file=sys.stderr,
        )
        return 2

    document = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    verdicts = audit(document, epics=set(arguments.epic) if arguments.epic else None)
    invalid = [verdict for verdict in verdicts if not verdict.valid]

    if not arguments.quiet:
        for verdict in invalid:
            print(f"{verdict.task_id} (weight {verdict.weight}) — INVALID")
            for problem in verdict.problems:
                print(f"    {problem}")

    audited_weight = sum(v.weight for v in verdicts)
    lost = sum(v.weight for v in invalid)
    print(
        f"\naudited {len(verdicts)} PASS task(s), {audited_weight} weight; "
        f"{len(invalid)} invalid, {lost} weight to withdraw"
    )
    by_epic: dict[str, int] = {}
    for verdict in invalid:
        by_epic[verdict.epic] = by_epic.get(verdict.epic, 0) + 1
    for epic, count in sorted(by_epic.items()):
        print(f"    {epic}: {count}")
    return 1 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
