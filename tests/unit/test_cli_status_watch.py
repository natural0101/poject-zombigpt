"""``pz-agent status --watch``: the frames, the refusals, and the goal block.

The one-shot report is covered in ``tests/unit/test_cli_app.py``; nothing here
re-reads it. What these own is the loop around it — the two invocations it
refuses before drawing anything, the shape of a frame on a pipe versus a
terminal, and the one fact a frame carries that no file on this machine holds:
what the sidecar's goal channel answered.

Every test is instant and none of them sleeps. The seams the handler takes for
exactly this reason are used exactly this way: a sleeper that counts its calls
(and, in one test, raises the ``KeyboardInterrupt`` a user's Ctrl-C raises), a
frame bound so the loop terminates, and a goal source that answers a prepared
:class:`~pz_agent_mcp.ports.GoalChannelStatus` without a socket. The default
goal source is *not* faked away in
:func:`test_the_default_goal_source_reports_a_machine_with_no_sidecar_unreachable`,
which drives the real link against a state directory no sidecar has ever
published into — the state every machine is in until ``pz-agent start`` runs.

The assertion that recurs is the tri-state one. "Unreachable" and "nothing is
waiting" are different sentences about different worlds, and a HUD that printed
the second about the first would show a calm queue to somebody whose sidecar is
gone.
"""

from __future__ import annotations

import dataclasses
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, CliContext, Workspace
from pz_agent_cli.status import (
    CLEAR_SCREEN,
    FRAME_SEPARATOR,
    MAX_PENDING_SHOWN,
    WATCH_MIN_INTERVAL,
    run_status_watch,
)
from pz_agent_core.goals import (
    GOAL_SPECS,
    GoalKind,
    GoalParams,
    GoalRecord,
    GoalState,
)
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import SessionMode
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import MOD_VERSION
from pz_agent_mcp.ports import GoalChannelStatus, GoalProgress, PausedGoalRecord
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.platform_trees import CYRILLIC_USER

BUILD: Final = "42.20"

#: Long enough to be raised by the clamp, and not a value the floor is.
TIGHT_INTERVAL: Final = 0.05


# ---------------------------------------------------------------------------
# the harness
# ---------------------------------------------------------------------------


class CountingSleeper:
    """A sleeper that waits for nothing and remembers what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class InterruptingSleeper(CountingSleeper):
    """The user's Ctrl-C, arriving where it really arrives: in the wait."""

    def __call__(self, seconds: float) -> None:
        super().__call__(seconds)
        raise KeyboardInterrupt


class TtyStream(io.StringIO):
    """A stream that claims to be a terminal, so the redraw branch is reachable."""

    def isatty(self) -> bool:
        return True


def _publish_heartbeat(world: CliWorld, *, armed: bool = False) -> None:
    """The game's own file, written by the monitor the sidecar writes it with."""
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    HeartbeatMonitor(layout, clock=lambda: world.clock.now_ms).publish(
        Peer.GAME,
        session_id=DEFAULT_SESSION,
        nonce="nonce-1",
        version=MOD_VERSION,
        build=BUILD,
        player_present=True,
        armed=armed,
        mode=SessionMode.OBSERVE,
    )


def _record(
    *,
    goal_id: str = "goal-1",
    kind: GoalKind = GoalKind.SATISFY_HUNGER,
    state: GoalState = GoalState.PENDING,
    sequence: int = 1,
    suspended_by: str | None = None,
) -> GoalRecord:
    """One goal record with the bookkeeping its own invariants insist on."""
    extra: dict[str, Any] = {}
    if suspended_by is not None:
        extra = {"suspended_by": suspended_by, "suspensions": 1}
    return GoalRecord(
        goal_id=goal_id,
        kind=kind,
        params=GoalParams(),
        budget=GOAL_SPECS[kind].budget,
        key_digest="0" * 16,
        sequence=sequence,
        state=state,
        submitted_at_ms=1_000,
        started_at_ms=None if state is GoalState.PENDING else 2_000,
        **extra,
    )


def _answers(
    status: GoalChannelStatus | None,
) -> Callable[[Workspace], GoalChannelStatus | None]:
    """A goal source that answers *status* to every frame, dialling nothing."""

    def source(workspace: Workspace) -> GoalChannelStatus | None:
        assert workspace.state_dir.name, "the handler asked without resolving a workspace"
        return status

    return source


def _watch(
    world: CliWorld,
    *,
    goals: GoalChannelStatus | None = None,
    frames: int = 1,
    interval: float = 1.0,
    sleeper: CountingSleeper | None = None,
    ctx: CliContext | None = None,
) -> int:
    return run_status_watch(
        ctx if ctx is not None else world.ctx,
        interval=interval,
        as_json=False,
        max_frames=frames,
        sleeper=sleeper if sleeper is not None else CountingSleeper(),
        goal_source=_answers(goals),
    )


# ---------------------------------------------------------------------------
# the refusals, through the real command line
# ---------------------------------------------------------------------------


