#!/usr/bin/env python3
"""Emit ``docs/control/MASTER_PLAN.yaml`` from the task definitions.

The YAML is the artefact; these modules are where it is written, so a task is
reviewed as code and the file is regenerable rather than hand-maintained. Status
lives in the YAML, not here: the generator seeds a new task as ``NOT_STARTED``
and never overwrites a status that is already recorded.

``--check`` regenerates and compares, so a drifted file fails a gate instead of
being silently rebuilt.
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

from scripts.plan_epics_a import E01, E02  # noqa: E402
from scripts.plan_epics_b import E03, E04, E05  # noqa: E402
from scripts.plan_epics_c import E06, E07, E08, E09, E10  # noqa: E402
from scripts.plan_epics_d import E11, E12, E13, E14, E15  # noqa: E402
from scripts.plan_model import BANDS, METRICS, STATUSES, weight_band  # noqa: E402

EPICS: Final = (E01, E02, E03, E04, E05, E06, E07, E08, E09, E10, E11, E12, E13, E14, E15)

PLAN_PATH: Final = REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml"

#: Fields whose value is decided by a run rather than by the plan's author, and
#: which are therefore carried across a regeneration rather than reset.
_CARRIED: Final = (
    "status",
    "implementation_commit",
    "verification_commit",
    "commit",
    "ci_url",
    "reason",
)


def _existing() -> dict[str, dict[str, Any]]:
    """Recorded per-task state, keyed by id, from the plan already on disk."""
    if not PLAN_PATH.exists():
        return {}
    import yaml  # noqa: PLC0415

    document = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8")) or {}
    recorded: dict[str, dict[str, Any]] = {}
    for epic in document.get("epics", []):
        for milestone in epic.get("milestones", []):
            for task in milestone.get("tasks", []):
                recorded[task["id"]] = {key: task.get(key) for key in _CARRIED}
            for check in milestone.get("checks", []):
                recorded[check["id"]] = {"status": check.get("status")}
    return recorded


def build() -> dict[str, Any]:
    recorded = _existing()
    epics: list[dict[str, Any]] = []
    for epic in EPICS:
        as_dict = epic.to_dict()
        for milestone in as_dict["milestones"]:
            for task in milestone["tasks"]:
                for key, value in (recorded.get(task["id"]) or {}).items():
                    if value is not None:
                        task[key] = value
            for check in milestone["checks"]:
                carried = (recorded.get(check["id"]) or {}).get("status")
                if carried is not None:
                    check["status"] = carried
        epics.append(as_dict)
    return {
        "format": "pz-agent-master-plan/1",
        "repository": "https://github.com/natural0101/poject-zombigpt",
        "rule": (
            "progress = sum(weight of PASS tasks) / sum(weight of all tasks) * 100. "
            "Task count is not progress. An epic does not close because its tasks did."
        ),
        "weight_bands": {name: list(span) for name, span in BANDS.items()},
        "statuses": list(STATUSES),
        "metrics": {name: list(ids) for name, ids in METRICS.items()},
        "epics": epics,
    }


def validate(document: dict[str, Any]) -> list[str]:
    """Structural problems with the plan itself, before any status is considered."""
    problems: list[str] = []
    seen: set[str] = set()
    weights_by_band: dict[str, set[int]] = {}
    for epic in document["epics"]:
        for milestone in epic["milestones"]:
            for task in milestone["tasks"]:
                if task["id"] in seen:
                    problems.append(f"duplicate id {task['id']}")
                seen.add(task["id"])
                if not weight_band(task["weight"], task["band"]):
                    problems.append(
                        f"{task['id']}: weight {task['weight']} outside band {task['band']}"
                    )
                weights_by_band.setdefault(task["band"], set()).add(task["weight"])
                for field in (
                    "action",
                    "pass_criterion",
                    "verify_command",
                    "evidence",
                    "subsystem",
                ):
                    if not task.get(field):
                        problems.append(f"{task['id']}: {field} is empty")
                if task["owner"] not in {"remote", "local"}:
                    problems.append(f"{task['id']}: owner {task['owner']!r} is not remote or local")
                if task["status"] not in STATUSES:
                    problems.append(f"{task['id']}: status {task['status']!r} is unknown")
            for check in milestone["checks"]:
                if check["id"] in seen:
                    problems.append(f"duplicate id {check['id']}")
                seen.add(check["id"])
    # The rule that gives weight its meaning: documentation, transport, MCP
    # end-to-end and live work may not be worth the same.
    for left, right in (
        ("doc", "transport"),
        ("doc", "live"),
        ("transport", "live"),
        ("integration", "live"),
        ("doc", "integration"),
    ):
        if weights_by_band.get(left, set()) & weights_by_band.get(right, set()):
            problems.append(
                f"bands {left} and {right} share a weight; they must not be worth the same"
            )
    for dependency in _all_dependencies(document):
        if dependency not in seen:
            problems.append(f"dependency {dependency} names no task")
    return problems


def _all_dependencies(document: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for epic in document["epics"]:
        for milestone in epic["milestones"]:
            for task in milestone["tasks"]:
                found.extend(task["depends_on"])
    return found


def _render(document: dict[str, Any]) -> str:
    import yaml  # noqa: PLC0415

    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the file on disk has drifted")
    parser.add_argument(
        "--stats", action="store_true", help="print counts and total weight as JSON"
    )
    args = parser.parse_args()

    document = build()
    problems = validate(document)
    if problems:
        print(f"the plan is not well formed: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems[:40]:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    rendered = _render(document)
    if args.check:
        if not PLAN_PATH.exists() or PLAN_PATH.read_text(encoding="utf-8") != rendered:
            print(f"{PLAN_PATH} has drifted from the definitions", file=sys.stderr)
            return 1
        print("the plan on disk matches the definitions")
        return 0

    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(rendered, encoding="utf-8")

    tasks = [
        task
        for epic in document["epics"]
        for milestone in epic["milestones"]
        for task in milestone["tasks"]
    ]
    checks = [
        check
        for epic in document["epics"]
        for milestone in epic["milestones"]
        for check in milestone["checks"]
    ]
    stats = {
        "tasks": len(tasks),
        "checks": len(checks),
        "total_weight": sum(task["weight"] for task in tasks),
        "remote_tasks": sum(1 for task in tasks if task["owner"] == "remote"),
        "local_tasks": sum(1 for task in tasks if task["owner"] == "local"),
        "remote_weight": sum(task["weight"] for task in tasks if task["owner"] == "remote"),
        "local_weight": sum(task["weight"] for task in tasks if task["owner"] == "local"),
        "epics": len(document["epics"]),
    }
    if args.stats:
        print(json.dumps(stats, indent=2))
    else:
        print(
            f"wrote {PLAN_PATH.relative_to(REPO_ROOT)}: {stats['tasks']} tasks, "
            f"{stats['checks']} checks, total weight {stats['total_weight']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
