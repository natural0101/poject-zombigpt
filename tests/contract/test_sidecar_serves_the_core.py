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

Everything is bounded: the loop by a tick budget and a stop flag, every wait by
a deadline, the thread joins by timeouts.
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
from pz_agent_core.ipc.clocks import system_clock_ms
from pz_agent_core.ipc.journal import JournalWriter
from pz_agent_core.protocol import ActionName, Observation, SessionMode
from pz_agent_core.rpc.descriptor import DescriptorError, load_descriptor
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION
from pz_agent_mcp.remote.client import (
    CoreRefused,
    RemoteCoreServices,
    SidecarUnavailable,
)
from pz_agent_mcp.remote.server import NO_GOAL_CHANNEL_REASON
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

    stop = threading.Event()
    ticking = threading.Thread(
        target=lambda: loop.run(should_stop=stop.is_set),
        name="sidecar-loop",
        daemon=True,
    )
    ticking.start()
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

        goals = remote.goals
        assert goals is not None
        with pytest.raises(CoreRefused) as refused_goal:
            goals.status()
        assert NO_GOAL_CHANNEL_REASON in str(refused_goal.value)
    finally:
        stop.set()
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
