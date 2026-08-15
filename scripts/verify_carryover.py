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
import tempfile
from pathlib import Path
from typing import Any, Final
from xml.etree import ElementTree

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._process import ENCODING, ERRORS  # noqa: E402

PLAN_PATH: Final = REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml"

#: Candidates: (task id prefix or exact id) -> the commit that did the work.
#: Only tasks named here are even considered. Everything else stays untouched,
#: because a task nobody claims has nothing to confirm.
CANDIDATES: Final[dict[str, str]] = {}


_CACHE: dict[str, tuple[bool, str]] = {}


def _run(test: str) -> tuple[bool, str]:
    """Run one pytest target. Returns (passed, why not).

    Cached by target: many tasks name the same file, and running it once per
    task would turn a two-minute confirmation into an hour of the same work.

    **A green exit is not enough.** pytest exits 0 when every test in a target
    skipped, and this whole script exists to stop a claim resting on something
    that did not happen. A target whose tests all skip — an optional dependency
    absent, a platform guard, a fixture that quietly bailed — proves nothing at
    all, and marking it ``PASS`` would be exactly the substitution the plan
    forbids: a test that did not run standing in for a behaviour that was never
    observed. So a passing count is required, not merely a zero exit.
    """
    if test in _CACHE:
        return _CACHE[test]
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "report.xml"
        result = subprocess.run(
            [
                # ``sys.executable -m pytest`` rather than ``.venv/bin/pytest``:
                # that path is the POSIX venv layout and does not exist on
                # Windows, where the entry point is ``.venv/Scripts/pytest.exe``.
                # The interpreter already running this script is the right one by
                # construction, on either platform.
                sys.executable,
                "-m",
                "pytest",
                test,
                "-x",
                f"--junitxml={report}",
            ],
            capture_output=True,
            text=True,
            encoding=ENCODING,
            errors=ERRORS,
            cwd=REPO_ROOT,
            timeout=900,
            check=False,
        )
        counts = _counts(report)

    if result.returncode != 0:
        tail = [ln for ln in result.stdout.splitlines() if ln.startswith(("FAILED", "ERROR"))]
        _CACHE[test] = (False, tail[0] if tail else f"exit {result.returncode}")
    elif counts is None:
        _CACHE[test] = (False, "pytest produced no report, so nothing can be said about it")
    elif counts["ran"] <= 0:
        _CACHE[test] = (
            False,
            f"the run was green but {counts['collected']} test(s) were collected and "
            f"{counts['skipped']} skipped, so nothing was observed",
        )
    else:
        _CACHE[test] = (True, "")
    return _CACHE[test]


def _counts(report: Path) -> dict[str, int] | None:
    """Read the counts out of a JUnit report.

    Parsed from XML rather than from pytest's summary line, which this project
    does not print: ``addopts`` already carries ``-q``, so passing another made
    it ``-qq`` and suppressed the summary entirely. The first version of this
    check read that missing line and declared twelve genuinely-passing targets
    unobserved — a measurement bug that would have *lowered* the reported
    figure, which is the direction that hides less but is no more true.
    """
    try:
        root = ElementTree.parse(report).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root)
    collected = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    return {
        "collected": collected,
        "skipped": skipped,
        "failed": failed,
        "ran": collected - skipped - failed,
    }


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


def evaluate(task: dict[str, Any], commit: str) -> tuple[str | None, str]:
    """(status, reason) for one candidate task, or ``(None, why)`` to decide nothing.

    ``None`` is not a soft ``FAIL``. It means this environment has no standing to
    say anything about the task, so whatever the plan records is left exactly as
    it is.
    """
    if task.get("owner") == "local":
        # ``check_master_plan.py`` refuses a ``local`` task marked PASS in as
        # many words — nothing here can produce its evidence. This function did
        # not know that rule, and a live task whose named test happens to pass
        # on Linux and whose evidence path happens to exist was returned as
        # PASS: one script writing precisely what the other exists to refuse.
        # Not reachable in today's plan (no local task has both an existing test
        # file and an existing evidence path) and closed anyway, because "not
        # reachable today" is a fact about the plan, not about this code.
        return None, (
            "owner is 'local': nothing in this environment can produce this task's "
            "evidence, so nothing here may decide its status either"
        )
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

    outcome: dict[str, tuple[str | None, str]] = {}
    for epic in document["epics"]:
        for milestone in epic["milestones"]:
            for task in milestone["tasks"]:
                commit = candidates.get(task["id"])
                if commit is None:
                    continue
                status, reason = evaluate(task, commit)
                outcome[task["id"]] = (status, reason)
                # A ``None`` status writes nothing at all — not the status, not
                # the reason, not the commit. Recording a reason against a task
                # this environment may not judge would still be this environment
                # having had an opinion about it.
                if args.apply and status is not None:
                    task["status"] = status
                    task["reason"] = reason
                    task["commit"] = commit if status == "PASS" else task.get("commit")

    confirmed = [i for i, (s, _) in outcome.items() if s == "PASS"]
    left_alone = {i: r for i, (s, r) in outcome.items() if s is None}
    rejected = {i: r for i, (s, r) in outcome.items() if s is not None and s != "PASS"}
    print(f"candidates: {len(outcome)}")
    print(f"confirmed PASS: {len(confirmed)}")
    print(f"not confirmed: {len(rejected)}")
    for identifier, reason in sorted(rejected.items()):
        print(f"  - {identifier}: {reason}")
    # Printed separately and never folded into the count above: "this cannot be
    # judged here" and "this was judged and failed" are different facts, and a
    # summary that merges them reads as a verdict nobody reached.
    print(f"left alone: {len(left_alone)}")
    for identifier, reason in sorted(left_alone.items()):
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
