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

One proof the tests above cannot make closes the file. Their client is the real
:class:`RemoteCoreServices`, but it runs in the server's own interpreter — the
``sys.path`` pytest assembled, the parent's already-imported modules, one
process's memory. ``test_a_second_process_reaches_the_real_core`` hands a
genuine child interpreter the state directory and nothing else, and reads back
facts only the real core holds; its negative companion runs the same child with
nothing serving and watches it refuse rather than invent.

Everything is bounded: the loop by a tick budget and a stop flag, every wait by
a deadline and a poll count, the heartbeat keeper by an iteration cap, the
thread joins by timeouts, the child interpreter by a hard subprocess timeout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

import pz_agent_core
import pz_agent_mcp
from pz_agent_cli.app import build_loop, build_supervisor
from pz_agent_cli.config import default_config
from pz_agent_cli.context import EXIT_OK, resolve_workspace
from pz_agent_cli.core_services import serve_core_rpc
from pz_agent_cli.doctor import run_checks
from pz_agent_cli.runtime import LoopLimits
from pz_agent_core.actions import ActionRequest
from pz_agent_core.goals import GoalKind, GoalParams, GoalRequest, GoalState
from pz_agent_core.ipc.clocks import system_clock_ms
from pz_agent_core.ipc.journal import JournalWriter
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.planner import GoalKind as PlannerGoalKind
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.rpc.descriptor import DescriptorError, load_descriptor
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION, PROTOCOL_VERSION
from pz_agent_mcp.remote.client import (
    SIDECAR_NOT_RUNNING,
    RemoteCoreServices,
    SidecarUnavailable,
)
from tests.fixtures import make_observation
from tests.fixtures.cli_worlds import make_world

pytestmark = pytest.mark.contract

#: Outer bound on every wait in this file. Long enough for a loaded CI runner —
#: the Windows package job has been observed running this suite five times
#: slower than Linux, so a fifteen-second poll timed out mid-handshake there —
#: short enough that a genuine hang still fails the test well under the suite's
#: 300-second per-test cap rather than riding it to the ceiling.
GRACE: Final = 30.0

#: How many heartbeats the fake mod publishes before its own safety cap stops
#: it. The real terminator is ``keeper_stop`` in the ``finally`` below; this cap
#: only guards against a leak if that is never set, so it is sized to the
#: suite's 300-second bound rather than to :data:`GRACE`. The earlier cap was
#: ``GRACE * 4`` — about thirty seconds of beats — and on a five-times-slower
#: Windows runner the test body outran it: the heartbeat lapsed, the loop read
#: the game as gone, and it stopped asking its planner for the goal. That was
#: the flake behind an intermittent "loop never asked its planner" on Windows.
_KEEPER_MAX_BEATS: Final = int(280 / 0.5)

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

    # The loop's exception is captured rather than left to the thread excepthook:
    # under ``filterwarnings=error`` an unhandled thread exception becomes an
    # opaque ERROR that says nothing about *why* the loop died, and a dead loop
    # reads downstream only as "it never asked its planner". Held here so a real
    # crash is the failure the test reports, not a warning beside a timeout.
    loop_failure: list[BaseException] = []

    def _run_loop() -> None:
        try:
            loop.run(should_stop=stop.is_set)
        except BaseException as exc:
            loop_failure.append(exc)

    stop = threading.Event()
    ticking = threading.Thread(target=_run_loop, name="sidecar-loop", daemon=True)
    ticking.start()

    # The fake mod keeps beating while the session is armed: a heartbeat that
    # went stale mid-test would be a *real* disarm (GAME_DISCONNECTED), and the
    # goal assertions below would be reading that instead of the levers under
    # test. Bounded by an iteration cap as well as by the stop event.
    keeper_stop = threading.Event()

    def keep_beating() -> None:
        session = attach.session
        assert session is not None
        for _ in range(_KEEPER_MAX_BEATS):
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

        # Remote actions are served now (TB-R2): a submission while disarmed
        # is admitted — 'accepted' is the honest word for queued — and the
        # loop's own tick ends it with the channel's NOT_ARMED refusal rather
        # than parking a stale intent until authority someday returns. The
        # armed dispatch, driven to the engine's own observed success over
        # this same socket, is tests/contract/test_remote_actions_served.py.
        submitted = remote.actions.submit(
            ActionRequest(
                action=ActionName.CONSUME_EAT,
                session_id=attach.session.session_id,
                idempotency_key="e2e-1",
                args={"item_ref": "i-1"},
            )
        )
        assert submitted.status is ActionStatus.ACCEPTED

        def action_ended() -> bool:
            record = remote.actions.status(submitted.action_id)
            return record is not None and record.terminal

        _until(action_ended, message="the disarmed submission was never ended by the loop")
        refused_record = remote.actions.status(submitted.action_id)
        assert refused_record is not None
        assert refused_record.status is ActionStatus.REJECTED
        assert refused_record.result is not None
        assert refused_record.result.reason_code is ReasonCode.NOT_ARMED

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

    # A loop that died carries the real reason; surface it as the failure rather
    # than letting a downstream "never asked its planner" stand in for it.
    if loop_failure:
        raise AssertionError("the sidecar loop thread died") from loop_failure[0]
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


