#!/usr/bin/env python3
"""Regenerate ``docs/control/STATUS.json`` from facts, never from memory.

STATUS.json was the file an independent monitor caught lying: it carried a HEAD
that was days old, a percentage of 50 that no calculation produced, a CI verdict
belonging to a different commit, and ``Open: none`` blockers next to a red build.
Every one of those was possible because the file was *written* rather than
*derived*.

So nothing here is typed by hand. The percentage comes from
``docs/control/MASTER_PLAN.yaml`` through the same function the report uses, the
HEAD comes from ``git rev-parse``, and the CI and RC verdicts have to be passed
in on the command line **with the commit they belong to** — a CI status with no
SHA cannot be recorded at all, which is what made "GREEN" stick around after the
commit it described had been superseded.

The rule that matters most is the last one: a workflow result for commit ``A``
is not evidence about commit ``B``. ``--linux-sha`` and ``--windows-sha`` are
required alongside their statuses, and if they do not equal the current HEAD the
status is written as ``STALE:<status>@<sha>`` rather than as the status itself.
A reader then cannot mistake it, and ``check_master_plan.py`` refuses a plan
whose STATUS claims a bare GREEN for a commit that is not HEAD.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))
for _package in (  # pragma: no cover - import plumbing
    "pz_agent_cli",
    "pz_agent_core",
    "pz_agent_mcp",
    "pz_agent_voice",
):
    _source = REPO_ROOT / "packages" / _package / "src"
    if _source.is_dir() and str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

from scripts import check_master_plan, master_report  # noqa: E402
from scripts._process import ENCODING, ERRORS  # noqa: E402

STATUS_PATH: Final = REPO_ROOT / "docs" / "control" / "STATUS.json"
PLAN_PATH: Final = REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml"

#: The only words a CI field may carry when it describes the current HEAD.
CI_STATES: Final = ("GREEN", "RED", "PENDING", "NOT_RUN")

#: What an RC may be. CURRENT requires its source commit to equal HEAD; there is
#: no fourth state, because "recent" is the word that let a stale ZIP be treated
#: as a certification.
RC_STATES: Final = ("CURRENT", "STALE", "NOT_BUILT")


def live_scenario_count() -> int:
    """How many live scenarios there are, asked of the catalogue that defines them.

    This was the literal ``20``. Two scenarios — ``S21_CRAFT`` and ``S22_BUILD``
    — were added afterwards and the literal did not move, so STATUS reported
    twenty scenarios awaiting a game while the runner owed twenty-two. It is the
    same defect the CLI already had once, where ``live-test status`` printed
    "All twenty" directly above a tally reading 22; that fix replaced the word in
    one file and this file kept the number.

    A number that is *counted* cannot drift, so the count is taken here and an
    unreadable catalogue is a hard failure rather than a fallback. A fallback
    would be a guess, and a guessed number in STATUS.json is precisely the class
    of value this script exists to make impossible.
    """
    from pz_agent_cli.livetest.scenarios import SCENARIO_IDS  # noqa: PLC0415

    return len(SCENARIO_IDS)


def head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding=ENCODING,
        errors=ERRORS,
        check=True,
        timeout=60,
    ).stdout.strip()


def _ci_field(status: str, sha: str, current: str) -> dict[str, Any]:
    """A CI verdict, marked stale unless it still describes HEAD's code.

    The predicate is :func:`check_master_plan.describes_the_code_at_head`, not
    SHA equality: committing this file moves HEAD, and a verdict that stopped
    belonging the moment it was recorded would be a verdict nothing can record.
    """
    if status not in CI_STATES:
        raise SystemExit(f"CI status must be one of {CI_STATES}, got {status!r}")
    belongs = sha == current or check_master_plan.describes_the_code_at_head(sha)
    return {
        "status": status if belongs else f"STALE:{status}",
        "commit": sha,
        "describes_current_head": belongs,
    }


def build(arguments: argparse.Namespace) -> dict[str, Any]:
    import yaml  # noqa: PLC0415

    document = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    tasks = check_master_plan.tasks_of(document)
    current = head()
    tally = master_report.counts(tasks)
    metrics = master_report.metric_values(document)

    rc_status = arguments.rc_status
    if rc_status not in RC_STATES:
        raise SystemExit(f"RC status must be one of {RC_STATES}, got {rc_status!r}")
    if rc_status == "CURRENT" and not (
        arguments.rc_sha == current
        or check_master_plan.describes_the_code_at_head(arguments.rc_sha or "")
    ):
        raise SystemExit(
            "an RC may only be CURRENT when it was built from the code at HEAD; "
            f"HEAD is {current[:8]} and the RC came from {(arguments.rc_sha or 'nothing')[:8]}"
        )

    return {
        "$comment": (
            "Generated by scripts/reconcile_status.py. Do not edit by hand: every "
            "field here is derived, and a hand-written value is the defect this "
            "file exists to prevent."
        ),
        "repository": "https://github.com/natural0101/poject-zombigpt",
        "branch": subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding=ENCODING,
            errors=ERRORS,
            check=True,
            timeout=60,
        ).stdout.strip(),
        "head_commit": current,
        "plan_of_record": "docs/control/MASTER_PLAN.yaml",
        "weighted_progress_percent": round(master_report.weighted(tasks), 2),
        "pass_weight": sum(int(t["weight"]) for t in tasks if t["status"] == "PASS"),
        "total_weight": sum(int(t["weight"]) for t in tasks),
        "total_tasks": len(tasks),
        "counts": tally,
        "metrics": {
            name: {"percent": round(percent, 1), "pass_weight": done, "total_weight": total}
            for name, (percent, done, total) in metrics.items()
        },
        "checks": {
            "pass": sum(1 for c in check_master_plan.checks_of(document) if c["status"] == "PASS"),
            "total": len(check_master_plan.checks_of(document)),
        },
        "linux_ci": _ci_field(arguments.linux, arguments.linux_sha, current),
        "windows_ci": _ci_field(arguments.windows, arguments.windows_sha, current),
        "release_candidate": {
            "status": rc_status,
            "source_commit": arguments.rc_sha,
            "workflow_run": arguments.rc_run,
            "archive_sha256": arguments.rc_sha256,
            "live_game": "NOT_RUN",
        },
        "live_scenarios": {"passed": 0, "failed": 0, "not_run": live_scenario_count()},
        "blockers_document": "docs/control/BLOCKERS.md",
    }


def _uncommitted_outside_control() -> list[str]:
    """Paths with uncommitted changes that STATUS would go stale against.

    ``docs/control/`` is excluded because a commit that only rewrites the control
    plane leaves every claim in STATUS true — which is exactly the exemption
    ``check_master_plan.py`` grants.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding=ENCODING,
        errors=ERRORS,
        check=False,
        timeout=120,
    )
    paths = []
    for line in result.stdout.splitlines():
        path = line[3:].strip()
        if path and not path.startswith("docs/control/"):
            paths.append(path)
    return sorted(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", required=True, choices=CI_STATES)
    parser.add_argument("--linux-sha", required=True)
    parser.add_argument("--windows", required=True, choices=CI_STATES)
    parser.add_argument("--windows-sha", required=True)
    parser.add_argument("--rc-status", required=True, choices=RC_STATES)
    parser.add_argument("--rc-sha", default=None)
    parser.add_argument("--rc-run", default=None)
    parser.add_argument("--rc-sha256", default=None)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="write STATUS even with uncommitted work outside docs/control/",
    )
    arguments = parser.parse_args()

    if not arguments.allow_dirty:
        dirty = _uncommitted_outside_control()
        if dirty:
            print(
                "REFUSED: there is uncommitted work outside docs/control/, so the commit "
                "that contains this STATUS would also change what it describes:\n  "
                + "\n  ".join(dirty[:10])
                + (f"\n  ... and {len(dirty) - 10} more" if len(dirty) > 10 else "")
                + "\n\nSTATUS.json names the commit it describes, and check_master_plan.py "
                "requires that commit to be an ancestor of HEAD with nothing outside "
                "docs/control/ changed since. Writing it now produces a file that is stale "
                "the moment it is committed — which has now happened twice, and CI caught "
                "it both times.\n\nCommit the code first, then run this, then commit "
                "docs/control/ alone. Use --allow-dirty only when you have a reason.",
                file=sys.stderr,
            )
            return 1

    document = build(arguments)
    STATUS_PATH.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"wrote {STATUS_PATH.relative_to(REPO_ROOT)}: "
        f"{document['weighted_progress_percent']}% at {document['head_commit'][:8]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
