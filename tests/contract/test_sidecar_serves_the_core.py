"""R-008's closing criterion: a second process reaches the *real* core.

Every other end-to-end test hosts the router in the test process over fakes.
This one does not. A real :class:`~pz_agent_cli.runtime.SidecarLoop` is
assembled by the same :func:`~pz_agent_cli.app.build_loop` the CLI runs, over a
real exchange directory whose mod side is faked at the files and nowhere else —
a heartbeat written where the mod writes one, an observation appended to the
journal the mod appends to. The router is served by
:func:`~pz_agent_cli.core_services.serve_core_rpc`, which is the very function
``pz-agent start --foreground`` calls, not a copy of its wiring. The client is
:meth:`RemoteCoreServices.from_state_dir` — the exact path the MCP executable
takes when an MCP client hands it a state directory and nothing else.

What is asserted crossed a process-shaped boundary for real: a descriptor and a
token read off disk, a Unix socket dialled, the loop ticking on its own thread
while the serving thread answers. The mode is the loop's mode, the observation
is the one the fake mod chose (down to a sequence number no default produces),
and the surfaces this build does not serve refuse with their *named* refusals —
never an invented success, never silence.

The typed goal channel is served here too, end to end: the client submits a
goal with fixture-chosen parameters (``satisfy_to=0.73`` — no default produces
it), and the recording planner injected into the real loop proves the plan the
planner is asked for is the goal the queue activated — same minted id, same
kind, over a record whose parameters are the client's own. ``goal.status`` is
the channel's real lifecycle, ``goal.cancel`` the real lever applied by the
loop's next tick, and a disarm leaves no goal in flight, through the queue's
own vocabulary.

Everything is bounded: the loop by a tick budget and a stop flag, every wait by
a deadline and a poll count, the heartbeat keeper by an iteration cap, the
thread joins by timeouts.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.app import build_loop, build_supervisor
from pz_agent_cli.config import default_config
from pz_agent_cli.context import EXIT_OK, resolve_workspace
from pz_agent_cli.core_services import REMOTE_ACTIONS_UNSERVED, serve_core_rpc
from pz_agent_cli.doctor import run_checks
from pz_agent_cli.runtime import LoopLimits
from pz_agent_core.actions import ActionRequest
from pz_agent_core.goals import GoalKind, GoalParams, GoalRequest, GoalState
from pz_agent_core.ipc.clocks import system_clock_ms
from pz_agent_core.ipc.journal import JournalWriter
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.planner import GoalKind as PlannerGoalKind
from pz_agent_core.protocol import ActionName, Observation, ReasonCode, SessionMode
from pz_agent_core.rpc.descriptor import DescriptorError, load_descriptor
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION
from pz_agent_mcp.remote.client import (
    CoreRefused,
    RemoteCoreServices,
    SidecarUnavailable,
)
from tests.fixtures import make_observation
from tests.fixtures.cli_worlds import make_world

pytestmark = pytest.mark.contract

#: Outer bound on every wait in this file. Long enough for a loaded CI runner,
#: short enough that a hang fails the test rather than the suite's timeout.
GRACE: Final = 15.0

#: The sequence number the fake mod chooses. Nothing defaults to it: the
#: fixture default is 1, a fresh store holds none, so reading it back proves
#: the value travelled from the journal through the real store over the link.
CHOSEN_SEQ: Final = 47

#: The goal parameter the client chooses. No default produces it — the channel
#: has no default ``satisfy_to`` at all — so reading it back off the activated
#: record proves the parameter travelled from the client into the queue whose
#: goal the loop's planner was asked for.
CHOSEN_SATISFY_TO: Final = 0.73


class RecordingGoalPlanner:
    """The planner injected into the real loop: records the goals it is asked for.

    It answers ``None`` to everything, which keeps every submitted goal active
    (nothing serves it this tick) so the test can read the lifecycle over the
    link at its own pace. ``asked`` is appended on the loop's tick thread and
    read from the test thread; a list append under the GIL and a bounded
    ``_until`` poll on the reading side are the whole synchronisation needed.
    """

    def __init__(self) -> None:
        self.asked: list[PlannerGoal] = []

    def propose(self, observation: Observation) -> ActionRequest | None:
        return None

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        self.asked.append(goal)
        return None


def _until(predicate: object, *, message: str) -> None:
    """Poll *predicate* (a callable) with a deadline and a poll bound."""
    assert callable(predicate)
    deadline = time.monotonic() + GRACE
    for _ in range(int(GRACE / 0.05) + 1):
        if predicate():
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    pytest.fail(message)


def test_sidecar_serves_core(tmp_path: Path) -> None:
    # -- the machine: a fake install and profile, the CLI's own discovery ----
    world = make_world(tmp_path, username="u")
    # The state directory is pinned short because a POSIX socket path must fit
    # sun_path; the exchange directory stays where discovery puts it.
    ctx = replace(world.ctx, clock_ms=system_clock_ms, state_dir_override=tmp_path / "s")
    workspace = resolve_workspace(ctx)
    assert workspace.ipc_root is not None

    # -- the real loop, assembled the way `pz-agent start` assembles it ------
    limits = LoopLimits(tick_interval_ms=10, tick_budget=6_000)
    loop = build_loop(ctx, workspace, limits=limits)
    attach = loop.attach()
    assert attach.attached, attach.detail
    assert attach.session is not None

    # -- the mod, faked at the exchange directory and nowhere else -----------
    monitor = HeartbeatMonitor(loop.layout, clock=system_clock_ms)
    monitor.publish(
        Peer.GAME,
        session_id=attach.session.session_id,
        nonce="fake-mod-nonce",
        version=PRODUCT_VERSION,
        build="42.20",
        player_present=True,
    )
    written = make_observation(
        session_id=attach.session.session_id,
        seq=CHOSEN_SEQ,
        timestamp_ms=system_clock_ms(),
    )
    writer = JournalWriter(loop.layout, loop.layout.observation_events)
    try:
        writer.append(written.to_dict())
    finally:
        writer.close()

    # -- serve, through the SHIPPED wiring, then run the loop on its thread --
    supervisor = build_supervisor(ctx, workspace)
    endpoint = serve_core_rpc(
        supervisor,
        loop,
        doctor=lambda: run_checks(ctx, workspace),
        log_file=workspace.logs_dir / "pz-agent.jsonl",
    )
    assert endpoint.descriptor_file.is_file(), "the link was not published"

    # The recording planner goes in AFTER the shipped wiring built the port
    # bundle over the real AutonomyPlanner (which is what proves a goal-capable
    # planner was assembled); the loop reads its planner every tick, so from
    # here on the goals the loop serves are recorded where the test can see.
    planner = RecordingGoalPlanner()
    loop.planner = planner

    stop = threading.Event()
    ticking = threading.Thread(
        target=lambda: loop.run(should_stop=stop.is_set),
        name="sidecar-loop",
        daemon=True,
    )
    ticking.start()

    # The fake mod keeps beating while the session is armed: a heartbeat that
    # went stale mid-test would be a *real* disarm (GAME_DISCONNECTED), and the
    # goal assertions below would be reading that instead of the levers under
    # test. Bounded by an iteration cap as well as by the stop event.
    keeper_stop = threading.Event()

    def keep_beating() -> None:
        session = attach.session
        assert session is not None
        for _ in range(int(GRACE * 4) + 1):
            if keeper_stop.is_set():
                return
            monitor.publish(
                Peer.GAME,
                session_id=session.session_id,
                nonce="fake-mod-nonce",
                version=PRODUCT_VERSION,
                build="42.20",
                player_present=True,
            )
            keeper_stop.wait(0.5)

    beating = threading.Thread(target=keep_beating, name="fake-mod-heartbeat", daemon=True)
    beating.start()
    try:
        # -- the second process's exact client path --------------------------
        remote = RemoteCoreServices.from_state_dir(workspace.state_dir, deadline=GRACE)

        # The session is the real loop's, not a heartbeat's echo.
        snapshot = remote.session.status()
        assert snapshot.session_id == attach.session.session_id
        assert snapshot.mode is SessionMode.OBSERVE
        assert snapshot.mode is loop.mode
        assert snapshot.armed is loop.armed is False

        # The observation is the one the fake mod wrote — a chosen value, not
        # a default — read out of the loop's real store across the link.
        seen: list[Observation | None] = [None]

        def arrived() -> bool:
            seen[0] = remote.observations.latest()
            return seen[0] is not None

        _until(arrived, message="the fake mod's observation never crossed the link")
        assert seen[0] == written
        assert seen[0] is not None and seen[0].seq == CHOSEN_SEQ

        # The capability report is the ledger the real scan produced.
        ledger = loop.capabilities
        assert ledger is not None and ledger.report is not None
        assert remote.capabilities.report() == ledger.report

        # The memory is wired and truthfully holds nothing yet.
        assert remote.memory.query(kinds=[], limit=5) == ()

        # The doctor answers over the link with the shipped checks.
        checks = remote.diagnostics.doctor()
        assert checks, "the doctor answered nothing over the link"
        assert all(check.code.startswith("PZD") for check in checks)

        # Disarm travels the shipped control channel and is judged by the loop.
        disarmed = remote.session.disarm()
        assert disarmed.armed is False
        assert disarmed.mode is SessionMode.OBSERVE

        # A surface this build does not serve refuses with its named refusal —
        # the honest answer, never a stub and never an invented success.
        with pytest.raises(CoreRefused) as refused_action:
            remote.actions.submit(
                ActionRequest(
                    action=ActionName.CONSUME_EAT,
                    session_id=attach.session.session_id,
                    idempotency_key="e2e-1",
                    args={"item_ref": "i-1"},
                )
            )
        assert REMOTE_ACTIONS_UNSERVED in str(refused_action.value)

        # -- the typed goal channel, served by the real loop -----------------
        goals = remote.goals
        assert goals is not None

        # Arm AUTONOMOUS through the shipped control channel: the loop's own
        # judgement, waited for and relayed, exactly as a second process gets.
        armed = remote.session.arm(SessionMode.AUTONOMOUS, confirm_backup=True)
        assert armed.armed is True
        assert armed.mode is SessionMode.AUTONOMOUS

        admission = goals.submit(
            GoalRequest(
                kind=GoalKind.SATISFY_HUNGER,
                idempotency_key="e2e-goal-1",
                params=GoalParams(satisfy_to=CHOSEN_SATISFY_TO),
            )
        )
        assert admission.refusal is None
        assert admission.goal is not None
        goal_id = admission.goal.goal_id
        assert admission.goal.params == GoalParams(satisfy_to=CHOSEN_SATISFY_TO)

        # A resubmitted key resolves to the same goal, marked duplicate — the
        # queue's real admission crossing the link, not an invented ack.
        again = goals.submit(
            GoalRequest(
                kind=GoalKind.SATISFY_HUNGER,
                idempotency_key="e2e-goal-1",
                params=GoalParams(satisfy_to=CHOSEN_SATISFY_TO),
            )
        )
        assert again.duplicate is True
        assert again.goal is not None and again.goal.goal_id == goal_id

        # THE assertion: the plan the loop's planner is asked for is the goal
        # the queue activated — the id the queue minted for the client's
        # submission and the client's kind, over the record whose parameters
        # are the client's own (asserted on `status` just below).
        _until(lambda: planner.asked, message="the loop never asked its planner for the goal")
        asked = planner.asked[0]
        assert asked.goal_id == goal_id
        assert asked.kind is PlannerGoalKind.SATISFY_HUNGER

        # goal.status is the real channel state: the submitted goal is the
        # active one, still carrying the fixture-chosen parameter.
        status = goals.status(goal_id)
        assert status.active is not None and status.active.goal_id == goal_id
        assert status.named is not None
        assert status.named.state is GoalState.ACTIVE
        assert status.named.kind is GoalKind.SATISFY_HUNGER
        assert status.named.params == GoalParams(satisfy_to=CHOSEN_SATISFY_TO)

        # goal.cancel is the real lever: requested now, applied by the loop's
        # next tick, reported in the queue's own vocabulary.
        cancellation = goals.cancel(goal_id)
        assert cancellation.requested is True

        def cancelled() -> bool:
            named = goals.status(goal_id).named
            return named is not None and named.state is GoalState.CANCELLED

        _until(cancelled, message="the cancel was never applied by the loop's tick")
        ended = goals.status(goal_id).named
        assert ended is not None
        assert ended.reason_code is ReasonCode.CANCELLED_BY_REQUEST

        # Disarm leaves no goal in flight: a second goal is activated for
        # real, then ends through the queue's own disarm when the session
        # drops back to OBSERVE.
        second = goals.submit(
            GoalRequest(kind=GoalKind.SATISFY_THIRST, idempotency_key="e2e-goal-2")
        )
        assert second.goal is not None
        second_id = second.goal.goal_id

        def second_active() -> bool:
            active = goals.status().active
            return active is not None and active.goal_id == second_id

        _until(second_active, message="the second goal was never activated")

        dropped = remote.session.disarm()
        assert dropped.armed is False

        def nothing_in_flight() -> bool:
            channel = goals.status(second_id)
            named = channel.named
            return (
                channel.active is None and named is not None and named.state is GoalState.CANCELLED
            )

        _until(nothing_in_flight, message="disarm left a goal in flight")
        after_disarm = goals.status(second_id).named
        assert after_disarm is not None
        assert after_disarm.reason_code is ReasonCode.NOT_ARMED
    finally:
        keeper_stop.set()
        stop.set()
        beating.join(timeout=GRACE)
        ticking.join(timeout=GRACE)
        shutdown = loop.shutdown(reason="test finished")
        closed = supervisor.stop_rpc()

    assert not ticking.is_alive(), "the loop did not stop inside the bound"
    assert shutdown.lock_released is True
    assert closed.descriptor_removed is True and closed.token_revoked is True

    # Down means down: the descriptor is gone and the client says so.
    with pytest.raises(DescriptorError):
        load_descriptor(workspace.state_dir)
    with pytest.raises(SidecarUnavailable):
        RemoteCoreServices.from_state_dir(workspace.state_dir).session.status()


def test_the_start_command_itself_serves_the_link(tmp_path: Path) -> None:
    """The call site, pinned: ``pz-agent start --foreground`` serves the link.

    The test above proves :func:`serve_core_rpc` works when called; on its own
    that is R-008's original shape — a server half only tests construct. This
    one runs the shipped command through ``main`` and reads the two facts only
    the call site in ``_start_foreground`` can produce: the ``rpc.serving``
    record its success branch writes (the failure branch writes ``rpc.refused``
    instead, so a broken serve cannot pass), and a state directory whose
    descriptor was withdrawn again by the same ``finally`` that shut the loop
    down. Deleting the call in ``app.py`` fails this test and nothing else.
    """
    world = make_world(tmp_path, username="u")
    # Pinned short for sun_path, exactly as above.
    world.ctx = replace(world.ctx, state_dir_override=tmp_path / "s")
    workspace = resolve_workspace(world.ctx)
    workspace.config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(default_config().to_toml(), encoding="utf-8")

    assert world.run("start", "--foreground", "--ticks", "2") == EXIT_OK, world.stderr

    log_file = workspace.logs_dir / "pz-agent.jsonl"
    assert log_file.is_file(), "the sidecar wrote no structured log to read the proof from"
    events = {
        json.loads(line)["event"] for line in log_file.read_text(encoding="utf-8").splitlines()
    }
    assert "rpc.serving" in events, "start --foreground never served the Core RPC link"
    assert "rpc.refused" not in events, "the link was attempted and refused, not served"
    with pytest.raises(DescriptorError):
        load_descriptor(workspace.state_dir)
