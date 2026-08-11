"""``pz-agent play``: the cold start, and every way it is allowed to end.

The command is a composition, so what is worth testing about it is not that the
pieces work — they have their own suites — but that the *joins* are honest. Four
of them, and each has a test here for both directions:

* it starts a sidecar when there is none, and reuses one when there is;
* it waits for the game a bounded time and calls the ceiling a failure, with
  what it observed in the sentence rather than a shrug;
* it asks for authority only through the control file the running loop reads,
  refuses in front of the panic latch before writing anything, and writes
  nothing at all when the game already reports the mode being asked for;
* it reports armed only when the *game's own heartbeat* said so.

Nothing here sleeps. The sleeper is injected, and the tests that need the world
to change mid-wait change it from inside a sleep — which is also the only way to
exercise "the game appeared while we were waiting" without a second thread.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pz_agent_cli import app, play
from pz_agent_cli.config import default_config
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, resolve_workspace
from pz_agent_cli.supervisor import ControlChannel, ControlKind, PidFile, SidecarSupervisor
from pz_agent_core.ipc.atomic import write_json_atomic
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import SessionMode
from pz_agent_core.session.handshake import SessionDescriptor
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.cli_worlds import CliWorld, make_world

#: The build the fake mod reports, matching the fake install's version file.
BUILD = "42.20"

#: The pid a spawn that never happened would have produced.
SPAWNED_PID = 4242


# ---------------------------------------------------------------------------
# the fake machine
# ---------------------------------------------------------------------------


@dataclass
class Sleeps:
    """A sleeper that never sleeps, and can change the world while it "does".

    ``on`` is keyed by the ordinal of the sleep, one-based, so a test says "the
    game appears during the second poll" in the same words it would use to
    describe the scenario.
    """

    calls: list[int] = field(default_factory=list)
    on: dict[int, Callable[[], None]] = field(default_factory=dict)

    def __call__(self, milliseconds: int) -> None:
        self.calls.append(milliseconds)
        action = self.on.get(len(self.calls))
        if action is not None:
            action()


def _configured(tmp_path: Path) -> CliWorld:
    """A fake machine with a valid configuration and a clock that stands still.

    The clock is frozen because every deadline in ``play`` is read off it: a
    clock that advances a second per read would expire the waits before the
    poll counts were reached, and the poll count is the bound under test.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(default_config().to_toml(), encoding="utf-8")
    world.clock.freeze()
    return world


def _layout(world: CliWorld) -> IpcLayout:
    workspace = resolve_workspace(world.ctx)
    assert workspace.ipc_root is not None
    layout = IpcLayout(workspace.ipc_root)
    layout.ensure()
    return layout


def _attach_game(
    world: CliWorld,
    *,
    armed: bool | None = None,
    mode: SessionMode | None = None,
    build: str = BUILD,
) -> None:
    """Publish the two files ``StatusReport.attached`` is decided from.

    A session descriptor the mod's heartbeat names, and a heartbeat fresh on the
    world's clock. Written by hand rather than by running a loop: what is under
    test is what ``play`` reads, and a real sidecar would only put the same two
    documents there more slowly.
    """
    layout = _layout(world)
    session = SessionDescriptor(
        session_id=DEFAULT_SESSION,
        nonce="play-session",
        created_at_ms=world.clock.now_ms,
        mode=SessionMode.OBSERVE,
        save_id="Survivor/play",
    )
    write_json_atomic(layout, layout.session, session.to_dict())
    HeartbeatMonitor(layout, clock=world.clock).publish(
        Peer.GAME,
        session_id=DEFAULT_SESSION,
        nonce="play-beat",
        version="0.1.0",
        build=build,
        armed=armed,
        mode=mode,
    )


def _use_sleeper(monkeypatch: pytest.MonkeyPatch, sleeps: Sleeps) -> None:
    """Drive the real command through the real dispatch, with no real pause.

    Only the sleeper is substituted: the parser, the dispatch and every handler
    below them are the ones a user runs.
    """
    monkeypatch.setattr(app, "run_play", lambda ctx, args: play.run_play(ctx, args, sleep=sleeps))


def _use_supervisor(
    monkeypatch: pytest.MonkeyPatch, world: CliWorld, *, spawned: list[tuple[str, ...]]
) -> None:
    """A supervisor whose spawn records a command line instead of starting a process."""
    state_dir = resolve_workspace(world.ctx).state_dir

    def spawn(argv: Sequence[str], cwd: Path, log_path: Path) -> int:
        spawned.append(tuple(str(item) for item in argv))
        return SPAWNED_PID

    def build(ctx: object, workspace: object) -> SidecarSupervisor:
        return SidecarSupervisor(state_dir, clock=world.clock, spawn=spawn)

    monkeypatch.setattr(app, "build_supervisor", build)


def _control(world: CliWorld) -> ControlChannel:
    return ControlChannel(resolve_workspace(world.ctx).state_dir / "sidecar.control.json")


# ---------------------------------------------------------------------------
# refusals that happen before anything is started
# ---------------------------------------------------------------------------


