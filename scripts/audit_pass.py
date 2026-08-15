#!/usr/bin/env python3
"""Audit every ``PASS`` in the master plan against the repository's own history.

``check_master_plan.py`` asks whether a claim is *well formed*: does the commit
field hold something, does the evidence path exist **now**, is the dependency
also ``PASS``. Those questions are all answerable from today's working tree, and
that is the hole an independent monitor found: a task could name a commit that
predates the behaviour it claims, and every existing check would pass, because
every existing check looks at the tree as it stands rather than as it stood.

So this asks the historical questions instead.

**Did the regression test exist where the plan says it was proved?** If a task
says "proved by ``tests/x.py::test_y``" and ``tests/x.py`` did not exist at that
commit, then whatever that commit did, it was not verified by that test. The
claim rests on a file written later. This is the automatable half of "evidence
must not predate implementation" — it cannot tell whether the *code* was there,
but a missing proof is decisive on its own.

*Where* the plan says it was proved is ``verification_commit``, not ``commit``.
The two are different fields on purpose: ``commit``/``implementation_commit`` is
where the behaviour landed, ``verification_commit`` is where the proof did, and
``check_master_plan.py`` is explicit that a proof written before or after its
implementation is ordinary work rather than a defect. This file asked both
questions of ``commit`` alone, and so accused ``E06-M04-T001`` — whose proof sits
exactly at the commit the plan names for it — of resting on a file written later.
An audit that makes a false accusation is worse than no audit: it gets argued
with once and switched off. ``commit`` is used only when no verification commit
is recorded, which is the honest reading of a task that names just the one.

**Did the specific test node exist there?** For a task pinned to
``file::Class::test_name``, the file existing is not enough; the named test has
to be in it. A file that existed and did not yet contain the test is the same
defect one level finer, and it is the level at which E07's tasks were written.

**Does the evidence path exist?** Asked of the working tree, because evidence is
a path that has to be there now for anyone to read it. ``check_master_plan.py``
asks the same question and this repeats it, so a verdict here is complete on its
own rather than true only in combination with another script's output.

**Is every dependency also PASS?** A claim standing on an open one is not a
claim.

Exit 0 when every ``PASS`` survives all four, 1 when one does not, 2 when the
clone is shallow and no historical question can be answered at all. The report
names each failure's question, because "some tasks are invalid" is not
actionable.

Whether the named test *passes* is not asked here and deliberately so: that is
what running the suite does, on every commit, in ``scripts/check.sh`` and in CI.
``verify_carryover.py`` is the tool for asking it task by task, under
``--junitxml`` so that a green run over zero executed tests is refused. This
script asks only the questions a test run cannot answer — the ones about
history.
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

from scripts._process import run_text  # noqa: E402

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


def proving_commit(task: dict[str, Any]) -> str:
    """The commit the plan says the proof landed at.

    ``verification_commit`` when the task records one, and ``commit`` when it
    does not. Never a silent fallback the other way: a task that names where it
    was verified is answered on that, and reading ``commit`` instead is what
    produced a false accusation against a sound claim.
    """
    return str(task.get("verification_commit") or task.get("commit") or "")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Git, decoded as UTF-8 rather than as whatever the host prefers.

    ``_path_at`` reads whole files out of history, and this repository's
    files are UTF-8 Russian prose. Left to ``text=True`` the Windows runner
    decoded them as cp1252 and raised inside subprocess's reader thread, so
    the audit could not run at all there. See :mod:`scripts._process`.
    """
    return run_text(["git", *args], cwd=REPO_ROOT, timeout=120)


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
        proved_at = proving_commit(task)
        if not _commit_exists(proved_at):
            verdict.problems.append(
                f"the verification commit {proved_at[:8]} does not resolve in this clone"
            )
            return
        path, node = _split_node(regression_test)
        content = _path_at(proved_at, path)
        if content is None:
            verdict.problems.append(
                f"the regression test {path} did not exist at {proved_at[:8]}, the commit "
                f"this task names as its verification, so nothing was proved by it there"
            )
        elif node and not _defines(content, node):
            verdict.problems.append(
                f"{path} existed at {proved_at[:8]} but did not contain {node}, so the "
                f"named proof was written after the commit that claims it"
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
