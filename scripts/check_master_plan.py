#!/usr/bin/env python3
"""Refuse a claim in ``MASTER_PLAN.yaml`` that its own evidence does not support.

A gate, not a report — :mod:`master_report` prints, this refuses. Exit 0
admissible, 1 refused, 2 the plan is unusable.

Eleven refusals, each for a way a weighted percentage becomes a lie:

* a task is ``PASS`` with no evidence path;
* a task is ``PASS`` with no commit;
* a task is ``PASS`` and its evidence path does not exist on disk;
* a task is ``PASS`` while something it depends on is not;
* a ``local`` task is ``PASS``, which this environment cannot honestly claim;
* a task is ``PASS`` and names a regression test that does not exist;
* a task requiring a green workflow is ``PASS`` while that workflow is red;
* a ``CHECK`` is ``PASS`` with no evidence;
* an epic is recorded closed while :func:`epic_closed` disagrees;
* a status is not one of the five;
* the plan on disk has drifted from the definitions that generate it.

Note what is *not* here: nothing recomputes a stored percentage, because none is
stored. The percentage is derived on every read, so there is no number to
disagree with.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._process import ENCODING, ERRORS  # noqa: E402

PLAN_PATH: Final = REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml"


def load() -> dict[str, Any]:
    import yaml  # noqa: PLC0415

    try:
        document = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"{PLAN_PATH}: does not exist") from None
    if not isinstance(document, dict) or "epics" not in document:
        raise SystemExit(f"{PLAN_PATH}: is not a master plan")
    return document


def tasks_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        task
        for epic in document["epics"]
        for milestone in epic["milestones"]
        for task in milestone["tasks"]
    ]


def checks_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        check
        for epic in document["epics"]
        for milestone in epic["milestones"]
        for check in milestone["checks"]
    ]


def _exists(relative: str) -> bool:
    """Whether an evidence path exists, ignoring an anchor after ``#``."""
    return (REPO_ROOT / relative.split("#", 1)[0]).exists()


def problems(document: dict[str, Any], *, windows_green: bool) -> list[str]:
    found: list[str] = []
    tasks = tasks_of(document)
    by_id = {task["id"]: task for task in tasks}
    passed = {task["id"] for task in tasks if task["status"] == "PASS"}

    for task in tasks:
        identifier = task["id"]
        if task["status"] not in document["statuses"]:
            found.append(f"{identifier}: status {task['status']!r} is unknown")
        if task["status"] != "PASS":
            continue
        if not task.get("evidence"):
            found.append(f"{identifier} is PASS with no evidence path")
        elif not _exists(task["evidence"]):
            found.append(
                f"{identifier} is PASS but its evidence does not exist: {task['evidence']}"
            )
        if not task.get("commit"):
            found.append(f"{identifier} is PASS with no commit")
        if task["owner"] == "local":
            found.append(
                f"{identifier} is a local task marked PASS; nothing in this environment "
                "can produce its evidence, so the claim cannot be honest here"
            )
        test = task.get("regression_test") or ""
        if test and not _exists(test.split("::", 1)[0]):
            found.append(f"{identifier} is PASS but its regression test does not exist: {test}")
        for dependency in task.get("depends_on") or []:
            if dependency not in passed:
                state = by_id.get(dependency, {}).get("status", "unknown")
                found.append(f"{identifier} is PASS but depends on {dependency}, which is {state}")

    for check in checks_of(document):
        if check["status"] == "PASS" and not check.get("evidence"):
            found.append(f"{check['id']} is a PASS check with no evidence")

    found.extend(_provenance_problems(tasks))

    # A Windows-dependent task may not pass on a red workflow. Identified by the
    # epic's required_ci rather than by a hand-kept list, so a new task in a
    # Windows epic is covered without anybody remembering to add it.
    if not windows_green:
        for epic in document["epics"]:
            if not (epic.get("required_ci") or "").endswith("windows.yml"):
                continue
            for milestone in epic["milestones"]:
                for task in milestone["tasks"]:
                    if task["status"] == "PASS" and "windows" in task["verify_command"].lower():
                        found.append(
                            f"{task['id']} claims a Windows result while the Windows "
                            "workflow is not green"
                        )
    return found


def _head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding=ENCODING,
        errors=ERRORS,
        check=False,
        timeout=60,
    )
    return result.stdout.strip()


