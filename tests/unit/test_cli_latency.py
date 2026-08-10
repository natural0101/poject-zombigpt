"""``pz-agent latency``: the table, the JSON document, and the exit-code rule.

The rule under test in the exit-code cases is the one that keeps CI honest on
a gameless machine: UNMEASURED is exit 0 even under ``--targets``, because a
runner with no live data has nothing to fail about; only a target that was
measured and missed fails the command, and only when the comparison was asked
for.
"""

from __future__ import annotations

import json
from pathlib import Path

from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK
from pz_agent_core.ipc.journal import JournalWriter
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import ActionResult, ActionStatus, Command, ReasonCode
from tests.fixtures.cli_worlds import START_MS, CliWorld, make_world
from tests.fixtures.ipc_builders import make_command


def _exchange(world: CliWorld) -> IpcLayout:
    root = world.ipc_root
    assert root is not None
    layout = IpcLayout(root)
    layout.ensure()
    return layout


def _write_command(layout: IpcLayout, command: Command) -> None:
    writer = JournalWriter(layout, layout.command_queue)
    try:
        writer.append(command.to_dict())
    finally:
        writer.close()


def _write_accepted(layout: IpcLayout, command: Command, *, timestamp_ms: int) -> None:
    ack = ActionResult(
        session_id=command.session_id,
        seq=0,
        command_id=command.command_id,
        action=command.action.value,
        status=ActionStatus.ACCEPTED,
        reason_code=ReasonCode.POSTCONDITION_MET,
        timestamp_ms=timestamp_ms,
    )
    writer = JournalWriter(layout, layout.command_ack)
    try:
        writer.append(ack.to_dict())
    finally:
        writer.close()


def test_latency_on_a_fresh_machine_reports_unmeasured_and_succeeds(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert world.run("latency") == EXIT_OK
    assert "pz-agent latency" in world.stdout
    assert "unmeasured" in world.stdout


def test_latency_json_carries_the_raw_document(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert world.run("latency", "--json") == EXIT_OK

    document = json.loads(world.stdout)
    assert document["distributions"]["submit_to_accepted"]["count"] == 0
    assert document["distributions"]["submit_to_accepted"]["cross_clock"] is True
    assert "ipc_root" in document
    assert "targets" not in document


def test_latency_measures_what_the_journals_recorded(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    layout = _exchange(world)
    command = make_command(issued_at_ms=START_MS)
    _write_command(layout, command)
    _write_accepted(layout, command, timestamp_ms=START_MS + 120)

    assert world.run("latency", "--json") == EXIT_OK

    document = json.loads(world.stdout)
    accepted = document["distributions"]["submit_to_accepted"]
    assert (accepted["count"], accepted["p95_ms"]) == (1, 120)
    assert document["pending"] == 1


def test_unmeasured_targets_exit_zero_so_a_gameless_ci_does_not_fail(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)

    assert world.run("latency", "--targets") == EXIT_OK
    assert "UNMEASURED" in world.stdout
    assert "MISSED" not in world.stdout


def test_a_measured_miss_under_targets_is_a_nonzero_exit(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    layout = _exchange(world)
    command = make_command(issued_at_ms=START_MS)
    _write_command(layout, command)
    _write_accepted(layout, command, timestamp_ms=START_MS + 400)

    assert world.run("latency", "--targets") == EXIT_FAILURE
    assert "MISSED" in world.stdout


def test_the_same_miss_without_the_targets_flag_is_a_report_that_succeeded(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    layout = _exchange(world)
    command = make_command(issued_at_ms=START_MS)
    _write_command(layout, command)
    _write_accepted(layout, command, timestamp_ms=START_MS + 400)

    assert world.run("latency") == EXIT_OK


def test_targets_json_carries_the_verdicts(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    layout = _exchange(world)
    command = make_command(issued_at_ms=START_MS)
    _write_command(layout, command)
    _write_accepted(layout, command, timestamp_ms=START_MS + 400)

    assert world.run("latency", "--targets", "--json") == EXIT_FAILURE

    document = json.loads(world.stdout)
    verdicts = {check["name"]: check["verdict"] for check in document["targets"]}
    assert verdicts["submit_to_accepted"] == "missed"
    assert verdicts["terminal_ack_visibility"] == "unmeasured"


def test_a_truncated_journal_is_a_refusal_with_an_error_message(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    layout = _exchange(world)
    _write_command(layout, make_command(issued_at_ms=START_MS))
    with layout.command_queue.open("ab") as handle:
        handle.write(b'{"cut": ')

    assert world.run("latency") == EXIT_FAILURE
    assert "latency could not be measured" in world.stderr


def test_a_machine_with_no_zomboid_directory_still_answers(tmp_path: Path) -> None:
    world = make_world(tmp_path, with_user_dir=False)

    assert world.run("latency") == EXIT_OK
    assert "(none)" in world.stdout
