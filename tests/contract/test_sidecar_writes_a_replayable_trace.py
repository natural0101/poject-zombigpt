"""``pz-agent replay <trace>`` had no trace to replay.

``TraceWriter`` is the sibling of the defect one file over. It was written,
bounded, redacted, rotated, and given a reader that reconstructs the world
snapshot by snapshot — and, like ``DiagnosticLog``, it was constructed nowhere
outside the test suite. Resting on it:

- ``docs/QUICKSTART.md`` tells a user to run ``pz-agent replay <trace>`` under
  the heading "When something goes wrong";
- ``pz-agent logs --bundle`` packs ``traces/*.jsonl`` into the archive a user is
  told to attach to a bug report;
- ``pz-agent replay`` is a shipped command, parsed and documented.

Three things pointed at a file the product could not produce.

The wiring is a seam rather than a call: the engine returns a *result*, and the
command it actually sent — with the arguments the adapter built, which is the
half that says what was asked for — never leaves it. So ``ActionEngine`` gained
``on_dispatch`` and the loop pairs the two. What that buys is checked here by
replaying, not by inspecting: a trace is only worth writing if
:func:`replay_observations` can rebuild the world from it, and that function
refuses a diff with no baseline rather than guessing.

Which makes rotation the sharp edge, and the reason this file exists at all. A
trace is bounded — a two-hour run rotates it — and a rotated file that begins
with a diff is unreplayable from its first line. That is tested below with a
real rotation rather than reasoned about.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli import app
from pz_agent_cli.config import default_config
from pz_agent_cli.context import EXIT_OK, resolve_workspace
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.diagnostics import (
    LogLimits,
    TraceKind,
    TraceWriter,
    read_trace,
    replay_observations,
)
from pz_agent_core.protocol import ActionName, Observation
from tests.fixtures.cli_worlds import make_world
from tests.fixtures.sidecar_worlds import SidecarWorld, attached_world

TRACE_NAME: Final = "session.jsonl"


class Waiting:
    """A planner that asks for the cheapest real action there is, once."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.calls = 0

    def propose(self, observation: Observation) -> ActionRequest | None:
        self.calls += 1
        if self.calls > 1:
            return None
        return ActionRequest(
            action=ActionName.ACTION_WAIT,
            session_id=self.session_id,
            idempotency_key="trace-wait",
            args={"game_seconds": 1.0},
        )


def _writer(tmp_path: Path, **limits: Any) -> TraceWriter:
    path = tmp_path / "traces" / TRACE_NAME
    if limits:
        return TraceWriter(path, limits=LogLimits(**limits))
    return TraceWriter(path)


def _observe_and_tick(world: SidecarWorld, times: int) -> None:
    for _ in range(times):
        world.beat_game()
        world.observe()
        world.loop.tick()


def test_a_run_leaves_a_trace_that_replays_back_to_what_was_observed(
    tmp_path: Path,
) -> None:
    """The whole point of the format: the world, rebuilt from the file."""
    trace = _writer(tmp_path)
    with attached_world(tmp_path, trace=trace) as world:
        _observe_and_tick(world, 3)

    read = read_trace(trace.path)
    assert read.problems == (), read.problems

    steps = replay_observations(read.entries)

    assert len(steps) == 3, "each observed tick should leave one entry"
    assert steps[-1].observation.seq == 3


def test_only_the_anchors_are_written_full(tmp_path: Path) -> None:
    """What keeps a long run affordable.

    A snapshot is an order of magnitude larger than a diff. Writing every tick
    full would rotate the file away within minutes and leave an operator with
    the last few seconds of a two-hour session.
    """
    trace = _writer(tmp_path)
    with attached_world(tmp_path, trace=trace) as world:
        _observe_and_tick(world, 4)

    read = read_trace(trace.path)

    kinds = [entry.kind for entry in read.entries]
    assert kinds[0] is TraceKind.OBSERVATION, "a replay needs a snapshot to start from"
    assert kinds[1:] == [TraceKind.OBSERVATION_DIFF] * 3, kinds


def test_a_rotated_trace_still_replays_from_its_first_line(tmp_path: Path) -> None:
    """The bound and the format pull against each other; this is the seam.

    ``replay_observations`` refuses a diff it has no baseline for — correctly,
    since applying one to the wrong snapshot is the single way this module can
    produce a plausible lie. So a rotation that left a diff as the first line
    of the new file would turn every long run's trace into a refusal, and a
    two-hour scenario is exactly when somebody reaches for one.
    """
    # A snapshot is ~900 bytes and a diff a few hundred, so this rotates
    # every few entries — repeatedly, rather than once at the end.
    trace = _writer(tmp_path, max_bytes=1500, keep=2, max_record_bytes=1500)
    with attached_world(tmp_path, trace=trace) as world:
        _observe_and_tick(world, 10)

    assert trace.rotations > 0, "the file did not rotate, so this proves nothing"
    read = read_trace(trace.path)

    steps = replay_observations(read.entries)

    assert steps, "the rotated file replayed to nothing"


