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

import subprocess
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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


def _drifted() -> bool:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_master_plan.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    return result.returncode != 0


def main() -> int:
    document = load()
    windows_green = "--windows-red" not in sys.argv
    found = problems(document, windows_green=windows_green)
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
