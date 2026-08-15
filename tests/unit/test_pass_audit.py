"""The historical audit of every ``PASS``, run and shown to work.

``scripts/audit_pass.py`` asks the questions ``check_master_plan.py`` cannot:
that gate reads the tree as it stands today, so a task could name a commit that
predated the proof it claims and every check would pass. The audit asks the tree
as it *stood*.

It had never been run by anything. It is in no workflow, no ``check.sh`` step
and no test, and running it turned up eight invalid claims — which is what a
gate nobody runs is for. Of the eight:

* **seven were real.** The E11 packaging tasks name
  ``tests/unit/test_windows_workflow_contract.py`` as their proof, and that file
  was first added at ``f4fa0b2``, a *descendant* of every commit those tasks
  recorded as their verification. The behaviour was there and the test passes
  today; what was false was the claim about *where* it had been proved. Repaired
  by pointing ``verification_commit`` at ``f4fa0b2``, whose eight tests match the
  seven criteria one for one — not by withdrawing a PASS that a real proof backs,
  and not by relaxing the question.
* **one was a false accusation by the audit itself.** ``E06-M04-T001``'s proof
  sits exactly at the commit the plan names for it, and the audit looked at
  ``commit`` — the implementation — because it never read ``verification_commit``
  at all. The two fields exist precisely because those are different events;
  ``check_master_plan.py`` says so in as many words. An audit that accuses a
  sound claim gets argued with once and then switched off, so this counted as a
  defect in the audit and was fixed with the rest.

Both halves are asserted below. The clean run is the substantive assertion; the
planted ones are what stop it from being satisfied by an audit that lost the
ability to see anything. Nothing here uses a fixture plan: the questions are
about this repository's git history, so a hand-built document would answer them
with nothing.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))

from scripts import audit_pass  # noqa: E402

PLAN_PATH: Final = REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml"

#: A commit early in this branch's history, used to date a proof back before it
#: existed. Any commit that resolves and predates the tests would do; this one is
#: pinned so a failure names a fixed point rather than a moving one.
_EARLY: Final = "873037c081800cf4f4373b9307fc1cdff3140e99"


@pytest.fixture(scope="module")
def plan() -> dict[str, Any]:
    return dict(yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8")))


def _tasks(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        task
        for epic in document["epics"]
        for milestone in epic["milestones"]
        for task in milestone["tasks"]
    ]


def _first_passing_task_with_a_test(document: dict[str, Any]) -> dict[str, Any]:
    for task in _tasks(document):
        if task["status"] == "PASS" and (task.get("regression_test") or "").strip():
            return task
    raise AssertionError("no PASS task names a regression test; the audit would be vacuous")


def _invalid(document: dict[str, Any]) -> dict[str, list[str]]:
    return {v.task_id: v.problems for v in audit_pass.audit(document) if not v.valid}


def test_the_clone_can_answer_a_historical_question() -> None:
    """A shallow clone makes every assertion below vacuously true.

    Both workflows check out with ``fetch-depth: 0`` and this environment has the
    full history, so this is a guard against the day one of those changes, not a
    condition anybody expects to hit. Asserted rather than skipped: a skip here
    would quietly remove the whole file from the run.
    """
    assert not audit_pass._shallow(), "shallow clone: no historical question can be answered"
    assert audit_pass._commit_exists(_EARLY), f"{_EARLY[:8]} does not resolve"


def test_every_recorded_pass_survives_the_audit(plan: dict[str, Any]) -> None:
    """The substantive one. It was seven-invalid before this branch."""
    assert _invalid(plan) == {}


def test_the_audit_is_looking_at_something(plan: dict[str, Any]) -> None:
    """An audit of nothing returns no problems too."""
    verdicts = audit_pass.audit(plan)

    assert len(verdicts) == sum(1 for task in _tasks(plan) if task["status"] == "PASS")
    assert len(verdicts) > 300, f"only {len(verdicts)} PASS task(s) audited"
    assert all(verdict.commit for verdict in verdicts)


# ---------------------------------------------------------------------------
# each question, planted
# ---------------------------------------------------------------------------


def test_a_proof_dated_before_it_existed_is_caught(plan: dict[str, Any]) -> None:
    planted = copy.deepcopy(plan)
    task = _first_passing_task_with_a_test(planted)
    task["verification_commit"] = _EARLY

    problems = _invalid(planted).get(task["id"], [])

    assert problems, f"{task['id']} kept its PASS with its proof dated to {_EARLY[:8]}"
    assert "did not exist at" in problems[0]


def test_a_named_test_node_missing_from_the_file_is_caught(plan: dict[str, Any]) -> None:
    """One level finer: the file was there, the test in it was not."""
    planted = copy.deepcopy(plan)
    task = _first_passing_task_with_a_test(planted)
    path, _ = audit_pass._split_node(str(task["regression_test"]))
    task["regression_test"] = f"{path}::test_a_name_no_commit_ever_carried"

    problems = _invalid(planted).get(task["id"], [])

    assert problems, f"{task['id']} kept its PASS naming a test node that never existed"
    assert "did not contain" in problems[0] or "did not exist at" in problems[0]


def test_a_missing_evidence_path_is_caught(plan: dict[str, Any]) -> None:
    planted = copy.deepcopy(plan)
    task = _first_passing_task_with_a_test(planted)
    task["evidence"] = "docs/control/evidence/a-path-nobody-wrote.txt"

    problems = _invalid(planted).get(task["id"], [])

    assert problems, f"{task['id']} kept its PASS with evidence that is not on disk"
    assert any("evidence path" in problem for problem in problems)


def test_a_pass_standing_on_an_open_dependency_is_caught(plan: dict[str, Any]) -> None:
    planted = copy.deepcopy(plan)
    dependent = next(
        task
        for task in _tasks(planted)
        if task["status"] == "PASS" and (task.get("depends_on") or [])
    )
    open_id = str(dependent["depends_on"][0])
    for task in _tasks(planted):
        if task["id"] == open_id:
            task["status"] = "NOT_STARTED"

    problems = _invalid(planted).get(dependent["id"], [])

    assert problems, f"{dependent['id']} kept its PASS while {open_id} was reopened"
    assert any(open_id in problem for problem in problems)


def test_a_pass_with_no_commit_at_all_is_caught(plan: dict[str, Any]) -> None:
    planted = copy.deepcopy(plan)
    task = _first_passing_task_with_a_test(planted)
    task["commit"] = ""
    task["verification_commit"] = ""
    task["implementation_commit"] = ""

    assert _invalid(planted).get(task["id"]) == ["no commit recorded"]


# ---------------------------------------------------------------------------
# the false accusation the audit used to make
# ---------------------------------------------------------------------------


def test_a_proof_that_landed_after_its_implementation_is_not_an_accusation(
    plan: dict[str, Any],
) -> None:
    """``E06-M04-T001``, and the shape it stands for.

    A test written after the code it proves is ordinary work — the plan carries
    two commit fields precisely so that ordering is recordable rather than
    suspicious. Reading ``commit`` for the proof question turns every such task
    into an accusation, which is how this audit accused a sound claim.
    """
    task = next(t for t in _tasks(plan) if t["id"] == "E06-M04-T001")
    proof, _ = audit_pass._split_node(str(task["regression_test"]))

    assert task["verification_commit"] != task["implementation_commit"], (
        "this task no longer demonstrates the shape; pick another that does"
    )
    assert audit_pass.proving_commit(task) == task["verification_commit"]
    assert audit_pass._path_at(str(task["implementation_commit"]), proof) is None
    assert audit_pass._path_at(str(task["verification_commit"]), proof) is not None
    assert task["id"] not in _invalid(plan)


def test_a_task_naming_only_one_commit_is_still_audited(plan: dict[str, Any]) -> None:
    """The fallback is to ``commit``, not to letting the question go unasked."""
    planted = copy.deepcopy(plan)
    task = _first_passing_task_with_a_test(planted)
    task.pop("verification_commit", None)
    task["commit"] = _EARLY

    assert audit_pass.proving_commit(task) == _EARLY
    assert _invalid(planted).get(task["id"])


# ---------------------------------------------------------------------------
# and it is wired to something that runs it
# ---------------------------------------------------------------------------


def test_the_audit_is_a_step_of_the_local_gate() -> None:
    """It went unrun because nothing ran it; that is the part worth pinning.

    ``check.sh`` is what a person and CI both invoke, so naming the script there
    is what makes a future regression fail somewhere anybody looks.
    """
    gate = (REPO_ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")

    assert "scripts/audit_pass.py" in gate


def test_the_command_exits_nonzero_on_an_invalid_claim(tmp_path: Path) -> None:
    """End to end, as ``check.sh`` runs it: the process, not the function."""
    clean = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "audit_pass.py"), "--quiet"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "0 invalid" in clean.stdout
