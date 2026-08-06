"""The sidecar loop: attaches in OBSERVE, guards first, acts last, and stays bounded."""

from __future__ import annotations

import json
import signal
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pz_agent_cli import app
from pz_agent_cli.config import default_config
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, resolve_workspace
from pz_agent_cli.runtime import (
    ActionBudget,
    ArmOutcome,
    JournalObservationSource,
    LoopError,
    LoopLimits,
    ObservationFeed,
    QueueCommandSink,
    SidecarLoop,
    StopCause,
)
from pz_agent_cli.supervisor import (
    CONTROL_MAX_AGE_MS,
    ControlChannel,
    ControlKind,
    PidFile,
    SidecarSupervisor,
)
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.ipc.journal import JournalReader
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.ipc.queue import CommandQueue
from pz_agent_core.ipc.snapshot import SnapshotReader, SnapshotWriter
from pz_agent_core.observation.store import ObservationStore
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.safety.reflex import ReflexGuard, ReflexSignals, SafetyEvent
from pz_agent_core.session.handshake import SessionDescriptor
from pz_agent_core.session.heartbeat import Peer
from tests.fixtures import make_player
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.ipc_builders import FakeClock
from tests.fixtures.sidecar_worlds import (
    TEST_LIMITS,
    SidecarWorld,
    attached_world,
    make_sidecar_world,
)

#: The heartbeat monitor's default timeout is 5 s; anything past it is silence.
PAST_HEARTBEAT_TIMEOUT_MS = 10_000


@dataclass
class RecordingPlanner:
    """A planner that always wants the same thing, and counts being asked."""

    session_id: str
    trace: list[str] = field(default_factory=list)
    calls: int = 0

    def propose(self, observation: Observation) -> ActionRequest | None:
        self.calls += 1
        self.trace.append("planner")
        return ActionRequest(
            action=ActionName.ACTION_WAIT,
            session_id=self.session_id,
            idempotency_key=f"wait-{self.calls}",
            args={"game_seconds": 1.0},
        )


#: Where a :class:`RecordingGuard` writes, keyed by the guard's identity.
#: ``ReflexGuard`` is a frozen slots dataclass, so the trace cannot live on the
#: instance — and subclassing it rather than replacing it keeps the real rules
#: in play, which is the point: the assertion is about ordering, not about a
#: double standing in for the guard.
_GUARD_TRACES: dict[int, list[str]] = {}


