"""The plan of record, and the gate that refuses a claim it cannot support.

``docs/control/MASTER_PLAN.yaml`` is the only place this project states how far
along it is, so the interesting question is not whether the number is high — it
is whether the number *can* be wrong in a direction nobody notices. Everything
here is aimed at that.

Four things are checked, and the third is the point of the file:

1. **The model.** Weight bands are disjoint and enforced, so a documentation
   task cannot quietly carry a live task's weight.
2. **The arithmetic.** Progress is summed weight of PASS over summed weight of
   all, derived on every read. Nothing stores a percentage, so nothing can be
   stale — and this file asserts that no percentage is stored, not merely that
   the derived one is right.
3. **Each refusal in the gate, proved by planting the violation it exists for.**
   A gate is only worth its refusals, and a refusal nobody has ever seen fire is
   indistinguishable from one that does not work. Every case in
   :func:`check_master_plan.problems` gets a plan constructed to violate exactly
   it, and the test asserts both that the gate refuses *and* that it refuses for
   the right reason — a gate that rejects everything would otherwise pass all of
   these.
4. **The closing rule for an epic**, which is deliberately not "enough tasks
   passed": the recurring defect in this repository is a subsystem that is
   complete, tested, green and connected to nothing, and a proportion of passing
   tasks is precisely the metric that cannot see it.

The real plan is read in a handful of places too, because a model that only ever
sees hand-built fixtures is a model that has never met its own data.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPTS: Final = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))

from scripts import build_master_plan, check_master_plan, master_report  # noqa: E402
from scripts.plan_model import (  # noqa: E402  # noqa: E402
    BANDS,
    METRICS,
    STATUSES,
    Check,
    Epic,
    Milestone,
    Task,
    epic_closed,
    weight_band,
)

PLAN_PATH: Final = REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml"

#: A path that certainly exists, for the fields the gate resolves on disk. Using
#: the plan itself keeps the fixture honest without inventing a file.
REAL_PATH: Final = "docs/control/MASTER_PLAN.yaml"
REAL_TEST: Final = "tests/unit/test_master_plan.py"


@pytest.fixture(scope="module")
def real_plan() -> dict[str, Any]:
    """The plan as it stands on disk."""
    return yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _task(identifier: str, **overrides: Any) -> dict[str, Any]:
    """A task dictionary that the gate accepts, before an override breaks it."""
    task = Task(
        id=identifier,
        subsystem="fixture",
        action="do the one thing this task is",
        weight=5,
        band="transport",
        owner="remote",
        pass_criterion="the postcondition is observed",
        verify_command="pytest tests/unit/test_master_plan.py",
        regression_test=REAL_TEST,
        evidence=REAL_PATH,
        status="PASS",
        commit="0" * 40,
    ).to_dict()
    task.update(overrides)
    return task


def _plan(*tasks: dict[str, Any], checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A whole plan document around *tasks*, shaped exactly like the real one."""
    return {
        "format": 1,
        "statuses": list(STATUSES),
        "metrics": {name: list(epics) for name, epics in METRICS.items()},
        "epics": [
            {
                "id": "E01",
                "title": "fixture",
                "subsystem": "fixture",
                "integration_scenario": "the fixture is exercised end to end",
                "required_ci": ".github/workflows/ci.yml",
                "milestones": [
                    {
                        "id": "E01-M01",
                        "title": "fixture",
                        "tasks": list(tasks),
                        "checks": checks or [],
                    }
                ],
            }
        ],
    }


def _refusals(document: dict[str, Any], *, windows_green: bool = True) -> list[str]:
    return check_master_plan.problems(document, windows_green=windows_green)