def test_play_refuses_a_configuration_that_does_not_validate(tmp_path: Path) -> None:
    """The same gate ``start`` applies, applied before a process exists."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text('[session]\ndefault_mode = "godmode"\n', encoding="utf-8")

    exit_code = world.run("play")

    assert exit_code == EXIT_FAILURE
    assert "the sidecar was not started and nothing was armed" in world.stderr
    assert "validate-config" in world.stderr
    assert not (workspace.state_dir / "sidecar.pid.json").exists()
    assert not (workspace.state_dir / "sidecar.control.json").exists()


def test_play_refuses_a_wait_that_is_not_a_number_of_seconds(tmp_path: Path) -> None:
    """A negative ceiling is a malformed invocation, and exits like one."""
    world = _configured(tmp_path)

    exit_code = world.run("play", "--wait-game", "-5")

    assert exit_code == EXIT_USAGE
    assert "a number of seconds between 1 and 3600" in world.stderr
    assert "-5" in world.stderr
    assert not (resolve_workspace(world.ctx).state_dir / "sidecar.pid.json").exists()


def test_play_refuses_a_wait_beyond_the_ceiling(tmp_path: Path) -> None:
    """The bound is a bound in both directions; an hour is where it stops."""
    world = _configured(tmp_path)

    exit_code = world.run("play", "--wait-game", str(play.MAX_WAIT_GAME_S + 1))

    assert exit_code == EXIT_USAGE
    assert "a number of seconds between 1 and 3600" in world.stderr


def test_a_refusal_says_played_false_and_why_under_json(tmp_path: Path) -> None:
    """``--json`` refusals are one document on stdout, with the reason in it."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text('[session]\ndefault_mode = "godmode"\n', encoding="utf-8")

    exit_code = world.run("play", "--json")

    assert exit_code == EXIT_FAILURE
    document = json.loads(world.stdout)
    assert document == {"played": False, "detail": document["detail"]}
    assert "configuration error" in document["detail"]


# ---------------------------------------------------------------------------
# the sidecar
# ---------------------------------------------------------------------------


def test_play_starts_a_sidecar_when_none_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detached branch of ``start``, reached by the same command line."""
    world = _configured(tmp_path)
    spawned: list[tuple[str, ...]] = []
    _use_supervisor(monkeypatch, world, spawned=spawned)
    _use_sleeper(monkeypatch, Sleeps())

    exit_code = world.run("play", "--wait-game", "2")

    # No game on this fake machine, so the command fails — but it fails *after*
    # having started the sidecar, which is what this test is about.
    assert exit_code == EXIT_FAILURE
    assert len(spawned) == 1
    assert spawned[0][-2:] == ("start", "--foreground")
    record = PidFile(
        resolve_workspace(world.ctx).state_dir / "sidecar.pid.json", clock=world.clock
    ).read()
    assert record is not None
    assert record.pid == SPAWNED_PID
    assert f"pid {SPAWNED_PID}" in world.stdout
    assert "sidecar.out" in world.stdout


def test_play_reuses_a_sidecar_that_is_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second loop on one exchange directory is exactly what must not happen."""
    world = _configured(tmp_path)
    state_dir = resolve_workspace(world.ctx).state_dir
    PidFile(state_dir / "sidecar.pid.json", clock=world.clock).claim(SPAWNED_PID)
    _attach_game(world)
    spawned: list[tuple[str, ...]] = []
    _use_supervisor(monkeypatch, world, spawned=spawned)
    _use_sleeper(monkeypatch, Sleeps())

    exit_code = world.run("play", "--observe")

    assert exit_code == EXIT_OK
    assert spawned == []
    assert "already running" in world.stdout


# ---------------------------------------------------------------------------
# waiting for the game
# ---------------------------------------------------------------------------


def test_play_calls_the_game_never_appearing_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling is a failure with the observation in it, never a success."""
    world = _configured(tmp_path)
    sleeps = Sleeps()
    _use_supervisor(monkeypatch, world, spawned=[])
    _use_sleeper(monkeypatch, sleeps)

    exit_code = world.run("play", "--wait-game", "3")

    assert exit_code == EXIT_FAILURE
    assert "the sidecar is running and the game never appeared" in world.stderr
    assert "after 3 s" in world.stderr
    assert "Nothing was armed" in world.stderr
    # Bounded by the poll count, since the frozen clock never reaches the
    # deadline: ceiling / period, plus the poll that closes the window.
    assert sleeps.calls == [play.GAME_POLL_MS] * 4
    assert _control(world).read() is None


def test_play_prints_the_instructions_once_and_before_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Told at the start, not once per poll — the same three lines are noise."""
    world = _configured(tmp_path)
    _use_supervisor(monkeypatch, world, spawned=[])
    _use_sleeper(monkeypatch, Sleeps())

    world.run("play", "--wait-game", "5")

    assert world.stdout.count("load a SINGLEPLAYER save") == 1
    assert world.stdout.count("enable PZ Agent Bridge") == 1
    assert "docs/QUICKSTART.md section 5" in world.stdout