#: Hard bound on the child interpreter. Generous, because a cold interpreter on
#: a loaded CI runner pays import costs the parent paid long ago; hard, because
#: a hung child must fail this test rather than the suite's outer timeout.
CHILD_DEADLINE: Final = 60.0

#: How the child learns where the core lives: one environment variable carrying
#: the state directory — the same single fact an MCP client hands
#: ``pz-agent-mcp``, and deliberately nothing more.
STATE_DIR_VARIABLE: Final = "PZ_AGENT_TEST_STATE_DIR"

#: The whole second process. It knows the state directory and nothing else —
#: not the session id, not the chosen sequence number, not that a loop exists —
#: so every fact it prints had to cross the link. ``dict(...)`` rather than a
#: dict literal so the f-string needs no doubled braces.
CHILD_SCRIPT: Final = f"""\
import json
import os
import sys
from pathlib import Path

from pz_agent_mcp.remote.client import RemoteCoreServices

snapshot = RemoteCoreServices.from_state_dir(
    Path(os.environ[{STATE_DIR_VARIABLE!r}])
).session.status()
json.dump(
    dict(
        pid=os.getpid(),
        session_id=snapshot.session_id,
        mode=snapshot.mode.value,
        armed=snapshot.armed,
        protocol_version=snapshot.protocol_version,
        observation_seq=snapshot.observation_seq,
    ),
    sys.stdout,
)
"""


def _child_env(state_dir: Path) -> dict[str, str]:
    """The environment the child needs: the import roots, and where the core is.

    The parent's interpreter finds these packages through pytest's
    ``pythonpath`` setting, which no child inherits, so the roots are put on
    ``PYTHONPATH`` — derived from where the packages were actually imported
    from rather than hard-coded, for the same reason
    ``test_mcp_subprocess_e2e`` derives them: a hard-coded ``packages/*/src``
    would pass here and tell an installed distribution nothing.
    """
    roots = {
        str(Path(module.__file__ or "").resolve().parents[1])
        for module in (pz_agent_mcp, pz_agent_core)
    }
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([*sorted(roots), *([existing] if existing else [])])
    env[STATE_DIR_VARIABLE] = str(state_dir)
    return env