class RecordingGuard(ReflexGuard):
    """The real guard, with its call recorded against a shared trace."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__()
        _GUARD_TRACES[id(self)] = trace

    def evaluate(
        self,
        previous: Observation | None,
        current: Observation,
        signals: ReflexSignals | None = None,
    ) -> list[SafetyEvent]:
        _GUARD_TRACES[id(self)].append("guard")
        return super().evaluate(previous, current, signals)


# ---------------------------------------------------------------------------
# attaching
# ---------------------------------------------------------------------------


def test_attaching_comes_up_in_observe_and_disarmed(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        assert world.loop.mode is SessionMode.OBSERVE
        assert world.loop.armed is False

        session = SessionDescriptor.from_dict(
            json.loads(world.layout.session.read_text(encoding="utf-8"))
        )
        assert session.mode is SessionMode.OBSERVE
        beat = world.monitor.read(Peer.SIDECAR)
        assert beat is not None
        assert beat.armed is False
        assert beat.mode is SessionMode.OBSERVE


def test_a_second_loop_is_refused_while_the_first_holds_the_lock(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        rival = SidecarLoop(
            layout=world.layout,
            state_dir=world.state_dir,
            registry=world.loop.registry,
            clock=world.clock,
            sleep=world.sleeper,
            limits=TEST_LIMITS,
        )

        outcome = rival.attach()

        assert outcome.attached is False
        assert "another sidecar holds the lock" in outcome.detail
        assert rival.session is None


def test_ticking_before_attaching_is_refused_rather_than_silently_idle(tmp_path: Path) -> None:
    world = make_sidecar_world(tmp_path)

    with pytest.raises(LoopError, match=r"has not attached"):
        world.loop.tick()


def test_shutdown_releases_the_session_lock(tmp_path: Path) -> None:
    world = attached_world(tmp_path)
    assert world.layout.sidecar_lock.exists()

    outcome = world.loop.shutdown(reason="user asked")

    assert outcome.lock_released is True
    assert not world.layout.sidecar_lock.exists()
    assert world.loop.session is None
    assert world.loop.armed is False


def test_shutdown_closes_in_flight_work_as_lost_not_as_failed(tmp_path: Path) -> None:
    world = attached_world(tmp_path)
    queue = world.loop.queue
    assert queue is not None
    queue.submit(queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=30_000))

    outcome = world.loop.shutdown(reason="user asked")

    assert [result.status for result in outcome.lost] == [ActionStatus.LOST]
    assert outcome.lost[0].reason_code is ReasonCode.CANCELLED_BY_REQUEST


# ---------------------------------------------------------------------------
# the guard runs first
# ---------------------------------------------------------------------------


def test_the_reflex_guard_runs_before_the_planner_is_consulted(tmp_path: Path) -> None:
    trace: list[str] = []
    with attached_world(tmp_path, guard=RecordingGuard(trace)) as world:
        planner = RecordingPlanner(world.session_id, trace)
        world.loop.planner = planner
        assert world.loop.arm().armed is True
        world.observe()

        world.loop.tick()

        assert trace == ["guard", "planner"]


def test_the_guard_runs_with_no_planner_attached_at_all(tmp_path: Path) -> None:
    trace: list[str] = []
    with attached_world(tmp_path, guard=RecordingGuard(trace)) as world:
        world.observe()

        outcome = world.loop.tick()

        assert trace == ["guard"]
        assert outcome.results == ()


def test_a_guard_event_stops_the_planner_being_asked_at_all(tmp_path: Path) -> None:
    trace: list[str] = []
    with attached_world(tmp_path, guard=RecordingGuard(trace)) as world:
        planner = RecordingPlanner(world.session_id, trace)
        world.loop.planner = planner
        world.loop.arm()
        world.observe(player=make_player(alive=False))

        outcome = world.loop.tick()

        assert [event.reason_code for event in outcome.events] == [ReasonCode.SESSION_TERMINATED]
        assert planner.calls == 0
        assert outcome.disarmed is True
        assert world.loop.armed is False


# ---------------------------------------------------------------------------
# a silent game
# ---------------------------------------------------------------------------


def test_a_missing_game_heartbeat_closes_in_flight_commands_as_lost(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.loop.arm()
        queue = world.loop.queue
        assert queue is not None
        queue.submit(queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=30_000))
        shipped = queue.in_flight
        assert shipped is not None
        world.clock.advance(PAST_HEARTBEAT_TIMEOUT_MS)

        outcome = world.loop.tick()

        assert outcome.game_alive is False
        assert [result.status for result in outcome.lost] == [ActionStatus.LOST]
        assert outcome.lost[0].reason_code is ReasonCode.GAME_DISCONNECTED
        assert outcome.lost[0].command_id == shipped.command_id
        assert queue.in_flight is None
        assert world.loop.armed is False


def test_a_silent_game_does_not_report_the_action_as_failed_or_succeeded(
    tmp_path: Path,
) -> None:
    with attached_world(tmp_path) as world:
        queue = world.loop.queue
        assert queue is not None
        command = queue.build(
            ActionName.INVENTORY_TRANSFER, idempotency_key="move-1", lease_ms=30_000
        )
        queue.submit(command)
        world.clock.advance(PAST_HEARTBEAT_TIMEOUT_MS)

        outcome = world.loop.tick()

        result = outcome.lost[0]
        assert result.status is not ActionStatus.FAILED
        assert result.status is not ActionStatus.SUCCEEDED
        assert result.message == (
            "the game stopped writing a heartbeat while this command was running"
        )


# ---------------------------------------------------------------------------
# panic
# ---------------------------------------------------------------------------


def test_panic_stop_disarms_and_keeps_the_planner_out_of_the_tick(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        planner = RecordingPlanner(world.session_id)
        world.loop.planner = planner
        assert world.loop.arm().armed is True
        world.panic()
        world.observe()

        outcome = world.loop.tick()

        assert outcome.panic is True
        assert world.loop.armed is False
        assert world.loop.mode is SessionMode.OBSERVE
        assert planner.calls == 0


def test_panic_stop_closes_in_flight_work_as_lost(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        queue = world.loop.queue
        assert queue is not None
        queue.submit(
            queue.build(ActionName.CONSUME_DRINK, idempotency_key="drink-1", lease_ms=30_000)
        )
        world.panic()

        outcome = world.loop.tick()

        assert [result.reason_code for result in outcome.lost] == [ReasonCode.PANIC_STOP]
        assert outcome.lost[0].status is ActionStatus.LOST


def test_arming_is_refused_while_the_panic_sentinel_is_present(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.panic()

        outcome = world.loop.arm()

        assert outcome == ArmOutcome(
            armed=False,
            mode=SessionMode.OBSERVE,
            detail="a panic-stop sentinel is present; clear it in the game before arming",
        )
        assert world.loop.armed is False


def test_clearing_the_sentinel_does_not_re_arm_anything(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.loop.arm()
        world.panic()
        world.observe()
        world.loop.tick()

        world.clear_panic()
        world.observe()
        outcome = world.loop.tick()

        assert outcome.panic is False
        assert world.loop.armed is False


# ---------------------------------------------------------------------------
# arming
# ---------------------------------------------------------------------------


def test_arming_is_refused_when_the_game_is_not_answering(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.clock.advance(PAST_HEARTBEAT_TIMEOUT_MS)

        outcome = world.loop.arm()

        assert outcome.armed is False
        assert "not writing a heartbeat" in outcome.detail


@pytest.mark.parametrize("mode", [SessionMode.OBSERVE, SessionMode.OFF, SessionMode.REFLEX_ONLY])
def test_arming_into_a_mode_this_build_does_not_grant_is_refused(
    tmp_path: Path, mode: SessionMode
) -> None:
    with attached_world(tmp_path) as world:
        outcome = world.loop.arm(mode)

        assert outcome.armed is False
        assert outcome.detail == f"{mode.value} is not a mode this build arms into"


def test_disarming_is_never_refused_even_with_no_game(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.loop.arm()
        world.clock.advance(PAST_HEARTBEAT_TIMEOUT_MS)

        outcome = world.loop.disarm(reason="user asked")

        assert outcome.armed is False
        assert outcome.changed is True
        assert outcome.mode is SessionMode.OBSERVE


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


def test_a_restart_comes_up_in_observe_however_it_was_left(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        assert world.loop.arm(SessionMode.AUTONOMOUS).armed is True

        world.restart()

        assert world.loop.mode is SessionMode.OBSERVE
        assert world.loop.armed is False


def test_an_arm_request_left_by_the_previous_run_does_not_re_arm_the_new_one(
    tmp_path: Path,
) -> None:
    """A crash leaves the request file behind; consuming it would re-arm blind."""
    with attached_world(tmp_path) as world:
        world.control.write(ControlKind.ARM, mode=SessionMode.AUTONOMOUS)
        world.clock.advance(1)
        world.restart()
        world.beat_game()
        world.observe()

        outcome = world.loop.tick()

        assert world.loop.armed is False
        assert outcome.mode is SessionMode.OBSERVE
        assert world.control.pending() is False


def test_an_arm_request_older_than_the_ceiling_is_ignored(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.control.write(ControlKind.ARM, mode=SessionMode.ASSISTED)
        world.clock.advance(CONTROL_MAX_AGE_MS + 1)
        world.beat_game()
        world.observe()

        world.loop.tick()

        assert world.loop.armed is False
        assert world.control.pending() is False


def test_a_fresh_arm_request_is_applied(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.clock.advance(1)
        world.control.write(ControlKind.ARM, mode=SessionMode.ASSISTED)
        world.observe()

        outcome = world.loop.tick()

        assert world.loop.armed is True
        assert outcome.mode is SessionMode.ASSISTED


def test_an_arm_request_racing_a_disarming_event_is_refused(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.clock.advance(1)
        world.control.write(ControlKind.ARM, mode=SessionMode.ASSISTED)
        world.observe(player=make_player(alive=False))

        world.loop.tick()

        assert world.loop.armed is False


def test_a_disarm_request_is_applied(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.loop.arm()
        world.clock.advance(1)
        world.control.write(ControlKind.DISARM)
        world.observe()

        world.loop.tick()

        assert world.loop.armed is False


def test_a_stop_request_ends_the_run_and_names_why(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.clock.advance(1)
        world.control.write(ControlKind.STOP)

        summary = world.loop.run()

        assert summary.cause is StopCause.STOP_REQUESTED
        assert summary.ticks == 1


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------


def test_the_tick_budget_is_a_ceiling_not_a_suggestion(tmp_path: Path) -> None:
    with attached_world(tmp_path, limits=LoopLimits(tick_interval_ms=10, tick_budget=4)) as world:
        summary = world.loop.run()

        assert summary.ticks == 4
        assert summary.cause is StopCause.TICK_BUDGET
        assert world.loop.ticks == 4
        # Three, not four: the loop does not pause before returning.
        assert world.sleeper.requests == [10, 10, 10]


def test_the_caller_can_stop_the_run_before_the_budget_is_spent(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        seen: list[int] = []

        def stop_after_two() -> bool:
            seen.append(world.loop.ticks)
            return len(seen) >= 2

        summary = world.loop.run(should_stop=stop_after_two)

        assert summary.cause is StopCause.CALLER_ASKED
        assert summary.ticks == 2


def test_the_action_rate_cap_stops_the_planner_being_consulted(tmp_path: Path) -> None:
    limits = LoopLimits(
        tick_interval_ms=10,
        tick_budget=6,
        max_actions_per_window=2,
        action_window_ms=600_000,
        observations_per_tick=16,
        observation_window=8,
    )
    with attached_world(tmp_path, limits=limits) as world:
        planner = RecordingPlanner(world.session_id)
        world.loop.planner = planner
        world.loop.arm()
        world.observe()

        def keep_alive() -> bool:
            world.beat_game()
            return False

        summary = world.loop.run(should_stop=keep_alive)

        assert summary.ticks == 6
        assert planner.calls == limits.max_actions_per_window


def test_the_action_budget_refuses_to_be_spent_past_its_cap() -> None:
    budget = ActionBudget(limit=2, window_ms=1_000)
    budget.spend(0)
    budget.spend(10)

    assert budget.allows(20) is False
    with pytest.raises(LoopError, match=r"action budget of 2 per window is spent"):
        budget.spend(20)


def test_the_action_budget_refills_once_the_window_has_passed() -> None:
    budget = ActionBudget(limit=1, window_ms=1_000)
    budget.spend(0)

    assert budget.allows(999) is False
    assert budget.allows(1_000) is True
    assert budget.spent(1_000) == 0


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("tick_budget", 0, r"tick_budget must be within"),
        ("tick_budget", 5_000_001, r"tick_budget must be within"),
        ("tick_interval_ms", -1, r"tick_interval_ms must be within"),
        ("max_actions_per_window", 0, r"max_actions_per_window must be within"),
        ("max_actions_per_window", 241, r"max_actions_per_window must be within"),
        ("action_window_ms", 0, r"action_window_ms must be positive"),
        ("observations_per_tick", 0, r"observations_per_tick must be within"),
        ("observations_per_tick", 1_025, r"observations_per_tick must be within"),
    ],
)
def test_every_loop_bound_is_validated(field_name: str, value: int, message: str) -> None:
    with pytest.raises(LoopError, match=message):
        LoopLimits(**{field_name: value})


def test_the_observation_ring_stays_at_its_capacity_however_many_arrive(
    tmp_path: Path,
) -> None:
    with attached_world(tmp_path, limits=LoopLimits(tick_budget=4, observation_window=4)) as world:
        for _ in range(20):
            world.observe()

        world.loop.tick()

        assert world.loop.store.size == 4
        assert world.loop.store.capacity == 4


def test_the_retained_event_ring_is_bounded(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        for _ in range(60):
            world.observe(player=make_player(alive=False))
            world.loop.tick()

        assert len(world.loop.recent_events) <= 32


# ---------------------------------------------------------------------------
# the ports
# ---------------------------------------------------------------------------


def _feed(world: SidecarWorld) -> ObservationFeed:
    return ObservationFeed(
        layout=world.layout,
        reader=JournalReader(world.layout, world.layout.observation_events),
        snapshots=SnapshotReader(world.layout),
    )


def test_an_unparseable_observation_is_counted_and_skipped_not_fatal(tmp_path: Path) -> None:
    with attached_world(tmp_path) as world:
        world.write_raw_observation({"schema_version": "1.0", "seq": "not a number"})
        good = world.observe()
        store = ObservationStore(capacity=4)
        feed = _feed(world)

        accepted = feed.drain(store)

        assert accepted == 1
        assert feed.rejected == 1
        assert store.latest() is not None
        assert store.latest().seq == good.seq  # type: ignore[union-attr]
        assert any("unusable observation" in line for line in feed.diagnostics)


def test_the_feed_recovers_a_baseline_from_the_snapshot_when_it_has_none(
    tmp_path: Path,
) -> None:
    with attached_world(tmp_path) as world:
        observation = world.observe()
        SnapshotWriter(world.layout, clock=world.clock).publish(observation.to_dict())
        store = ObservationStore(capacity=4)
        feed = ObservationFeed(
            layout=world.layout,
            reader=JournalReader(world.layout, world.layout.observation_events),
            snapshots=SnapshotReader(world.layout),
        )
        # Position past the journal so only the snapshot can supply a baseline.
        feed.reader.seek_to_end()

        accepted = feed.drain(store)

        assert accepted == 1
        assert store.needs_full_snapshot is False


def test_the_observation_source_gives_up_rather_than_spinning_on_a_stopped_clock(
    tmp_path: Path,
) -> None:
    with attached_world(tmp_path) as world:
        frozen = FakeClock(world.clock.now)
        polls: list[int] = []
        source = JournalObservationSource(
            feed=_feed(world),
            store=ObservationStore(capacity=4),
            clock=frozen,
            sleep=polls.append,
            max_polls=5,
        )

        assert source.wait_for_next(after_seq=99, timeout_ms=10_000) is None
        assert len(polls) == 5


def test_the_sink_withdraws_a_command_with_plan_cancel_not_with_a_stop(
    tmp_path: Path,
) -> None:
    layout = IpcLayout(tmp_path / "pz_agent")
    layout.ensure()
    clock = FakeClock()
    session_id = "00000000-0000-0000-0000-0000000009ce"
    queue = CommandQueue(layout, session_id=session_id, clock=clock)
    try:
        sink = QueueCommandSink(queue, clock=clock)
        command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=30_000)
        sink.send(command)

        sink.cancel(queue.in_flight or command, ReasonCode.NO_PROGRESS)

        written = [
            line
            for line in layout.command_queue.read_text(encoding="utf-8").splitlines()
            if '"plan.cancel"' in line
        ]
        assert len(written) == 1
        assert '"safety.stop"' not in layout.command_queue.read_text(encoding="utf-8")
        assert sink.cancellations == 1
    finally:
        queue.close()


# ---------------------------------------------------------------------------
# the CLI subcommands the loop and the supervisor stand behind
# ---------------------------------------------------------------------------


def _configured(tmp_path: Path) -> CliWorld:
    """A fake machine with a valid configuration already in place."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(default_config().to_toml(), encoding="utf-8")
    return world


