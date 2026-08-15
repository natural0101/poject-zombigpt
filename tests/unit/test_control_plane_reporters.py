"""What a person reads about progress must be what the control plane records.

Two scripts count and two gate, and they are paired to two different plans:

===============================  ======================  ======================
plan of record                   counter                 gate
===============================  ======================  ======================
``docs/control/PLAN.md``         ``progress_report.py``  ``check_progress.py``
``docs/control/MASTER_PLAN.yaml``  ``master_report.py``  ``check_master_plan.py``
===============================  ======================  ======================

The first pair is retired. When the plan of record moved, ``STATUS.json`` was
regenerated in the new shape — no ``steps`` key at all — and nothing noticed,
because ``progress_report.py`` reads every field through ``.get`` with a
default. Run against the current file it printed, in full and without a warning:

    PROGRESS: 0%
    STEP: 1/100
    STATUS: NOT_STARTED
    RC ARTIFACT: None
    LIVE SCENARIOS: 0/20
    EVIDENCE: 0 path(s) recorded in docs/control/EVIDENCE_INDEX.md

at a commit where the same file recorded 73.31%, a fully identified release
candidate, 22 live scenarios and 400 passing tasks. ``--write`` — the form
``docs/control/COMMAND_LOG.md`` told an operator to run to "recount and store" —
then stored ``overall_percent: 0`` and six more zeroed keys into the file whose
own ``$comment`` says every field is derived and a hand-written value is the
defect it exists to prevent.

That is this project's FALSE SUCCESS family run backwards: not a green that was
never earned, but a set of confident zeroes standing where measured numbers
were. Nothing in the suite touched either script, so the only thing that would
have caught it is a test that *runs* them, which is what this file does. Every
assertion below is against a real subprocess invocation of the real script; none
of them reads the source.

Four questions, in both directions:

* the retired pair refuses the plan it does not count, and says which one does;
* refusing happens before ``--write`` can store anything;
* the retired pair still works on the plan it *was* written for, so the refusal
  is about the plan of record and not a blanket "always fail" that would pass
  this file while making the script useless;
* the live report prints the figures ``STATUS.json`` records — the check whose
  absence let the false report stand.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPTS: Final = REPO_ROOT / "scripts"
STATUS_PATH: Final = REPO_ROOT / "docs" / "control" / "STATUS.json"

#: The retired pair, with the successor each refusal has to name.
RETIRED: Final = (
    ("progress_report.py", "scripts/master_report.py", 1),
    ("check_progress.py", "scripts/check_master_plan.py", 2),
)


def _run(script: Path, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _status() -> dict[str, Any]:
    document: Any = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _hundred_step_status() -> dict[str, Any]:
    """A ``STATUS.json`` in the shape the retired pair was written for.

    Deliberately minimal and entirely unstarted: this exists to show the scripts
    still run end to end on their own plan, not to re-test their counting rules.
    """
    return {
        "plan_of_record": "docs/control/PLAN.md",
        "branch": "dev",
        "head_commit": "0" * 40,
        "overall_percent": 0,
        "remote_percent": 0,
        "live_game_percent": 0,
        "release_percent": 0,
        "linux_ci": {"status": "PENDING"},
        "windows_ci": {"status": "PENDING"},
        "live_scenarios": {"passed": 0, "failed": 0, "not_run": 22},
        "steps": [
            {"id": number, "title": f"step {number}", "status": "NOT_STARTED"}
            for number in range(1, 101)
        ],
    }


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A tree the retired scripts resolve as their repository root.

    Both take ``REPO_ROOT`` from ``__file__.parents[1]``, so a copy of the script
    under ``scripts/`` reads the ``docs/control/STATUS.json`` placed beside it
    and never the repository's own.
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs" / "control").mkdir(parents=True)
    for name, _successor, _code in RETIRED:
        shutil.copy(SCRIPTS / name, tmp_path / "scripts" / name)
    return tmp_path


def _write_status(root: Path, document: dict[str, Any]) -> Path:
    path = root / "docs" / "control" / "STATUS.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# the retired pair refuses a plan it does not read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "successor", "code"), RETIRED)
def test_the_retired_script_refuses_the_plan_of_record_it_does_not_read(
    name: str, successor: str, code: int
) -> None:
    """Against the repository's own ``STATUS.json``, as an operator would run it."""
    result = _run(SCRIPTS / name, cwd=REPO_ROOT)

    assert result.returncode == code, result.stdout + result.stderr
    assert "docs/control/MASTER_PLAN.yaml" in result.stderr
    assert successor in result.stderr
    # The exact string the defect produced. A refusal that still printed the
    # report would be no better than the report.
    assert "PROGRESS:" not in result.stdout
    assert "0%" not in result.stdout


