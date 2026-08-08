"""TB-R2's closing criterion: remote actions are served by the *real* loop.

The machinery is ``test_sidecar_serves_the_core.py``'s, deliberately: a real
:class:`~pz_agent_cli.runtime.SidecarLoop` assembled by the same
:func:`~pz_agent_cli.app.build_loop` the CLI runs, the mod faked at the
exchange-directory files and nowhere else, the router served by the very
:func:`~pz_agent_cli.core_services.serve_core_rpc` that ``pz-agent start``
calls, and the client the exact :meth:`RemoteCoreServices.from_state_dir` path
the MCP executable takes. What this file adds is the action channel, proven end
to end in both directions the port can go:

* **Disarmed**: a submission is admitted — ``accepted`` is the honest word for
  queued — and the loop's own tick ends it with the channel's ``NOT_ARMED``
  refusal in the record. Nothing runs, and nothing pretends to.
* **Armed**: a submission crosses the socket, is drained by the tick thread
  through the same funnel a planner proposal takes, and is driven by the real
  :class:`~pz_agent_core.actions.ActionEngine` to its *own* terminal result —
  ``action.wait`` succeeding on observed world-time evidence the fake mod's
  advancing clock provides, with ``POSTCONDITION_MET`` and the elapsed game
  seconds read back over the link. The mod acks nothing; the world itself is
  the proof, which is the engine's one route to success.

A resubmitted idempotency key resolves to the id it already created (a retried
submission never runs twice), and an id this core never minted answers ``None``
across the link. Everything is bounded: the loop by a tick budget and a stop
flag, every wait by a deadline and a poll count, the fake mod's two feeder
threads by iteration caps and stop events, the joins by timeouts.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.app import build_loop, build_supervisor
from pz_agent_cli.context import resolve_workspace
from pz_agent_cli.core_services import serve_core_rpc
from pz_agent_cli.doctor import run_checks
from pz_agent_cli.runtime import LoopLimits
from pz_agent_core.actions import ActionRequest
from pz_agent_core.ipc.clocks import system_clock_ms
from pz_agent_core.ipc.journal import JournalWriter
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION
from pz_agent_mcp.ports import ActionRecord
from pz_agent_mcp.remote.client import RemoteCoreServices
from tests.fixtures import make_game, make_observation
from tests.fixtures.cli_worlds import make_world

pytestmark = pytest.mark.contract

#: Outer bound on every wait in this file; same reasoning as the sibling test.
GRACE: Final = 15.0

#: The fake mod's world clock starts here and gains this many game seconds per
#: observation — far past the one game second the submitted wait asks for, so
#: the first fresh observation after dispatch already carries the proof.
WORLD_EPOCH: Final = datetime(1993, 7, 9, 14, 20, 0)
GAME_SECONDS_PER_OBSERVATION: Final = 60


class IdlePlanner:
    """A planner with no initiative, so the only dispatches are the client's."""

    def propose(self, observation: Observation) -> ActionRequest | None:
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


def _wait_request(session_id: str, key: str) -> ActionRequest:
    return ActionRequest(
        action=ActionName.ACTION_WAIT,
        session_id=session_id,
        idempotency_key=key,
        args={"game_seconds": 1.0},
    )