class TestTheWeightBands:
    """A weight is a judgement about size, so it has to be checkable."""

    def test_a_weight_inside_its_band_is_legal(self) -> None:
        assert weight_band(1, "doc")
        assert weight_band(2, "doc")
        assert weight_band(10, "live")

    def test_a_weight_outside_its_band_is_not(self) -> None:
        assert not weight_band(3, "doc")
        assert not weight_band(0, "doc")
        assert not weight_band(8, "live")

    def test_documentation_cannot_carry_a_live_weight(self) -> None:
        """The specific confusion the bands exist to prevent.

        Writing a paragraph and getting a scenario to pass against a running game
        are both "one task". Every weight legal for ``live`` must be illegal for
        ``doc``, or the plan can call them the same size.
        """
        low, high = BANDS["live"]
        for weight in range(low, high + 1):
            assert not weight_band(weight, "doc"), (
                f"weight {weight} is legal for both live and doc, so a documentation "
                f"task can be recorded with the weight of a live one"
            )

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("doc", "transport"),
            ("doc", "integration"),
            ("doc", "live"),
            ("transport", "integration"),
            ("transport", "live"),
            ("integration", "live"),
        ],
    )
    def test_the_four_named_kinds_cannot_share_a_weight(self, left: str, right: str) -> None:
        """Documentation, RPC transport, MCP end-to-end and live tests, pairwise.

        These four are named because they are the four this project kept treating
        as interchangeable. Their bands must not overlap at all: if a transport
        task may be a 6 and an E2E task may also be a 6, then two of the four are
        recorded as the same size and the weighting has stopped saying anything
        about them.

        ``transport`` and ``integration`` overlapped at 6 and 7 until the bands
        were narrowed to (5,6) and (7,8), which moved 48 recorded weights.

        Other pairs are not asserted here — ``portability`` and ``packaging`` do
        share (3,5). That is not covered by this rule, and pretending otherwise
        would make this test enforce something nobody decided.
        """
        low, high = BANDS[left]
        overlap = [weight for weight in range(low, high + 1) if weight_band(weight, right)]
        assert not overlap, (
            f"{left} and {right} both admit weight(s) {overlap}, so a {left} task and "
            f"a {right} task can be recorded as the same size"
        )

    def test_the_real_plan_respects_every_band(self, real_plan: dict[str, Any]) -> None:
        offenders = [
            f"{task['id']} weight {task['weight']} in band {task['band']}"
            for task in check_master_plan.tasks_of(real_plan)
            if not weight_band(task["weight"], task["band"])
        ]
        assert not offenders, offenders


class TestTheArithmetic:
    """Progress is derived. There is no stored number to go stale."""

    def test_progress_is_passing_weight_over_total_weight(self) -> None:
        tasks = [
            _task("A", weight=3, band="portability", status="PASS"),
            _task("B", weight=7, band="transport", status="NOT_STARTED"),
        ]
        assert master_report.weighted(tasks) == pytest.approx(30.0)

    def test_a_plan_with_nothing_done_is_zero_not_an_error(self) -> None:
        tasks = [_task("A", status="NOT_STARTED"), _task("B", status="FAIL")]
        assert master_report.weighted(tasks) == 0.0

    def test_an_empty_plan_does_not_divide_by_zero(self) -> None:
        assert master_report.weighted([]) == 0.0

    def test_task_count_and_weight_disagree_on_purpose(self) -> None:
        """Half the tasks is not half the weight, and the plan must show that.

        This is the assertion behind retiring the step-counting model: one heavy
        task passing while one light one does not is 50% of tasks and 90% of
        weight, and only the second figure means anything.
        """
        tasks = [
            _task("heavy", weight=9, band="live", status="PASS"),
            _task("light", weight=1, band="doc", status="NOT_STARTED"),
        ]
        by_count = sum(1 for t in tasks if t["status"] == "PASS") / len(tasks) * 100
        assert by_count == pytest.approx(50.0)
        assert master_report.weighted(tasks) == pytest.approx(90.0)

    def test_no_percentage_is_stored_anywhere_in_the_plan(self) -> None:
        """A number on disk is a number that can be wrong. There is none."""
        text = PLAN_PATH.read_text(encoding="utf-8")
        assert "progress:" not in text.lower(), (
            "the plan stores a progress figure. Derived is the whole point: a stored "
            "one disagrees with the tasks the moment a status changes."
        )

    def test_a_metric_covers_only_its_own_epics(self, real_plan: dict[str, Any]) -> None:
        """Seven figures, so one subsystem at zero cannot hide inside an average."""
        values = master_report.metric_values(real_plan)
        assert set(values) == set(METRICS)
        for name, epic_ids in METRICS.items():
            expected = sum(
                task["weight"]
                for epic in real_plan["epics"]
                if epic["id"] in epic_ids
                for milestone in epic["milestones"]
                for task in milestone["tasks"]
            )
            assert values[name][2] == expected, f"{name} sums the wrong epics"

    def test_the_live_metric_is_not_diluted_by_the_others(self, real_plan: dict[str, Any]) -> None:
        """The claim the user asked to be impossible to hide.

        LIVE_GAME_VALIDATION must be reported over its own epics only. If it were
        folded into the overall figure it would read as progress that nothing has
        observed, because nothing here can run Project Zomboid.
        """
        live_percent, _, live_total = master_report.metric_values(real_plan)["LIVE_GAME_VALIDATION"]
        assert live_total > 0, "the live metric covers no weight at all"
        overall = master_report.weighted(check_master_plan.tasks_of(real_plan))
        if live_percent == 0.0:
            assert overall > live_percent, (
                "the overall figure equals the live figure, so one of them is not "
                "measuring what it says"
            )


