"""A correct live run must be able to clear the release bar, and one thing stops it.

Every test of ``scripts/check_release.py`` asserts that some particular check is
absent from the failures. None asked the question an operator's whole session
depends on: given a complete, correct evidence tree, does the gate *certify*?

Measured over the tree, it does not — and the reason is not the evidence. Fifteen
of sixteen checks pass; the sixteenth is ``evidence.version``:

    the evidence names product version 1.0.0, this checkout declares 0.1.0,
    and the release is v1.0.0

``finalize`` stamps ``PRODUCT_VERSION`` into the manifest, and the gate requires
that number to be the version being released. ``PRODUCT_VERSION`` is ``0.1.0``.
So a live session run against this tree produces evidence the release bar refuses,
and the gate's own remediation is *"bump version.py … then re-run the scenarios"*
— after twenty-two scenarios, a thirty-minute run and a two-hour run, on a machine
this repository cannot reach.

Nothing said so before the session. The playbook and the local-agent prompt did
not mention the version at all; the operator met it at the end or not at all.
``live-test prepare`` now names the number it will stamp, and this file pins the
two halves of the fact: the bar is otherwise reachable, and the one thing between
a correct run and certification is a version bump that has to happen *first*.

Deliberately not fixed by bumping the version here. What this repository declares
itself to be is a product decision, and taking it inside a test would be exactly
the yardstick-moving that
``tests/contract/test_the_blueprint_is_the_baseline.py`` exists to prevent.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_core.version import PRODUCT_VERSION

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

for _extra in (REPO_ROOT / "packaging" / "windows", REPO_ROOT / "scripts", REPO_ROOT):
    if str(_extra) not in sys.path:  # pragma: no cover - import plumbing
        sys.path.insert(0, str(_extra))

build_rc = importlib.import_module("build_rc")
check_release = importlib.import_module("check_release")

#: The release gate's own tests own the fixture that builds a complete evidence
#: tree. Reused rather than rebuilt: a second fixture would drift from the one
#: the gate is actually developed against, and then this file would be measuring
#: its own idea of a passing run.
release_tests = importlib.import_module("tests.unit.test_check_release")


def _full_release_run(tmp_path: Path) -> list[Any]:
    manifest, evidence = release_tests._evidence(tmp_path / "evidence")
    findings = release_tests._run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)
    return list(findings)


def test_a_complete_evidence_tree_clears_every_check_but_the_version(tmp_path: Path) -> None:
    findings = _full_release_run(tmp_path)
    failed = sorted(f.check for f in findings if not f.ok)

    assert len(findings) >= 16, f"only {len(findings)} checks ran; the gate is not being exercised"
    assert failed == ["evidence.version"], (
        "a complete, correct evidence tree should clear every check except the "
        f"version bump, and instead these failed: {failed}. If a new check has "
        "landed that a real live run cannot satisfy, the operator's session is "
        "spent before they learn of it."
    )


def test_the_version_is_the_only_thing_between_a_live_run_and_v1(tmp_path: Path) -> None:
    """Named as a fact, so that bumping the version has to update this file too.

    When ``PRODUCT_VERSION`` becomes the release version this test fails, and
    the person doing the bump is the right person to decide what it should then
    say — rather than a green suite quietly outliving the reason it was written.
    """
    assert PRODUCT_VERSION != build_rc.RELEASE_VERSION, (
        f"PRODUCT_VERSION is now {PRODUCT_VERSION}, the release version. The "
        "obstacle this file documents is gone: re-run the release gate over a "
        "complete evidence tree and rewrite this file around whatever it says now."
    )

    findings = _full_release_run(tmp_path)
    version = next(f for f in findings if f.check == "evidence.version")

    assert not version.ok
    assert PRODUCT_VERSION in version.detail
    assert build_rc.RELEASE_VERSION in version.detail
    assert "re-run the scenarios" in (version.remediation or ""), (
        "the remediation must say the scenarios have to be run again, because "
        "that is the cost the operator is being asked to pay"
    )


def test_prepare_tells_the_operator_the_version_it_will_stamp() -> None:
    """The last cheap moment to hear it, checked at the source that says it.

    ``run`` refuses unless ``prepare`` wrote a ready record, so ``prepare`` is
    the one command every live session passes through. It publishes the number
    rather than refusing on it: whether this tree should declare the release
    version is a product decision, and a live run made for some other reason is
    legitimate.
    """
    source = (
        REPO_ROOT
        / "packages"
        / "pz_agent_cli"
        / "src"
        / "pz_agent_cli"
        / "livetest"
        / "commands.py"
    ).read_text(encoding="utf-8")

    assert '"product_version": PRODUCT_VERSION' in source
    assert "finalize stamps this into the manifest" in source


@pytest.mark.parametrize(
    "document",
    [
        REPO_ROOT / "docs" / "LIVE_TEST_PLAYBOOK.md",
        REPO_ROOT / "docs" / "LOCAL_AGENT_PROMPT.md",
    ],
    ids=lambda path: path.name,
)
def test_the_handoff_documents_warn_before_the_session_is_spent(document: Path) -> None:
    """Neither of these mentioned the version at all, which is how it stays lost.

    The 84 open tasks say ``follow docs/LIVE_TEST_PLAYBOOK.md``; the prompt is
    what the local agent is given. A warning that lives only in the gate's
    output arrives after the last scenario.
    """
    text = document.read_text(encoding="utf-8")

    assert "version.py" in text, f"{document.name} does not mention the version bump at all"
    assert "re-run" in text or "run again" in text, (
        f"{document.name} names the bump without naming its cost — the scenarios "
        "have to be run again, and that is the part that decides whether an "
        "operator does it first"
    )
