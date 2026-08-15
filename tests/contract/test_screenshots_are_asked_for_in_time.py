"""Seven scenarios owe a screenshot, and nothing told the operator to take one.

The sibling of the log defect beside it, and worse in the one way that matters:
a log exists on disk until the next game launch, so a late ``collect`` recovers
some of them. A screenshot is a moment in a running game. **No command produces
one** — ``live-test collect`` gathers logs and journals and never touches the
screenshots directory — and the moment is over when the scenario ends.

What the operator was told, in full: ``— screenshots required`` appended to the
**Evidence** line of seven playbook sections. Not the directory. Not when to
take it. Not that ``finalize`` refuses without it.
``LOCAL_AGENT_PROMPT.md``, ``LOCAL_GAME_HANDOFF.md`` and ``LOCAL_DEBUG_MAP.md``
mentioned screenshots zero times between them.

Measured rather than assumed: ``S11_CONTAINER`` is driven to PASS through the
real runner, every declared log is written, and the real ``finalize`` still
refuses — *"no screenshot was collected for a scenario that requires one"*.

The path in the instruction is composed from ``SCREENSHOTS_DIR_NAME``, the same
constant ``EvidenceLayout.screenshots_dir`` uses, so a rename moves the document
with the code instead of leaving the operator pointed at a directory that no
longer exists.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.livetest.evidence import SCREENSHOTS_DIR_NAME, EvidenceLayout
from pz_agent_cli.livetest.runner import FinalizeRefused, ObservedRun, finalize
from pz_agent_cli.livetest.scenarios import SCENARIO_IDS, SCENARIOS, Check, LiveScenario, by_id
from pz_agent_cli.livetest.state import LiveState, StateStore
from tests.unit.test_livetest_runner import FakeClock, FakeDriver, run

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCHEMA_SOURCE: Final = REPO_ROOT / "evidence" / "schema"
PLAYBOOK: Final = REPO_ROOT / "docs" / "LIVE_TEST_PLAYBOOK.md"
PROMPT: Final = REPO_ROOT / "docs" / "LOCAL_AGENT_PROMPT.md"

REQUIRING: Final = [scenario for scenario in SCENARIOS if scenario.screenshots_required]


def _place(document: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    cursor = document
    for part in parents:
        cursor = cursor.setdefault(part, {})
    cursor[leaf] = value


def _satisfying(scenario: LiveScenario) -> ObservedRun:
    """A run that passes every one of *scenario*'s postconditions."""
    observations: dict[str, Any] = {}
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for condition in scenario.postconditions:
        if condition.check.reads_snapshots:
            was, now = {
                Check.INCREASED: (1, 2),
                Check.DECREASED: (2, 1),
                Check.CHANGED: ("before", "after"),
                Check.UNCHANGED: ("same", "same"),
            }[condition.check]
            _place(before, condition.field, was)
            _place(after, condition.field, now)
        else:
            value: Any = "observed"
            if condition.check in {Check.EQUALS, Check.AT_LEAST, Check.AT_MOST}:
                value = condition.expected
            elif condition.check is Check.IS_TRUE:
                value = True
            elif condition.check is Check.IS_FALSE:
                value = False
            _place(observations, condition.field, value)
    return ObservedRun(
        before=before,
        after=after,
        observations=observations,
        game_build="42.20",
        latencies_ms=(120, 130) if scenario.measures_latency else (),
    )


def _section(text: str, scenario: LiveScenario) -> str:
    return text.split(f"## {scenario.id}\n", 1)[1].split("\n## ", 1)[0]


def test_the_catalogue_still_asks_for_screenshots() -> None:
    """Without a requiring scenario every test below would pass over nothing."""
    assert REQUIRING, "no scenario declares screenshots_required"