class TestEveryRefusalFires:
    """Each refusal, proved by planting the violation it exists to catch.

    A refusal that has never fired is indistinguishable from one that does not
    work. Each test asserts the gate refuses *and* names the right cause, because
    a gate that rejected everything would otherwise satisfy all of them.
    """

    def test_a_clean_plan_is_admitted(self) -> None:
        """The control. Without it every test below could pass on a broken gate."""
        assert _refusals(_plan(_task("E01-M01-T001"))) == []

    def test_pass_with_no_evidence(self) -> None:
        found = _refusals(_plan(_task("E01-M01-T001", evidence="")))
        assert any("no evidence path" in problem for problem in found), found

    def test_pass_with_no_commit(self) -> None:
        found = _refusals(_plan(_task("E01-M01-T001", commit=None)))
        assert any("no commit" in problem for problem in found), found

    def test_pass_whose_evidence_path_is_not_on_disk(self) -> None:
        found = _refusals(_plan(_task("E01-M01-T001", evidence="docs/control/no-such-file.txt")))
        assert any("evidence does not exist" in problem for problem in found), found

    def test_pass_whose_regression_test_is_not_on_disk(self) -> None:
        found = _refusals(_plan(_task("E01-M01-T001", regression_test="tests/unit/test_nope.py")))
        assert any("regression test does not exist" in problem for problem in found), found

    def test_pass_with_an_unmet_dependency(self) -> None:
        found = _refusals(
            _plan(
                _task("E01-M01-T001", status="NOT_STARTED"),
                _task("E01-M01-T002", depends_on=["E01-M01-T001"]),
            )
        )
        assert any("depends on E01-M01-T001" in problem for problem in found), found

    def test_a_local_task_cannot_pass_from_this_environment(self) -> None:
        """The rule that keeps the live epics honest.

        Nothing in this container can start Project Zomboid, so a ``local`` task
        marked PASS here is a claim about a machine this process has never seen.
        """
        found = _refusals(_plan(_task("E01-M01-T001", owner="local")))
        assert any("local task marked PASS" in problem for problem in found), found

    def test_a_windows_claim_on_a_red_windows_workflow(self) -> None:
        task = _task("E01-M01-T001", verify_command="a windows-latest run reports 0 failed")
        document = _plan(task)
        document["epics"][0]["required_ci"] = ".github/workflows/windows.yml"
        found = _refusals(document, windows_green=False)
        assert any("Windows workflow is not green" in problem for problem in found), found

    def test_the_same_windows_claim_is_admitted_when_the_workflow_is_green(self) -> None:
        """The other direction: the refusal must be about the workflow, not the word."""
        task = _task("E01-M01-T001", verify_command="a windows-latest run reports 0 failed")
        document = _plan(task)
        document["epics"][0]["required_ci"] = ".github/workflows/windows.yml"
        assert _refusals(document, windows_green=True) == []

    def test_a_check_that_passes_with_no_evidence(self) -> None:
        check = Check(
            id="E01-M01-C001",
            statement="the parts are joined",
            command="pytest tests/contract",
            status="PASS",
            evidence="",
        ).to_dict()
        found = _refusals(_plan(_task("E01-M01-T001"), checks=[check]))
        assert any("PASS check with no evidence" in problem for problem in found), found

    def test_a_status_outside_the_five(self) -> None:
        found = _refusals(_plan(_task("E01-M01-T001", status="MOSTLY_DONE")))
        assert any("is unknown" in problem for problem in found), found

    def test_a_not_started_task_is_not_asked_for_evidence(self) -> None:
        """The refusals are about *claims*. An open task claims nothing."""
        open_task = _task("E01-M01-T001", status="NOT_STARTED", evidence="", commit=None)
        assert _refusals(_plan(open_task)) == []