def test_a_second_process_reaches_the_real_core(tmp_path: Path) -> None:
    """The criterion taken literally: a second OS process reaches the real core.

    ``test_sidecar_serves_core`` above drives the shipped client against the
    shipped server, and still falls one step short of the criterion's words:
    its client runs in the server's own interpreter. The same ``sys.path``
    pytest assembled, the same already-imported modules, one process whose
    memory holds the loop — so a wiring mistake that only exists across a real
    process boundary passes it. An import that resolves only under pytest, a
    descriptor published somewhere no other process looks, a token a second
    process cannot read: none of them can fail an in-process test, and any of
    them would strand a real MCP client.

    So this test hands a genuine child interpreter the state directory and
    nothing else, exactly the hand-off an MCP client performs when it launches
    ``pz-agent-mcp``. The child dials the link cold — descriptor and token off
    disk, socket from the descriptor — asks ``session.status``, and prints the
    answer. What the parent then pins are facts nothing but the real core
    holds: the session id the loop's own attach minted in *this* run, which no
    fixture defaults to because it is minted fresh per attach, and the
    observation sequence number the fake mod chose (:data:`CHOSEN_SEQ`), which
    travelled journal -> store -> link -> child. Remove ``serve_core_rpc``,
    or put anything but the real loop behind it, and the child either refuses
    (a dead link exits non-zero) or answers different facts; either way the
    assertions below go red.
    """
    world = make_world(tmp_path, username="u")
    # The state directory is pinned short because a POSIX socket path must fit
    # sun_path, exactly as in the tests above.
    ctx = replace(world.ctx, clock_ms=system_clock_ms, state_dir_override=tmp_path / "s")
    workspace = resolve_workspace(ctx)
    assert workspace.ipc_root is not None

    # -- the real loop and the real link, the same wiring as above -----------
    limits = LoopLimits(tick_interval_ms=10, tick_budget=6_000)
    loop = build_loop(ctx, workspace, limits=limits)
    attach = loop.attach()
    assert attach.attached, attach.detail
    assert attach.session is not None
    session_id = attach.session.session_id

    monitor = HeartbeatMonitor(loop.layout, clock=system_clock_ms)
    monitor.publish(
        Peer.GAME,
        session_id=session_id,
        nonce="fake-mod-nonce",
        version=PRODUCT_VERSION,
        build="42.20",
        player_present=True,
    )
    written = make_observation(
        session_id=session_id,
        seq=CHOSEN_SEQ,
        timestamp_ms=system_clock_ms(),
    )
    writer = JournalWriter(loop.layout, loop.layout.observation_events)
    try:
        writer.append(written.to_dict())
    finally:
        writer.close()

    supervisor = build_supervisor(ctx, workspace)
    endpoint = serve_core_rpc(
        supervisor,
        loop,
        doctor=lambda: run_checks(ctx, workspace),
        log_file=workspace.logs_dir / "pz-agent.jsonl",
    )
    assert endpoint.descriptor_file.is_file(), "the link was not published"

    stop = threading.Event()
    ticking = threading.Thread(
        target=lambda: loop.run(should_stop=stop.is_set),
        name="sidecar-loop-for-child",
        daemon=True,
    )
    ticking.start()
    try:
        # The chosen observation must be in the loop's store before the child
        # asks, or a slow first tick would read as a broken link. Waited for on
        # the store itself — the parent owns the loop, so it may look — while
        # the child is handed nothing but the state directory.
        _until(
            lambda: loop.store.latest() is not None,
            message="the loop never ingested the fake mod's observation",
        )

        # No heartbeat keeper thread here, on purpose: everything the child
        # reads — mode, armed, the session id, the stored observation —
        # survives the fake mod's single heartbeat going stale mid-call. Only
        # `connected` would flip, and nothing below pins it.
        child = subprocess.run(
            [sys.executable, "-c", CHILD_SCRIPT],
            capture_output=True,
            text=True,
            timeout=CHILD_DEADLINE,
            env=_child_env(workspace.state_dir),
            check=False,
        )
    finally:
        stop.set()
        ticking.join(timeout=GRACE)
        shutdown = loop.shutdown(reason="test finished")
        closed = supervisor.stop_rpc()

    assert not ticking.is_alive(), "the loop did not stop inside the bound"
    assert shutdown.lock_released is True
    assert closed.descriptor_removed is True and closed.token_revoked is True

    # The child's stderr is the whole diagnosis when this goes red — a refusal,
    # an import error, a traceback — so it is quoted in the failure message.
    assert child.returncode == 0, (
        f"the child exited {child.returncode}; its stderr:\n{child.stderr}"
    )
    answer = json.loads(child.stdout)
    assert answer["pid"] != os.getpid(), "the 'child' answered from this process"
    assert answer["session_id"] == session_id, answer
    assert answer["observation_seq"] == CHOSEN_SEQ, answer
    assert answer["mode"] == SessionMode.OBSERVE.value
    assert answer["armed"] is False
    assert answer["protocol_version"] == PROTOCOL_VERSION


def test_the_same_child_refuses_when_nothing_serves_the_link(tmp_path: Path) -> None:
    """The negative that keeps the proof above honest.

    A child that printed a canned answer would pass the test above, so the
    identical script is run against a state directory nobody has ever served —
    no descriptor, no token, no socket. It must refuse the way the shipped
    client refuses: exit non-zero, say :data:`SIDECAR_NOT_RUNNING`'s sentence
    on stderr, and print no answer at all, because a success invented over a
    dead link is exactly what this project forbids.
    """
    state_dir = tmp_path / "s"
    state_dir.mkdir()

    child = subprocess.run(
        [sys.executable, "-c", CHILD_SCRIPT],
        capture_output=True,
        text=True,
        timeout=CHILD_DEADLINE,
        env=_child_env(state_dir),
        check=False,
    )

    assert child.returncode != 0, "the child answered with nothing serving the link"
    assert child.stdout == "", f"the child printed an answer anyway: {child.stdout!r}"
    assert SIDECAR_NOT_RUNNING in child.stderr, child.stderr
