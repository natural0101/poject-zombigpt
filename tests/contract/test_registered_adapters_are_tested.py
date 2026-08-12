"""Every adapter the registry serves must be exercised by a test that builds it.

An adapter reaches a user through exactly one door: ``registry.register(...)`` in
:mod:`pz_agent_core.actions.adapters`. Everything past that door is reachable —
the engine dispatches to it, the MCP surface offers it, and a planner can name
it. A registered adapter with no test is therefore not "an untested helper"; it
is a live code path whose refusals, arguments and postcondition nobody has ever
watched run.

``DrinkSourceAdapter`` was in exactly that state: registered, dispatched,
exported from the package, offered as an MCP action, and constructed by no test
anywhere in this repository. It was found by hand while chasing an unrelated
defect, which is not a method. The suite was fully green throughout — this is the
same shape as a test group placed after ``Harness.finish``, one layer up: a green
count that does not cover what it appears to cover.

The check is deliberately crude. It asks only whether some test *builds* each
adapter, not whether the test is any good, because the alternative — a coverage
percentage or a per-method census — measures something nobody has agreed on and
fails for reasons unrelated to the gap it exists to catch. One construction is a
low bar that ``DrinkSourceAdapter`` still could not clear.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: ``registry.register(SomeAdapter())`` — the one door onto the dispatcher.
_REGISTERED: Final = re.compile(r"registry\.register\(\s*(\w+)\(\)\s*\)")

_ADAPTERS_INIT: Final = (
    REPO_ROOT
    / "packages"
    / "pz_agent_core"
    / "src"
    / "pz_agent_core"
    / "actions"
    / "adapters"
    / "__init__.py"
)


def _registered_adapters() -> tuple[str, ...]:
    text = _ADAPTERS_INIT.read_text(encoding="utf-8")
    return tuple(dict.fromkeys(_REGISTERED.findall(text)))


def _test_sources() -> list[Path]:
    """Every test file, except this one.

    Excluding this file matters: it names all of them in its own failure
    message, and a check that counted its own diagnostics as coverage would pass
    the moment it started failing.
    """
    here = Path(__file__).resolve()
    return [p for p in REPO_ROOT.joinpath("tests").rglob("*.py") if p.resolve() != here]


def test_the_registry_scan_finds_the_adapters() -> None:
    """A regex that stopped matching would make the check below vacuous."""
    found = _registered_adapters()
    assert len(found) >= 20, (
        f"only {len(found)} registered adapters were found in {_ADAPTERS_INIT.name}; "
        "the scan has stopped seeing registrations and everything below is hollow"
    )
    assert "MoveToAdapter" in found, "the scan is not finding registrations it should"


def test_every_registered_adapter_is_built_by_some_test() -> None:
    """A registered adapter nobody constructs is a live path nobody has run."""
    sources = _test_sources()
    assert len(sources) >= 50, "the test-file glob has stopped finding the suite"
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in sources)

    unbuilt = [name for name in _registered_adapters() if not re.search(rf"\b{name}\(", corpus)]
    assert unbuilt == [], (
        "these adapters are registered — dispatched, exported and offered — and no "
        "test constructs them, so their refusals and postconditions have never run"
    )