class TestTheGateAsACommand:
    """The gate has to refuse from the command line, not only as a function."""

    def test_the_real_plan_is_admissible(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "check_master_plan.py")],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
            timeout=300,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    def test_a_regeneration_keeps_the_checks_evidence(self) -> None:
        """Check evidence is decided by a run, and a rebuild must not unsay it.

        The generator carried task statuses but only a check's *status*: the
        first regeneration after the 48 checks were established wiped every
        check's evidence, and the gate then refused each PASS check as
        evidence-free. Status and evidence travel together or not at all.
        """
        rebuilt = build_master_plan.build()
        stripped = [
            check["id"]
            for epic in rebuilt["epics"]
            for milestone in epic["milestones"]
            for check in milestone["checks"]
            if check["status"] == "PASS" and not check.get("evidence")
        ]
        assert stripped == [], "these PASS checks lost their evidence in regeneration"

    def test_the_plan_has_not_drifted_from_the_definitions_that_generate_it(self) -> None:
        """A hand-edited plan is a plan the generator will silently overwrite."""
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "build_master_plan.py"), "--check"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
            timeout=300,
        )
        assert result.returncode == 0, (
            "MASTER_PLAN.yaml has drifted from scripts/plan_epics_*.py:\n"
            f"{result.stdout}\n{result.stderr}"
        )


class TestStatusCannotBeWrittenIntoStalenessRE:
    """The generator refuses when the commit containing it would also move.

    ``STATUS.json`` names the commit it describes, and the gate above requires
    that commit to be an ancestor of HEAD with nothing outside ``docs/control/``
    changed since. Regenerating STATUS while code is uncommitted therefore
    produces a file that is stale the moment it is committed — the two are the
    same commit, and it changed both.

    That is not a hypothetical. It reached CI twice: once at ``ed35e81`` and
    again at ``e481288``, each time as a red ``python 3.11`` and ``3.12`` on a
    tree whose 6487 other tests passed. A rule a careful operator keeps tripping
    is a rule that needs a guard at the point of the mistake rather than a
    sentence in a document, so the generator now refuses and says what order to
    do it in.
    """

    def test_the_generator_refuses_while_code_is_uncommitted(self, tmp_path: Path) -> None:
        """Proved against a scratch repository, not against this one.

        Running it here would depend on whether *this* tree happens to be dirty,
        which is the kind of test that passes for a reason unrelated to what it
        claims.
        """
        repo = _scratch_repo(tmp_path)
        (repo / "packages" / "thing.py").write_text("x = 2\n", encoding="utf-8")

        result = _reconcile_in(repo)

        assert result.returncode == 1, result.stdout
        assert "uncommitted work outside docs/control/" in result.stderr
        assert "packages/thing.py" in result.stderr
        assert "Commit the code first" in result.stderr

    def test_it_writes_when_only_the_control_plane_is_dirty(self, tmp_path: Path) -> None:
        """The other direction, so the guard is about staleness and not about dirt.

        A commit that only rewrites ``docs/control/`` leaves every claim in
        STATUS true, which is exactly the exemption the gate grants — so the
        generator must not refuse here.
        """
        repo = _scratch_repo(tmp_path)
        (repo / "docs" / "control" / "NOTES.md").write_text("scratch\n", encoding="utf-8")

        result = _reconcile_in(repo)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    def test_allow_dirty_is_an_escape_hatch_and_not_the_default(self, tmp_path: Path) -> None:
        repo = _scratch_repo(tmp_path)
        (repo / "packages" / "thing.py").write_text("x = 3\n", encoding="utf-8")

        assert _reconcile_in(repo).returncode == 1
        assert _reconcile_in(repo, "--allow-dirty").returncode == 0


