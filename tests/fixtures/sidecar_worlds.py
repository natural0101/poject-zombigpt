"""A whole fake sidecar environment: an exchange directory, a state directory, a clock.

The loop's interesting behaviour is all about *time* and *file state* — a
heartbeat that stops, a sentinel that appears, a budget that runs out — so both
arrive here under the test's control. Nothing sleeps: the sleeper advances the
fake clock by the milliseconds it was asked for and returns, which keeps every
deadline in the engine and the heartbeat monitor meaningful while a test drives
hundreds of ticks in no time at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pz_agent_cli.runtime import LoopLimits, SidecarLoop
from pz_agent_cli.supervisor import ControlChannel, PidFile, SidecarSupervisor
from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.builtin import register_builtins
from pz_agent_core.ipc.journal import JournalWriter
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import Observation
from pz_agent_core.session.heartbeat import Heartbeat, HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION

from . import make_observation
from .ipc_builders import BASE_TIME_MS, FakeClock

#: Small enough that a test can name every tick it expects, large enough that
#: the action rate cap is not the first bound anything hits.
TEST_LIMITS = LoopLimits(
    tick_interval_ms=10,
    tick_budget=8,
    max_actions_per_window=2,
    action_window_ms=600_000,
    observations_per_tick=16,
    observation_window=8,
)


class RecordingSleeper:
    """Advances the fake clock instead of sleeping, and remembers every request."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.requests: list[int] = []

    def __call__(self, milliseconds: int) -> None:
        self.requests.append(milliseconds)
        if milliseconds > 0:
            self._clock.advance(milliseconds)


def _make_loop(
    *,
    layout: IpcLayout,
    state_dir: Path,
    clock: FakeClock,
    sleeper: RecordingSleeper,
    monitor: HeartbeatMonitor,
    limits: LoopLimits,
    registry: AdapterRegistry | None,
    with_pid_file: bool,
    loop_fields: dict[str, Any],
) -> SidecarLoop:
    return SidecarLoop(
        layout=layout,
        state_dir=state_dir,
        registry=registry if registry is not None else register_builtins(AdapterRegistry()),
        clock=clock,
        sleep=sleeper,
        limits=limits,
        monitor=monitor,
        pid_file=PidFile(state_dir / "sidecar.pid.json", clock=clock) if with_pid_file else None,
        **loop_fields,
    )