def test_a_passing_scenario_with_every_log_is_still_refused_without_a_screenshot(
    tmp_path: Path,
) -> None:
    """The measured fact. Real runner, real finalize, nothing stubbed.

    The logs are all written on purpose: this has to fail for the screenshot
    alone, or it would only be re-proving the log requirement next door.
    """
    scenario = by_id("S11_CONTAINER")
    assert scenario.screenshots_required

    layout = EvidenceLayout(tmp_path / "evidence")
    layout.ensure_tree(SCENARIO_IDS)
    for schema in SCHEMA_SOURCE.glob("*.json"):
        (layout.schema_dir / schema.name).write_bytes(schema.read_bytes())
    store = StateStore(layout.root)
    store.initialise(SCENARIO_IDS)

    run(layout, store, scenario.id, FakeDriver(_satisfying(scenario)))
    assert store.read(scenario.id).state is LiveState.PASS, "the fixture did not reach a PASS"
    for name in scenario.logs:
        (layout.logs_dir(scenario.id) / name).write_text("collected", encoding="utf-8")

    with pytest.raises(FinalizeRefused) as caught:
        finalize(
            layout=layout,
            store=store,
            scenarios=[scenario],
            output=tmp_path / "evidence-manifest.json",
            commit="0123456789abcdef",
            clock=FakeClock(),
        )

    missing = list(caught.value.missing)
    assert len(missing) == 1, f"expected the screenshot alone to be missing, got {missing}"
    assert SCREENSHOTS_DIR_NAME in missing[0]


def test_one_screenshot_satisfies_it(tmp_path: Path) -> None:
    """The control, and the proof the requirement is satisfiable at all."""
    scenario = by_id("S11_CONTAINER")
    layout = EvidenceLayout(tmp_path / "evidence")
    layout.ensure_tree(SCENARIO_IDS)
    for schema in SCHEMA_SOURCE.glob("*.json"):
        (layout.schema_dir / schema.name).write_bytes(schema.read_bytes())
    store = StateStore(layout.root)
    store.initialise(SCENARIO_IDS)

    run(layout, store, scenario.id, FakeDriver(_satisfying(scenario)))
    for name in scenario.logs:
        (layout.logs_dir(scenario.id) / name).write_text("collected", encoding="utf-8")
    (layout.screenshots_dir(scenario.id) / "container-open.png").write_bytes(b"\x89PNG\r\n")

    _, document = finalize(
        layout=layout,
        store=store,
        scenarios=[scenario],
        output=tmp_path / "evidence-manifest.json",
        commit="0123456789abcdef",
        clock=FakeClock(),
    )

    assert document["complete"] is True


def test_no_command_gathers_screenshots() -> None:
    """The premise of the instruction, held against the code rather than recalled.

    If ``collect`` ever learns to gather them, "nothing can produce it later"
    stops being true and the playbook's wording has to change with it.
    """
    commands = (
        REPO_ROOT / "packages" / "pz_agent_cli" / "src" / "pz_agent_cli" / "livetest"
    ) / "commands.py"

    assert "screenshot" not in commands.read_text(encoding="utf-8"), (
        "live-test now touches screenshots; the playbook says nothing can produce "
        "one after the scenario, and that claim needs revisiting"
    )


@pytest.mark.parametrize("scenario", REQUIRING, ids=lambda s: s.id)
def test_the_playbook_names_the_directory_for_every_requiring_scenario(
    scenario: LiveScenario,
) -> None:
    """The operator has to know where to put the file, per scenario."""
    section = _section(PLAYBOOK.read_text(encoding="utf-8"), scenario)

    assert f"evidence/{scenario.id}/{SCREENSHOTS_DIR_NAME}/" in section


@pytest.mark.parametrize("scenario", REQUIRING, ids=lambda s: s.id)
def test_the_playbook_says_when_to_take_it(scenario: LiveScenario) -> None:
    """A path with no moment attached is an instruction an operator meets too late."""
    section = _section(PLAYBOOK.read_text(encoding="utf-8"), scenario)

    assert re.search(r"while the scenario is running", section, re.IGNORECASE), (
        f"{scenario.id} does not say when the screenshot has to be taken"
    )


def test_the_playbook_asks_only_where_the_runner_asks() -> None:
    """The other direction: no instruction where no screenshot is required."""
    text = PLAYBOOK.read_text(encoding="utf-8")
    for scenario in SCENARIOS:
        section = _section(text, scenario)
        names_directory = f"evidence/{scenario.id}/{SCREENSHOTS_DIR_NAME}/" in section
        assert names_directory is scenario.screenshots_required, (
            f"{scenario.id}: playbook names a screenshots directory "
            f"{names_directory}, catalogue requires one {scenario.screenshots_required}"
        )


def test_the_prompt_mentions_screenshots_at_all() -> None:
    """It mentioned them zero times, in a document followed step by step."""
    text = PROMPT.read_text(encoding="utf-8")

    assert "screenshots_required" in text or "Скриншот" in text, (
        "the prompt handed to the local agent never mentions screenshots, and "
        "seven scenarios are refused at finalize without one"
    )
    assert SCREENSHOTS_DIR_NAME in text, "the prompt does not name the directory"
