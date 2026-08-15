"""``verify_carryover.py``: what it may confirm, and what it may not touch.

The script re-derives which tasks deserve a ``PASS`` by *running their tests*.
Nothing is inherited — that is its whole reason to exist, and 400 of the plan's
claims went through it. It was also the last script in ``scripts/`` that no test
ran, and running it turned up two defects:

**It could mint a ``PASS`` for a task only a running game can close.**
``check_master_plan.py`` refuses a ``local`` task marked ``PASS`` in as many
words — nothing in this environment can produce its evidence. ``evaluate()`` did
not know that rule, so a live task whose named regression test happens to pass on
Linux and whose evidence path happens to exist came back ``PASS``: one script
writing precisely what the other exists to refuse. Demonstrated against the real
pair before the fix — ``evaluate`` said ``PASS``, ``check_master_plan.problems``
answered *"E14-M01-T001 is a local task marked PASS; nothing in this environment
can produce its evidence"* about the very same task.

Not reachable in today's plan: no ``local`` task has both an existing test file
and an existing evidence path, measured over all 84 of them. Closed anyway,
because "not reachable today" is a fact about the plan rather than about this
code, and the plan is edited every iteration.

**It called a pytest that does not exist on Windows.** ``.venv/bin/pytest`` is
the POSIX venv layout; the Windows entry point is ``.venv/Scripts/pytest.exe``.
The same class of defect as the decoding one that took the release build red two
commits ago, found by looking rather than by waiting. It runs
``sys.executable -m pytest`` now, which is right on either platform and needs no
venv at all.

What was checked and found sound, so it is recorded rather than left implied:
the refusal of a green run over zero executed tests genuinely works. pytest exits
0 when every test in a target skips, and a ``PASS`` resting on that would be the
exact substitution the plan forbids. Asserted below against a target built to
skip everything.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))

from scripts import check_master_plan, verify_carryover  # noqa: E402

PLAN_PATH: Final = REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml"

#: A real, cheap, passing test node — used as a stand-in "proof that passes".
_A_PASSING_NODE: Final = (
    "tests/unit/test_pass_audit.py::test_the_clone_can_answer_a_historical_question"
)

#: A path that exists, to stand in for "evidence is on disk".
_AN_EXISTING_PATH: Final = "docs/control/MASTER_PLAN.yaml"


@pytest.fixture(autouse=True)
def _no_cached_runs() -> Any:
    """``_run`` memoises by target; a stale entry would make a test lie."""
    verify_carryover._CACHE.clear()
    yield
    verify_carryover._CACHE.clear()


def _task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": "E14-M01-T001",
        "owner": "remote",
        "band": "live",
        "weight": 9,
        "subsystem": "live",
        "action": "run the scenario",
        "verify_command": "",
        "regression_test": _A_PASSING_NODE,
        "evidence": _AN_EXISTING_PATH,
        "depends_on": [],
        "status": "NOT_STARTED",
        "commit": "",
        "ci_url": "",
        "reason": "",
    }
    task.update(overrides)
    return task


# ---------------------------------------------------------------------------
# the hole: a task this environment may not judge
# ---------------------------------------------------------------------------


def test_a_local_task_is_never_given_a_status() -> None:
    """The defect, stated as the rule it broke."""
    status, reason = verify_carryover.evaluate(_task(owner="local"), "HEAD")

    assert status is None, f"a local task was decided here as {status}"
    assert "local" in reason


def test_the_same_task_owned_remotely_is_judged_normally() -> None:
    """The other direction, without which refusing everything would pass.

    Identical in every field but ``owner``, so what is demonstrated is that the
    ownership decides it and not some other property of the fixture.
    """
    status, _ = verify_carryover.evaluate(_task(owner="remote"), "HEAD")

    assert status == "PASS"


def test_what_this_script_would_confirm_the_plan_gate_would_accept() -> None:
    """The invariant that was violated, asserted directly on both scripts.

    A writer that can write what the reader refuses is a contradiction whichever
    one is right, and this is the assertion that would have caught it without
    anybody thinking of ``owner`` at all.
    """
    for owner in ("remote", "local"):
        task = _task(owner=owner)
        status, _ = verify_carryover.evaluate(task, "HEAD")
        if status != "PASS":
            continue
        document = {
            "statuses": ["PASS", "NOT_STARTED", "IN_PROGRESS", "FAIL", "BLOCKED"],
            "epics": [
                {
                    "id": "E14",
                    "required_ci": "",
                    "milestones": [
                        {
                            "id": "M01",
                            # Provenance filled in, because a ``live`` band PASS
                            # needs both commits and this test is about
                            # ownership alone. Worth noting in passing:
                            # ``--apply`` writes neither field, so a task it
                            # confirms in those bands is refused downstream
                            # until someone records where it was implemented and
                            # proved. That refusal is the gate working.
                            "tasks": [
                                dict(
                                    task,
                                    status="PASS",
                                    commit="HEAD",
                                    implementation_commit="HEAD",
                                    verification_commit="HEAD",
                                )
                            ],
                            "checks": [],
                        }
                    ],
                }
            ],
        }
        assert check_master_plan.problems(document, windows_green=True) == [], (
            f"verify_carryover would confirm a {owner} task the plan gate refuses"
        )


def test_apply_leaves_a_local_task_exactly_as_it_found_it(tmp_path: Path) -> None:
    """``--apply`` is the writing form; the refusal has to hold there too."""
    recorded = _task(owner="local", status="NOT_STARTED", reason="", commit="")
    before = json.dumps(recorded, sort_keys=True)

    status, reason = verify_carryover.evaluate(recorded, "HEAD")
    if status is not None:  # pragma: no cover - the defect, were it back
        recorded["status"], recorded["reason"] = status, reason

    assert json.dumps(recorded, sort_keys=True) == before


def test_no_local_task_in_the_real_plan_could_have_reached_the_hole() -> None:
    """The measurement the fix is documented with, kept honest.

    If this ever fails the hole became reachable, which is a reason to be glad
    it was closed rather than a reason to change the number.
    """
    document = yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))
    tasks = [t for e in document["epics"] for m in e["milestones"] for t in m["tasks"]]
    local = [t for t in tasks if t["owner"] == "local"]

    assert local, "no local tasks at all; this test is measuring nothing"
    reachable = [
        task["id"]
        for task in local
        if (test := (task.get("regression_test") or "").strip())
        and (REPO_ROOT / test.split("::", 1)[0]).exists()
        and (evidence := (task.get("evidence") or "").split("#", 1)[0])
        and (REPO_ROOT / evidence).exists()
    ]

    assert reachable == [], f"{len(reachable)} local task(s) now satisfy every other condition"


# ---------------------------------------------------------------------------
# a green run over nothing is not a proof
# ---------------------------------------------------------------------------


def test_a_target_where_everything_skipped_is_refused(tmp_path: Path) -> None:
    """pytest exits 0 on an all-skipped target; this is why exit code is not enough."""
    target = tmp_path / "test_all_skipped.py"
    target.write_text(
        "import pytest\n\n"
        '@pytest.mark.skip(reason="nothing here runs")\n'
        "def test_one() -> None:\n"
        "    raise AssertionError\n",
        encoding="utf-8",
    )

    bare = subprocess.run(
        [sys.executable, "-m", "pytest", str(target), "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
    )
    passed, why = verify_carryover._run(str(target))

    assert bare.returncode == 0, "pytest no longer exits 0 on an all-skipped target"
    assert passed is False, "a green run over zero executed tests was accepted"
    assert "nothing was observed" in why


def test_a_target_that_really_runs_is_accepted(tmp_path: Path) -> None:
    """The control: the refusal above must be about skipping, not about tmp files."""
    target = tmp_path / "test_runs.py"
    target.write_text("def test_one() -> None:\n    assert True\n", encoding="utf-8")

    passed, why = verify_carryover._run(str(target))

    assert passed is True, why


def test_a_failing_target_is_refused_with_the_failure_named(tmp_path: Path) -> None:
    target = tmp_path / "test_fails.py"
    target.write_text("def test_one() -> None:\n    assert False\n", encoding="utf-8")

    passed, why = verify_carryover._run(str(target))

    assert passed is False
    assert "test_one" in why or "FAILED" in why or why.startswith("exit ")


# ---------------------------------------------------------------------------
# the interpreter it reaches for
# ---------------------------------------------------------------------------


def test_it_does_not_reach_for_a_posix_only_venv_path() -> None:
    """``.venv/bin/pytest`` does not exist on Windows, where it is ``Scripts/``.

    Read from the source because there is no way to observe the other platform
    from here; the behavioural half is every ``_run`` test above, which now
    passes on whichever interpreter is running them.
    """
    source = (REPO_ROOT / "scripts" / "verify_carryover.py").read_text(encoding="utf-8")

    assert '".venv"' not in source
    assert "sys.executable" in source


def test_the_other_two_reasons_a_task_is_not_confirmed(tmp_path: Path) -> None:
    """A named test that is not on disk, and evidence that is not."""
    missing_test, _ = verify_carryover.evaluate(
        _task(regression_test="tests/unit/test_nobody_wrote_this.py"), "HEAD"
    )
    missing_evidence, _ = verify_carryover.evaluate(
        _task(evidence="docs/control/evidence/nobody-wrote-this.txt"), "HEAD"
    )
    no_test, reason = verify_carryover.evaluate(_task(regression_test=""), "HEAD")

    assert missing_test == "NOT_STARTED"
    assert missing_evidence == "IN_PROGRESS"
    assert no_test == "IN_PROGRESS"
    assert "no regression test" in reason