class TestAVerdictSurvivesItsOwnRecording:
    """GREEN and CURRENT are judged by what the code did, not by SHA equality.

    The first version of the CI-verdict rule read ``entry.commit != HEAD`` — an
    unsatisfiable rule of exactly the shape the ``head_commit`` rule's own
    comment warns about. Recording a green run requires a commit; the commit
    moves HEAD; the recorded verdict is refused by the very commit that did
    nothing but record it. That shipped once, at ``3c70712``: a STATUS written
    at the verified commit failed its own CI. So both rules now share the
    ``head_commit`` predicate: the verdict's commit must be an ancestor of HEAD
    with nothing outside ``docs/control/`` changed since.
    """

    @staticmethod
    def _commit(repo: Path, message: str) -> None:
        for command in (["git", "add", "-A"], ["git", "commit", "-q", "-m", message]):
            subprocess.run(command, cwd=repo, check=True, capture_output=True, timeout=120)

    @staticmethod
    def _describes(repo: Path, sha: str) -> bool:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, '.'); "
                "from scripts.check_master_plan import describes_the_code_at_head; "
                f"sys.exit(0 if describes_the_code_at_head({sha!r}) else 1)",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert result.returncode in (0, 1), result.stderr
        return result.returncode == 0

    def test_a_control_plane_commit_does_not_unsay_a_verdict(self, tmp_path: Path) -> None:
        """The recording commit itself — the shape that failed at 3c70712."""
        repo = _scratch_repo(tmp_path)
        verified = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        (repo / "docs" / "control" / "STATUS.json").write_text("{}\n", encoding="utf-8")
        self._commit(repo, "record the verdict")

        assert self._describes(repo, verified)

    def test_a_code_commit_does_unsay_it(self, tmp_path: Path) -> None:
        repo = _scratch_repo(tmp_path)
        verified = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        (repo / "packages" / "thing.py").write_text("x = 2\n", encoding="utf-8")
        self._commit(repo, "change the code")

        assert not self._describes(repo, verified)

    def test_the_generator_agrees_a_recorded_verdict_is_still_green(self, tmp_path: Path) -> None:
        """Reconciling after the recording commit keeps GREEN, not STALE:GREEN.

        Without this, the generator and the gate would disagree about the same
        history: the gate admitting a verdict the generator re-marks stale.
        """
        repo = _scratch_repo(tmp_path)
        verified = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        (repo / "docs" / "control" / "NOTES.md").write_text("recorded\n", encoding="utf-8")
        self._commit(repo, "control plane only")

        result = _reconcile_in_with_shas(repo, "GREEN", verified)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        written = json.loads(
            (repo / "docs" / "control" / "STATUS.json").read_text(encoding="utf-8")
        )
        assert written["linux_ci"]["status"] == "GREEN"
        assert written["linux_ci"]["describes_current_head"] is True

    def test_the_generator_still_marks_a_code_moved_verdict_stale(self, tmp_path: Path) -> None:
        repo = _scratch_repo(tmp_path)
        verified = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        (repo / "packages" / "thing.py").write_text("x = 2\n", encoding="utf-8")
        self._commit(repo, "change the code")

        result = _reconcile_in_with_shas(repo, "GREEN", verified)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        written = json.loads(
            (repo / "docs" / "control" / "STATUS.json").read_text(encoding="utf-8")
        )
        assert written["linux_ci"]["status"] == "STALE:GREEN"
        assert written["linux_ci"]["describes_current_head"] is False