def test_the_retired_counter_stores_nothing_into_a_plan_it_cannot_count(scratch: Path) -> None:
    """``--write`` is the documented form, and the one that corrupted the file."""
    status = _write_status(scratch, _status())
    before = status.read_bytes()

    result = _run(scratch / "scripts" / "progress_report.py", "--write", cwd=scratch)

    assert result.returncode != 0
    assert status.read_bytes() == before, "--write edited a status file it refused to read"


def test_the_refusal_names_a_status_file_that_claims_no_plan(scratch: Path) -> None:
    """An unnamed plan of record is not this one either, and must not default to it."""
    _write_status(scratch, {"branch": "dev"})

    result = _run(scratch / "scripts" / "progress_report.py", cwd=scratch)

    assert result.returncode != 0
    assert "unnamed" in result.stderr
    assert "PROGRESS:" not in result.stdout


# ---------------------------------------------------------------------------
# ... and still works on the plan it was written for
# ---------------------------------------------------------------------------


def test_the_retired_counter_still_counts_its_own_plan(scratch: Path) -> None:
    """The other direction: without this, refusing unconditionally would pass.

    A guard that always says no is indistinguishable from the defect it replaced
    when only the refusal is asserted, so the accepting case is asserted too.
    """
    _write_status(scratch, _hundred_step_status())

    result = _run(scratch / "scripts" / "progress_report.py", cwd=scratch)

    assert result.returncode == 0, result.stderr
    assert "PROGRESS: 0%" in result.stdout
    assert "STEP: 1/100" in result.stdout
    # Counted from the tally rather than the literal 20 that used to be printed
    # here regardless of what the catalogue defines.
    assert "LIVE SCENARIOS: 0/22" in result.stdout


def test_the_retired_gate_still_judges_its_own_plan(scratch: Path) -> None:
    _write_status(scratch, _hundred_step_status())

    result = _run(scratch / "scripts" / "check_progress.py", cwd=scratch)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "admissible" in result.stdout


def test_the_retired_gate_separates_refused_from_unreadable(scratch: Path) -> None:
    """Exit 2 is documented as "cannot read", exit 1 as "refused"."""
    unearned = _hundred_step_status()
    unearned["steps"][0]["status"] = "PASS"  # PASS with no evidence and no commit

    _write_status(scratch, unearned)
    refused = _run(scratch / "scripts" / "check_progress.py", cwd=scratch)

    (scratch / "docs" / "control" / "STATUS.json").write_text("{not json", encoding="utf-8")
    unreadable = _run(scratch / "scripts" / "check_progress.py", cwd=scratch)

    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert unreadable.returncode == 2, unreadable.stdout + unreadable.stderr


# ---------------------------------------------------------------------------
# the live report agrees with the file
# ---------------------------------------------------------------------------


def test_the_live_report_prints_the_figures_the_status_file_records() -> None:
    """The check whose absence let a counter print 0% for a 73%-complete tree.

    ``master_report.py`` derives from ``MASTER_PLAN.yaml`` and
    ``reconcile_status.py`` writes ``STATUS.json`` from the same plan, so the two
    are independent readings of one source and must land on the same numbers.
    Run rather than read: the point is what the command prints, since that is
    what a person acts on.
    """
    result = _run(SCRIPTS / "master_report.py", "--json", cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    reported = json.loads(result.stdout)
    recorded = _status()

    assert reported["overall_weighted_progress"] == recorded["weighted_progress_percent"]
    assert reported["total_tasks"] == recorded["total_tasks"]
    assert reported["total_weight"] == recorded["total_weight"]
    assert reported["counts"] == recorded["counts"]
    assert reported["metrics"] == {
        name: values["percent"] for name, values in recorded["metrics"].items()
    }


def test_the_printed_report_carries_the_same_overall_figure_as_the_json() -> None:
    """``--json`` is what the test above reads; a human reads the other form."""
    printed = _run(SCRIPTS / "master_report.py", cwd=REPO_ROOT)
    structured = _run(SCRIPTS / "master_report.py", "--json", cwd=REPO_ROOT)
    assert printed.returncode == 0, printed.stderr
    assert structured.returncode == 0, structured.stderr

    overall = json.loads(structured.stdout)["overall_weighted_progress"]
    weights = json.loads(structured.stdout)["total_weight"]

    assert f"OVERALL WEIGHTED PROGRESS: {overall:.1f}%" in printed.stdout
    assert f"/{weights} weight)" in printed.stdout
