"""The sidecar writes the log nineteen scenarios tell an operator to collect.

``DiagnosticLog`` was written, tested, rotated, redacted, level-filtered — and
constructed nowhere outside the test suite. So ``logs/pz-agent.log`` and
``logs/pz-agent.jsonl`` did not exist, could not exist, and:

- nineteen of the twenty live scenarios name ``pz-agent.log`` among the logs to
  collect, and three name ``pz-agent.jsonl``;
- ``docs/LOCAL_DEBUG_MAP.md`` sends an operator to it by name;
- ``pz-agent logs`` reads it;
- ``pz-agent logs --bundle`` packs the directory it lives in, and
  ``docs/TROUBLESHOOTING.md`` tells a user to attach that bundle to a report.

``live-test collect`` reported "copied 0 file(s), skipped 15" and named each
absent file, which is exactly the honest behaviour — and exactly the line I read
past twice before asking *why* the sidecar's own log was among the missing.

The eighth defect of this branch was ``pz_agent_voice``: complete, tested, and
imported by nothing. This is the same shape, and the cost was larger, because
four documents and twenty scenarios were built on top of the hole.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pz_agent_cli.app import build_loop
from pz_agent_cli.config import default_config
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, resolve_workspace
from pz_agent_cli.runtime import LoopLimits
from tests.fixtures.cli_worlds import CliWorld, make_world

#: The two stems ``DiagnosticLog`` writes, and the two the scenarios name.
STRUCTURED: Final = "pz-agent.jsonl"
HUMAN: Final = "pz-agent.log"


def _configured(tmp_path: Path) -> CliWorld:
    """A fake machine an operator could actually start: config already written."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(default_config().to_toml(), encoding="utf-8")
    return world


def _run_briefly(world: CliWorld) -> int:
    """A foreground sidecar with a tick budget, which is a whole session."""
    world.reset_streams()
    return world.run("start", "--foreground", "--ticks", "2")


def _logs_dir(world: CliWorld) -> Path:
    """Where the CLI itself would put them, resolved the way the CLI resolves it.

    Read off ``resolve_workspace`` rather than rebuilt from ``state_dir`` here,
    so a change to the layout moves the test with the code instead of leaving
    it asserting about a directory nothing writes to any more.
    """
    return resolve_workspace(world.ctx).logs_dir


def test_a_session_leaves_both_log_files_behind(tmp_path: Path) -> None:
    world = _configured(tmp_path)

    assert _run_briefly(world) == EXIT_OK, world.stderr

    for name in (HUMAN, STRUCTURED):
        path = _logs_dir(world) / name
        assert path.is_file(), f"{name} was not written; nineteen scenarios ask for it"
        assert path.read_text(encoding="utf-8").strip(), f"{name} is empty"


def test_the_structured_stream_is_one_json_object_per_line(tmp_path: Path) -> None:
    """``pz-agent logs --json`` and the support bundle both parse it."""
    world = _configured(tmp_path)
    _run_briefly(world)

    lines = (_logs_dir(world) / STRUCTURED).read_text(encoding="utf-8").splitlines()

    assert lines, "no records were written"
    for line in lines:
        record = json.loads(line)
        assert record["event"], "a record with no event names nothing"
        assert record["level"], "a record with no level cannot be filtered"


def test_the_session_and_how_it_ended_are_both_recorded(tmp_path: Path) -> None:
    """The two facts a failed scenario is diagnosed from.

    Which session the run attached to, and why it stopped. Without the first,
    the acks in the mod's journals cannot be tied to this run at all.
    """
    world = _configured(tmp_path)
    _run_briefly(world)

    events = {
        json.loads(line)["event"]
        for line in (_logs_dir(world) / STRUCTURED).read_text(encoding="utf-8").splitlines()
    }

    assert "attach.ok" in events, "nothing records which session this run held"
    assert "run.finished" in events, "nothing records why the loop stopped"
    assert "shutdown" in events, "nothing records whether the lock was released"


def test_a_refused_attach_is_recorded_rather_than_only_printed(tmp_path: Path) -> None:
    """The failure an operator most needs in a file they can attach.

    A sidecar that would not attach prints to a terminal that is gone by the
    time they ask for help. The refusal is made real rather than simulated:
    another loop holds the lock, which is the way this actually happens — a
    sidecar left running in a window the operator forgot about.
    """
    world = _configured(tmp_path)
    workspace = resolve_workspace(world.ctx)
    holder = build_loop(world.ctx, workspace, limits=LoopLimits(tick_budget=1))
    assert holder.attach().attached, "the holding loop did not take the lock"
    try:
        assert _run_briefly(world) == EXIT_FAILURE, "a held lock must refuse the second sidecar"
    finally:
        holder.shutdown(reason="the test is done with the lock")

    events = [
        json.loads(line)
        for line in (_logs_dir(world) / STRUCTURED).read_text(encoding="utf-8").splitlines()
    ]

    refusals = [record for record in events if record["event"] == "attach.refused"]
    assert refusals, "the refusal reached the terminal and nothing else"
    assert "lock" in refusals[-1]["fields"]["detail"], "the record does not say what refused it"


def test_logging_never_ends_a_session(tmp_path: Path) -> None:
    """A log directory that cannot be written is not a reason to stop.

    The sidecar drives a character. Trading that for a diagnostic would be the
    wrong way round, so the writer is optional and every write is guarded.
    """
    world = _configured(tmp_path)
    logs = _logs_dir(world)
    logs.parent.mkdir(parents=True, exist_ok=True)
    # A file where the directory should be: mkdir and every write below it fail.
    logs.write_text("not a directory", encoding="utf-8")

    assert _run_briefly(world) == EXIT_OK, (
        "an unwritable log directory stopped the sidecar; it must only lose the log"
    )
