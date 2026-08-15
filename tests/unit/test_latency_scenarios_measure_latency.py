"""A scenario that declares it measures latency has to have measured it.

``measures_latency`` is declared by three scenarios — ``S04_MOVE``,
``S19_AUTONOMOUS_30_MIN``, ``S20_AUTONOMOUS_2_HOURS`` — and until this branch it
was read by exactly two things, both of which only *described* it:
``generate_playbook.py``, which prints "**latency measured** (p50/p95 recorded in
``result.json``)" under the scenario, and ``latency_summary``, which writes
``"measured": false, "samples": 0, "p50_ms": null`` when no samples were
supplied.

So the shipped document promised a measurement, the evidence recorded that none
was made, and the verdict said ``PASS``. Nothing downstream ever looked:
``finalize`` does not read the latency block, and neither does
``check_release.py``. That is what separates this from the ``game_build`` defect
beside it — there the release gate caught it eventually, late and expensively.
Here no gate anywhere would have caught it, and the promise in the playbook
would have shipped as evidence of a measurement nobody made.

The playbook made it certain rather than likely: its observations skeleton had
no ``latencies_ms`` field at all, so an operator following the document supplied
none by construction.

Naming a real source matters as much as the refusal. ``pz-agent latency --json``
publishes one entry per command in ``traces``, each carrying ``issued_at_ms`` and
``terminal_at_ms``; the sample is their difference. Without that the gate would
push an operator toward plausible invented numbers, which is worse than the
empty list it replaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.livetest.runner import (
    LATENCY_NOT_MEASURED,
    ObservedRun,
    build_result,
    decide,
    latency_summary,
    unmet_evidence,
)
from pz_agent_cli.livetest.scenarios import SCENARIOS, Check, LiveScenario, Postcondition, by_id
from pz_agent_cli.livetest.state import LiveState

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PLAYBOOK: Final = REPO_ROOT / "docs" / "LIVE_TEST_PLAYBOOK.md"
RESULT_SCHEMA: Final = REPO_ROOT / "evidence" / "schema" / "result.schema.json"

OBSERVED_BUILD: Final = "42.20.2"

#: Milliseconds, as they would come out of ``traces``: terminal minus issued.
SAMPLES: Final = (120, 138, 141, 205, 260)

MEASURING: Final = [scenario for scenario in SCENARIOS if scenario.measures_latency]


def _satisfying(scenario: LiveScenario, **overrides: Any) -> ObservedRun:
    """A run that passes every one of *scenario*'s postconditions."""
    observations: dict[str, Any] = {}
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for condition in scenario.postconditions:
        if condition.check.reads_snapshots:
            was, now = _snapshot_pair(condition.check)
            _place(before, condition.field, was)
            _place(after, condition.field, now)
        else:
            _place(observations, condition.field, _observed_value(condition))
    fields: dict[str, Any] = {
        "before": before,
        "after": after,
        "observations": observations,
        "game_build": OBSERVED_BUILD,
    }
    fields.update(overrides)
    return ObservedRun(**fields)


def _observed_value(condition: Postcondition) -> Any:
    if condition.check in {Check.EQUALS, Check.AT_LEAST, Check.AT_MOST}:
        return condition.expected
    if condition.check is Check.IS_TRUE:
        return True
    if condition.check is Check.IS_FALSE:
        return False
    return "observed"


def _snapshot_pair(check: Check) -> tuple[Any, Any]:
    return {
        Check.INCREASED: (1, 2),
        Check.DECREASED: (2, 1),
        Check.CHANGED: ("before", "after"),
        Check.UNCHANGED: ("same", "same"),
    }[check]