def _reconcile_in_with_shas(repo: Path, status: str, sha: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "reconcile_status.py"),
            "--linux",
            status,
            "--linux-sha",
            sha,
            "--windows",
            status,
            "--windows-sha",
            sha,
            "--rc-status",
            "NOT_BUILT",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _scratch_repo(tmp_path: Path) -> Path:
    """A git repository carrying just enough for the generator to run."""
    repo = tmp_path / "repo"
    (repo / "docs" / "control").mkdir(parents=True)
    (repo / "packages").mkdir()
    # Committed so the directory is tracked: git reports an untracked directory
    # as one entry ("packages/") and never the file inside it, and the assertion
    # below is about the guard naming the precise path it refused for.
    (repo / "packages" / "kept.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    for name in (
        "reconcile_status.py",
        "master_report.py",
        "check_master_plan.py",
        "plan_model.py",
    ):
        shutil.copy(SCRIPTS / name, repo / "scripts" / name)
    (repo / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    # Without this the scratch repo reports the __pycache__ the generator's own
    # run creates as uncommitted work, and the guard — correctly, since git told
    # it so — refuses for a reason the test is not about.
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    shutil.copy(PLAN_PATH, repo / "docs" / "control" / "MASTER_PLAN.yaml")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "base"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True, timeout=120)
    return repo


def _reconcile_in(repo: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "reconcile_status.py"),
            "--linux",
            "PENDING",
            "--linux-sha",
            head,
            "--windows",
            "PENDING",
            "--windows-sha",
            head,
            "--rc-status",
            "NOT_BUILT",
            *extra,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


class TestAnEpicDoesNotCloseOnACount:
    """Five conditions, all required. None of them is a proportion."""

    @staticmethod
    def _epic(*, tasks: tuple[Task, ...], checks: tuple[Check, ...] = ()) -> Epic:
        return Epic(
            id="E01",
            title="fixture",
            subsystem="fixture",
            integration_scenario="the fixture is exercised end to end",
            required_ci=".github/workflows/ci.yml",
            milestones=(Milestone(id="E01-M01", title="fixture", tasks=tasks, checks=checks),),
        )

    @staticmethod
    def _passing(identifier: str) -> Task:
        return Task(
            id=identifier,
            subsystem="fixture",
            action="do it",
            weight=5,
            band="transport",
            owner="remote",
            pass_criterion="observed",
            verify_command="pytest",
            regression_test=REAL_TEST,
            evidence=REAL_PATH,
            status="PASS",
            commit="0" * 40,
        )

    def test_an_epic_with_everything_satisfied_closes(self) -> None:
        epic = self._epic(tasks=(self._passing("E01-M01-T001"),))
        closed, why = epic_closed(epic, ci_green=True)
        assert closed, why

    def test_one_open_task_keeps_it_open_however_many_passed(self) -> None:
        """Nineteen of twenty is not closed. That is the whole rule."""
        tasks = tuple(self._passing(f"E01-M01-T{n:03d}") for n in range(1, 20))
        open_one = Task(
            id="E01-M01-T020",
            subsystem="fixture",
            action="the load-bearing one",
            weight=5,
            band="transport",
            owner="remote",
            pass_criterion="observed",
            verify_command="pytest",
            regression_test=REAL_TEST,
            evidence=REAL_PATH,
            status="NOT_STARTED",
        )
        closed, why = epic_closed(self._epic(tasks=(*tasks, open_one)), ci_green=True)
        assert not closed
        assert "E01-M01-T020" in why

    def test_an_open_check_keeps_it_open_with_every_task_passing(self) -> None:
        """The recurring defect, stated as a rule: the parts pass, nothing joins."""
        check = Check(
            id="E01-M01-C001",
            statement="the subsystem is reachable from a client",
            command="pytest tests/contract",
            status="NOT_STARTED",
        )
        epic = self._epic(tasks=(self._passing("E01-M01-T001"),), checks=(check,))
        closed, why = epic_closed(epic, ci_green=True)
        assert not closed
        assert "E01-M01-C001" in why

    def test_a_red_workflow_keeps_it_open(self) -> None:
        epic = self._epic(tasks=(self._passing("E01-M01-T001"),))
        closed, why = epic_closed(epic, ci_green=False)
        assert not closed
        assert "ci.yml" in why

    def test_no_declared_integration_scenario_keeps_it_open(self) -> None:
        epic = Epic(
            id="E01",
            title="fixture",
            subsystem="fixture",
            integration_scenario="",
            required_ci=None,
            milestones=(
                Milestone(id="E01-M01", title="fixture", tasks=(self._passing("E01-M01-T001"),)),
            ),
        )
        closed, why = epic_closed(epic, ci_green=True)
        assert not closed
        assert "integration scenario" in why

    def test_an_epic_with_no_tasks_is_not_closed_by_vacuous_truth(self) -> None:
        """ "All of nothing passed" is the emptiest way to report done."""
        closed, why = epic_closed(self._epic(tasks=()), ci_green=True)
        assert not closed
        assert "no tasks" in why


