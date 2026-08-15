"""The wrappers' comments are the operator's manual, and two of them were wrong.

``tests/contract/test_bat_wrappers_invoke_the_real_cli.py`` checks the command
line each wrapper *hands to the CLI*. This checks the part above it: the ``rem``
block an operator reads before double-clicking. On the machine that has the game
those comments are the manual — nobody there is reading ``scenarios.py`` — and
two of them contradicted the catalogue they describe.

**The count.** ``run-live-tests.bat`` opened by naming twenty live scenarios
and a range ending two short and ``finalize-release.bat`` with *"only when all twenty
scenarios are PASS"*. The catalogue holds twenty-two, ``S01_INSTALL`` through
``S22_BUILD``, and the playbook's own preamble discusses S21 and S22 at length as
the two irreversible rungs. An operator reading the wrapper would take the run to
be complete two scenarios early, and the two they would drop are the craft and
the placement — the only ones that change the world irreversibly.

The range half of this moved to
``test_scenario_ranges_match_the_catalogue.py``, which sweeps the whole tree —
this file's version was scoped to the wrappers, and a later sweep found the same
literal in six more places it could not see.

This is the third stale literal count this project has found: ``LIVE SCENARIOS:
0/20`` in the retired progress reporter, "twenty-two" spelled into an error
message in ``scenarios.py``, and now these. So the rule here is not "say
twenty-two" — that is only a fresher literal waiting to rot in a file that
cannot import anything. **A static wrapper states no count at all**, and this
asserts that, with the reason recorded so the next edit does not helpfully add
one back.

**The flag pair.** ``run-live-tests.bat`` advertised

    run-live-tests.bat --observations obs.json    hand it what you read back

which cannot work. ``--observations`` describes one scenario, so without
``--scenario`` the run selects every pending one and refuses:
*"--observations describes one scenario, but 22 were selected."* Measured, not
read: the example's own tokens go through the real ``resolve``. That form is the
only one that can produce a PASS — every run without observations is BLOCKED —
so the wrapper advertised the combination that never passes and omitted the one
that does.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.livetest.scenarios import SCENARIO_IDS, UnknownScenarioError, resolve

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BAT_DIR: Final = REPO_ROOT / "packaging" / "windows" / "bat"

WRAPPERS: Final = sorted(BAT_DIR.glob("*.bat"))

#: A scenario id as it appears in prose or on a command line.
_SCENARIO_ID: Final = re.compile(r"\bS\d{2}_[A-Z0-9_]+")

#: A claim about how many scenarios there are. Digits and the number words this
#: project has actually used; deliberately anchored to the word "scenario" so an
#: unrelated number in a comment is not accused.
_COUNT_CLAIM: Final = re.compile(
    r"\b(?:\d+|one|two|three|ten|eleven|twelve|twenty|twenty-one|twenty-two|"
    r"all\s+\w+)\s+(?:live\s+)?scenarios\b",
    re.IGNORECASE,
)

#: One ``rem`` usage example: the wrapper's own name followed by its flags.
_EXAMPLE: Final = re.compile(r"^rem\s+(?P<name>[a-z0-9-]+\.bat)\s+(?P<args>--\S.*?)\s*$", re.M)


def test_there_are_wrappers_to_check() -> None:
    """A glob that matched nothing would make every test below vacuous."""
    assert len(WRAPPERS) >= 11, f"only {len(WRAPPERS)} wrapper(s) found in {BAT_DIR}"


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=lambda p: p.name)
def test_every_scenario_id_a_wrapper_names_exists(wrapper: Path) -> None:
    """A renamed or dropped scenario leaves the wrapper pointing at nothing."""
    text = wrapper.read_text(encoding="utf-8")
    unknown = [found for found in _SCENARIO_ID.findall(text) if found not in set(SCENARIO_IDS)]

    assert unknown == [], f"{wrapper.name} names scenario id(s) not in the catalogue: {unknown}"


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=lambda p: p.name)
def test_no_wrapper_states_a_scenario_count(wrapper: Path) -> None:
    """A ``.bat`` cannot import the catalogue, so any count it states will rot.

    Not "state the right count": the right count today is the wrong count after
    the next scenario is added, and this project has already been caught by that
    literal three times. The wrappers say "every scenario" and point at the
    playbook, which is generated.
    """
    text = wrapper.read_text(encoding="utf-8")
    claims = _COUNT_CLAIM.findall(text)

    assert claims == [], (
        f"{wrapper.name} states a scenario count ({claims}); the catalogue holds "
        f"{len(SCENARIO_IDS)} and a static wrapper cannot track it. Say 'every scenario'."
    )


def _examples(wrapper: Path) -> list[tuple[str, list[str]]]:
    """Every ``rem`` usage line in *wrapper*, as (name, argv)."""
    text = wrapper.read_text(encoding="utf-8")
    return [(match.group("name"), match.group("args").split()) for match in _EXAMPLE.finditer(text)]


def test_the_run_wrapper_shows_at_least_one_example() -> None:
    """Otherwise the pairing test below would pass over an empty list."""
    assert _examples(BAT_DIR / "run-live-tests.bat"), (
        "run-live-tests.bat documents no usage form, so the operator has nothing to copy"
    )


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=lambda p: p.name)
def test_an_observations_example_names_exactly_one_scenario(wrapper: Path) -> None:
    """The rule the CLI states in its own refusal, applied to the manual.

    ``resolve`` is the real selector the run uses, so this asks what the
    operator's copied line would actually select rather than re-deriving it.
    """
    for name, argv in _examples(wrapper):
        if "--observations" not in argv:
            continue
        tokens = [argv[index + 1] for index, flag in enumerate(argv) if flag == "--scenario"]
        try:
            selected = resolve(tokens or None)
        except UnknownScenarioError:
            # The id itself is wrong, which the test above reports by name.
            # Letting the exception out here would bury that clear message under
            # a traceback from a test asking a different question.
            continue
        assert len(selected) == 1, (
            f"{wrapper.name} shows `{name} {' '.join(argv)}`, which selects "
            f"{len(selected)} scenario(s). --observations describes one, so the run "
            f"refuses this exact line; the form that works names --scenario too"
        )


def test_the_run_wrapper_shows_the_only_form_that_can_pass() -> None:
    """A wrapper whose examples all end in BLOCKED is a manual to a dead end.

    Every run without ``--observations`` uses the unavailable driver and records
    BLOCKED. So at least one documented form has to carry both flags, or the
    operator's first copied command cannot produce a PASS however correctly they
    played the scenario.
    """
    examples = _examples(BAT_DIR / "run-live-tests.bat")
    workable = [argv for _, argv in examples if "--observations" in argv and "--scenario" in argv]

    assert workable, (
        "run-live-tests.bat documents no form carrying both --scenario and "
        "--observations, and no other form can produce a PASS"
    )