def test_watch_with_json_is_refused_and_names_the_command_to_poll(tmp_path: Path) -> None:
    """A stream of documents separated by a redraw is not a format.

    Refused rather than served, and refused with the alternative named: the
    caller who asked for this wants a poll loop, and the loop they should own is
    around the one-shot command.
    """
    world = make_world(tmp_path)

    exit_code = world.run("status", "--watch", "--json")

    assert exit_code == EXIT_USAGE
    assert "does not emit JSON" in world.stderr
    assert "pz-agent status --json" in world.stderr
    assert world.stdout == "", "a refused watch drew a frame"


@pytest.mark.parametrize("interval", ["0", "-2"])
def test_an_interval_that_names_no_schedule_is_a_usage_error(tmp_path: Path, interval: str) -> None:
    world = make_world(tmp_path)

    exit_code = world.run("status", "--watch", "--interval", interval)

    assert exit_code == EXIT_USAGE
    assert "greater than zero" in world.stderr
    assert world.stdout == ""


def test_an_interval_under_the_floor_is_raised_and_the_change_is_said_out_loud(
    tmp_path: Path,
) -> None:
    """Silently doing something other than what was asked teaches nothing."""
    world = make_world(tmp_path)
    sleeper = CountingSleeper()

    exit_code = _watch(world, frames=2, interval=TIGHT_INTERVAL, sleeper=sleeper)

    assert exit_code == EXIT_OK
    assert f"raised to {WATCH_MIN_INTERVAL}" in world.stderr
    assert sleeper.calls == [WATCH_MIN_INTERVAL], "the loop slept for the interval it refused"


def test_a_machine_with_no_zomboid_directory_is_refused_rather_than_watched(
    tmp_path: Path,
) -> None:
    """No directory can appear by being waited on, so the loop never starts."""
    world = make_world(tmp_path, with_user_dir=False)

    exit_code = _watch(world)

    assert exit_code == EXIT_FAILURE
    assert "no exchange directory to watch" in world.stderr
    assert world.stdout == ""


# ---------------------------------------------------------------------------
# frames: what a pipe gets, and what a terminal gets
# ---------------------------------------------------------------------------


def test_frames_into_a_pipe_are_separated_and_carry_no_escape_bytes(tmp_path: Path) -> None:
    """The redraw is a terminal's affordance; a pipe gets a transcript.

    The assertion that matters is the negative one. Escape bytes written into a
    file are corruption that survives every later read of it, so the branch has
    to be chosen by what the stream *is* rather than by what the loop would
    prefer to draw.
    """
    world = make_world(tmp_path)
    _publish_heartbeat(world)

    assert _watch(world, frames=3) == EXIT_OK

    printed = world.stdout
    assert "\x1b" not in printed, "an escape sequence reached a pipe"
    assert printed.count(FRAME_SEPARATOR) == 2, "three frames need two separators"
    assert printed.count("pz-agent status — frame 1") == 1
    assert "pz-agent status — frame 3" in printed


def test_a_frame_reports_the_game_the_sidecar_and_the_session_it_read(tmp_path: Path) -> None:
    """The compact line, and it is compact: one line for the game, not six."""
    world = make_world(tmp_path)
    _publish_heartbeat(world, armed=True)

    assert _watch(world) == EXIT_OK

    printed = world.stdout
    assert f"build {BUILD}" in printed
    assert "armed yes" in printed
    assert f"mode {SessionMode.OBSERVE.value}" in printed
    assert "sidecar" in printed
    assert "session" in printed
    # The page the one-shot command prints stays there: a watched screen that
    # redraws settings every two seconds is one nobody reads a change out of.
    assert "capabilities" not in printed
    assert "backup" not in printed


