"""Every scenario owes its logs, and the prompt told the operator to collect at the end.

``docs/LOCAL_AGENT_PROMPT.md`` is not a reference — it is the text pasted into a
fresh session on the machine with the game, and it is followed step by step. Two
of its steps were wrong in a way that costs the run.

**The collection order.** §5 collected evidence *after a FAIL*, and §7 step 4
said ``collect-evidence.bat`` — collect everything — once every scenario had
passed. But ``finalize`` requires each scenario's declared logs whether it
passed or not: ``_audit_one`` adds every name in ``scenario.logs`` as
``required=True`` with no reference to the verdict, and all twenty-two scenarios
declare some. So an operator whose run went well the first time would collect
nothing, and meet a refusal naming five missing files per scenario.

That one does not repair. ``console.txt`` is rewritten on every game launch —
``collect-evidence.bat``'s own header says so — and the session trace rotates.
By the end of a day of scenarios the logs of the early ones are gone, and the
only remedy is to run them again, in the game, for hours.

``docs/LOCAL_GAME_HANDOFF.md`` did carry the right rule — *"run ``live-test
collect`` at the end of each scenario rather than at the end of the day"* — six
hundred lines in, inside a paragraph about trace rotation, justified by the
trace rather than by the refusal. Two shipped documents in one handoff bundle
disagreed about the order of operations, and the one written as instructions
had it wrong.

**The count.** The same prompt said *"двадцать сценариев S01-S20"* (with an en dash) and headed
its final section *"После того как все двадцать сценариев PASS"*, against a
catalogue of twenty-two ending at ``S22_BUILD`` — while its sibling
``LOCAL_GAME_HANDOFF.md`` says twenty-two correctly, twice.

The rule for prose is not the rule for a ``.bat``. A wrapper cannot import
anything, so it states no count at all; a document may reasonably say how many
there are, and ``LOCAL_GAME_HANDOFF.md`` does it correctly. So what is asserted
here is that a count, where one is stated, equals the catalogue's — checked in
both English and Russian, because this bundle is written in both.

``CHANGELOG.md`` and ``FINAL_IMPLEMENTATION_REPORT.md`` are excluded by name.
They record what was true when a defect was found — *"nineteen of the twenty
live scenarios name that file"* — and editing a historical record to today's
number would falsify it rather than fix it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.livetest.evidence import EvidenceLayout
from pz_agent_cli.livetest.runner import FinalizeRefused, finalize
from pz_agent_cli.livetest.scenarios import SCENARIO_IDS, SCENARIOS, by_id
from pz_agent_cli.livetest.state import LiveState, StateStore
from tests.unit.test_livetest_runner import (
    MOVE,
    FakeClock,
    FakeDriver,
    _passing_move,
    run,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCHEMA_SOURCE: Final = REPO_ROOT / "evidence" / "schema"
PROMPT: Final = REPO_ROOT / "docs" / "LOCAL_AGENT_PROMPT.md"

#: The documents an operator follows as instructions. Named rather than globbed:
#: a glob would sweep in the historical records excluded above, and widening
#: this list should be a deliberate act.
INSTRUCTIONS: Final = (
    PROMPT,
    REPO_ROOT / "docs" / "LOCAL_GAME_HANDOFF.md",
    REPO_ROOT / "docs" / "LIVE_TEST_PLAYBOOK.md",
)

#: Number words this bundle actually uses, in both its languages.
_WORDS: Final = {
    "twenty": 20,
    "twenty-one": 21,
    "twenty-two": 22,
    "twenty-three": 23,
    "двадцать": 20,
    "двадцати": 20,
    "двадцать одного": 21,
    "двадцать два": 22,
    "двадцать двух": 22,
}

_COUNT: Final = re.compile(
    r"\b(?P<count>\d+|" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")\s+"
    r"(?:live\s+|живых\s+)?(?:scenarios|сценариев|сценария)\b",
    re.IGNORECASE,
)

# The dashes are spelled by codepoint: a literal en or em dash in a pattern
# is flagged as ambiguous, and a range in prose is as likely to use one.
_RANGE: Final = re.compile(r"\bS\d{2}\b\s*(?:to|through|[-\u2013\u2014])\s*\bS(?P<last>\d{2})\b")


def test_a_scenario_that_passed_still_owes_every_log_it_declares(tmp_path: Path) -> None:
    """The measured fact the documentation has to reflect.

    Not a claim about prose: the real runner drives ``S04_MOVE`` to PASS, no
    logs are collected, and the real ``finalize`` is asked what it makes of
    that. If this ever stops refusing, the instruction below stops being
    load-bearing and should be revisited rather than kept out of habit.
    """
    layout = EvidenceLayout(tmp_path / "evidence")
    layout.ensure_tree(SCENARIO_IDS)
    for schema in SCHEMA_SOURCE.glob("*.json"):
        (layout.schema_dir / schema.name).write_bytes(schema.read_bytes())
    store = StateStore(layout.root)
    store.initialise(SCENARIO_IDS)

    run(layout, store, MOVE, FakeDriver(_passing_move()))
    assert store.read(MOVE).state is LiveState.PASS, "the fixture did not reach a PASS"

    with pytest.raises(FinalizeRefused) as caught:
        finalize(
            layout=layout,
            store=store,
            scenarios=[by_id(MOVE)],
            output=tmp_path / "evidence-manifest.json",
            commit="0123456789abcdef",
            clock=FakeClock(),
        )

    missing = list(caught.value.missing)
    assert len(missing) == len(by_id(MOVE).logs), (
        f"expected one refusal per declared log, got {missing}"
    )
    assert any("console.txt" in entry for entry in missing)


def test_every_scenario_declares_logs_so_none_is_exempt() -> None:
    """A scenario declaring none would make the instruction conditional."""
    silent = [scenario.id for scenario in SCENARIOS if not scenario.logs]

    assert silent == [], f"these scenarios declare no logs: {silent}"


def test_the_prompt_tells_the_operator_to_collect_after_every_scenario() -> None:
    """The instruction the measured fact above requires.

    Prose, and narrow on purpose: what is asserted is that the per-scenario
    collect command appears together with the reason it cannot wait, not any
    particular wording around it.
    """
    text = PROMPT.read_text(encoding="utf-8")

    assert "collect-evidence.bat --scenario" in text
    assert "console.txt" in text, "the prompt does not say which file is destroyed"
    assert re.search(r"после\s+КАЖДОГО\s+сценария", text, re.IGNORECASE), (
        "the prompt does not tell the operator to collect after every scenario, "
        "so an operator whose run went well collects nothing and finalize refuses"
    )


def test_the_prompt_no_longer_collects_only_at_the_end() -> None:
    """The step that produced the defect: one collect, after everything passed."""
    text = PROMPT.read_text(encoding="utf-8")

    assert "`collect-evidence.bat` — собрать всё." not in text, (
        "§7 still presents collection as a single end-of-run step"
    )


@pytest.mark.parametrize("document", INSTRUCTIONS, ids=lambda p: p.name)
def test_a_stated_scenario_count_matches_the_catalogue(document: Path) -> None:
    """Where a document states a count, it must be the catalogue's."""
    text = document.read_text(encoding="utf-8")
    wrong = []
    for match in _COUNT.finditer(text):
        raw = match.group("count").lower()
        value = _WORDS.get(raw, raw if not raw.isdigit() else int(raw))
        if isinstance(value, int) and value != len(SCENARIO_IDS):
            wrong.append(match.group(0))

    assert wrong == [], (
        f"{document.name} states {wrong}, and the catalogue holds {len(SCENARIO_IDS)}"
    )


@pytest.mark.parametrize("document", INSTRUCTIONS, ids=lambda p: p.name)
def test_a_stated_scenario_range_ends_where_the_catalogue_does(document: Path) -> None:
    """ "S01-S20" is the same stale literal wearing a different shape."""
    text = document.read_text(encoding="utf-8")
    last = SCENARIO_IDS[-1][1:3]
    wrong = [match.group(0) for match in _RANGE.finditer(text) if match.group("last") != last]

    assert wrong == [], (
        f"{document.name} names range(s) {wrong}; the catalogue ends at {SCENARIO_IDS[-1]}"
    )


def test_the_count_pattern_would_catch_the_defect_it_was_written_for() -> None:
    """A control over the matcher itself, in both languages.

    A pattern that matched nothing would make the two tests above pass over any
    document at all, which is the failure mode of every prose check.
    """
    caught = [
        match.group("count").lower()
        for match in _COUNT.finditer(
            "двадцать сценариев S01\u2013S20 ... all twenty scenarios ... 20 scenarios"
        )
    ]

    assert caught == ["двадцать", "twenty", "20"]
    assert all(_WORDS.get(word, 20 if word == "20" else None) == 20 for word in caught)