class TestTheRealPlanIsWellFormed:
    """The model has to survive meeting its own data."""

    def test_the_archive_tasks_require_the_per_member_verification(
        self, real_plan: dict[str, Any]
    ) -> None:
        """E11-M04-T007's criterion, asserted on the wiring that makes it true.

        "A ZIP whose contents were never checked is not counted" is realised
        by nothing but the ordering: every archive task must depend, directly
        or transitively, on E11-M03-T003 — the per-member digest verification.
        The criterion-coverage audit found this chain asserted nowhere, so
        deleting one link would have silently unmade the guarantee.
        """
        by_id = {
            task["id"]: task
            for epic in real_plan["epics"]
            for milestone in epic["milestones"]
            for task in milestone["tasks"]
        }

        def reaches_verification(identifier: str, seen: frozenset[str]) -> bool:
            if identifier == "E11-M03-T003":
                return True
            if identifier in seen:
                return False
            return any(
                reaches_verification(dep, seen | {identifier})
                for dep in by_id[identifier].get("depends_on") or []
            )

        unverified = [
            identifier
            for identifier in by_id
            if identifier.startswith("E11-M03-T")
            and identifier > "E11-M03-T003"
            and not reaches_verification(identifier, frozenset())
        ]
        assert unverified == [], (
            "these archive tasks no longer require the per-member verification "
            f"to have run: {unverified}"
        )

    def test_a_build_task_cannot_pass_on_a_spec_alone(self, real_plan: dict[str, Any]) -> None:
        """E11-M04-T006's criterion, in the form the plan can actually enforce.

        A PyInstaller spec exists whether or not any executable was ever
        built, and so does the CI-log evidence file — so the evidence-exists
        check cannot tell a built tree from a described one. What can: every
        PASS build task must carry a ci_url naming the run that produced and
        answered the executables. Flipping one to PASS with only a spec on
        disk fails here.
        """
        build_tasks = [
            task
            for epic in real_plan["epics"]
            for milestone in epic["milestones"]
            if milestone["id"] in {"E11-M02", "E11-M03"}
            for task in milestone["tasks"]
        ]
        assert build_tasks, "the build milestones have vanished from the plan"
        unwitnessed = [
            task["id"]
            for task in build_tasks
            if task["status"] == "PASS" and not (task.get("ci_url") or "").startswith("https://")
        ]
        assert unwitnessed == [], (
            f"these build tasks are PASS with no run to witness the build: {unwitnessed}"
        )

    def test_every_task_carries_every_field(self, real_plan: dict[str, Any]) -> None:
        required = {
            "id",
            "subsystem",
            "action",
            "weight",
            "band",
            "owner",
            "depends_on",
            "pass_criterion",
            "verify_command",
            "regression_test",
            "evidence",
            "status",
            "commit",
            "ci_url",
            "reason",
        }
        for task in check_master_plan.tasks_of(real_plan):
            missing = required - set(task)
            assert not missing, f"{task['id']} is missing {sorted(missing)}"

    def test_task_ids_are_unique(self, real_plan: dict[str, Any]) -> None:
        ids = [task["id"] for task in check_master_plan.tasks_of(real_plan)]
        duplicates = sorted({identifier for identifier in ids if ids.count(identifier) > 1})
        assert not duplicates, duplicates

    def test_every_dependency_names_a_task_that_exists(self, real_plan: dict[str, Any]) -> None:
        tasks = check_master_plan.tasks_of(real_plan)
        known = {task["id"] for task in tasks}
        dangling = [
            f"{task['id']} -> {dependency}"
            for task in tasks
            for dependency in task["depends_on"]
            if dependency not in known
        ]
        assert not dangling, dangling

    def test_every_owner_is_remote_or_local(self, real_plan: dict[str, Any]) -> None:
        owners = {task["owner"] for task in check_master_plan.tasks_of(real_plan)}
        assert owners <= {"remote", "local"}, owners

    def test_the_live_epics_are_owned_locally(self, real_plan: dict[str, Any]) -> None:
        """Nothing in this environment can run the game, and the plan must say so."""
        for epic in real_plan["epics"]:
            if epic["id"] not in METRICS["LIVE_GAME_VALIDATION"]:
                continue
            for milestone in epic["milestones"]:
                for task in milestone["tasks"]:
                    assert task["owner"] == "local", (
                        f"{task['id']} is a live-validation task owned by 'remote'. "
                        f"This environment has no Project Zomboid, so it could be "
                        f"marked PASS by something that never ran the game."
                    )

    def test_next_task_never_points_at_something_that_cannot_be_started(
        self, real_plan: dict[str, Any]
    ) -> None:
        following = master_report.next_task(real_plan)
        if following is None:
            pytest.skip("every remote task is closed; there is no next one to check")
        passed = {t["id"] for t in check_master_plan.tasks_of(real_plan) if t["status"] == "PASS"}
        unmet = [d for d in following["depends_on"] if d not in passed]
        assert not unmet, f"{following['id']} is offered as next but waits on {unmet}"
        assert following["owner"] == "remote"