def test_start_in_the_foreground_attaches_in_observe_and_releases_the_lock(
    tmp_path: Path,
) -> None:
    world = _configured(tmp_path)

    exit_code = world.run("start", "--foreground", "--ticks", "1", "--json")

    assert exit_code == EXIT_OK
    document = json.loads(world.stdout)
    assert document["mode"] == SessionMode.OBSERVE.value
    assert document["ticks"] == 1
    assert document["lock_released"] is True
    assert "in OBSERVE" in document["attached"]


def test_start_refuses_to_run_on_a_configuration_that_does_not_validate(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    workspace.config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text('[session]\ndefault_mode = "godmode"\n', encoding="utf-8")

    exit_code = world.run("start", "--foreground", "--ticks", "1")

    assert exit_code == EXIT_FAILURE
    assert "the sidecar was not started" in world.stderr
    assert not (workspace.state_dir / "sidecar.pid.json").exists()


def test_start_detaches_a_child_and_records_its_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _configured(tmp_path)
    spawned: list[tuple[str, ...]] = []

    def spawn(argv: object, cwd: Path, log_path: Path) -> int:
        assert isinstance(argv, list)
        spawned.append(tuple(str(item) for item in argv))
        return 5150

    def supervisor(ctx: object, workspace: object) -> SidecarSupervisor:
        return SidecarSupervisor(
            resolve_workspace(world.ctx).state_dir, clock=world.clock, spawn=spawn
        )

    monkeypatch.setattr(app, "build_supervisor", supervisor)

    exit_code = world.run("start", "--json")

    assert exit_code == EXIT_OK
    assert spawned[0][-2:] == ("start", "--foreground")
    document = json.loads(world.stdout)
    assert document["started"] is True
    assert document["record"]["pid"] == 5150
    assert document["mode"] == SessionMode.OBSERVE.value
    assert any("pz_agent_mcp" in line for line in document["mcp"])


def test_arm_publishes_a_request_for_the_running_sidecar(tmp_path: Path) -> None:
    world = _configured(tmp_path)
    state_dir = resolve_workspace(world.ctx).state_dir
    PidFile(state_dir / "sidecar.pid.json", clock=world.clock).claim(4242)

    exit_code = world.run("arm", "--mode", "autonomous")

    assert exit_code == EXIT_OK
    request = ControlChannel(state_dir / "sidecar.control.json").read()
    assert request is not None
    assert request.kind is ControlKind.ARM
    assert request.mode is SessionMode.AUTONOMOUS


def test_disarm_publishes_a_request_for_the_running_sidecar(tmp_path: Path) -> None:
    world = _configured(tmp_path)
    state_dir = resolve_workspace(world.ctx).state_dir
    PidFile(state_dir / "sidecar.pid.json", clock=world.clock).claim(4242)

    assert world.run("disarm") == EXIT_OK

    request = ControlChannel(state_dir / "sidecar.control.json").read()
    assert request is not None
    assert request.kind is ControlKind.DISARM


def test_stop_signals_the_recorded_process_and_leaves_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signal is injected: a real one would be delivered to the test runner."""
    world = _configured(tmp_path)
    state_dir = resolve_workspace(world.ctx).state_dir
    PidFile(state_dir / "sidecar.pid.json", clock=world.clock).claim(4242)
    signalled: list[tuple[int, int]] = []

    def supervisor(ctx: object, workspace: object) -> SidecarSupervisor:
        return SidecarSupervisor(
            state_dir,
            clock=world.clock,
            signaller=lambda pid, number: signalled.append((pid, number)),
            signals_supported=True,
        )

    monkeypatch.setattr(app, "build_supervisor", supervisor)

    exit_code = world.run("stop", "--json")

    assert exit_code == EXIT_OK
    document = json.loads(world.stdout)
    assert document["requested"] is True
    assert document["signalled"] is True
    assert signalled == [(4242, int(signal.SIGTERM))]
    assert ControlChannel(state_dir / "sidecar.control.json").read() is not None


def test_arming_a_sidecar_that_is_not_running_fails_rather_than_pretending(
    tmp_path: Path,
) -> None:
    world = _configured(tmp_path)

    exit_code = world.run("arm")

    assert exit_code == EXIT_FAILURE
    assert "no sidecar is running" in world.stderr
    assert not (resolve_workspace(world.ctx).state_dir / "sidecar.control.json").exists()
