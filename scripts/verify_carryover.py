#!/usr/bin/env python3
"""Re-derive which tasks may be called PASS, by running their tests.

Nothing is inherited. The previous plan recorded fifty passing steps; this
script does not read that record at all. For each candidate it requires, in
order:

1. a regression test named in the plan;
2. that test file existing on disk;
3. that test **actually passing**, run here, now;
4. an evidence path that exists;
5. a commit that exists in this repository.

Anything short of all five is ``IN_PROGRESS`` if the work has visibly begun and
``NOT_STARTED`` otherwise, with the reason recorded. A task whose ``PASS``
depended on a bookkeeping act rather than a behaviour — "commit the block",
"record the branches" — has no test to run and cannot be confirmed this way, so
it is not confirmed.

Run with ``--apply`` to write the results into the plan. Without it, prints what
it would do and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PLAN_PATH: Final = REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml"

#: Candidates: (task id prefix or exact id) -> the commit that did the work.
#: Only tasks named here are even considered. Everything else stays untouched,
#: because a task nobody claims has nothing to confirm.
CANDIDATES: Final[dict[str, str]] = {}


_CACHE: dict[str, tuple[bool, str]] = {}


def _run(test: str) -> tuple[bool, str]:
    """Run one pytest target. Returns (passed, first line of failure).

    Cached by target: many tasks name the same file, and running it once per
    task would turn a two-minute confirmation into an hour of the same work.
    """
    if test in _CACHE:
        return _CACHE[test]
    result = subprocess.run(
        [str(REPO_ROOT / ".venv" / "bin" / "pytest"), test, "-q", "--no-header", "-x"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=900,
        check=False,
    )
    if result.returncode == 0:
        _CACHE[test] = (True, "")
    else:
        tail = [ln for ln in result.stdout.splitlines() if ln.startswith(("FAILED", "ERROR"))]
        _CACHE[test] = (False, tail[0] if tail else f"exit {result.returncode}")
    return _CACHE[test]


def _commit_exists(sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            cwd=REPO_ROOT,
            check=False,
        ).returncode
        == 0
    )


def evaluate(task: dict[str, Any], commit: str) -> tuple[str, str]:
    """(status, reason) for one candidate task."""
    test = (task.get("regression_test") or "").strip()
    if not test:
        return "IN_PROGRESS", (
            "the work is done but no regression test names it, so a PASS here would "
            "rest on a commit rather than on a behaviour"
        )
    target = test.split("::", 1)[0]
    if not (REPO_ROOT / target).exists():
        return "NOT_STARTED", f"the named regression test does not exist: {target}"
    evidence = (task.get("evidence") or "").split("#", 1)[0]
    if not evidence or not (REPO_ROOT / evidence).exists():
        return "IN_PROGRESS", f"the evidence path does not exist: {evidence or '(none)'}"
    if not _commit_exists(commit):
        return "IN_PROGRESS", f"the recorded commit does not exist here: {commit}"
    passed, detail = _run(test)
    if not passed:
        return "FAIL", f"the regression test does not pass: {detail}"
    return "PASS", ""


def main() -> int:
    import yaml  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--candidates", type=Path, required=True, help="JSON mapping task id -> commit sha"
    )
    args = parser.parse_args()

    candidates: dict[str, str] = json.loads(args.candidates.read_text(encoding="utf-8"))
    document = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))

    outcome: dict[str, tuple[str, str]] = {}
    for epic in document["epics"]:
        for milestone in epic["milestones"]:
            for task in milestone["tasks"]:
                commit = candidates.get(task["id"])
                if commit is None:
                    continue
                status, reason = evaluate(task, commit)
                outcome[task["id"]] = (status, reason)
                if args.apply:
                    task["status"] = status
                    task["reason"] = reason
                    task["commit"] = commit if status == "PASS" else task.get("commit")

    confirmed = [i for i, (s, _) in outcome.items() if s == "PASS"]
    rejected = {i: r for i, (s, r) in outcome.items() if s != "PASS"}
    print(f"candidates: {len(outcome)}")
    print(f"confirmed PASS: {len(confirmed)}")
    print(f"not confirmed: {len(rejected)}")
    for identifier, reason in sorted(rejected.items()):
        print(f"  - {identifier}: {reason}")

    if args.apply:
        import yaml as _yaml  # noqa: PLC0415

        PLAN_PATH.write_text(
            _yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        print(f"wrote {PLAN_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
