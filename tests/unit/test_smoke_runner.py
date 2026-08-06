"""The game-smoke harness.

The assertions that matter here are about what the runner refuses to claim. A
harness that reported an unrun scenario as passing would make the release gate
a formality, so most of these tests are about the accounting rather than the
happy path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pz_agent_cli.smoke import (
    MAX_EVIDENCE_ENTRIES,
    MAX_SCENARIO_BYTES,
    Outcome,
    Scenario,
    SmokeError,
    collect_evidence,
    load_scenario,
    load_scenarios,
    plan_dry_run,
    read_field,
    run_smoke,
    select,
    write_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = REPO_ROOT / "tests" / "game-smoke"
NOW_MS = 1_700_000_000_000


def write_scenario(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


MINIMAL = """
id: T01
title: A minimal scenario
requires_live_session: true
steps:
  - {id: 1, action: do the thing, manual: false}
  - {id: 2, action: press a key, manual: true}
evidence:
  - field: action_result.reason_code
    assertion: POSTCONDITION_MET
"""


class TestLoading:
    def test_the_repositorys_own_scenarios_all_load(self) -> None:
        scenarios = load_scenarios(SCENARIO_DIR)

        assert len(scenarios) == 16
        assert [s.id for s in scenarios[:3]] == ["S01", "S02", "S03"]
        assert all(s.evidence for s in scenarios)

    def test_every_scenario_declares_it_needs_a_live_session(self) -> None:
        # If one ever does not, the release gate's "not run" accounting changes
        # shape, so the assumption is pinned rather than assumed.
        assert all(s.requires_live_session for s in load_scenarios(SCENARIO_DIR))

    def test_a_manual_step_is_recognised(self, tmp_path: Path) -> None:
        scenario = load_scenario(write_scenario(tmp_path, "T01.yaml", MINIMAL))

        assert [s.action for s in scenario.manual_steps] == ["press a key"]
        assert not scenario.is_automatable

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("id: T01\ntitle: t\nevidence: []\n", "evidence must be a non-empty list"),
            ("id: T01\nevidence:\n  - {field: a, assertion: b}\n", "title is missing"),
            ("title: t\nevidence:\n  - {field: a, assertion: b}\n", "id is missing"),
            ("- not\n- a\n- mapping\n", "must be a mapping"),
            ("id: T01\ntitle: t\nevidence:\n  - {assertion: b}\n", "has no field"),
            ("id: T01\ntitle: t\nevidence:\n  - {field: a}\n", "has no assertion"),
            (
                "id: T01\ntitle: t\nevidence:\n  - {field: a, assertion: [a, b]}\n",
                "has no assertion",
            ),
            (
                "id: T01\ntitle: t\nsteps: [{id: 1}]\nevidence:\n  - {field: a, assertion: b}\n",
                "has no action",
            ),
            (
                "id: 'a name with spaces'\ntitle: t\nevidence:\n  - {field: a, assertion: b}\n",
                "short and alphanumeric",
            ),
        ],
    )
    def test_a_malformed_scenario_is_an_error_not_a_skip(
        self, tmp_path: Path, body: str, expected: str
    ) -> None:
        path = write_scenario(tmp_path, "T01.yaml", body)

        with pytest.raises(SmokeError, match=expected):
            load_scenario(path)

    def test_invalid_yaml_names_the_file(self, tmp_path: Path) -> None:
        path = write_scenario(tmp_path, "T01.yaml", "id: [unclosed\n")

        with pytest.raises(SmokeError, match=r"T01\.yaml: is not valid YAML"):
            load_scenario(path)

    def test_an_oversized_file_is_refused_before_it_is_parsed(self, tmp_path: Path) -> None:
        path = write_scenario(tmp_path, "T01.yaml", "x: " + "y" * (MAX_SCENARIO_BYTES + 10))

        with pytest.raises(SmokeError, match="exceeds the"):
            load_scenario(path)

    def test_the_evidence_list_is_bounded(self, tmp_path: Path) -> None:
        entries = "\n".join(
            f"  - {{field: f{i}, assertion: a}}" for i in range(MAX_EVIDENCE_ENTRIES + 1)
        )
        path = write_scenario(tmp_path, "T01.yaml", f"id: T01\ntitle: t\nevidence:\n{entries}\n")

        with pytest.raises(SmokeError, match=f"exceeds the {MAX_EVIDENCE_ENTRIES} cap"):
            load_scenario(path)

    def test_a_duplicate_id_is_refused(self, tmp_path: Path) -> None:
        write_scenario(tmp_path, "a.yaml", MINIMAL)
        write_scenario(tmp_path, "b.yaml", MINIMAL)

        with pytest.raises(SmokeError, match="used by both"):
            load_scenarios(tmp_path)

    def test_an_empty_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SmokeError, match="contains no scenario files"):
            load_scenarios(tmp_path)


class TestSelection:
    def test_a_filtered_out_scenario_is_still_accounted_for(self) -> None:
        scenarios = load_scenarios(SCENARIO_DIR)

        selected, rejected = select(scenarios, only=["S05"])

        assert [s.id for s in selected] == ["S05"]
        # The whole point: filtering changes what runs, never what is reported.
        assert len(selected) + len(rejected) == len(scenarios)

    def test_an_unknown_id_is_refused_rather_than_ignored(self) -> None:
        with pytest.raises(SmokeError, match="S99999"):
            select(load_scenarios(SCENARIO_DIR), only=["S99999"])

    def test_no_filter_selects_everything(self) -> None:
        scenarios = load_scenarios(SCENARIO_DIR)
        selected, rejected = select(scenarios, only=None)

        assert len(selected) == len(scenarios)
        assert rejected == ()


class TestDryRun:
    def test_nothing_passes_in_a_dry_run(self) -> None:
        report = plan_dry_run(load_scenarios(SCENARIO_DIR), timestamp_ms=NOW_MS)

        # A dry run touched no game. If any scenario came back "passed", the
        # harness would be manufacturing the coverage it exists to account for.
        assert report.counts()["passed"] == 0
        assert not report.any_conclusive

    def test_selected_scenarios_are_blocked_on_the_live_session(self) -> None:
        report = plan_dry_run(load_scenarios(SCENARIO_DIR), only=["S05"], timestamp_ms=NOW_MS)
        by_id = {r.scenario_id: r for r in report.results}

        assert by_id["S05"].outcome is Outcome.BLOCKED
        assert "running Project Zomboid session" in by_id["S05"].reason

    def test_deselected_scenarios_are_reported_as_not_run(self) -> None:
        report = plan_dry_run(load_scenarios(SCENARIO_DIR), only=["S05"], timestamp_ms=NOW_MS)
        by_id = {r.scenario_id: r for r in report.results}

        assert by_id["S01"].outcome is Outcome.NOT_RUN
        assert by_id["S01"].reason == "not selected for this run"

    def test_every_scenario_appears_exactly_once(self) -> None:
        scenarios = load_scenarios(SCENARIO_DIR)
        report = plan_dry_run(scenarios, only=["S05", "S09"], timestamp_ms=NOW_MS)

        ids = [r.scenario_id for r in report.results]
        assert sorted(ids) == sorted(s.id for s in scenarios)
        assert len(ids) == len(set(ids))

    def test_manual_steps_are_reported_rather_than_executed(self) -> None:
        report = plan_dry_run(load_scenarios(SCENARIO_DIR), only=["S02"], timestamp_ms=NOW_MS)
        result = next(r for r in report.results if r.scenario_id == "S02")

        assert result.manual_steps_pending
        assert any("panic hotkey" in step for step in result.manual_steps_pending)

    def test_evidence_is_listed_as_unobserved(self) -> None:
        report = plan_dry_run(load_scenarios(SCENARIO_DIR), only=["S05"], timestamp_ms=NOW_MS)
        result = next(r for r in report.results if r.scenario_id == "S05")

        assert result.collected
        assert all(not value.observed for value in result.collected)
        assert all(value.value is None for value in result.collected)

    def test_the_stamp_records_no_build_rather_than_guessing_one(self) -> None:
        report = plan_dry_run(load_scenarios(SCENARIO_DIR), timestamp_ms=NOW_MS)

        # An artefact that named a build it never saw could be mistaken for one
        # produced against an installation.
        assert "dry run" in report.stamp.build
        assert report.stamp.timestamp_ms == NOW_MS

    def test_the_summary_says_nothing_was_exercised(self) -> None:
        report = plan_dry_run(load_scenarios(SCENARIO_DIR), timestamp_ms=NOW_MS)

        assert any("Nothing was exercised" in line for line in report.render())


class TestEvidence:
    def test_a_dotted_path_is_read(self) -> None:
        source: dict[str, Any] = {"action_result": {"evidence": {"hunger_after": 0.31}}}

        assert read_field(source, "action_result.evidence.hunger_after") == (0.31, True)

    def test_a_missing_path_is_distinguished_from_a_null_value(self) -> None:
        source: dict[str, Any] = {"a": {"b": None}}

        # These mean opposite things: one was observed and is null, the other
        # was never seen at all.
        assert read_field(source, "a.b") == (None, True)
        assert read_field(source, "a.c") == (None, False)

    def test_collection_reports_what_was_missing(self, tmp_path: Path) -> None:
        scenario = load_scenario(write_scenario(tmp_path, "T01.yaml", MINIMAL))

        collected, missing = collect_evidence(scenario, {"action_result": {}})

        assert missing == ["action_result.reason_code"]
        assert collected[0].observed is False

    def test_a_boolean_assertion_survives_yaml(self) -> None:
        # `assertion: false` reads as "must be false"; YAML hands it over as a
        # bool, and requiring the author to quote it would push a parser detail
        # into a document written to be read.
        scenario = next(s for s in load_scenarios(SCENARIO_DIR) if s.id == "S01")
        armed = next(e for e in scenario.evidence if e.field_path == "heartbeat.game.armed")

        assert armed.assertion == "False"

    def test_an_observed_value_travels_with_its_assertion(self, tmp_path: Path) -> None:
        scenario = load_scenario(write_scenario(tmp_path, "T01.yaml", MINIMAL))

        collected, missing = collect_evidence(
            scenario, {"action_result": {"reason_code": "POSTCONDITION_MET"}}
        )

        assert missing == []
        assert collected[0].value == "POSTCONDITION_MET"
        assert collected[0].assertion == "POSTCONDITION_MET"


class TestEvidenceFiles:
    def test_unrun_scenarios_get_a_file_too(self, tmp_path: Path) -> None:
        report = plan_dry_run(load_scenarios(SCENARIO_DIR), only=["S05"], timestamp_ms=NOW_MS)

        write_evidence(report, tmp_path)

        # A directory holding only what ran would let a reader infer coverage
        # from what is present, which is the inference this refuses to support.
        assert (tmp_path / "S01.json").exists()
        assert (tmp_path / "S05.json").exists()

    def test_each_file_carries_the_stamp_and_the_outcome(self, tmp_path: Path) -> None:
        report = plan_dry_run(load_scenarios(SCENARIO_DIR), only=["S05"], timestamp_ms=NOW_MS)
        write_evidence(report, tmp_path)

        payload = json.loads((tmp_path / "S05.json").read_text(encoding="utf-8"))

        assert payload["outcome"] == "blocked"
        assert payload["dry_run"] is True
        assert payload["stamp"]["mod_version"]
        assert payload["stamp"]["timestamp_ms"] == NOW_MS

    def test_the_summary_counts_every_state(self, tmp_path: Path) -> None:
        report = plan_dry_run(load_scenarios(SCENARIO_DIR), only=["S05"], timestamp_ms=NOW_MS)
        write_evidence(report, tmp_path)

        summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
        counts = summary["counts"]

        assert counts["passed"] == 0
        assert counts["blocked"] == 1
        assert counts["not_run"] == 15
        assert sum(counts.values()) == 16


class TestCommand:
    def test_a_dry_run_succeeds_and_reports(self, tmp_path: Path) -> None:
        lines: list[str] = []

        code = run_smoke(
            scenario_dir=SCENARIO_DIR,
            evidence_dir=tmp_path,
            only=None,
            dry_run=True,
            timestamp_ms=NOW_MS,
            emit=lines.append,
        )

        assert code == 0
        assert any("not run" in line or "blocked" in line for line in lines)
        assert (tmp_path / "summary.json").exists()

    def test_a_live_run_is_refused_rather_than_faked(self, tmp_path: Path) -> None:
        lines: list[str] = []

        code = run_smoke(
            scenario_dir=SCENARIO_DIR,
            evidence_dir=tmp_path,
            only=None,
            dry_run=False,
            timestamp_ms=NOW_MS,
            emit=lines.append,
        )

        # Reporting success for a live run this process cannot perform is the
        # exact failure the harness is built to prevent.
        assert code == 1
        assert any("running Project Zomboid session" in line for line in lines)

    def test_a_bad_scenario_directory_fails_with_a_message(self, tmp_path: Path) -> None:
        lines: list[str] = []

        code = run_smoke(
            scenario_dir=tmp_path / "nowhere",
            evidence_dir=None,
            only=None,
            dry_run=True,
            timestamp_ms=NOW_MS,
            emit=lines.append,
        )

        assert code == 1
        assert any("not a directory" in line for line in lines)

    def test_an_unknown_scenario_id_fails_with_a_message(self) -> None:
        lines: list[str] = []

        code = run_smoke(
            scenario_dir=SCENARIO_DIR,
            evidence_dir=None,
            only=["S404"],
            dry_run=True,
            timestamp_ms=NOW_MS,
            emit=lines.append,
        )

        assert code == 1
        assert any("S404" in line for line in lines)

    def test_json_output_is_parseable(self, tmp_path: Path) -> None:
        lines: list[str] = []

        run_smoke(
            scenario_dir=SCENARIO_DIR,
            evidence_dir=None,
            only=["S01"],
            dry_run=True,
            timestamp_ms=NOW_MS,
            emit=lines.append,
            as_json=True,
        )

        payload = json.loads("\n".join(lines))
        assert payload["dry_run"] is True
        assert len(payload["scenarios"]) == 16


def test_outcome_conclusiveness() -> None:
    assert Outcome.PASSED.is_conclusive
    assert Outcome.FAILED.is_conclusive
    # Neither of these exercised anything, and treating them as conclusive is
    # how an unrun suite becomes a green release.
    assert not Outcome.NOT_RUN.is_conclusive
    assert not Outcome.BLOCKED.is_conclusive


def test_a_scenario_is_frozen() -> None:
    scenario = load_scenarios(SCENARIO_DIR)[0]

    with pytest.raises(AttributeError):
        scenario.id = "S99"  # type: ignore[misc]

    assert isinstance(scenario, Scenario)