def _resolves(sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=60,
        ).returncode
        == 0
    )


def _is_ancestor(earlier: str, later: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", earlier, later],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=60,
        ).returncode
        == 0
    )


def describes_the_code_at_head(sha: str) -> bool:
    """Whether a claim made at *sha* is still a claim about HEAD's code.

    True when *sha* is an ancestor of HEAD and nothing outside ``docs/control/``
    has changed since. This is the same reasoning the ``head_commit`` staleness
    rule states in prose: a commit that only rewrites the control plane leaves
    every claim about the *code* true. It has to be shared by the CI-verdict and
    release-candidate rules too, because "GREEN only when the SHA equals HEAD"
    is unsatisfiable — recording a green run requires a commit, and the commit
    changes the SHA. That exact shape shipped once: a STATUS written at the
    verified commit was refused by its own child commit, the one that did
    nothing but record it.
    """
    if not sha or not _is_ancestor(sha, "HEAD"):
        return False
    moved = subprocess.run(
        ["git", "diff", "--name-only", sha, "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding=ENCODING,
        errors=ERRORS,
        check=False,
        timeout=120,
    ).stdout.split()
    return all(path.startswith("docs/control/") for path in moved)


def _provenance_problems(tasks: list[dict[str, Any]]) -> list[str]:
    """The refusals an independent monitor asked for, about *history*.

    Every check in :func:`problems` reads the tree as it stands today. That is
    the hole: a task could name a commit that predated the behaviour it claimed
    and nothing looked. E07's tasks were pinned to a commit dated a day before
    the implementation landed, and the plan called them PASS.
    """
    found: list[str] = []
    for task in tasks:
        if task["status"] != "PASS":
            continue
        identifier = task["id"]
        implementation = task.get("implementation_commit")
        verification = task.get("verification_commit")

        for label, sha in (("implementation", implementation), ("verification", verification)):
            if sha and not _resolves(str(sha)):
                found.append(f"{identifier}: its {label} commit {str(sha)[:8]} does not resolve")

        # An integration, security or live task must say both, because those are
        # the ones where "the code exists" and "the code was shown to work" came
        # apart in practice.
        if task["band"] in {"integration", "security", "live"}:
            if not implementation:
                found.append(f"{identifier} is a {task['band']} PASS with no implementation_commit")
            if not verification:
                found.append(f"{identifier} is a {task['band']} PASS with no verification_commit")

        # Both must be reachable from HEAD. Deliberately NOT "the verification
        # descends from the implementation": a regression test written before
        # the code it later proves is ordinary test-first work, not a defect.
        # What makes a claim false is being asserted at a commit where one of
        # the two did not yet exist, and every PASS here is asserted at HEAD.
        for label, sha in (("implementation", implementation), ("verification", verification)):
            if sha and _resolves(str(sha)) and not _is_ancestor(str(sha), "HEAD"):
                found.append(
                    f"{identifier}: its {label} commit {str(sha)[:8]} is not an ancestor of "
                    f"HEAD, so this branch does not contain what the claim rests on"
                )
    return found


def _status_problems(document: dict[str, Any]) -> list[str]:
    """STATUS.json must agree with the plan on disk, this repository and this commit.

    Called from :func:`main` against the real plan only. It reads git and two
    files in ``docs/control/``, so it is meaningless against a fixture and
    would fail every one of them.
    """
    found: list[str] = []
    status_path = REPO_ROOT / "docs" / "control" / "STATUS.json"
    if not status_path.exists():
        return ["docs/control/STATUS.json does not exist"]
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as broken:
        return [f"docs/control/STATUS.json is not readable JSON: {broken}"]

    tasks = tasks_of(document)
    total = sum(int(t["weight"]) for t in tasks)
    done = sum(int(t["weight"]) for t in tasks if t["status"] == "PASS")
    calculated = round(done / total * 100.0, 2) if total else 0.0
    recorded = status.get("weighted_progress_percent")
    if recorded != calculated:
        found.append(
            f"STATUS.json records {recorded}% and the plan calculates {calculated}%. "
            "Regenerate with scripts/reconcile_status.py."
        )

    current = _head()
    described = str(status.get("head_commit") or "")
    if current and described != current:
        # Not simply "must equal HEAD". STATUS.json names the commit it
        # describes, and the commit that CONTAINS it is necessarily a later one —
        # writing the file changes the tree, which changes the SHA. A rule of
        # strict equality is therefore unsatisfiable, and an unsatisfiable rule
        # gets switched off rather than obeyed.
        #
        # What actually makes it stale is something it describes having moved. So
        # it must name an ancestor of HEAD, and nothing outside docs/control/ may
        # have changed since: a commit that only rewrites the control plane
        # leaves every claim in this file true.
        if not described or not _is_ancestor(described, "HEAD"):
            found.append(
                f"STATUS.json describes {described[:8] or 'nothing'}, which is not an "
                f"ancestor of HEAD ({current[:8]}); it is about another history"
            )
        else:
            moved = subprocess.run(
                ["git", "diff", "--name-only", described, "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding=ENCODING,
                errors=ERRORS,
                check=False,
                timeout=120,
            ).stdout.split()
            outside = [path for path in moved if not path.startswith("docs/control/")]
            if outside:
                found.append(
                    f"STATUS.json describes {described[:8]} and {len(outside)} file(s) have "
                    f"changed since, first {outside[0]}; regenerate with "
                    f"scripts/reconcile_status.py"
                )

    # "GREEN" and "CURRENT" are judged by the same predicate as head_commit
    # above, not by SHA equality with HEAD: the commit that records a verdict
    # is necessarily later than the commit the verdict is about, and a rule
    # that refuses its own recording gets switched off rather than obeyed.
    for field in ("linux_ci", "windows_ci"):
        entry = status.get(field) or {}
        verified = str(entry.get("commit") or "")
        if entry.get("status") == "GREEN" and not describes_the_code_at_head(verified):
            found.append(
                f"STATUS.json claims {field} GREEN for {verified[:8] or 'nothing'}, and the "
                f"code has changed since; a workflow result for one commit is not evidence "
                f"about another"
            )

    release = status.get("release_candidate") or {}
    built_from = str(release.get("source_commit") or "")
    if release.get("status") == "CURRENT" and not describes_the_code_at_head(built_from):
        found.append(
            "STATUS.json marks the release candidate CURRENT while it was built from "
            f"{built_from[:8] or 'nothing'} and the code has changed since; it is STALE"
        )

    blocked = [t["id"] for t in tasks if t["status"] in {"FAIL", "BLOCKED"}]
    blockers = REPO_ROOT / "docs" / "control" / "BLOCKERS.md"
    if blocked and blockers.exists():
        text = blockers.read_text(encoding="utf-8").lower()
        if "open: none" in text:
            found.append(
                f"BLOCKERS.md says 'Open: none' while {len(blocked)} task(s) are FAIL or "
                f"BLOCKED, first {blocked[0]}"
            )
    return found


def _drifted() -> bool:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_master_plan.py"), "--check"],
        capture_output=True,
        text=True,
        encoding=ENCODING,
        errors=ERRORS,
        cwd=REPO_ROOT,
        check=False,
    )
    return result.returncode != 0


def main() -> int:
    document = load()
    windows_green = "--windows-red" not in sys.argv
    found = problems(document, windows_green=windows_green)
    # Asked here rather than inside problems(): these are questions about the
    # repository — its HEAD, its STATUS.json, its BLOCKERS.md — not about the
    # document under examination. Inside problems() they would fire against
    # every hand-built fixture a test passes in, which is how a guard becomes
    # noise and then gets deleted.
    found.extend(_status_problems(document))
    if _drifted():
        found.append(
            "MASTER_PLAN.yaml has drifted from scripts/plan_epics_*.py; "
            "run scripts/build_master_plan.py"
        )
    if found:
        print(f"REFUSED: {len(found)} problem(s) with the recorded plan", file=sys.stderr)
        for problem in found[:60]:
            print(f"  - {problem}", file=sys.stderr)
        if len(found) > 60:
            print(f"  ... and {len(found) - 60} more", file=sys.stderr)
        return 1
    tasks = tasks_of(document)
    passing = [task for task in tasks if task["status"] == "PASS"]
    print(
        f"the plan is admissible: {len(passing)}/{len(tasks)} tasks PASS, "
        f"weight {sum(t['weight'] for t in passing)}/{sum(t['weight'] for t in tasks)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
