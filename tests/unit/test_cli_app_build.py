"""The capability ledger's own failure branches, below the seam test.

``tests/contract/test_sidecar_capability_wiring.py`` proves the wiring works on
a machine where everything is where it should be. These are the branches that
only appear when it is not: a record file written by a different version, a
state directory that cannot be written into, and a loop handed two answers to
the same question.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pz_agent_cli import app
from pz_agent_cli.context import resolve_workspace
from pz_agent_cli.runtime import (
    CAPABILITY_FILE_NAME,
    CapabilityLedger,
    CapabilityRecord,
    LoopError,
    SidecarLoop,
    read_capability_record,
)
from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.builtin import register_builtins
from pz_agent_core.capabilities.probes import SURVIVAL_REST
from pz_agent_core.ipc.layout import IpcLayout
from tests.fixtures.cli_worlds import make_world

#: What a ledger says when the scan behind it never produced a report.
UNSCANNED = "the capability scan could not run — no installation to scan"


def a_closed_ledger(record_path: Path) -> CapabilityLedger:
    """A ledger in the fail-closed state: no report, and a reason for it."""
    return CapabilityLedger(record_path=record_path, detail=UNSCANNED)


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def test_a_ledger_with_no_report_refuses_every_capability(tmp_path: Path) -> None:
    ledger = a_closed_ledger(tmp_path / CAPABILITY_FILE_NAME)

    assert ledger.resolved is False
    assert ledger.usable(SURVIVAL_REST) is False
    assert ledger.usable("anything_at_all") is False
    assert ledger.record().resolved is False


def test_a_ledger_must_say_how_it_reached_its_answer(tmp_path: Path) -> None:
    """A ledger with no detail would refuse everything and explain nothing."""
    with pytest.raises(LoopError, match=r"how it reached its report, or why not"):
        CapabilityLedger(record_path=tmp_path / CAPABILITY_FILE_NAME, detail="")


def test_a_record_that_cannot_be_written_is_noted_and_not_raised(tmp_path: Path) -> None:
    """Losing the status file is not a reason to stop driving the game."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    ledger = a_closed_ledger(blocked / CAPABILITY_FILE_NAME)

    record = ledger.publish()

    assert record.resolved is False
    assert any("could not be written" in note for note in ledger.notes)


def test_the_loop_refuses_to_hold_a_ledger_and_a_separate_check(tmp_path: Path) -> None:
    """Two answers to "may this run" is the disagreement the wiring exists to close."""
    layout = IpcLayout(tmp_path / "pz_agent")
    with pytest.raises(LoopError, match=r"either a capability ledger or a capability_check"):
        SidecarLoop(
            layout=layout,
            state_dir=tmp_path / "state",
            registry=register_builtins(AdapterRegistry()),
            capabilities=a_closed_ledger(tmp_path / CAPABILITY_FILE_NAME),
            capability_check=lambda _name: True,
        )


# ---------------------------------------------------------------------------
# the record on disk
# ---------------------------------------------------------------------------


def test_a_record_survives_a_round_trip_through_the_state_directory(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    ledger = app.build_capabilities(workspace)

    written = ledger.publish()
    read_back = read_capability_record(workspace.state_dir / CAPABILITY_FILE_NAME)

    assert read_back == written
    assert read_back is not None
    assert SURVIVAL_REST in read_back.usable
    assert read_back.verified == ()


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({}, "nothing says whether anything was resolved"),
        ({"resolved": "yes", "detail": "x"}, "resolved is not a boolean"),
        ({"resolved": True}, "no detail"),
        ({"resolved": True, "detail": "x", "usable": "survival_rest"}, "usable is not an array"),
        ({"resolved": True, "detail": "x", "refused": []}, "refused is not an object"),
        ({"resolved": True, "detail": "x", "revision": "3"}, "revision is not an integer"),
    ],
)
def test_a_malformed_record_reads_as_absent_rather_than_half_understood(
    tmp_path: Path, payload: dict[str, object], why: str
) -> None:
    """Half a record would be read as "resolved" by whichever field survived."""
    path = tmp_path / CAPABILITY_FILE_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert read_capability_record(path) is None, why
    with pytest.raises(LoopError):
        CapabilityRecord.from_dict(payload)


def test_a_missing_record_is_absent_not_unresolved(tmp_path: Path) -> None:
    assert read_capability_record(tmp_path / CAPABILITY_FILE_NAME) is None


def test_an_unresolved_record_must_carry_its_reason(tmp_path: Path) -> None:
    with pytest.raises(LoopError, match=r"must say why it is unresolved"):
        CapabilityRecord.unresolved("")