def test_a_terminal_is_homed_and_cleared_instead_of_being_scrolled(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    _publish_heartbeat(world)
    screen = TtyStream()
    ctx = dataclasses.replace(world.ctx, stdout=screen)

    assert _watch(world, frames=2, ctx=ctx) == EXIT_OK

    printed = screen.getvalue()
    assert printed.count(CLEAR_SCREEN) == 2, "a terminal frame was drawn without clearing"
    assert FRAME_SEPARATOR not in printed, "a terminal got the pipe's separator as well"


def test_a_panic_stop_sentinel_is_reported_only_when_it_is_there(tmp_path: Path) -> None:
    """A line printed every frame is a line a watching user stops seeing."""
    world = make_world(tmp_path)
    _publish_heartbeat(world)
    assert _watch(world) == EXIT_OK
    assert "panic stop" not in world.stdout

    world.reset_streams()
    assert world.ipc_root is not None
    IpcLayout(world.ipc_root).panic_stop.write_text("stop", encoding="utf-8")

    assert _watch(world) == EXIT_OK
    assert "panic-stop sentinel is present" in world.stdout


# ---------------------------------------------------------------------------
# the goal block, which is three states and never two
# ---------------------------------------------------------------------------


def test_a_channel_that_did_not_answer_is_unreachable_and_not_an_empty_queue(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    _publish_heartbeat(world)

    assert _watch(world, goals=None) == EXIT_OK

    printed = world.stdout
    assert "unreachable — the sidecar is not serving RPC" in printed
    assert "nothing is waiting" not in printed, "a dead link was reported as a calm queue"
    assert "no goal is active" not in printed


def test_the_default_goal_source_reports_a_machine_with_no_sidecar_unreachable(
    tmp_path: Path,
) -> None:
    """No fake here: the real link, against a state directory nothing published.

    This is the state every machine is in until ``pz-agent start`` is run, and
    it is the one a faked goal source can never prove anything about — the
    handler has to dial and come back empty-handed inside one frame.
    """
    world = make_world(tmp_path)
    _publish_heartbeat(world)

    exit_code = run_status_watch(
        world.ctx,
        interval=1.0,
        as_json=False,
        max_frames=1,
        sleeper=CountingSleeper(),
    )

    assert exit_code == EXIT_OK
    assert "unreachable — the sidecar is not serving RPC" in world.stdout


def test_a_reachable_channel_reports_the_active_goal_the_backlog_and_a_suspension(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    _publish_heartbeat(world)
    status = GoalChannelStatus(
        active=_record(goal_id="active-1", kind=GoalKind.LOOT_AREA, state=GoalState.ACTIVE),
        pending=(
            _record(
                goal_id="waiting-1", kind=GoalKind.EXPLORE_AREA, sequence=2, suspended_by="active-1"
            ),
            _record(goal_id="waiting-2", kind=GoalKind.SATISFY_HUNGER, sequence=3),
        ),
        progress=GoalProgress(phase="approach", counters={"legs": 2}),
    )

    assert _watch(world, goals=status) == EXIT_OK

    printed = world.stdout
    assert f"{GoalKind.LOOT_AREA.value} active-1" in printed
    assert "approach legs=2" in printed
    assert f"2 — {GoalKind.EXPLORE_AREA.value} (stepped aside for active-1)" in printed
    assert GoalKind.SATISFY_HUNGER.value in printed


def test_a_backlog_longer_than_the_display_says_how_much_it_left_out(tmp_path: Path) -> None:
    """The queue bounds itself; the display bounds what it prints and admits it."""
    world = make_world(tmp_path)
    _publish_heartbeat(world)
    pending = tuple(
        _record(goal_id=f"waiting-{index}", sequence=index + 1)
        for index in range(MAX_PENDING_SHOWN + 2)
    )

    assert _watch(world, goals=GoalChannelStatus(pending=pending)) == EXIT_OK

    printed = world.stdout
    assert f"{len(pending)} — " in printed
    assert "+2 more" in printed
    assert "no goal is active" in printed


def test_tails_the_answer_did_not_carry_are_unreported_rather_than_no(tmp_path: Path) -> None:
    """``None`` is "nothing said", and an older peer's codec says it too.

    Printing "no" here would tell a user that the agent is not paused on the
    strength of a field that never crossed the wire.
    """
    world = make_world(tmp_path)
    _publish_heartbeat(world)
    status = GoalChannelStatus(
        active=_record(goal_id="active-1", state=GoalState.ACTIVE),
        progress=None,
        paused=None,
    )

    assert _watch(world, goals=status) == EXIT_OK

    printed = world.stdout
    assert "progress               unreported" in printed
    assert "paused                 unreported" in printed


def test_a_takeover_marker_is_printed_through_the_redactor(tmp_path: Path) -> None:
    """The loop's sentence is free text from another process, like every other."""
    world = make_world(tmp_path)
    _publish_heartbeat(world)
    assert world.user_dir is not None
    status = GoalChannelStatus(
        paused=PausedGoalRecord(
            goal_id="parked-1",
            kind=GoalKind.LOOT_AREA.value,
            reason=f"manual takeover under {world.user_dir}",
            paused_at_ms=4_000,
        ),
    )

    assert _watch(world, goals=status) == EXIT_OK

    printed = world.stdout
    assert "parked at 4000ms" in printed
    assert CYRILLIC_USER not in printed, "a user's own directory name reached the screen"


# ---------------------------------------------------------------------------
# how the loop ends
# ---------------------------------------------------------------------------


def test_the_frame_bound_is_respected_and_the_last_frame_is_not_slept_on(
    tmp_path: Path,
) -> None:
    """Three frames, two waits: nothing waits for a redraw that will not happen."""
    world = make_world(tmp_path)
    _publish_heartbeat(world)
    sleeper = CountingSleeper()

    assert _watch(world, frames=3, interval=2.0, sleeper=sleeper) == EXIT_OK

    assert world.stdout.count("pz-agent status — frame") == 3
    assert sleeper.calls == [2.0, 2.0]
    assert "watch ended after 3 frames" in world.stdout


def test_an_interrupt_ends_the_watch_as_an_ending_rather_than_a_failure(
    tmp_path: Path,
) -> None:
    """Ctrl-C is how this command is meant to stop, so it reports success."""
    world = make_world(tmp_path)
    _publish_heartbeat(world)

    exit_code = _watch(world, frames=9, sleeper=InterruptingSleeper())

    assert exit_code == EXIT_OK
    assert world.stdout.count("pz-agent status — frame") == 1
    assert "watch ended after 1 frame" in world.stdout
