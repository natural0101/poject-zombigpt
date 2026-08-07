#!/usr/bin/env python3
"""Refuse a progress claim that is not backed by evidence.

``docs/control/STATUS.json`` is the only place progress is recorded, and this is
the only thing that decides whether what it records is admissible. It is a gate,
not a report — :mod:`progress_report` prints, this refuses.

Nine refusals, each for a way a percentage becomes a lie:

* the percentage disagrees with the number of ``PASS`` steps;
* a ``PASS`` step carries no evidence path;
* a ``PASS`` step carries no commit;
* there is a gap: step *n* passed while an earlier one did not, so the sequence
  is a set of cherry-picked wins rather than progress;
* a step passed while something it depends on did not;
* a live-game step passed without evidence — the one class of step this
  environment can never produce, so a ``PASS`` there is a fabrication by
  definition;
* a Windows step passed while the Windows workflow is not green;
* step 100 passed with no GitHub Release;
* an evidence path that does not exist on disk.

Exit codes: 0 admissible, 1 refused, 2 the status file itself is unusable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
STATUS_PATH: Final = REPO_ROOT / "docs" / "control" / "STATUS.json"

#: Steps only an agent holding Project Zomboid can pass.
LIVE_STEPS: Final = frozenset({96, 97, 98})

#: Steps that *assert Windows works*. Only a green Windows workflow supports
#: one, because a Linux run says nothing about Windows.
#:
#: Deliberately not every step in the Windows stages. Step 12 is "reproduce the
#: Windows failures", and its evidence is a **red** run — requiring green there
#: would make the honest reproduction of a failure unrecordable. Steps 14-29 are
#: implementations whose own evidence is a cross-platform test; what they roll
#: up into is step 30, and that is where the workflow has to be green. The
#: distinction matters: a rule that is strict in the wrong places gets loosened
#: wholesale the first time it blocks something legitimate.
WINDOWS_STEPS: Final = frozenset({30, 39, 40}) | frozenset(range(91, 96))

VALID_STATUSES: Final = frozenset({"NOT_STARTED", "IN_PROGRESS", "BLOCKED", "FAIL", "PASS"})

TOTAL_STEPS: Final = 100


def _load() -> dict[str, Any]:
    try:
        document: Any = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(
            f"{STATUS_PATH}: does not exist; create it before claiming progress"
        ) from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{STATUS_PATH}: is not JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise SystemExit(f"{STATUS_PATH}: must be a JSON object")
    return document


def _steps(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw = document.get("steps")
    if not isinstance(raw, list):
        raise SystemExit(f"{STATUS_PATH}: 'steps' must be a list")
    steps: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise SystemExit(f"{STATUS_PATH}: every step must be an object")
        steps.append(entry)
    if len(steps) != TOTAL_STEPS:
        raise SystemExit(f"{STATUS_PATH}: {len(steps)} steps recorded, expected {TOTAL_STEPS}")
    return steps


def _problems(document: dict[str, Any], steps: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    by_id = {int(step["id"]): step for step in steps}
    passed = {number for number, step in by_id.items() if step.get("status") == "PASS"}

    for number, step in sorted(by_id.items()):
        status = step.get("status")
        if status not in VALID_STATUSES:
            problems.append(
                f"step {number}: status {status!r} is not one of {sorted(VALID_STATUSES)}"
            )
        if status != "PASS":
            continue
        if not step.get("evidence"):
            problems.append(f"step {number} is PASS with no evidence")
        if not step.get("commit"):
            problems.append(f"step {number} is PASS with no commit")
        for path in step.get("evidence") or []:
            if not (REPO_ROOT / str(path).split("#", 1)[0]).exists():
                problems.append(f"step {number}: evidence path does not exist: {path}")
        for dependency in step.get("depends_on") or []:
            if int(dependency) not in passed:
                problems.append(f"step {number} is PASS but depends on {dependency}, which is not")
        if number in LIVE_STEPS and not step.get("evidence"):
            problems.append(f"step {number} is a live-game step and cannot pass without evidence")

    # A gap: the passed set must be a prefix. Anything else is cherry-picking.
    if passed:
        highest = max(passed)
        missing = sorted(set(range(1, highest + 1)) - passed)
        if missing:
            problems.append(
                f"steps {missing} are not PASS while step {highest} is; progress has gaps"
            )

    recorded = document.get("overall_percent")
    if recorded != len(passed):
        problems.append(
            f"overall_percent is {recorded}, but {len(passed)} step(s) are PASS. "
            "The percentage is counted, never written."
        )

    windows = document.get("windows_ci", {})
    windows_status = windows.get("status") if isinstance(windows, dict) else None
    for number in sorted(passed & WINDOWS_STEPS):
        if windows_status != "GREEN":
            problems.append(
                f"step {number} claims a Windows result while windows_ci.status is "
                f"{windows_status!r}; only a green Windows workflow supports one"
            )
            break

    if 100 in passed:
        release = document.get("github_release")
        if not release:
            problems.append("step 100 is PASS with no GitHub Release recorded")

    for band, (low, high) in {
        "remote_percent": (1, 95),
        "live_game_percent": (96, 98),
    }.items():
        expected = len({n for n in passed if low <= n <= high})
        if document.get(band) != expected:
            problems.append(f"{band} is {document.get(band)}, counted {expected}")

    return problems


def main() -> int:
    document = _load()
    steps = _steps(document)
    problems = _problems(document, steps)
    if problems:
        print(f"REFUSED: {len(problems)} problem(s) with the recorded progress", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    passed = sum(1 for step in steps if step.get("status") == "PASS")
    print(f"progress is admissible: {passed} step(s) PASS, evidence present for each")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
