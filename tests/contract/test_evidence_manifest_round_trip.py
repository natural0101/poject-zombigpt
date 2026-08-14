"""A manifest the runner really wrote, read by the gate that really guards the release.

This is the last mile of the operator's work. They run the scenarios inside
Project Zomboid — the catalogue's declared budget is 20 460 seconds, five hours
and forty-one minutes — then ``pz-agent live-test finalize`` writes
``release/evidence-manifest.json``, and ``check_release.py --release`` reads it.
If the two disagree about that document, the disagreement is discovered *after*
the hours in the game, and the evidence has to be produced again.

Nothing put one side's output into the other's reader. ``test_livetest_runner``
asserts what ``finalize`` writes; ``test_check_release`` builds manifests by
hand and asserts what the gate makes of them. Each side was tested against its
own idea of the document, which is the shape of every seam defect this project
has found.

**Scope, stated plainly.** Two scenarios are driven, not twenty-two. The fixture
that drives one to PASS supplies observations shaped for that scenario —
``S04_MOVE`` and ``S07_NESTED_INVENTORY`` have them, the rest would need
observations invented here, and a fixture invented to satisfy a postcondition is
the thing this repository refuses on the critical path. So this checks the
*document* across the seam, not the completeness of a real run: the gate's
scenario list is passed explicitly rather than taken from the catalogue.
Completeness is what ``finalize`` itself enforces, and ``test_livetest_runner``
proves it refuses a tree with anything missing, not passed, or tampered with.

What that leaves is the direction that matters here, the same one the ack seam
has: the gate can tighten and no runner test can know that it did.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.livetest.evidence import EvidenceLayout
from pz_agent_cli.livetest.runner import finalize
from pz_agent_cli.livetest.scenarios import SCENARIO_IDS, by_id
from pz_agent_cli.livetest.state import StateStore
from tests.unit.test_livetest_runner import MOVE, TRANSFER, FakeClock, complete_scenario

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))

check_release = importlib.import_module("scripts.check_release")

#: The evidence schemas, which live beside the tree they validate rather than
#: in ``schemas/`` — the runner refuses to write a result it cannot validate.
SCHEMA_SOURCE: Final = REPO_ROOT / "evidence" / "schema"

#: The two the fixture can drive honestly. Named rather than derived, so adding
#: a third is a deliberate act by someone who has written its observations.
DRIVEN: Final = (MOVE, TRANSFER)


@pytest.fixture
def written(tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    """A manifest produced by ``finalize`` over a real evidence tree."""
    layout = EvidenceLayout(tmp_path / "evidence")
    layout.ensure_tree(SCENARIO_IDS)
    for schema in SCHEMA_SOURCE.glob("*.json"):
        (layout.schema_dir / schema.name).write_bytes(schema.read_bytes())
    store = StateStore(layout.root)
    store.initialise(SCENARIO_IDS)
    for scenario_id in DRIVEN:
        complete_scenario(layout, store, scenario_id)

    destination = tmp_path / "evidence-manifest.json"
    path, document = finalize(
        layout=layout,
        store=store,
        scenarios=[by_id(scenario_id) for scenario_id in DRIVEN],
        output=destination,
        commit="0123456789abcdef",
        clock=FakeClock(),
    )
    return document, path, layout.root


def test_the_fixture_really_produced_a_manifest(
    written: tuple[dict[str, Any], Path, Path],
) -> None:
    """A refusal or an empty document would make everything below vacuous."""
    document, path, _ = written

    assert path.is_file()
    assert document["complete"] is True
    assert document["scenario_count"] == len(DRIVEN)
    assert document["artefacts"], "the manifest lists no artefacts"


def test_the_gate_recognises_the_format_the_runner_stamps(
    written: tuple[dict[str, Any], Path, Path],
) -> None:
    """One string, written by one module and compared by another.

    ``finalize`` stamps ``MANIFEST_FORMAT``; the gate refuses anything whose
    ``format`` is not ``LIVETEST_MANIFEST_FORMAT``, and refuses it before
    reading a single scenario — so a drift here costs the whole run and says
    nothing about what was observed.
    """
    document, _, _ = written

    assert document["format"] == check_release.LIVETEST_MANIFEST_FORMAT


def test_the_gate_reads_every_driven_scenario_as_passed(
    written: tuple[dict[str, Any], Path, Path],
) -> None:
    """``state`` crosses as a string and is compared against a literal.

    ``finalize`` writes ``audit.state.value``; the gate tests it against
    ``"PASS"``. Those are two spellings of one idea in two languages of the same
    program, and nothing had compared them.
    """
    document, _, _ = written

    findings = check_release._scenario_verdicts(document, list(DRIVEN))

    assert [finding.detail for finding in findings if not finding.ok] == []


def test_the_gate_verifies_the_digests_the_runner_recorded(
    written: tuple[dict[str, Any], Path, Path],
) -> None:
    """The artefact entries, hashed by one side and re-hashed by the other.

    This is the part that would cost the most: the gate opens each recorded path
    under the evidence root and compares its digest. A path written relative to
    one root and read relative to another passes every test on both sides and
    fails only here.
    """
    document, _, evidence_root = written

    findings = check_release._artefact_digests(document, list(DRIVEN), evidence_root)

    assert [finding.detail for finding in findings if not finding.ok] == []


def test_a_tampered_artefact_is_caught_by_the_gate_reading_the_runners_manifest(
    written: tuple[dict[str, Any], Path, Path],
) -> None:
    """The pair working, not merely agreeing.

    Both halves can agree on a document and still verify nothing. Editing a file
    after the manifest recorded it is the case the digests exist for, and it has
    to fail across the seam rather than only inside the runner's own audit.
    """
    document, _, evidence_root = written
    required = next(entry for entry in document["artefacts"] if entry.get("required"))
    victim = evidence_root / required["path"]
    victim.write_bytes(victim.read_bytes() + b"tampered")

    findings = check_release._artefact_digests(document, list(DRIVEN), evidence_root)

    assert [finding for finding in findings if not finding.ok], (
        "the gate accepted an artefact whose bytes no longer match the manifest"
    )
