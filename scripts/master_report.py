#!/usr/bin/env python3
"""Compute and print the weighted progress. Nothing here is stored.

    progress = sum(weight of PASS tasks) / sum(weight of all tasks) * 100

Derived on every read so there is no recorded number to drift, and so a task
cannot be "counted" twice by editing a total.

Seven metrics are printed beside the overall figure because an overall figure
hides a subsystem at zero. That is the whole reason they exist: 74% of the
weight of this project is reachable from an environment with no game in it, and
a single number would let that read as nearly done.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.check_master_plan import checks_of, load, tasks_of  # noqa: E402
from scripts.plan_model import Check, Epic, Milestone, Task, epic_closed  # noqa: E402

STATUS_ORDER: Final = ("PASS", "IN_PROGRESS", "FAIL", "BLOCKED", "NOT_STARTED")


def weighted(tasks: list[dict[str, Any]]) -> float:
    # int() rather than a cast: the weights come out of YAML as Any, and a task
    # whose weight parsed as a string would otherwise divide silently wrong.
    total = sum(int(task["weight"]) for task in tasks)
    if not total:
        return 0.0
    done = sum(int(task["weight"]) for task in tasks if task["status"] == "PASS")
    return done / total * 100.0


def counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    return {status: sum(1 for t in tasks if t["status"] == status) for status in STATUS_ORDER}


def metric_values(document: dict[str, Any]) -> dict[str, tuple[float, int, int]]:
    """Each metric as (percent, passed weight, total weight)."""
    by_epic: dict[str, list[dict[str, Any]]] = {}
    for epic in document["epics"]:
        by_epic[epic["id"]] = [t for m in epic["milestones"] for t in m["tasks"]]
    out: dict[str, tuple[float, int, int]] = {}
    for name, epic_ids in document["metrics"].items():
        tasks = [t for epic_id in epic_ids for t in by_epic.get(epic_id, [])]
        total = sum(t["weight"] for t in tasks)
        done = sum(t["weight"] for t in tasks if t["status"] == "PASS")
        out[name] = ((done / total * 100.0) if total else 0.0, done, total)
    return out


def _as_epic(raw: dict[str, Any]) -> Epic:
    return Epic(
        id=raw["id"],
        title=raw["title"],
        subsystem=raw["subsystem"],
        integration_scenario=raw["integration_scenario"],
        required_ci=raw["required_ci"],
        milestones=tuple(
            Milestone(
                id=m["id"],
                title=m["title"],
                tasks=tuple(
                    Task(
                        id=t["id"],
                        subsystem=t["subsystem"],
                        action=t["action"],
                        weight=t["weight"],
                        band=t["band"],
                        owner=t["owner"],
                        pass_criterion=t["pass_criterion"],
                        verify_command=t["verify_command"],
                        regression_test=t["regression_test"],
                        evidence=t["evidence"],
                        depends_on=tuple(t["depends_on"]),
                        status=t["status"],
                        commit=t["commit"],
                        ci_url=t["ci_url"],
                        reason=t["reason"],
                    )
                    for t in m["tasks"]
                ),
                checks=tuple(
                    Check(
                        id=c["id"],
                        statement=c["statement"],
                        command=c["command"],
                        status=c["status"],
                        evidence=c["evidence"],
                    )
                    for c in m["checks"]
                ),
            )
            for m in raw["milestones"]
        ),
    )


def next_task(document: dict[str, Any]) -> dict[str, Any] | None:
    """The first remote task whose dependencies are all satisfied.

    Not "the lowest-numbered unfinished task": a task whose dependency is open
    cannot be started, and reporting it as next would send the reader at
    something they cannot do.
    """
    tasks = tasks_of(document)
    passed = {t["id"] for t in tasks if t["status"] == "PASS"}
    for task in tasks:
        if task["status"] == "PASS" or task["owner"] != "remote":
            continue
        if all(dependency in passed for dependency in task["depends_on"]):
            return task
    return None


def render(document: dict[str, Any], *, windows_green: bool) -> str:
    tasks = tasks_of(document)
    tally = counts(tasks)
    metrics = metric_values(document)
    following = next_task(document)
    epics = [_as_epic(raw) for raw in document["epics"]]
    current_epic = next(
        (e.id for e in epics if not epic_closed(e, ci_green=windows_green)[0]), "none open"
    )
    passing_checks = sum(1 for c in checks_of(document) if c["status"] == "PASS")
    lines = [
        f"OVERALL WEIGHTED PROGRESS: {weighted(tasks):.1f}%"
        f"  ({sum(t['weight'] for t in tasks if t['status'] == 'PASS')}"
        f"/{sum(t['weight'] for t in tasks)} weight)",
        f"TOTAL TASKS: {len(tasks)}",
        f"PASS: {tally['PASS']}",
        f"IN_PROGRESS: {tally['IN_PROGRESS']}",
        f"FAIL: {tally['FAIL']}",
        f"BLOCKED: {tally['BLOCKED']}",
        f"NOT_STARTED: {tally['NOT_STARTED']}",
        "",
    ]
    for name, (percent, done, total) in metrics.items():
        lines.append(f"{name.replace('_', ' ')}: {percent:.1f}%  ({done}/{total} weight)")
    lines += [
        "",
        f"CHECKS: {passing_checks}/{len(checks_of(document))} PASS",
        f"CURRENT TASK: {following['id'] + ' — ' + following['action'] if following else 'none'}",
        f"CURRENT EPIC: {current_epic}",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--windows-red", action="store_true")
    args = parser.parse_args()

    document = load()
    tasks = tasks_of(document)
    if args.json:
        print(
            json.dumps(
                {
                    "overall_weighted_progress": round(weighted(tasks), 2),
                    "total_tasks": len(tasks),
                    "total_weight": sum(t["weight"] for t in tasks),
                    "counts": counts(tasks),
                    "metrics": {k: round(v[0], 2) for k, v in metric_values(document).items()},
                    "next_task": (next_task(document) or {}).get("id"),
                },
                indent=2,
            )
        )
        return 0
    print(render(document, windows_green=not args.windows_red))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