class TestThePlantedViolationsAreDetectedInTheRealPlan:
    """The refusals above use fixtures. These use the plan that ships.

    A gate proved only against hand-built documents is a gate that has never read
    the file it guards; the shapes differ, and the difference is where a refusal
    stops firing.
    """

    @pytest.fixture
    def mutable(self, real_plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
        yield copy.deepcopy(real_plan)

    @staticmethod
    def _first_passing(document: dict[str, Any]) -> dict[str, Any]:
        for task in check_master_plan.tasks_of(document):
            if task["status"] == "PASS":
                return task
        pytest.skip("no task in the real plan is PASS yet")

    def test_the_real_plan_as_it_stands_is_admitted(self, real_plan: dict[str, Any]) -> None:
        assert _refusals(real_plan) == []

    def test_stripping_the_evidence_from_a_real_passing_task_is_refused(
        self, mutable: dict[str, Any]
    ) -> None:
        task = self._first_passing(mutable)
        task["evidence"] = ""
        found = _refusals(mutable)
        assert any(task["id"] in problem and "no evidence" in problem for problem in found), found

    def test_stripping_the_commit_from_a_real_passing_task_is_refused(
        self, mutable: dict[str, Any]
    ) -> None:
        task = self._first_passing(mutable)
        task["commit"] = None
        found = _refusals(mutable)
        assert any(task["id"] in problem and "no commit" in problem for problem in found), found

    def test_flipping_a_real_passing_task_to_local_is_refused(
        self, mutable: dict[str, Any]
    ) -> None:
        task = self._first_passing(mutable)
        task["owner"] = "local"
        found = _refusals(mutable)
        assert any(task["id"] in problem and "local task" in problem for problem in found), found