def test_remote_actions_are_served_end_to_end(tmp_path: Path) -> None:
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
    session = attach.session
    assert session is not None

    monitor = HeartbeatMonitor(loop.layout, clock=system_clock_ms)

    # -- serve, through the SHIPPED wiring, then run the loop on its thread --
    supervisor = build_supervisor(ctx, workspace)
    endpoint = serve_core_rpc(
        supervisor,
        loop,
        doctor=lambda: run_checks(ctx, workspace),
        log_file=workspace.logs_dir / "pz-agent.jsonl",
    )
    assert endpoint.descriptor_file.is_file(), "the link was not published"

    # The idle planner goes in AFTER the shipped wiring built the port bundle
    # over the real AutonomyPlanner, exactly as the sibling test swaps its
    # recorder in: from here on the only thing that can reach the engine is
    # what the client submits, so every dispatch below is attributable.
    loop.planner = IdlePlanner()

    stop = threading.Event()
    ticking = threading.Thread(
        target=lambda: loop.run(should_stop=stop.is_set),
        name="sidecar-loop",
        daemon=True,
    )
    ticking.start()

    # The fake mod, at the files: a heartbeat every half second, and an
    # observation stream whose world clock advances a minute per record —
    # which is what lets `action.wait` verify against the world rather than
    # against anybody's say-so. Both threads are bounded by iteration caps as
    # well as by their stop events.
    feeder_stop = threading.Event()

    def keep_beating() -> None:
        for _ in range(int(GRACE * 4) + 1):
            if feeder_stop.is_set():
                return
            monitor.publish(
                Peer.GAME,
                session_id=session.session_id,
                nonce="fake-mod-nonce",
                version=PRODUCT_VERSION,
                build="42.20",
                player_present=True,
            )
            feeder_stop.wait(0.5)

    def keep_observing() -> None:
        writer = JournalWriter(loop.layout, loop.layout.observation_events)
        try:
            for seq in range(1, int(GRACE * 40) + 1):
                if feeder_stop.is_set():
                    return
                stamp = WORLD_EPOCH + timedelta(seconds=seq * GAME_SECONDS_PER_OBSERVATION)
                observation = make_observation(
                    session_id=session.session_id,
                    seq=seq,
                    timestamp_ms=system_clock_ms(),
                    game=make_game(world_time=stamp.isoformat()),
                )
                writer.append(observation.to_dict())
                feeder_stop.wait(0.05)
        finally:
            writer.close()

    beating = threading.Thread(target=keep_beating, name="fake-mod-heartbeat", daemon=True)
    observing = threading.Thread(target=keep_observing, name="fake-mod-observations", daemon=True)
    beating.start()
    observing.start()
    try:
        # -- the second process's exact client path --------------------------
        remote = RemoteCoreServices.from_state_dir(workspace.state_dir, deadline=GRACE)

        # An id this core never minted is None across the link — true, not
        # a placeholder: nothing has been submitted yet.
        assert remote.actions.status("never-minted") is None

        # -- disarmed: the record carries the refusal ------------------------
        disarmed = remote.actions.submit(_wait_request(session.session_id, "e2e-act-disarmed"))
        assert disarmed.status is ActionStatus.ACCEPTED, "queued is 'accepted', never more"

        def disarmed_ended() -> bool:
            record = remote.actions.status(disarmed.action_id)
            return record is not None and record.terminal

        _until(disarmed_ended, message="the disarmed submission was never ended by the loop")
        refused = remote.actions.status(disarmed.action_id)
        assert refused is not None
        assert refused.status is ActionStatus.REJECTED
        assert refused.result is not None
        assert refused.result.reason_code is ReasonCode.NOT_ARMED

        # -- armed: the dispatch drives the real engine to observed success --
        armed = remote.session.arm(SessionMode.ASSISTED, confirm_backup=True)
        assert armed.armed is True
        assert armed.mode is SessionMode.ASSISTED

        request = _wait_request(session.session_id, "e2e-act-armed")
        submitted = remote.actions.submit(request)
        assert submitted.status is ActionStatus.ACCEPTED

        # A resubmitted key resolves to the same action, whatever state it has
        # reached by now — a retried submission is never a second dispatch.
        again = remote.actions.submit(request)
        assert again.action_id == submitted.action_id

        seen: list[ActionRecord | None] = [None]

        def armed_ended() -> bool:
            seen[0] = remote.actions.status(submitted.action_id)
            return seen[0] is not None and seen[0].terminal

        _until(armed_ended, message="the armed submission never reached a terminal record")
        final = seen[0]
        assert final is not None
        assert final.status is ActionStatus.SUCCEEDED
        result = final.result
        assert result is not None, "a succeeded record cannot exist without its result"
        assert result.reason_code is ReasonCode.POSTCONDITION_MET
        observed = result.evidence["observed"]
        assert isinstance(observed, dict)
        assert observed["elapsed_game_seconds"] >= 1.0, (
            "success rests on the world clock the fake mod advanced, not on an ack"
        )
        assert observed["required_game_seconds"] == 1.0
    finally:
        feeder_stop.set()
        stop.set()
        beating.join(timeout=GRACE)
        observing.join(timeout=GRACE)
        ticking.join(timeout=GRACE)
        shutdown = loop.shutdown(reason="test finished")
        closed = supervisor.stop_rpc()

    assert not ticking.is_alive(), "the loop did not stop inside the bound"
    assert shutdown.lock_released is True
    assert closed.descriptor_removed is True and closed.token_revoked is True