def test_play_arms_once_the_game_appears_and_the_game_confirms_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path: cold start, a game that turns up mid-wait, a confirmed arm."""
    world = _configured(tmp_path)
    sleeps = Sleeps()
    sleeps.on[2] = lambda: _attach_game(world)
    sleeps.on[3] = lambda: _attach_game(world, armed=True, mode=SessionMode.ASSISTED)
    _use_supervisor(monkeypatch, world, spawned=[])
    _use_sleeper(monkeypatch, sleeps)

    exit_code = world.run("play", "--wait-game", "30")

    assert exit_code == EXIT_OK
    assert "armed in ASSISTED — the game confirmed it" in world.stdout
    assert DEFAULT_SESSION in world.stdout
    assert BUILD in world.stdout
    assert "pz-agent status --watch" in world.stdout
    assert "pz-agent goal status" in world.stdout
    assert "pz-agent stop" in world.stdout
    # The request travelled the control file the running loop reads, and nothing
    # in this process ever set `armed` itself.
    request = _control(world).read()
    assert request is not None
    assert request.kind is ControlKind.ARM
    assert request.mode is SessionMode.ASSISTED


def test_play_observe_attaches_and_asks_for_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--observe`` is where ``start`` leaves a session, reached in one command."""
    world = _configured(tmp_path)
    PidFile(resolve_workspace(world.ctx).state_dir / "sidecar.pid.json", clock=world.clock).claim(
        SPAWNED_PID
    )
    _attach_game(world)
    _use_supervisor(monkeypatch, world, spawned=[])
    _use_sleeper(monkeypatch, Sleeps())

    exit_code = world.run("play", "--observe")

    assert exit_code == EXIT_OK
    assert "attached, and nothing was armed" in world.stdout
    assert _control(world).read() is None


# ---------------------------------------------------------------------------
# arming, and the two ways it does not happen
# ---------------------------------------------------------------------------


def test_play_refuses_in_front_of_the_panic_latch_without_clearing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latch is the game's. play reads it, reports it, and leaves it alone."""
    world = _configured(tmp_path)
    PidFile(resolve_workspace(world.ctx).state_dir / "sidecar.pid.json", clock=world.clock).claim(
        SPAWNED_PID
    )
    _attach_game(world)
    panic = _layout(world).panic_stop
    panic.write_text("{}", encoding="utf-8")
    _use_supervisor(monkeypatch, world, spawned=[])
    _use_sleeper(monkeypatch, Sleeps())

    exit_code = world.run("play")

    assert exit_code == EXIT_FAILURE
    assert "a panic-stop sentinel is present; clear it in the game before arming" in world.stderr
    assert panic.is_file(), "play cleared a latch it must never touch"
    assert _control(world).read() is None, "a request was written in front of the latch"


def test_play_reports_an_arm_the_game_never_confirms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that was sent and never granted is a failure that says so."""
    world = _configured(tmp_path)
    PidFile(resolve_workspace(world.ctx).state_dir / "sidecar.pid.json", clock=world.clock).claim(
        SPAWNED_PID
    )
    _attach_game(world, armed=False, mode=SessionMode.OBSERVE)
    sleeps = Sleeps()
    _use_supervisor(monkeypatch, world, spawned=[])
    _use_sleeper(monkeypatch, sleeps)

    exit_code = world.run("play", "--mode", "autonomous")

    assert exit_code == EXIT_FAILURE
    assert "the arm into AUTONOMOUS was requested and never confirmed within 30 s" in world.stderr
    assert "the game reports not armed, in OBSERVE" in world.stderr
    assert f"pid {SPAWNED_PID}" in world.stderr
    assert sleeps.calls == [play.ARM_POLL_MS] * 31
    # The request stands: the loop may yet answer it, and this command has not
    # withdrawn anything on the user's behalf.
    request = _control(world).read()
    assert request is not None
    assert request.mode is SessionMode.AUTONOMOUS


def test_play_writes_no_request_when_the_game_already_reports_the_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An armed session asked for the mode it is in needs no single-shot file."""
    world = _configured(tmp_path)
    PidFile(resolve_workspace(world.ctx).state_dir / "sidecar.pid.json", clock=world.clock).claim(
        SPAWNED_PID
    )
    _attach_game(world, armed=True, mode=SessionMode.ASSISTED)
    _use_supervisor(monkeypatch, world, spawned=[])
    _use_sleeper(monkeypatch, Sleeps())

    exit_code = world.run("play", "--mode", "assisted", "--json")

    assert exit_code == EXIT_OK
    assert _control(world).read() is None
    document = json.loads(world.stdout)
    assert document["played"] is True
    assert document["mode"] == SessionMode.ASSISTED.value
    assert document["armed"] is True
    assert document["session_id"] == DEFAULT_SESSION
    assert document["build"] == BUILD
    assert document["game"]["verdict"] == "running"
    assert set(document) == {"played", "mode", "session_id", "build", "game", "armed"}
    # Progress notes are diagnostics; stdout carries the document alone.
    assert "already reports armed" in world.stderr
