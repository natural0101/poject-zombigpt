"""The acks the mod writes, read by the reader the sidecar actually uses.

The ack is the document the action lifecycle turns on. ``ActionResult.status``
is what the executor retires a command by, ``reason_code`` is what its recovery
table is keyed on, and an unknown code is raised rather than softened because
mapping it to ``INTERNAL_ERROR`` would hide a version mismatch.

**What this file adds, stated narrowly.** This was written expecting to find the
ack seam unchecked, the way the observation seam was. It is not. ``Handle:ack``
is pinned hard by ``tests/lua/test_action_runtime.lua``: dropping
``schema_version`` from the record, mapping ``LOST`` to a non-terminal status,
or removing ``INTERRUPTED`` from ``TERMINAL_PHASES`` each fail that suite
already. Those were tried, one at a time, against the real mod. So the gap this
file closes is not "the mod is unchecked" — it is one direction of the seam, and
only one:

    the sidecar's reader can tighten, and no Lua test can know that it did.

``ActionResult.from_dict`` requires eight fields, rejects an unknown reason code
outright, and wants both ids UUID-shaped. The mod's suite checks the fields *it*
knows to check. The two coincide today. Adding a requirement to ``from_dict``
that the mod does not satisfy leaves every Lua suite green — verified, by
planting exactly that — and fails here. That is the realistic direction of drift,
because the sidecar is where fields get added.

The second thing it holds is the correspondence across the rename:
``finished_at_ms`` is stamped by the mod from ``TERMINAL_PHASES``, keyed by a
phase the sidecar never sees, while the sidecar retires on
``ActionStatus.is_terminal`` over the status it was sent. ``interrupted``
travels as ``cancelled``, so the two tables have to agree through a rename
rather than by matching names.

The acks are appended by ``Handle:ack`` through the real runtime — commands
driven through ``ActionRuntime`` with spy adapters — and read here by
``ActionResult.from_dict``. The fakes in the dumper stand in for the filesystem
and the engine, never for the document.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_core.protocol import TERMINAL_STATUSES
from pz_agent_core.protocol.enums import ActionStatus
from pz_agent_core.protocol.messages import ActionResult

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DUMPER: Final = REPO_ROOT / "tests" / "lua" / "support" / "dump_action_acks.lua"

_INTERPRETERS: Final = ("lua5.4", "lua")


def _interpreter() -> str:
    for name in _INTERPRETERS:
        found = shutil.which(name)
        if found is not None:
            return found
    pytest.skip("no Lua interpreter is installed; the mod's own suite needs one too")


@pytest.fixture(scope="module")
def dumped() -> dict[str, Any]:
    assert DUMPER.is_file(), f"missing dumper: {DUMPER}"
    completed = subprocess.run(
        [_interpreter(), str(DUMPER)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert isinstance(document, dict)
    return document


@pytest.fixture(scope="module")
def acks(dumped: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ack from every scenario, flattened."""
    found = [ack for records in dumped["acks"].values() for ack in records]
    assert found, "the dumper produced no acks, so this file would prove nothing"
    return found


def test_the_runs_reached_more_than_one_status(acks: list[dict[str, Any]]) -> None:
    """A fixture that only ever reached ``accepted`` would assert almost nothing.

    Named statuses rather than a count: the interesting coverage is that a
    terminal ack and a non-terminal one both appear, since the whole file is
    about the boundary between them.
    """
    seen = {ack["status"] for ack in acks}

    assert "accepted" in seen
    assert seen & {status.value for status in TERMINAL_STATUSES}, (
        f"no run reached a terminal status; only {sorted(seen)} appeared"
    )


def test_every_ack_the_mod_writes_parses_as_an_action_result(
    acks: list[dict[str, Any]],
) -> None:
    """The assertion the two sides never made about each other.

    ``from_dict`` requires ``schema_version``, ``session_id``, ``seq``,
    ``command_id``, ``action``, ``status``, ``timestamp_ms`` and ``reason_code``,
    rejects an unknown reason code outright, and wants both ids UUID-shaped. A
    field the mod stopped writing is not a degraded ack — it is an ack the
    sidecar refuses, and a mod that looks silent while it is answering.
    """
    for ack in acks:
        result = ActionResult.from_dict(ack)

        assert result.action == ack["action"]
        assert result.status.value == ack["status"]
        assert result.reason_code.value == ack["reason_code"]


def test_the_mod_stamps_a_finish_time_exactly_on_the_acks_the_sidecar_retires(
    acks: list[dict[str, Any]],
) -> None:
    """``finished_at_ms`` is the mod's own answer; ``is_terminal`` is the sidecar's.

    The mod sets it from ``TERMINAL_PHASES``, keyed by a phase the sidecar never
    sees. If the two ever disagree, this is where it shows: a finished time on a
    command still running, or none on one that is over.
    """
    for ack in acks:
        result = ActionResult.from_dict(ack)
        stamped = "finished_at_ms" in ack

        assert stamped is result.status.is_terminal, (
            f"{ack['status']}: mod stamped finished_at_ms={stamped}, "
            f"sidecar calls it terminal={result.status.is_terminal}"
        )


# ---------------------------------------------------------------------------
# the tables behind the acks
#
# The mod's own suite pins these; both plants tried against them failed it. They
# are kept because they are cheap and state the rule in the sidecar's terms —
# `ActionStatus.is_terminal` rather than a second list — so a status added to the
# Python enum with the wrong terminality is caught here too.
# ---------------------------------------------------------------------------


def test_every_phase_maps_to_a_status_the_sidecar_knows(dumped: dict[str, Any]) -> None:
    """A phase mapped to a word that is not an ``ActionStatus`` is an unparseable ack.

    Checked over the whole table rather than over the phases these runs happened
    to reach: an unreachable-in-fixture phase is exactly where such a typo would
    survive.
    """
    wire_status = dumped["wire_status"]
    known = {status.value for status in ActionStatus}

    assert set(wire_status) == set(dumped["phases"].values()), (
        "WIRE_STATUS and PHASE name different phases"
    )
    unknown = {phase: status for phase, status in wire_status.items() if status not in known}
    assert not unknown, f"phases mapped to statuses the sidecar does not know: {unknown}"


def test_the_mod_retires_a_phase_exactly_when_its_status_is_terminal(
    dumped: dict[str, Any],
) -> None:
    """The whole mapping, not just the phases a fixture can drive to.

    ``interrupted`` is the case worth naming: it is a phase of its own on the
    mod side and travels as ``cancelled``, so the two tables have to agree
    through a rename rather than by matching names.
    """
    wire_status = dumped["wire_status"]
    terminal_phases = set(dumped["terminal_phases"])

    for phase, status in wire_status.items():
        sidecar_retires = ActionStatus(status).is_terminal

        assert (phase in terminal_phases) is sidecar_retires, (
            f"phase {phase!r} travels as {status!r}: mod retires="
            f"{phase in terminal_phases}, sidecar retires={sidecar_retires}"
        )
