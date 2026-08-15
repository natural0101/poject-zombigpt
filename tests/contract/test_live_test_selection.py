"""A selection that names nothing must be refused, not answered with nothing.

``pz-agent live-test run`` is the command that produces the evidence for every
live task in the plan — the 84 the whole project is blocked on. Given
``--scenario ""`` it printed

    nothing to run: every scenario is PASS.

and exited **0**, with all twenty-two scenarios ``NOT_RUN``. Two false
statements in one line: none of them was PASS, and the reason nothing ran was
that the selection resolved to nothing, not that the work was done. An operator
reading that at the game machine has been told their run succeeded.

The trigger is not exotic. ``--scenario "$SCENARIO"`` with the variable unset is
how a scripted live session produces exactly this, and the live session is
scripted — ``docs/LIVE_TEST_PLAYBOOK.md`` is generated precisely so the operator
can work from a list.

The root was in :func:`resolve`: it drops blank tokens (a repeated flag picks up
stray whitespace, which is fine) and then returned an empty tuple when *every*
token was blank. ``_selection`` asks ``if only:`` — true for ``[""]`` — so an
explicit request reached the branch meant for "nothing left to do". Both halves
are fixed and both are held here: ``resolve`` refuses a selection that names
nothing, and the run's empty branch says how many scenarios it means.

These tests drive the real CLI through ``app.main`` against a real evidence tree,
because what was wrong was the exit code and the sentence a person reads, not an
internal return value.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli import app
from pz_agent_cli.livetest import EvidenceLayout
from pz_agent_cli.livetest.scenarios import SCENARIO_IDS, UnknownScenarioError, resolve

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: Values a shell hands over when a variable did not expand, or when a list was
#: built with a trailing separator. Every one of them is a request that names
#: nothing.
BLANK: Final = ("", "   ", "\t")


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Path, Path]:
    """An evidence tree that has passed ``prepare``, so ``run`` gets past its guard."""
    zomboid = tmp_path / "Zomboid"
    zomboid.mkdir()
    evidence = tmp_path / "evidence"
    layout = EvidenceLayout(evidence)
    layout.ensure_tree(SCENARIO_IDS)
    layout.prepare_path.write_text(json.dumps({"ready": True}), encoding="utf-8")
    return zomboid, evidence


def _live(prepared: tuple[Path, Path], *argv: str) -> tuple[int, str]:
    zomboid, evidence = prepared
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        try:
            code = app.main(
                [
                    "--zomboid-dir",
                    str(zomboid),
                    "live-test",
                    "--evidence-dir",
                    str(evidence),
                    *argv,
                ]
            )
        except SystemExit as exc:  # pragma: no cover - argparse only
            code = int(exc.code or 0)
    return code, captured.getvalue()


# ---------------------------------------------------------------------------
# the false success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", BLANK)
@pytest.mark.parametrize("subcommand", ["run", "collect"])
def test_a_selection_that_names_nothing_is_refused(
    prepared: tuple[Path, Path], subcommand: str, blank: str
) -> None:
    code, output = _live(prepared, subcommand, "--scenario", blank)

    assert code != 0, f"{subcommand} --scenario {blank!r} succeeded having done nothing"
    assert "names no scenario" in output
    assert "every scenario is PASS" not in output
    assert "all 22 scenarios are PASS" not in output


def test_the_refusal_names_the_scenarios_that_do_exist(prepared: tuple[Path, Path]) -> None:
    """A refusal an operator cannot act on sends them to the source instead."""
    _, output = _live(prepared, "run", "--scenario", "")

    for scenario_id in (SCENARIO_IDS[0], SCENARIO_IDS[-1]):
        assert scenario_id in output


def test_nothing_claims_a_pass_that_was_never_recorded(prepared: tuple[Path, Path]) -> None:
    """The specific sentence, against the state that makes it false.

    Every scenario in this tree is ``NOT_RUN``. Any output claiming they are
    ``PASS`` is the defect, whatever the exit code.
    """
    status_code, status_output = _live(prepared, "status")
    _, run_output = _live(prepared, "run", "--scenario", "")

    assert status_code != 0
    # The tally line, not a count of the word: the per-scenario rows and the
    # summary both say NOT_RUN, and counting occurrences made this assert about
    # the report's layout rather than about the tree's state.
    assert "PASS 0" in status_output and f"NOT_RUN {len(SCENARIO_IDS)}" in status_output, (
        f"the fixture is not in the state this test is about:\n{status_output}"
    )
    assert "PASS" not in run_output


# ---------------------------------------------------------------------------
# ... without breaking the selections that mean something
# ---------------------------------------------------------------------------


def test_omitting_the_flag_still_selects_the_whole_catalogue() -> None:
    """The other direction: refusing everything would satisfy the tests above."""
    assert len(resolve(None)) == len(SCENARIO_IDS)


def test_a_named_scenario_still_resolves_to_exactly_that_one() -> None:
    assert [scenario.id for scenario in resolve(["S04_MOVE"])] == ["S04_MOVE"]


def test_a_named_scenario_with_stray_whitespace_still_resolves() -> None:
    """Blank tokens are dropped on purpose; this is the case that needs them to be."""
    assert [scenario.id for scenario in resolve(["  S04_MOVE  ", ""])] == ["S04_MOVE"]


def test_an_unknown_id_is_still_refused_by_name() -> None:
    with pytest.raises(UnknownScenarioError) as caught:
        resolve(["S19_AUTONOMOUS_30MIN"])

    assert "S19_AUTONOMOUS_30MIN" in str(caught.value)


# ---------------------------------------------------------------------------
# the count is counted
# ---------------------------------------------------------------------------


def test_the_unknown_scenario_message_counts_the_catalogue() -> None:
    """It said "the twenty-two are" beside the list it was printing.

    The catalogue already grew once, from twenty to twenty-two, and left written
    counts behind elsewhere in the tree — a literal ``20`` in the status
    reconciler and a ``/20`` in the progress report, both removed for the same
    reason. This is the same literal in the one place an operator reads it, next
    to the list that contradicts it.
    """
    with pytest.raises(UnknownScenarioError) as caught:
        resolve(["S99_NOT_A_SCENARIO"])
    message = str(caught.value)

    assert f"the {len(SCENARIO_IDS)} are" in message
    assert "twenty-two" not in message
    assert message.count(",") == len(SCENARIO_IDS) - 1, (
        "the message no longer lists exactly the catalogue"
    )


def test_the_empty_run_message_counts_the_catalogue_too() -> None:
    """Read from the source: reaching that branch needs all 22 scenarios PASS."""
    commands = (
        REPO_ROOT
        / "packages"
        / "pz_agent_cli"
        / "src"
        / "pz_agent_cli"
        / "livetest"
        / "commands.py"
    ).read_text(encoding="utf-8")

    assert "nothing to run: every scenario is PASS." not in commands
    assert "all {len(SCENARIO_IDS)} scenarios are PASS" in commands


def test_this_module_is_importable_from_the_installed_package() -> None:
    """Guard the guard: a rename of the entry point would skip everything above."""
    assert hasattr(app, "main")
    assert sys.modules["pz_agent_cli.livetest.scenarios"].SCENARIO_IDS == SCENARIO_IDS
