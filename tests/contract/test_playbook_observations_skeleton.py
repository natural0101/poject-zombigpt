"""The playbook's observations skeleton is the document the runner reads.

``docs/LIVE_TEST_PLAYBOOK.md`` tells an operator, twenty-two times, to pass
``--observations <file>``. Until this branch it never said what the file had to
contain. The runner reads every postcondition by an exact dotted path — sixty-six
across the catalogue — and those paths existed nowhere but ``scenarios.py``: the
generated playbook published each postcondition's key and prose and dropped its
``field``. An operator working from the document they were handed could not write
a file the runner would read, and a wrong guess fails the postcondition,
correctly and unhelpfully. That is the last thing standing between the RC and the
eighty-four ``local`` tasks, and it is a documentation defect rather than a code
one.

The skeleton is generated from the same ``SCENARIOS`` the runner executes, so it
cannot drift — ``scripts/check.sh`` runs ``generate_playbook.py --check``. But
"generated from the same source" is not the same claim as "the runner would read
this", and this project has been caught by exactly that gap before. So these
tests **run the producer**: the JSON is lifted out of the published markdown,
fed through the real ``parse_observations``, and every postcondition is asked —
through the same ``read_field`` ``evaluate`` uses — whether the path it wants is
present in the document the operator was given.

Both directions, because a skeleton listing *everything* would pass a
presence-only check while telling the operator to record fields nothing reads:
every path in the document must belong to a postcondition, and every
postcondition must have its path in the document.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.livetest.runner import parse_observations
from pz_agent_cli.livetest.scenarios import SCENARIOS, LiveScenario
from pz_agent_cli.smoke import read_field

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PLAYBOOK: Final = REPO_ROOT / "docs" / "LIVE_TEST_PLAYBOOK.md"

_SKELETON: Final = re.compile(
    r"### The observations file.*?```json\n(?P<body>.*?)```",
    re.DOTALL,
)


def _skeletons() -> dict[str, dict[str, Any]]:
    """Every published skeleton, parsed, keyed by the scenario it names."""
    text = PLAYBOOK.read_text(encoding="utf-8")
    found: dict[str, dict[str, Any]] = {}
    for match in _SKELETON.finditer(text):
        document = json.loads(match.group("body"))
        found[str(document["scenario_id"])] = document
    return found


_PUBLISHED: Final = _skeletons()


def _paths(document: Any, prefix: str = "") -> set[str]:
    """Every leaf path in a nested document, dotted."""
    if not isinstance(document, dict):
        return {prefix} if prefix else set()
    if not document:
        return {prefix} if prefix else set()
    leaves: set[str] = set()
    for key, value in document.items():
        here = f"{prefix}.{key}" if prefix else str(key)
        leaves |= _paths(value, here) if isinstance(value, dict) and value else {here}
    return leaves


def test_the_playbook_publishes_a_skeleton_for_every_scenario() -> None:
    """A generator that quietly stopped emitting them would make this file vacuous."""
    assert set(_PUBLISHED) == {scenario.id for scenario in SCENARIOS}
    assert len(_PUBLISHED) == len(SCENARIOS)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_the_skeleton_parses_as_an_observations_document(scenario: LiveScenario) -> None:
    """Through the real parser the ``--observations`` flag uses, not a stand-in."""
    run = parse_observations(_PUBLISHED[scenario.id], scenario=scenario)

    assert run.blocked_reason == ""
    assert run.failure_code == ""


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_every_postcondition_finds_its_field_in_the_skeleton(scenario: LiveScenario) -> None:
    """The load-bearing one: the runner's own reader, over the operator's form.

    ``read_field`` is what ``evaluate`` calls. Asking it here means the assertion
    is about the path the runner will actually walk, not about two lists of
    strings agreeing.
    """
    run = parse_observations(_PUBLISHED[scenario.id], scenario=scenario)

    missing = []
    for condition in scenario.postconditions:
        sources = (
            [("before", run.before), ("after", run.after)]
            if condition.check.reads_snapshots
            else [("observations", run.observations)]
        )
        for label, document in sources:
            _, found = read_field(document, condition.field)
            if not found:
                missing.append(f"{condition.key}: {label}.{condition.field}")

    assert missing == [], (
        f"{scenario.id}'s skeleton does not carry {len(missing)} path(s) the runner "
        f"reads: {missing}"
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_the_skeleton_asks_for_nothing_the_runner_ignores(scenario: LiveScenario) -> None:
    """The other direction. A form that asks for more than is read wastes the
    operator's time on a machine where each scenario costs a session."""
    document = _PUBLISHED[scenario.id]
    wanted = {condition.field for condition in scenario.postconditions}

    for section in ("observations", "before", "after"):
        for path in _paths(document.get(section, {})):
            assert path in wanted, (
                f"{scenario.id}'s skeleton asks for {section}.{path}, which no postcondition reads"
            )


def test_the_skeleton_is_blank_so_an_unfilled_one_cannot_pass() -> None:
    """Handing the form back untouched must fail, not default to something.

    Every check refuses an unread value — asserted in
    ``tests/unit/test_postcondition_needs_a_reading.py`` — so ``null`` here is a
    form to complete rather than a value to accept. Pinned because a future edit
    that seeded "sensible" placeholders would turn the skeleton into a way to
    pass a scenario nobody ran.
    """
    for scenario_id, document in _PUBLISHED.items():
        for section in ("observations", "before", "after"):
            for path in _paths(document.get(section, {})):
                value, _ = read_field(document[section], path)
                assert value is None, (
                    f"{scenario_id}'s skeleton pre-fills {section}.{path} with {value!r}"
                )


def test_the_playbook_names_the_path_beside_every_postcondition() -> None:
    """The prose half: the operator has to know which path each statement means."""
    text = PLAYBOOK.read_text(encoding="utf-8")

    for scenario in SCENARIOS:
        for condition in scenario.postconditions:
            where = "before/after" if condition.check.reads_snapshots else "observations"
            assert f"`{where}.{condition.field}`" in text, (
                f"{scenario.id}/{condition.key} does not publish its field path"
            )