def test_an_action_and_its_result_are_recorded_together(tmp_path: Path) -> None:
    """What was asked for, next to what came of it.

    The command carries the arguments the adapter built, which the result does
    not; the result carries the terminal status, which the command does not.
    Either alone answers half of "why did nothing happen".
    """
    trace = _writer(tmp_path)
    with attached_world(tmp_path, trace=trace) as world:
        world.loop.planner = Waiting(world.session_id)
        assert world.loop.arm().armed is True
        # The world keeps moving while the engine waits inside the tick. Without
        # this the engine never gets a fresh observation, refuses before it
        # sends anything, and the entry under test is a different one.
        world.sleeper.while_waiting = world.observe
        _observe_and_tick(world, 1)

    read = read_trace(trace.path)
    actions = read.of_kind(TraceKind.ACTION)

    assert actions, "an armed loop drove an action and the trace does not mention it"
    payload = actions[0].payload
    assert payload["action"] == ActionName.ACTION_WAIT.value
    assert payload["status"], "the entry records a command with no outcome"
    # Both halves, explicitly. Without the command the entry still names an
    # action and a status, and reads like a complete record of a refusal that
    # never happened — which is what an unwired ``on_dispatch`` would produce.
    assert payload["command"]["args"], "the arguments the adapter built are missing"
    assert payload["result"], "the terminal result is missing"


def test_an_action_refused_before_it_was_sent_is_still_recorded(tmp_path: Path) -> None:
    """The case an operator is most likely to be reading a trace for.

    They asked for something and nothing happened in the world. A gate that
    fires before dispatch — a stale observation, an unusable capability, a
    denied policy — produces a result and no command at all, so recording only
    the pairs would leave the trace silent about exactly this.
    """
    trace = _writer(tmp_path)
    with attached_world(tmp_path, trace=trace) as world:
        world.loop.planner = Waiting(world.session_id)
        assert world.loop.arm().armed is True
        # No ``while_waiting``: the world stands still, so the engine never gets
        # the fresh observation it needs and refuses before sending anything.
        _observe_and_tick(world, 1)

    actions = read_trace(trace.path).of_kind(TraceKind.ACTION)

    assert actions, "a refusal left no trace at all"
    payload = actions[0].payload
    assert "command" not in payload, "nothing was sent, so no command may be claimed"
    assert payload["reason_code"], "a refusal that does not say why explains nothing"


def test_the_command_line_the_documentation_gives_a_user_works(tmp_path: Path) -> None:
    """``pz-agent replay <trace>`` — the real command, over a real trace.

    docs/QUICKSTART.md prints this line under "When something goes wrong". It
    is exercised end to end because every part of it was fine in isolation and
    the sequence produced nothing.
    """
    trace = _writer(tmp_path)
    with attached_world(tmp_path, trace=trace) as world:
        _observe_and_tick(world, 2)

    cli = make_world(tmp_path / "machine")
    assert cli.run("replay", str(trace.path)) == EXIT_OK, cli.stderr
    assert "entries" in cli.stdout
    assert "observations rebuilt" in cli.stdout


def test_the_start_command_is_what_hands_the_loop_its_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring itself, which every other test here would pass without.

    Each test above builds the writer and hands it to the loop by hand, which
    is exactly the shape of the defect: a subsystem that works perfectly when a
    test assembles it and is assembled by nothing else. So this drives the real
    ``start`` and looks at the loop the CLI actually built.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(default_config().to_toml(), encoding="utf-8")
    built: list[Any] = []
    real = app.build_loop

    def spy(*args: Any, **kwargs: Any) -> Any:
        loop = real(*args, **kwargs)
        built.append(loop)
        return loop

    monkeypatch.setattr(app, "build_loop", spy)

    assert world.run("start", "--foreground", "--ticks", "1") == EXIT_OK, world.stderr

    assert built, "start did not build a loop at all"
    assert built[0].trace is not None, "the CLI built a loop that records nothing"


def test_a_trace_that_cannot_be_written_does_not_end_the_tick(tmp_path: Path) -> None:
    """The loop is driving a character. A diagnostic is never worth that."""

    class Failing(TraceWriter):
        def record_observation(self, observation: Observation) -> Any:
            raise OSError("the disk is full")

        def record_observation_diff(self, **kwargs: Any) -> Any:
            raise OSError("the disk is full")

    with attached_world(tmp_path, trace=Failing(tmp_path / "traces" / TRACE_NAME)) as world:
        _observe_and_tick(world, 2)

        assert world.loop.ticks == 2, "a failing trace stopped the loop"