def _place(document: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    cursor = document
    for part in parents:
        cursor = cursor.setdefault(part, {})
    cursor[leaf] = value


def test_the_catalogue_still_declares_a_measuring_scenario() -> None:
    """Without one every parametrized test below would silently pass on nothing."""
    assert MEASURING, "no scenario declares measures_latency, so this file proves nothing"


@pytest.mark.parametrize("scenario", MEASURING, ids=lambda s: s.id)
def test_a_run_with_samples_passes(scenario: LiveScenario) -> None:
    """The control, and the proof that the gate is satisfiable at all."""
    run = _satisfying(scenario, latencies_ms=SAMPLES)
    status, outcomes = decide(scenario, run)

    assert all(outcome.passed for outcome in outcomes)
    assert status is LiveState.PASS


@pytest.mark.parametrize("scenario", MEASURING, ids=lambda s: s.id)
def test_the_same_run_with_no_samples_is_not_a_pass(scenario: LiveScenario) -> None:
    """The load-bearing one: a promised measurement that was never made."""
    run = _satisfying(scenario, latencies_ms=())
    status, outcomes = decide(scenario, run)

    assert all(outcome.passed for outcome in outcomes)
    assert status is LiveState.FAIL
    assert unmet_evidence(scenario, run) == LATENCY_NOT_MEASURED


@pytest.mark.parametrize("scenario", MEASURING, ids=lambda s: s.id)
def test_the_result_never_says_measured_false_beside_a_pass(scenario: LiveScenario) -> None:
    """The claim and the verdict, held against each other in the written document.

    This is the assertion the defect was: ``"measured": false`` and
    ``"status": "PASS"`` in the same result, under a playbook section that says
    the latency was measured.
    """
    run = _satisfying(scenario, latencies_ms=())
    status, outcomes = decide(scenario, run)
    document = build_result(
        scenario,
        run,
        status=status,
        outcomes=outcomes,
        attempt=1,
        started_at_ms=0,
        finished_at_ms=1,
        commit="0" * 40,
        failure_code=LATENCY_NOT_MEASURED,
    )
    latency = document["latency"]

    assert isinstance(latency, dict)
    assert latency["measured"] is False
    assert document["status"] != LiveState.PASS.value


@pytest.mark.parametrize(
    "scenario", [s for s in SCENARIOS if not s.measures_latency], ids=lambda s: s.id
)
def test_a_scenario_that_declares_nothing_is_not_held_to_it(scenario: LiveScenario) -> None:
    """Nineteen scenarios do not repeat an operation, and must not be blocked.

    A rule that demanded samples everywhere would be a gate nobody can pass,
    which gets switched off wholesale the first time it blocks something
    legitimate.
    """
    status, _ = decide(scenario, _satisfying(scenario, latencies_ms=()))

    assert status is LiveState.PASS


def test_the_summary_reports_the_percentiles_it_was_given() -> None:
    """The block the playbook promises, over samples that exist."""
    scenario = by_id("S04_MOVE")
    summary = latency_summary(scenario, _satisfying(scenario, latencies_ms=SAMPLES))

    assert summary["measured"] is True
    assert summary["samples"] == len(SAMPLES)
    assert summary["p50_ms"] is not None
    assert summary["p95_ms"] is not None


@pytest.mark.parametrize("scenario", MEASURING, ids=lambda s: s.id)
def test_the_playbook_gives_the_operator_the_field(scenario: LiveScenario) -> None:
    """The skeleton had no ``latencies_ms`` at all, so none was ever supplied."""
    text = PLAYBOOK.read_text(encoding="utf-8")
    section = text.split(f"## {scenario.id}\n", 1)[1].split("\n## ", 1)[0]

    assert '"latencies_ms": []' in section


@pytest.mark.parametrize("scenario", MEASURING, ids=lambda s: s.id)
def test_the_playbook_names_where_the_samples_come_from(scenario: LiveScenario) -> None:
    """A required field with no honest source invites invented numbers."""
    text = PLAYBOOK.read_text(encoding="utf-8")
    section = text.split(f"## {scenario.id}\n", 1)[1].split("\n## ", 1)[0]

    assert "pz-agent latency" in section
    assert "terminal_at_ms - issued_at_ms" in section


def test_the_skeleton_asks_for_samples_only_where_they_are_read() -> None:
    """The other direction, so the form does not cost time it cannot spend."""
    text = PLAYBOOK.read_text(encoding="utf-8")
    for scenario in SCENARIOS:
        section = text.split(f"## {scenario.id}\n", 1)[1].split("\n## ", 1)[0]
        assert ('"latencies_ms"' in section) is scenario.measures_latency


def test_the_schema_refuses_a_measurement_with_no_samples_behind_it() -> None:
    """A tightening, stated as one: ``latency_summary`` cannot produce this shape.

    It refuses a result assembled by any other route, where ``measured: true``
    over zero samples would be a percentile over nothing.
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(RESULT_SCHEMA.read_text(encoding="utf-8"))
    scenario = by_id("S04_MOVE")
    run = _satisfying(scenario, latencies_ms=SAMPLES)
    status, outcomes = decide(scenario, run)
    document = build_result(
        scenario,
        run,
        status=status,
        outcomes=outcomes,
        attempt=1,
        started_at_ms=0,
        finished_at_ms=1,
        commit="0" * 40,
        failure_code="",
    )

    jsonschema.validate(document, schema)  # the control

    document["latency"] = {"measured": True, "samples": 0, "p50_ms": None, "p95_ms": None}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)