@dataclass
class SidecarWorld:
    """One exchange directory, its state directory, and the loop over them."""

    root: Path
    layout: IpcLayout
    state_dir: Path
    clock: FakeClock
    sleeper: RecordingSleeper
    loop: SidecarLoop
    monitor: HeartbeatMonitor
    limits: LoopLimits = TEST_LIMITS
    _game_nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    _seq: int = 0

    @property
    def session_id(self) -> str:
        """The session the loop attached to.

        Raises:
            AssertionError: before :meth:`SidecarLoop.attach` has succeeded.
        """
        session = self.loop.session
        assert session is not None, "the loop has not attached yet"
        return session.session_id

    @property
    def supervisor(self) -> SidecarSupervisor:
        return SidecarSupervisor(self.state_dir, clock=self.clock)

    @property
    def control(self) -> ControlChannel:
        return ControlChannel(self.state_dir / "sidecar.control.json", clock=self.clock)

    def beat_game(self, *, session_id: str | None = None, **fields: Any) -> Heartbeat:
        """Write the game's heartbeat as the mod would, at the current instant."""
        return self.monitor.publish(
            Peer.GAME,
            session_id=session_id or self.session_id,
            nonce=self._game_nonce,
            version=PRODUCT_VERSION,
            build="42.20",
            player_present=True,
            **fields,
        )

    def observe(self, **overrides: Any) -> Observation:
        """Append one observation to the journal, numbering it in sequence."""
        self._seq += 1
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "seq": self._seq,
            "timestamp_ms": self.clock.now,
        }
        payload.update(overrides)
        observation = make_observation(**payload)
        writer = JournalWriter(self.layout, self.layout.observation_events)
        try:
            writer.append(observation.to_dict())
        finally:
            writer.close()
        return observation

    def write_raw_observation(self, payload: dict[str, Any]) -> None:
        """Append a record that is *not* a valid observation."""
        writer = JournalWriter(self.layout, self.layout.observation_events)
        try:
            writer.append(payload)
        finally:
            writer.close()

    def panic(self) -> Path:
        """Drop the mod's panic sentinel into the exchange directory."""
        self.layout.panic_stop.write_text("stop", encoding="utf-8")
        return self.layout.panic_stop

    def clear_panic(self) -> None:
        self.layout.panic_stop.unlink(missing_ok=True)

    def restart(self, **loop_fields: Any) -> SidecarLoop:
        """Shut the loop down and bring a fresh one up over the same directories.

        The previous loop is shut down first: two loops on one exchange
        directory is exactly what the lock refuses, and a test that meant to
        model a restart would otherwise be modelling a collision.
        """
        if self.loop.session is not None:
            self.loop.shutdown(reason="restarting")
        self.loop = _make_loop(
            layout=self.layout,
            state_dir=self.state_dir,
            clock=self.clock,
            sleeper=self.sleeper,
            monitor=self.monitor,
            limits=self.limits,
            registry=None,
            with_pid_file=False,
            loop_fields=loop_fields,
        )
        outcome = self.loop.attach()
        assert outcome.attached, outcome.detail
        return self.loop

    def __enter__(self) -> SidecarWorld:
        return self

    def __exit__(self, *exc: object) -> None:
        """Shut the loop down so the journal handle it owns is closed.

        Not politeness: ``filterwarnings = ["error"]`` turns the ResourceWarning
        from an unclosed journal into a test failure, which is exactly the signal
        wanted — a loop that was not shut down leaked a file handle.
        """
        if self.loop.session is not None:
            self.loop.shutdown(reason="test finished")


def make_sidecar_world(
    tmp_path: Path,
    *,
    limits: LoopLimits = TEST_LIMITS,
    registry: AdapterRegistry | None = None,
    with_pid_file: bool = False,
    **loop_fields: Any,
) -> SidecarWorld:
    """Build an exchange directory and a loop over it, with everything injected."""
    root = tmp_path / "Zomboid"
    layout = IpcLayout(root / "Lua" / "pz_agent")
    layout.ensure()
    state_dir = root / "pz-agent"
    state_dir.mkdir(parents=True, exist_ok=True)
    clock = FakeClock(BASE_TIME_MS)
    sleeper = RecordingSleeper(clock)
    monitor = HeartbeatMonitor(layout, clock=clock)
    loop = _make_loop(
        layout=layout,
        state_dir=state_dir,
        clock=clock,
        sleeper=sleeper,
        monitor=monitor,
        limits=limits,
        registry=registry,
        with_pid_file=with_pid_file,
        loop_fields=loop_fields,
    )
    return SidecarWorld(
        root=root,
        layout=layout,
        state_dir=state_dir,
        clock=clock,
        sleeper=sleeper,
        loop=loop,
        monitor=monitor,
        limits=limits,
    )


def attached_world(tmp_path: Path, **kwargs: Any) -> SidecarWorld:
    """A world whose loop has attached, with the game already beating.

    The first heartbeat has to be written before the attach, because a session
    is only resumable while the game is answering; it is rewritten afterwards
    under the real session id. A test that wants the disconnected path simply
    stops refreshing it and advances the clock past the timeout.
    """
    world = make_sidecar_world(tmp_path, **kwargs)
    world.monitor.publish(
        Peer.GAME,
        session_id=str(uuid.uuid4()),
        nonce=uuid.uuid4().hex,
        version=PRODUCT_VERSION,
        build="42.20",
        player_present=True,
    )
    outcome = world.loop.attach()
    assert outcome.attached, outcome.detail
    world.beat_game()
    return world
