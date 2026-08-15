"""A PASS has to name the game it passed against.

``game_build`` is the one field a live result carries that no postcondition
covers. Twenty-one of the twenty-two scenarios say nothing about the build, and
the twenty-second, ``S01_INSTALL``, has a ``build_string`` postcondition that
reads ``observations.game.build`` — a *different* value from the top-level
``game_build`` the result records and the manifest gathers. So before this
branch every scenario, including S01, could reach ``PASS`` with the top-level
build unread.

What that costs is not abstract. ``build_result`` writes ``(not observed)``,
``finalize`` gathers it into the manifest's ``game_builds``, and
``check_release.py --rc`` refuses the archive: *"the evidence names build(s)
(not observed) … which is the runner saying nobody looked"*. That refusal is
right. Its timing was not: it arrives after all twenty-two live sessions have
been spent, on a machine the project does not have, and the remedy — one string
in a file the operator still has — was knowable at the first scenario.

The playbook made it likely rather than merely possible. Its skeleton emits
``"game_build": ""`` and its instruction reads *"fill every ``null``"*, which
names every field except this one.

Two refusals, because they fail differently. :func:`decide` refuses the verdict,
so an operator running the scenario is told at the scenario. The result schema
refuses the document, so an archive assembled by any other route is refused too.
The schema has to spell the marker literally, which is the drift this repository
keeps finding, so the last test here holds its literal against the constant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.livetest.runner import (
    BUILD_NOT_OBSERVED,
    POSTCONDITION_FAILED,
    UNOBSERVED_BUILD,
    ObservedRun,
    build_result,
    decide,
)
from pz_agent_cli.livetest.scenarios import SCENARIOS, Check, LiveScenario, Postcondition, by_id
from pz_agent_cli.livetest.state import LiveState

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
RESULT_SCHEMA: Final = REPO_ROOT / "evidence" / "schema" / "result.schema.json"

#: A build string good enough to stand in for one read off a running game.
#: Not a supported one on purpose: this file is about *naming* a build, and
#: whether the named build is supported is ``check_release.py``'s question.
OBSERVED_BUILD: Final = "42.20.2"


def _satisfying(scenario: LiveScenario, *, game_build: str) -> ObservedRun:
    """A run that passes every one of *scenario*'s postconditions.

    Built from the postconditions themselves rather than hand-written per
    scenario, so this stays true of a catalogue that grows.
    """
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

    return ObservedRun(
        before=before,
        after=after,
        observations=observations,
        game_build=game_build,
        # Supplied wherever the scenario declares a measurement, so this file
        # stays about the build alone. Three scenarios repeat an operation and
        # a PASS there requires samples too; leaving them out would make every
        # assertion here pass for the neighbouring reason.
        latencies_ms=(140, 150, 160) if scenario.measures_latency else (),
    )


def _observed_value(condition: Postcondition) -> Any:
    """A reading that satisfies *condition*, derived from its check.

    The catalogue's ``expected`` is meaningful only for the three checks that
    compare against it; for the rest it is ``None``, which is precisely the
    absence every check refuses. Deriving from the check keeps the control test
    honest — a fixture that failed the postconditions would make the whole file
    vacuous, which is why the control asserts they hold.
    """
    if condition.check in {Check.EQUALS, Check.AT_LEAST, Check.AT_MOST}:
        return condition.expected
    if condition.check is Check.IS_TRUE:
        return True
    if condition.check is Check.IS_FALSE:
        return False
    return "observed"


def _snapshot_pair(check: Check) -> tuple[Any, Any]:
    """(before, after) values that satisfy a snapshot check."""
    return {
        Check.INCREASED: (1, 2),
        Check.DECREASED: (2, 1),
        Check.CHANGED: ("before", "after"),
        Check.UNCHANGED: ("same", "same"),
    }[check]


def _place(document: dict[str, Any], path: str, value: Any) -> None:
    """Write *value* at a dotted *path*, creating the nesting."""
    *parents, leaf = path.split(".")
    cursor = document
    for part in parents:
        cursor = cursor.setdefault(part, {})
    cursor[leaf] = value


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_a_run_with_the_build_read_passes(scenario: LiveScenario) -> None:
    """The control. Without it a rule that failed everything would look right."""
    run = _satisfying(scenario, game_build=OBSERVED_BUILD)
    status, outcomes = decide(scenario, run)

    assert all(outcome.passed for outcome in outcomes), (
        f"{scenario.id}: the fixture does not satisfy its own postconditions, so "
        f"this file proves nothing about it"
    )
    assert status is LiveState.PASS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_the_same_run_without_a_build_is_not_a_pass(scenario: LiveScenario) -> None:
    """The load-bearing one, for every scenario rather than a chosen few."""
    run = _satisfying(scenario, game_build="")
    status, outcomes = decide(scenario, run)

    assert all(outcome.passed for outcome in outcomes)
    assert status is LiveState.FAIL


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_whitespace_is_not_a_build_string(blank: str) -> None:
    """``" "`` is what a form gets filled with when nobody looked."""
    scenario = by_id("S04_MOVE")
    status, _ = decide(scenario, _satisfying(scenario, game_build=blank))

    assert status is LiveState.FAIL


def test_the_failure_names_the_build_rather_than_the_game() -> None:
    """A FAIL code sends the operator somewhere. This one must not misdirect.

    ``POSTCONDITION_FAILED`` would send them to inspect a game that behaved
    correctly — the entire scenario held — over a field they can fill from the
    session they already ran.
    """
    scenario = by_id("S04_MOVE")
    run = _satisfying(scenario, game_build="")
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
        failure_code=_code(status, run, outcomes, scenario),
    )

    assert document["failure_code"] == BUILD_NOT_OBSERVED
    assert document["game_build"] == UNOBSERVED_BUILD


def test_a_real_postcondition_failure_still_says_so() -> None:
    """The new code must not swallow the old one.

    A scenario that genuinely failed, with the build read, is still
    ``POSTCONDITION_FAILED``; the two causes stay distinguishable.
    """
    scenario = by_id("S04_MOVE")
    run = ObservedRun(game_build=OBSERVED_BUILD)
    status, outcomes = decide(scenario, run)

    assert status is LiveState.FAIL
    assert not all(outcome.passed for outcome in outcomes)
    assert _code(status, run, outcomes, scenario) == POSTCONDITION_FAILED


def test_a_blocked_attempt_is_not_blamed_on_the_build() -> None:
    """An attempt that never reached the game has no build to read.

    Calling that BUILD_NOT_OBSERVED would blame the operator for a session
    that did not happen; BLOCKED already says what went wrong.
    """
    scenario = by_id("S04_MOVE")
    run = ObservedRun(blocked_reason="the sidecar is not attached")
    status, outcomes = decide(scenario, run)

    assert status is LiveState.BLOCKED
    assert _code(status, run, outcomes, scenario) != BUILD_NOT_OBSERVED


def test_the_schema_refuses_the_document_too() -> None:
    """The other refusal, over a result assembled by any route at all.

    Read as data and applied with the real validator, so this is the rule the
    evidence bundle is actually held to rather than a restatement of it.
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(RESULT_SCHEMA.read_text(encoding="utf-8"))
    scenario = by_id("S04_MOVE")
    run = _satisfying(scenario, game_build=OBSERVED_BUILD)
    _, outcomes = decide(scenario, run)
    document = build_result(
        scenario,
        run,
        status=LiveState.PASS,
        outcomes=outcomes,
        attempt=1,
        started_at_ms=0,
        finished_at_ms=1,
        commit="0" * 40,
        failure_code="",
    )

    jsonschema.validate(document, schema)  # the control: this one is admissible

    document["game_build"] = UNOBSERVED_BUILD
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)

    document["game_build"] = ""
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_the_schema_spells_the_marker_the_runner_writes() -> None:
    """A schema that refuses a different string than the code writes refuses nothing.

    JSON Schema cannot import a constant, so the literal is duplicated by
    necessity. This is the check that keeps the duplicate honest.
    """
    schema = json.loads(RESULT_SCHEMA.read_text(encoding="utf-8"))
    clauses = [
        branch["then"]["properties"]["game_build"]
        for branch in schema["allOf"]
        if "game_build" in branch.get("then", {}).get("properties", {})
    ]

    assert clauses, "no branch of the result schema constrains game_build"
    assert [clause["not"]["const"] for clause in clauses] == [UNOBSERVED_BUILD]


def _code(status: LiveState, run: ObservedRun, outcomes: Any, scenario: LiveScenario) -> str:
    """The runner's own derivation, reached through its module."""
    from pz_agent_cli.livetest import runner  # noqa: PLC0415

    return runner._failure_code(status, run, outcomes, scenario)
