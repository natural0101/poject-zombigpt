"""The runner, driven over a temp directory with a fake clock and fake drivers.

No game is involved, and none is needed: the property under test is what the
runner refuses to claim, and that is decided entirely by what it was handed.

The four assertions the whole harness stands on have their own classes below —
NOT_RUN never becomes PASS without evidence, PASS is not overwritten, a tampered
``result.json`` is detected, and ``finalize`` refuses with a complete list of
what is missing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, TRACE_NAME, resolve_workspace
from pz_agent_cli.livetest.evidence import (
    EvidenceLayout,
    LiveTestError,
    TamperError,
    canonical_json,
    write_document,
)
from pz_agent_cli.livetest.runner import (
    NOT_OBSERVED,
    POSTCONDITION_FAILED,
    UNOBSERVED_BUILD,
    FileDriver,
    FinalizeRefused,
    ObservedRun,
    UnavailableDriver,
    audit_scenario,
    decide,
    evaluate,
    finalize,
    first_unpassed,
    latency_summary,
    parse_observations,
    percentile,
    read_commit,
    run_scenario,
    summarise,
    verify_result,
)
from pz_agent_cli.livetest.scenarios import SCENARIO_IDS, Check, LiveScenario, Postcondition, by_id
from pz_agent_cli.livetest.state import LiveState, StateStore
from tests.fixtures.cli_worlds import CliWorld, make_world

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SOURCE = REPO_ROOT / "evidence" / "schema"

MOVE = "S04_MOVE"
TRANSFER = "S07_NESTED_INVENTORY"


class FakeClock:
    """Milliseconds that advance by a fixed step, so timestamps are predictable."""

    def __init__(self, start_ms: int = 1_700_000_000_000, step_ms: int = 500) -> None:
        self._now = start_ms
        self._step = step_ms

    def __call__(self) -> int:
        current = self._now
        self._now += self._step
        return current


@dataclass(frozen=True)
class FakeDriver:
    """A driver that reports exactly what a test hands it. No verdict field exists."""

    run: ObservedRun

    def observe(self, scenario: LiveScenario) -> ObservedRun:
        return self.run


@dataclass(frozen=True)
class ExplodingDriver:
    """A driver whose observation fails. The runner must not turn that into a pass."""

    def observe(self, scenario: LiveScenario) -> ObservedRun:
        raise LiveTestError("the exchange directory went away mid-run")


@pytest.fixture
def layout(tmp_path: Path) -> EvidenceLayout:
    built = EvidenceLayout(tmp_path / "evidence")
    built.ensure_tree(SCENARIO_IDS)
    for schema in SCHEMA_SOURCE.glob("*.json"):
        (built.schema_dir / schema.name).write_bytes(schema.read_bytes())
    return built


@pytest.fixture
def store(layout: EvidenceLayout) -> StateStore:
    store = StateStore(layout.root)
    store.initialise(SCENARIO_IDS)
    return store


def passing_transfer() -> ObservedRun:
    """Everything S07_NESTED_INVENTORY declares, observed."""
    return ObservedRun(
        before={"inventory": {"main_count": 4}},
        after={"inventory": {"main_count": 5}},
        observations={
            "transfer": {"item_in_main_after": True, "item_in_source_after": False},
            "action_result": {"reason_code": "POSTCONDITION_MET"},
        },
        game_build="42.20",
        log_paths=("console.txt",),
    )


def run(
    layout: EvidenceLayout,
    store: StateStore,
    scenario_id: str,
    driver: Any,
    *,
    clock: FakeClock | None = None,
) -> Any:
    return run_scenario(
        by_id(scenario_id),
        layout=layout,
        store=store,
        driver=driver,
        commit="0123456789abcdef",
        clock=clock or FakeClock(),
    )


# ---------------------------------------------------------------------------
# NOT_RUN never becomes PASS without evidence
# ---------------------------------------------------------------------------


class TestNotRunNeverBecomesPassWithoutEvidence:
    def test_a_run_with_no_observations_is_blocked(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        outcome = run(layout, store, TRANSFER, UnavailableDriver("no session"))

        assert outcome.status is LiveState.BLOCKED
        assert outcome.failure_code == NOT_OBSERVED
        assert store.read(TRANSFER).state is LiveState.BLOCKED

    def test_an_empty_observations_document_fails_every_postcondition(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        outcome = run(layout, store, TRANSFER, FakeDriver(ObservedRun(game_build="42.20")))

        assert outcome.status is LiveState.FAIL
        assert set(outcome.failed_keys) == {
            condition.key for condition in by_id(TRANSFER).postconditions
        }
        assert outcome.failure_code == POSTCONDITION_FAILED

    def test_one_missing_postcondition_is_enough_to_fail(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        partial = passing_transfer()
        observations = dict(partial.observations)
        del observations["action_result"]
        outcome = run(
            layout,
            store,
            TRANSFER,
            FakeDriver(
                ObservedRun(
                    before=partial.before,
                    after=partial.after,
                    observations=observations,
                    game_build="42.20",
                )
            ),
        )

        assert outcome.status is LiveState.FAIL
        assert outcome.failed_keys == ("reason_code",)

    def test_a_driver_that_raises_becomes_blocked_not_passed(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        outcome = run(layout, store, TRANSFER, ExplodingDriver())

        assert outcome.status is LiveState.BLOCKED
        assert "went away mid-run" in json.loads(outcome.result_path.read_text())["detail"]

    def test_a_driver_cannot_report_success_because_the_type_has_no_field_for_it(
        self,
    ) -> None:
        assert not hasattr(ObservedRun(), "passed")
        assert not hasattr(ObservedRun(), "status")
        assert not hasattr(ObservedRun(), "succeeded")

    def test_a_run_that_observes_everything_passes(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        outcome = run(layout, store, TRANSFER, FakeDriver(passing_transfer()))

        assert outcome.status is LiveState.PASS
        assert outcome.failure_code == ""
        assert outcome.failed_keys == ()

    def test_a_build_nobody_read_is_recorded_as_unobserved(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        """Never defaulted to the supported build: that would be a guess in the record."""
        run(layout, store, TRANSFER, UnavailableDriver("no session"))

        document = json.loads(layout.result_path(TRANSFER).read_text(encoding="utf-8"))
        assert document["game_build"] == UNOBSERVED_BUILD

    def test_every_scenario_starts_at_not_run(self, store: StateStore) -> None:
        assert summarise(store.read_all(SCENARIO_IDS)) == {
            "NOT_RUN": 20,
            "PASS": 0,
            "FAIL": 0,
            "BLOCKED": 0,
        }


# ---------------------------------------------------------------------------
# PASS is not overwritten
# ---------------------------------------------------------------------------


class TestPassIsNotOverwritten:
    def test_a_failing_rerun_leaves_the_verdict_and_the_result_alone(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        first = run(layout, store, TRANSFER, FakeDriver(passing_transfer()))
        published = layout.result_path(TRANSFER).read_text(encoding="utf-8")

        second = run(layout, store, TRANSFER, FakeDriver(ObservedRun(game_build="42.20")))

        assert second.status is LiveState.FAIL
        assert second.state.state is LiveState.PASS
        assert second.state.attempt_count == 2
        assert layout.result_path(TRANSFER).read_text(encoding="utf-8") == published
        assert first.result_sha256 != second.result_sha256

    def test_both_attempts_keep_their_own_result_file(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        run(layout, store, TRANSFER, FakeDriver(passing_transfer()))
        run(layout, store, TRANSFER, FakeDriver(ObservedRun(game_build="42.20")))

        first = json.loads(layout.attempt_result_path(TRANSFER, 1).read_text(encoding="utf-8"))
        second = json.loads(layout.attempt_result_path(TRANSFER, 2).read_text(encoding="utf-8"))
        assert first["status"] == "PASS"
        assert second["status"] == "FAIL"

    def test_a_fix_after_a_failure_promotes_and_keeps_the_failure(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        run(layout, store, TRANSFER, FakeDriver(ObservedRun(game_build="42.20")))

        outcome = run(layout, store, TRANSFER, FakeDriver(passing_transfer()))

        assert outcome.state.state is LiveState.PASS
        assert [a.status for a in outcome.state.attempts] == [LiveState.FAIL, LiveState.PASS]
        assert json.loads(layout.result_path(TRANSFER).read_text())["attempt"] == 2

    def test_the_published_result_matches_the_ledger_digest(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        run(layout, store, TRANSFER, FakeDriver(passing_transfer()))
        run(layout, store, TRANSFER, FakeDriver(ObservedRun(game_build="42.20")))

        document = verify_result(layout, store.read(TRANSFER))

        assert document["status"] == "PASS"


# ---------------------------------------------------------------------------
# a tampered result.json is detected
# ---------------------------------------------------------------------------


class TestTamperDetection:
    def test_flipping_the_status_by_hand_is_caught(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        run(layout, store, TRANSFER, FakeDriver(ObservedRun(game_build="42.20")))
        path = layout.result_path(TRANSFER)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["status"] = "PASS"
        document["failure_code"] = ""
        path.write_text(canonical_json(document), encoding="utf-8")

        with pytest.raises(TamperError, match="modified after it was written"):
            verify_result(layout, store.read(TRANSFER))

    def test_even_a_whitespace_only_edit_is_caught(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        run(layout, store, TRANSFER, FakeDriver(passing_transfer()))
        path = layout.result_path(TRANSFER)
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        with pytest.raises(TamperError):
            verify_result(layout, store.read(TRANSFER))

    def test_an_untouched_result_verifies(self, layout: EvidenceLayout, store: StateStore) -> None:
        run(layout, store, TRANSFER, FakeDriver(passing_transfer()))

        assert verify_result(layout, store.read(TRANSFER))["status"] == "PASS"

    def test_a_scenario_that_never_ran_has_no_result_to_verify(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        with pytest.raises(LiveTestError, match="never run"):
            verify_result(layout, store.read(MOVE))

    def test_finalize_reports_tampering_by_name(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        run(layout, store, TRANSFER, FakeDriver(passing_transfer()))
        path = layout.result_path(TRANSFER)
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")

        audit = audit_scenario(by_id(TRANSFER), layout=layout, store=store)

        assert "does not match the recorded" in audit.tampered


# ---------------------------------------------------------------------------
# finalize refuses, and names everything
# ---------------------------------------------------------------------------


def complete_scenario(layout: EvidenceLayout, store: StateStore, scenario_id: str) -> None:
    """Drive one scenario to PASS and lay down every artefact it owes."""
    scenario = by_id(scenario_id)
    observations = passing_transfer() if scenario_id == TRANSFER else _passing_move()
    run(layout, store, scenario_id, FakeDriver(observations))
    for name in scenario.logs:
        (layout.logs_dir(scenario_id) / name).write_text(f"{name} content", encoding="utf-8")
    if scenario.screenshots_required:
        (layout.screenshots_dir(scenario_id) / "shot.png").write_bytes(b"\x89PNG\r\n")


def _passing_move() -> ObservedRun:
    return ObservedRun(
        before={"player": {"position": {"x": 1, "y": 1}}},
        after={"player": {"position": {"x": 4, "y": 1}}},
        observations={
            "move": {"arrived_at_target": True},
            "action_result": {"reason_code": "POSTCONDITION_MET"},
        },
        latencies_ms=(800, 900, 1000, 4000),
        game_build="42.20",
    )


class TestFinalizeRefuses:
    def test_it_refuses_and_names_every_scenario_that_has_not_passed(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        with pytest.raises(FinalizeRefused) as caught:
            finalize(
                layout=layout,
                store=store,
                scenarios=[by_id(sid) for sid in SCENARIO_IDS],
                output=tmp_path / "manifest.json",
                commit="abc",
                clock=FakeClock(),
            )

        assert len(caught.value.not_passed) == 20
        assert all("NOT_RUN" in entry for entry in caught.value.not_passed)

    def test_it_names_every_missing_artefact_not_only_the_first(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        run(layout, store, MOVE, FakeDriver(_passing_move()))

        with pytest.raises(FinalizeRefused) as caught:
            finalize(
                layout=layout,
                store=store,
                scenarios=[by_id(MOVE)],
                output=tmp_path / "manifest.json",
                commit="abc",
                clock=FakeClock(),
            )

        missing = caught.value.missing
        for name in by_id(MOVE).logs:
            assert any(name in entry for entry in missing), name
        assert len(missing) == len(by_id(MOVE).logs)

    def test_an_empty_log_counts_as_missing(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        complete_scenario(layout, store, MOVE)
        (layout.logs_dir(MOVE) / "console.txt").write_text("", encoding="utf-8")

        with pytest.raises(FinalizeRefused) as caught:
            finalize(
                layout=layout,
                store=store,
                scenarios=[by_id(MOVE)],
                output=tmp_path / "manifest.json",
                commit="abc",
                clock=FakeClock(),
            )

        assert any("console.txt (empty)" in entry for entry in caught.value.missing)

    def test_a_scenario_that_requires_a_screenshot_and_has_none_is_named(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        scenario = by_id("S13_MEDICAL")
        assert scenario.screenshots_required
        run(
            layout,
            store,
            scenario.id,
            FakeDriver(
                ObservedRun(
                    before={"player": {"untreated_wounds": 1}},
                    after={"player": {"untreated_wounds": 0}},
                    observations={"medical": {"wound_bandaged_after": True, "panel_agrees": True}},
                    game_build="42.20",
                )
            ),
        )
        for name in scenario.logs:
            (layout.logs_dir(scenario.id) / name).write_text("x", encoding="utf-8")

        with pytest.raises(FinalizeRefused) as caught:
            finalize(
                layout=layout,
                store=store,
                scenarios=[scenario],
                output=tmp_path / "manifest.json",
                commit="abc",
                clock=FakeClock(),
            )

        assert any("screenshot" in entry for entry in caught.value.missing)

    def test_nothing_is_written_when_it_refuses(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        destination = tmp_path / "manifest.json"

        with pytest.raises(FinalizeRefused):
            finalize(
                layout=layout,
                store=store,
                scenarios=[by_id(MOVE)],
                output=destination,
                commit="abc",
                clock=FakeClock(),
            )

        assert not destination.exists()

    def test_the_refusal_renders_every_problem_on_its_own_line(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        with pytest.raises(FinalizeRefused) as caught:
            finalize(
                layout=layout,
                store=store,
                scenarios=[by_id(MOVE), by_id(TRANSFER)],
                output=tmp_path / "manifest.json",
                commit="abc",
                clock=FakeClock(),
            )

        lines = caught.value.render_lines()
        assert any("not passed: S04_MOVE" in line for line in lines)
        assert any("not passed: S07_NESTED_INVENTORY" in line for line in lines)


class TestFinalizeSucceeds:
    def test_a_complete_tree_produces_a_hashed_manifest(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        complete_scenario(layout, store, MOVE)
        complete_scenario(layout, store, TRANSFER)
        destination = tmp_path / "release" / "evidence-manifest.json"

        path, document = finalize(
            layout=layout,
            store=store,
            scenarios=[by_id(MOVE), by_id(TRANSFER)],
            output=destination,
            commit="0123456789abcdef",
            clock=FakeClock(),
        )

        assert path == destination
        assert document["complete"] is True
        assert document["scenario_count"] == 2
        assert document["game_builds"] == ["42.20"]
        assert all(entry["sha256"] for entry in document["artefacts"])
        assert document["totals"]["artefact_count"] == len(document["artefacts"])

    def test_the_manifest_hashes_match_the_files_on_disk(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        complete_scenario(layout, store, MOVE)

        _, document = finalize(
            layout=layout,
            store=store,
            scenarios=[by_id(MOVE)],
            output=tmp_path / "manifest.json",
            commit="abc",
            clock=FakeClock(),
        )

        for entry in document["artefacts"]:
            path = layout.root / entry["path"]
            assert path.is_file(), entry["path"]
            assert path.stat().st_size == entry["size_bytes"]

    def test_a_scenario_fixed_after_a_failure_still_finalizes(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        run(layout, store, MOVE, FakeDriver(ObservedRun(game_build="42.20")))
        complete_scenario(layout, store, MOVE)

        _, document = finalize(
            layout=layout,
            store=store,
            scenarios=[by_id(MOVE)],
            output=tmp_path / "manifest.json",
            commit="abc",
            clock=FakeClock(),
        )

        assert document["scenarios"][0]["attempt_count"] == 2
        assert document["scenarios"][0]["state"] == "PASS"


# ---------------------------------------------------------------------------
# the check vocabulary
# ---------------------------------------------------------------------------


def condition(check: Check, field: str = "a.b", expected: Any = None) -> Postcondition:
    return Postcondition(key="k", statement="s", check=check, field=field, expected=expected)


class TestChecks:
    @pytest.mark.parametrize(
        ("check", "value", "expected", "passes"),
        [
            (Check.OBSERVED, "something", None, True),
            (Check.OBSERVED, 0, None, True),
            (Check.OBSERVED, False, None, True),
            (Check.OBSERVED, "", None, False),
            (Check.OBSERVED, [], None, False),
            (Check.OBSERVED, {}, None, False),
            (Check.IS_TRUE, True, None, True),
            (Check.IS_TRUE, "true", None, False),
            (Check.IS_TRUE, 1, None, False),
            (Check.IS_FALSE, False, None, True),
            (Check.IS_FALSE, True, None, False),
            (Check.EQUALS, "POSTCONDITION_MET", "POSTCONDITION_MET", True),
            (Check.EQUALS, "ACTION_TIMEOUT", "POSTCONDITION_MET", False),
            (Check.AT_LEAST, 3, 3, True),
            (Check.AT_LEAST, 2, 3, False),
            (Check.AT_MOST, 1, 1, True),
            (Check.AT_MOST, 2, 1, False),
            (Check.AT_MOST, "two", 1, False),
        ],
    )
    def test_observation_checks(
        self, check: Check, value: Any, expected: Any, passes: bool
    ) -> None:
        run = ObservedRun(observations={"a": {"b": value}})

        assert evaluate(condition(check, expected=expected), run).passed is passes

    def test_is_false_on_a_literal_false_still_counts_as_observed(self) -> None:
        """False is a reading, not an absence, and the emptiness rule must not eat it."""
        outcome = evaluate(condition(Check.IS_FALSE), ObservedRun(observations={"a": {"b": False}}))

        assert outcome.present
        assert outcome.passed

    @pytest.mark.parametrize(
        ("check", "before", "after", "passes"),
        [
            (Check.INCREASED, 4, 5, True),
            (Check.INCREASED, 5, 5, False),
            (Check.INCREASED, 5, 4, False),
            (Check.DECREASED, 0.7, 0.3, True),
            (Check.DECREASED, 0.3, 0.7, False),
            (Check.CHANGED, {"x": 1}, {"x": 2}, True),
            (Check.CHANGED, {"x": 1}, {"x": 1}, False),
            (Check.UNCHANGED, 100, 100, True),
            (Check.UNCHANGED, 100, 90, False),
            (Check.INCREASED, "a", "b", False),
        ],
    )
    def test_snapshot_checks(self, check: Check, before: Any, after: Any, passes: bool) -> None:
        run = ObservedRun(before={"a": {"b": before}}, after={"a": {"b": after}})

        assert evaluate(condition(check), run).passed is passes

    @pytest.mark.parametrize("check", list(Check))
    def test_no_check_passes_on_an_absent_field(self, check: Check) -> None:
        """The single most important property in the module."""
        outcome = evaluate(condition(check, expected=1), ObservedRun())

        assert not outcome.passed
        assert not outcome.present

    def test_a_snapshot_check_needs_both_sides(self) -> None:
        run = ObservedRun(before={"a": {"b": 1}}, after={})

        outcome = evaluate(condition(Check.INCREASED), run)

        assert not outcome.passed
        assert "before and after" in outcome.detail

    def test_the_outcome_carries_the_values_it_was_decided_from(self) -> None:
        run = ObservedRun(before={"a": {"b": 4}}, after={"a": {"b": 5}})

        outcome = evaluate(condition(Check.INCREASED), run)

        assert outcome.observed_before == 4
        assert outcome.observed == 5


class TestDecide:
    def test_blocked_wins_over_the_postconditions(self) -> None:
        """An attempt that never reached the game must not be blamed on the code."""
        status, outcomes = decide(by_id(TRANSFER), ObservedRun(blocked_reason="no session"))

        assert status is LiveState.BLOCKED
        assert not any(outcome.passed for outcome in outcomes)

    def test_a_scenario_with_no_postconditions_cannot_pass_vacuously(self) -> None:
        empty = LiveScenario(
            id="S99_EMPTY",
            title="t",
            goal="g",
            world_preparation=("w",),
            required_state=("r",),
            command="c",
            operator_steps=("o",),
            expected_result="e",
            postconditions=(),
            time_budget_s=1,
            logs=("console.txt",),
            suspect_module="m",
        )

        with pytest.raises(LiveTestError, match="declares no postconditions"):
            decide(empty, ObservedRun())


# ---------------------------------------------------------------------------
# latency, commit, resume
# ---------------------------------------------------------------------------


class TestLatency:
    def test_nearest_rank_returns_a_sample_that_was_measured(self) -> None:
        samples = [10, 20, 30, 40]

        assert percentile(samples, 0.5) in samples
        assert percentile(samples, 0.95) == 40

    def test_an_empty_series_has_no_percentile(self) -> None:
        assert percentile([], 0.5) is None

    def test_only_a_measuring_scenario_reports_percentiles(self) -> None:
        run = ObservedRun(latencies_ms=(100, 200, 300))

        assert latency_summary(by_id(MOVE), run)["measured"] is True
        assert latency_summary(by_id(TRANSFER), run)["measured"] is False
        assert latency_summary(by_id(TRANSFER), run)["p95_ms"] is None

    def test_a_measuring_scenario_with_no_samples_reports_null(self) -> None:
        summary = latency_summary(by_id(MOVE), ObservedRun())

        assert summary["measured"] is False
        assert summary["p50_ms"] is None


class TestCommit:
    def test_a_detached_head_is_read_directly(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("a" * 40, encoding="utf-8")

        assert read_commit(tmp_path) == "a" * 40

    def test_a_branch_ref_is_followed(self, tmp_path: Path) -> None:
        git = tmp_path / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/dev\n", encoding="utf-8")
        (git / "refs" / "heads" / "dev").write_text("b" * 40 + "\n", encoding="utf-8")

        assert read_commit(tmp_path) == "b" * 40

    def test_a_packed_ref_is_found(self, tmp_path: Path) -> None:
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "packed-refs").write_text(
            f"# pack-refs with: peeled\n{'c' * 40} refs/heads/main\n", encoding="utf-8"
        )

        assert read_commit(tmp_path) == "c" * 40

    def test_no_git_directory_is_unknown_rather_than_guessed(self, tmp_path: Path) -> None:
        assert read_commit(tmp_path) == "(unknown)"


class TestResume:
    def test_it_starts_from_the_first_scenario_that_is_not_pass(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        for scenario_id in SCENARIO_IDS[:3]:
            store.record(
                scenario_id,
                status=LiveState.PASS,
                started_at_ms=1,
                finished_at_ms=2,
                result_sha256="d" * 64,
            )

        assert first_unpassed(store) == SCENARIO_IDS[3]

    def test_a_blocked_scenario_is_resumed_from(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        store.record(
            SCENARIO_IDS[0],
            status=LiveState.BLOCKED,
            started_at_ms=1,
            finished_at_ms=2,
            result_sha256="d" * 64,
        )

        assert first_unpassed(store) == SCENARIO_IDS[0]

    def test_nothing_to_resume_when_every_scenario_passed(self, store: StateStore) -> None:
        for scenario_id in SCENARIO_IDS:
            store.record(
                scenario_id,
                status=LiveState.PASS,
                started_at_ms=1,
                finished_at_ms=2,
                result_sha256="d" * 64,
            )

        assert first_unpassed(store) is None


# ---------------------------------------------------------------------------
# the observations document
# ---------------------------------------------------------------------------


class TestObservationsDocument:
    def test_a_document_naming_another_scenario_is_refused(self) -> None:
        with pytest.raises(LiveTestError, match="not 'S04_MOVE'"):
            parse_observations({"scenario_id": TRANSFER}, scenario=by_id(MOVE))

    def test_sections_must_be_objects(self) -> None:
        with pytest.raises(LiveTestError, match="before must be an object"):
            parse_observations({"before": [1, 2]}, scenario=by_id(MOVE))

    def test_latency_samples_must_be_numbers(self) -> None:
        with pytest.raises(LiveTestError, match="is not a number"):
            parse_observations({"latencies_ms": ["fast"]}, scenario=by_id(MOVE))

    def test_the_sample_count_is_bounded(self) -> None:
        with pytest.raises(LiveTestError, match="exceeds the"):
            parse_observations({"latencies_ms": [1] * 20_000}, scenario=by_id(MOVE))

    def test_the_file_driver_reads_a_real_document(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        path = tmp_path / "observed.json"
        path.write_text(
            canonical_json(
                {
                    "scenario_id": MOVE,
                    "game_build": "42.20",
                    "before": {"player": {"position": {"x": 1, "y": 1}}},
                    "after": {"player": {"position": {"x": 4, "y": 1}}},
                    "observations": {
                        "move": {"arrived_at_target": True},
                        "action_result": {"reason_code": "POSTCONDITION_MET"},
                    },
                    "latencies_ms": [800, 900],
                }
            ),
            encoding="utf-8",
        )

        outcome = run(layout, store, MOVE, FileDriver(path=path))

        assert outcome.status is LiveState.PASS
        document = json.loads(layout.result_path(MOVE).read_text(encoding="utf-8"))
        assert document["latency"]["measured"] is True
        assert document["game_build"] == "42.20"

    def test_an_empty_observations_file_blocks_rather_than_fails(
        self, layout: EvidenceLayout, store: StateStore, tmp_path: Path
    ) -> None:
        path = tmp_path / "observed.json"
        path.write_text("", encoding="utf-8")

        outcome = run(layout, store, MOVE, FileDriver(path=path))

        assert outcome.status is LiveState.BLOCKED


# ---------------------------------------------------------------------------
# the result document itself
# ---------------------------------------------------------------------------


class TestResultDocument:
    def test_a_pass_validates_against_the_schema(
        self, layout: EvidenceLayout, store: StateStore
    ) -> None:
        """write_document validates before writing, so reaching disk is the assertion."""
        run(layout, store, TRANSFER, FakeDriver(passing_transfer()))

        document = json.loads(layout.result_path(TRANSFER).read_text(encoding="utf-8"))
        assert document["format"] == "pz-agent/livetest-result/1"
        assert document["commit"] == "0123456789abcdef"
        assert document["scenario_id"] == TRANSFER

    def test_the_schema_rejects_a_pass_whose_postcondition_was_not_observed(
        self, layout: EvidenceLayout
    ) -> None:
        """Belt and braces: the runner cannot build this, and the schema refuses it too."""
        forged = json.loads(
            canonical_json(
                _minimal_result(status="PASS", present=False, passed=True, failure_code="")
            )
        )

        with pytest.raises(LiveTestError):
            write_document(layout.result_path(TRANSFER), forged, schema=layout.result_schema)

    def test_the_schema_rejects_a_failure_with_no_code(self, layout: EvidenceLayout) -> None:
        forged = _minimal_result(status="FAIL", present=True, passed=False, failure_code="")

        with pytest.raises(LiveTestError):
            write_document(layout.result_path(TRANSFER), forged, schema=layout.result_schema)

    def test_the_minimal_document_the_tests_forge_is_otherwise_valid(
        self, layout: EvidenceLayout
    ) -> None:
        """Proves the two rejections above are about the rule, not a typo in the fixture."""
        good = _minimal_result(status="PASS", present=True, passed=True, failure_code="")

        digest = write_document(layout.result_path(TRANSFER), good, schema=layout.result_schema)

        assert digest.sha256


def _minimal_result(*, status: str, present: bool, passed: bool, failure_code: str) -> Any:
    return {
        "format": "pz-agent/livetest-result/1",
        "commit": "abc",
        "game_build": "42.20",
        "product_version": "0.1.0",
        "mod_version": "0.1.0",
        "schema_version": "1.0",
        "scenario_id": TRANSFER,
        "title": "t",
        "attempt": 1,
        "status": status,
        "failure_code": failure_code,
        "detail": "",
        "started_at_ms": 1,
        "finished_at_ms": 2,
        "timestamp": "2026-08-06T00:00:00+00:00",
        "duration_ms": 1,
        "time_budget_s": 60,
        "before": {},
        "after": {},
        "postconditions": [
            {
                "key": "k",
                "statement": "s",
                "check": "observed",
                "field": "a.b",
                "expected": None,
                "observed": "v",
                "observed_before": None,
                "present": present,
                "passed": passed,
                "detail": "",
            }
        ],
        "latency": {"measured": False, "samples": 0, "p50_ms": None, "p95_ms": None},
        "logs": [],
        "screenshots": [],
    }


# ---------------------------------------------------------------------------
# the command surface, driven the way a user drives it
# ---------------------------------------------------------------------------


@pytest.fixture
def prepared(tmp_path: Path) -> Iterator[tuple[CliWorld, Path]]:
    """An evidence tree in the state a completed ``prepare`` leaves it in.

    The prepare record is written here because ``run`` and ``resume`` refuse
    without it. That refusal is new: ``prepare.json`` was written by ``prepare``
    and read by nothing, so the check proving a test save and a verified backup
    exist produced a record nobody consulted. This fixture used to omit it and
    every test still passed, which is exactly how the gap survived.
    """
    world = make_world(tmp_path)
    root = tmp_path / "evidence"
    built = EvidenceLayout(root)
    built.ensure_tree(SCENARIO_IDS)
    for schema in SCHEMA_SOURCE.glob("*.json"):
        (built.schema_dir / schema.name).write_bytes(schema.read_bytes())
    built.prepare_path.write_text(
        json.dumps({"ready": True, "save_id": "test-world", "backup_id": "backup-1"}),
        encoding="utf-8",
    )
    yield world, root


@pytest.fixture
def unprepared(tmp_path: Path) -> Iterator[tuple[CliWorld, Path]]:
    """The same tree with no prepare record — what a first run actually meets."""
    world = make_world(tmp_path)
    root = tmp_path / "evidence"
    built = EvidenceLayout(root)
    built.ensure_tree(SCENARIO_IDS)
    for schema in SCHEMA_SOURCE.glob("*.json"):
        (built.schema_dir / schema.name).write_bytes(schema.read_bytes())
    yield world, root


def cli(world: CliWorld, root: Path, *argv: str) -> int:
    return world.run("live-test", "--evidence-dir", str(root), *argv)


class TestCommands:
    def test_status_lists_all_twenty_as_not_run(self, prepared: tuple[CliWorld, Path]) -> None:
        world, root = prepared

        exit_code = cli(world, root, "status")

        assert exit_code == EXIT_FAILURE
        for scenario_id in SCENARIO_IDS:
            assert scenario_id in world.stdout
        assert "NOT_RUN 20" in world.stdout

    def test_status_json_reports_the_counts(self, prepared: tuple[CliWorld, Path]) -> None:
        world, root = prepared

        cli(world, root, "--json", "status")

        document = json.loads(world.stdout)
        assert document["counts"]["NOT_RUN"] == 20
        assert len(document["scenarios"]) == 20

    def test_a_run_without_observations_records_blocked_and_fails(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        world, root = prepared

        exit_code = cli(world, root, "run", "--scenario", TRANSFER)

        assert exit_code == EXIT_FAILURE
        assert "BLOCKED" in world.stdout
        assert StateStore(root).read(TRANSFER).state is LiveState.BLOCKED

    def test_observations_for_one_scenario_cannot_drive_a_batch(
        self, prepared: tuple[CliWorld, Path], tmp_path: Path
    ) -> None:
        world, root = prepared
        observations = tmp_path / "observed.json"
        observations.write_text("{}", encoding="utf-8")

        exit_code = cli(world, root, "run", "--observations", str(observations))

        assert exit_code == EXIT_FAILURE
        assert "describes one scenario" in world.stderr

    def test_an_unknown_scenario_id_is_refused_with_the_list(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        world, root = prepared

        exit_code = cli(world, root, "run", "--scenario", "S07")

        assert exit_code == EXIT_FAILURE
        assert TRANSFER in world.stderr

    def test_prepare_refuses_a_save_that_is_not_marked_as_a_test_world(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        world, root = prepared

        exit_code = cli(world, root, "prepare", "--save", "Sandbox/Muldraugh")

        assert exit_code == EXIT_FAILURE
        assert "does not contain 'test'" in world.stderr

    def test_prepare_refuses_without_a_named_save(self, prepared: tuple[CliWorld, Path]) -> None:
        """There is no default: guessing which world to experiment on is the failure."""
        world, root = prepared

        exit_code = cli(world, root, "prepare")

        assert exit_code == EXIT_FAILURE
        assert "--save" in world.stderr

    def test_prepare_refuses_a_test_save_with_no_backup(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        world, root = prepared
        assert world.user_dir is not None
        (world.user_dir / "Saves" / "Sandbox" / "AgentTest").mkdir(parents=True)

        exit_code = cli(world, root, "prepare", "--save", "Sandbox/AgentTest")

        assert exit_code == EXIT_FAILURE
        assert "no backup" in world.stderr

    def test_prepare_succeeds_once_a_verified_backup_exists(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        world, root = prepared
        assert world.user_dir is not None
        save = world.user_dir / "Saves" / "Sandbox" / "AgentTest"
        save.mkdir(parents=True)
        (save / "map_t.bin").write_bytes(b"tiles")
        assert world.run("backup-save", "Sandbox/AgentTest") == EXIT_OK
        world.reset_streams()

        exit_code = cli(world, root, "prepare", "--save", "Sandbox/AgentTest")

        assert exit_code == EXIT_OK, world.stderr
        assert "ready" in world.stdout
        assert (root / "prepare.json").is_file()

    def test_prepare_verifies_the_newest_backup_not_the_oldest(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        """An older backup verifying says nothing about the world as it stands now."""
        world, root = prepared
        assert world.user_dir is not None
        save = world.user_dir / "Saves" / "Sandbox" / "AgentTest"
        save.mkdir(parents=True)
        (save / "map_t.bin").write_bytes(b"first")
        assert world.run("backup-save", "Sandbox/AgentTest") == EXIT_OK
        (save / "map_t.bin").write_bytes(b"second")
        assert world.run("backup-save", "Sandbox/AgentTest") == EXIT_OK
        newest = sorted(p.name for p in (world.state_dir / "backups").iterdir())[-1]
        # Break the *older* backup's data. prepare must not be satisfied by it,
        # and must not be tripped up by it either: it checks the newest.
        oldest = sorted(p.name for p in (world.state_dir / "backups").iterdir())[0]
        (world.state_dir / "backups" / oldest / "data" / "map_t.bin").write_bytes(b"corrupt")
        world.reset_streams()

        exit_code = cli(world, root, "--json", "prepare", "--save", "Sandbox/AgentTest")

        assert exit_code == EXIT_OK, world.stderr
        assert json.loads(world.stdout)["backup_id"] == newest

    def test_prepare_leaves_an_existing_ledger_alone(self, prepared: tuple[CliWorld, Path]) -> None:
        world, root = prepared
        store = StateStore(root)
        store.record(
            TRANSFER,
            status=LiveState.FAIL,
            started_at_ms=1,
            finished_at_ms=2,
            result_sha256="e" * 64,
        )

        cli(world, root, "prepare", "--save", "Sandbox/AgentTest")

        assert store.read(TRANSFER).attempt_count == 1

    def test_finalize_names_everything_that_is_missing(
        self, prepared: tuple[CliWorld, Path], tmp_path: Path
    ) -> None:
        world, root = prepared

        exit_code = cli(world, root, "finalize", "--output", str(tmp_path / "manifest.json"))

        assert exit_code == EXIT_FAILURE
        assert "refusing to build the evidence manifest" in world.stdout
        assert world.stdout.count("not passed:") == 20

    def test_finalize_json_lists_the_three_kinds_of_problem(
        self, prepared: tuple[CliWorld, Path], tmp_path: Path
    ) -> None:
        world, root = prepared

        cli(world, root, "--json", "finalize", "--output", str(tmp_path / "m.json"))

        document = json.loads(world.stdout)
        assert document["written"] is False
        assert len(document["not_passed"]) == 20
        assert document["missing"]
        assert document["tampered"] == []

    def test_collect_reports_a_declared_log_that_is_absent(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        world, root = prepared
        StateStore(root).record(
            MOVE,
            status=LiveState.FAIL,
            started_at_ms=1,
            finished_at_ms=2,
            result_sha256="f" * 64,
        )

        exit_code = cli(world, root, "collect", "--scenario", MOVE)

        assert exit_code == EXIT_OK
        assert "console.txt" in world.stdout
        assert "not found" in world.stdout

    def test_collect_copies_the_game_console(self, prepared: tuple[CliWorld, Path]) -> None:
        world, root = prepared
        assert world.user_dir is not None
        (world.user_dir / "console.txt").write_text("a lua error", encoding="utf-8")

        cli(world, root, "collect", "--scenario", MOVE)

        copied = EvidenceLayout(root).logs_dir(MOVE) / "console.txt"
        assert copied.read_text(encoding="utf-8") == "a lua error"

    def test_collect_takes_the_trace_no_scenario_knows_to_ask_for(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        """The evidence a failed scenario is diagnosed from, and the newest file.

        The twenty scenarios' ``logs`` lists were written when nothing produced
        a trace, so none of them names one. Collecting only what they declare
        would leave `pz-agent replay` — the command the handoff tells an
        operator to run on the evidence — with nothing in the evidence to read.
        """
        world, root = prepared
        workspace = resolve_workspace(world.ctx)
        workspace.trace_dir.mkdir(parents=True, exist_ok=True)
        (workspace.trace_dir / TRACE_NAME).write_text("{}\n", encoding="utf-8")

        cli(world, root, "collect", "--scenario", MOVE)

        copied = EvidenceLayout(root).logs_dir(MOVE) / TRACE_NAME
        assert copied.is_file(), "the trace was left behind in the workspace"

    def test_collect_takes_the_rotated_generations_of_the_trace_too(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        """A scenario long enough to rotate is the one worth having in full.

        The current file alone would be the last few minutes of a two-hour run,
        which is rarely the part that went wrong.
        """
        world, root = prepared
        workspace = resolve_workspace(world.ctx)
        workspace.trace_dir.mkdir(parents=True, exist_ok=True)
        (workspace.trace_dir / TRACE_NAME).write_text("{}\n", encoding="utf-8")
        (workspace.trace_dir / f"{TRACE_NAME}.1").write_text("older\n", encoding="utf-8")

        cli(world, root, "collect", "--scenario", MOVE)

        logs = EvidenceLayout(root).logs_dir(MOVE)
        assert (logs / f"{TRACE_NAME}.1").read_text(encoding="utf-8") == "older\n"

    def test_resume_reports_when_there_is_nothing_left(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        world, root = prepared
        store = StateStore(root)
        for scenario_id in SCENARIO_IDS:
            store.record(
                scenario_id,
                status=LiveState.PASS,
                started_at_ms=1,
                finished_at_ms=2,
                result_sha256="d" * 64,
            )

        exit_code = cli(world, root, "resume")

        assert exit_code == EXIT_OK
        assert "all twenty" in world.stdout

    def test_live_test_without_a_subcommand_says_what_it_needs(
        self, prepared: tuple[CliWorld, Path]
    ) -> None:
        world, root = prepared

        exit_code = cli(world, root)

        assert exit_code == EXIT_FAILURE
        assert "needs a subcommand" in world.stderr


def test_the_run_order_the_batch_files_walk_is_the_declared_order() -> None:
    ordered: Sequence[str] = SCENARIO_IDS

    assert ordered[0] == "S01_INSTALL"
    assert ordered[-1] == "S20_AUTONOMOUS_2_HOURS"
    assert len(set(ordered)) == 20


# ---------------------------------------------------------------------------
# the gate between prepare and the scenarios that destroy things
# ---------------------------------------------------------------------------


class TestPrepareIsRequired:
    """``prepare`` proves the world is safe to experiment on. Now it is consulted.

    Twenty scenarios that deliberately hurt the character, and one subcommand
    that checks a test save is named and a backup *reads back* before any of
    them start. That subcommand wrote ``prepare.json``; nothing read it. The
    only thing between those scenarios and somebody's main save was a check
    whose answer went nowhere.
    """

    @pytest.mark.parametrize("subcommand", ["run", "resume"])
    def test_the_destructive_subcommands_refuse_without_a_prepare_record(
        self, unprepared: tuple[CliWorld, Path], subcommand: str
    ) -> None:
        world, root = unprepared

        exit_code = cli(world, root, subcommand)

        assert exit_code == EXIT_FAILURE
        assert "prepare has not completed" in world.stderr
        assert "live-test prepare --save" in world.stderr, "the refusal must name the way out"

    @pytest.mark.parametrize("subcommand", ["status", "collect"])
    def test_the_harmless_subcommands_still_work(
        self, unprepared: tuple[CliWorld, Path], subcommand: str
    ) -> None:
        """Reading the table and gathering logs change nothing in the world.

        Gating them would make an operator unable to see why they are stuck.
        """
        world, root = unprepared

        cli(world, root, subcommand)

        assert "prepare has not completed" not in world.stderr

    def test_a_record_that_does_not_claim_readiness_is_not_a_record(
        self, unprepared: tuple[CliWorld, Path]
    ) -> None:
        """``prepare`` writes the file only when clean, but a hand-made one exists too."""
        world, root = unprepared
        EvidenceLayout(root).prepare_path.write_text(
            json.dumps({"ready": False, "problems": ["no verified backup"]}), encoding="utf-8"
        )

        exit_code = cli(world, root, "run")

        assert exit_code == EXIT_FAILURE
        assert "does not say the tree is ready" in world.stderr

    def test_an_unreadable_record_refuses_rather_than_assuming(
        self, unprepared: tuple[CliWorld, Path]
    ) -> None:
        world, root = unprepared
        EvidenceLayout(root).prepare_path.write_text("{not json", encoding="utf-8")

        exit_code = cli(world, root, "run")

        assert exit_code == EXIT_FAILURE
        assert "could not be read" in world.stderr
