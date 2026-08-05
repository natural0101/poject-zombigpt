"""End-to-end recovery across the session and IPC subsystems (§3.12).

Each test plays a whole failure sequence — sidecar killed, game restarted, save
swapped — because the property under test only exists across the boundary: the
session layer decides what is invalid, and the command stream is where "not
re-executed" is either true or not.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from pz_agent_core.ipc.journal import JournalReader
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.ipc.queue import CommandQueue
from pz_agent_core.protocol import ActionName, ActionStatus, Command, ReasonCode, SessionMode
from pz_agent_core.session.handshake import SessionManager, evaluate_handshake
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.session.lock import SidecarLock
from tests.fixtures.ipc_builders import FakeClock, make_layout, make_success, publish_acks

pytestmark = pytest.mark.integration


class _Mod:
    """Stands in for the Lua side: reads commands, remembers its offset."""

    def __init__(self, layout: IpcLayout) -> None:
        self._reader = JournalReader(layout, layout.command_queue)
        self.offset = 0
        self.serial: int | None = None

    def poll(self) -> list[Command]:
        read = self._reader.read()
        self.offset = read.offset
        self.serial = read.serial
        return [Command.from_dict(record.payload) for record in read.records]

    def reattach(self, layout: IpcLayout) -> None:
        self._reader = JournalReader(layout, layout.command_queue)
        self._reader.resume(offset=self.offset, serial=self.serial)


def test_a_sidecar_restart_does_not_re_execute_commands(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)
    session = manager.create(mode=SessionMode.ASSISTED, save_id="save-1")
    monitor.publish(
        Peer.GAME, session_id=session.session_id, nonce="game-1", version="0.1.0", build="42.20"
    )

    queue = CommandQueue(layout, session_id=session.session_id, clock=clock)
    command = queue.build(ActionName.MOVEMENT_MOVE_TO, idempotency_key="move-1", lease_ms=30_000)
    assert queue.submit(command).accepted
    mod = _Mod(layout)
    assert [c.command_id for c in mod.poll()] == [command.command_id]
    queue.close()

    # The sidecar dies here. A new process attaches to the same directory.
    clock.advance(2_000)
    restarted = SessionManager(layout, clock=clock, heartbeats=monitor)
    outcome = restarted.resume()
    assert outcome.resumed
    assert outcome.sidecar_nonce != session.nonce

    revived = CommandQueue(layout, session_id=session.session_id, clock=clock)
    mod.reattach(layout)
    assert mod.poll() == []

    # A consumer with no memory of its own starts past the old commands too,
    # rather than replaying the previous run from the top of the file.
    fresh_consumer = revived.command_reader_at_end()
    assert fresh_consumer.read().records == ()

    # New work still flows: skipping the past is not the same as being wedged.
    followup = revived.build(ActionName.WORLD_INSPECT, idempotency_key="look-1", lease_ms=5_000)
    assert revived.submit(followup).accepted
    assert [c.command_id for c in mod.poll()] == [followup.command_id]
    assert [r.payload["command_id"] for r in fresh_consumer.read().records] == [followup.command_id]
    revived.close()


def test_a_game_restart_invalidates_refs_and_closes_work_as_lost(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock, game_timeout_ms=1_000)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)
    session = manager.create(mode=SessionMode.AUTONOMOUS, save_id="save-1")
    monitor.publish(Peer.GAME, session_id=session.session_id, nonce="game-1", version="0.1.0")

    queue = CommandQueue(layout, session_id=session.session_id, clock=clock)
    moving = queue.build(ActionName.MOVEMENT_MOVE_TO, idempotency_key="move-1", lease_ms=30_000)
    queue.submit(moving)
    square = f"square:{session.session_id}:1200:3400:0"
    assert manager.is_ref_current(square)

    # The game process disappears: its heartbeat stops.
    clock.advance(5_000)
    assert manager.staleness().reason_code is ReasonCode.GAME_DISCONNECTED

    recovery = manager.on_game_restart(in_flight=list(queue.pending))
    lost = queue.close_in_flight(ReasonCode.GAME_DISCONNECTED)

    assert recovery.generation == 1
    assert not manager.is_ref_current(square)
    assert manager.requires_rearm
    assert lost is not None and lost.status is ActionStatus.LOST
    assert queue.cache.replay("move-1") is lost
    assert [result.status for result in recovery.lost] == [ActionStatus.LOST]
    queue.close()


def test_the_same_command_is_never_run_twice_across_a_reconnect(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    session_id = str(uuid.uuid4())
    queue = CommandQueue(layout, session_id=session_id, clock=clock)
    mod = _Mod(layout)

    eat = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    queue.submit(eat)
    delivered = mod.poll()
    publish_acks(layout, [make_success(delivered[0], seq=0, timestamp_ms=clock.now)])
    poll = queue.poll_acks()
    assert poll.results[0].status is ActionStatus.SUCCEEDED

    # The planner retries the same step after a hiccup; the queue answers with
    # the original result and the mod never sees a second command.
    clock.advance(500)
    retry = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    outcome = queue.submit(retry)
    assert outcome.replayed is poll.results[0]
    assert mod.poll() == []
    queue.close()


def test_a_save_swap_invalidates_refs_and_leaves_the_agent_disarmed(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)
    session = manager.create(mode=SessionMode.AUTONOMOUS, save_id="save-a")
    monitor.publish(Peer.GAME, session_id=session.session_id, nonce="game-1", version="0.1.0")
    item = f"item:{session.session_id}:player-main:8891:0"
    assert manager.is_ref_current(item)

    recovery = manager.note_save_id("save-b")
    assert recovery is not None
    assert recovery.reason_code is ReasonCode.SAVE_CHANGED
    assert not manager.is_ref_current(item)
    assert manager.requires_rearm

    manager.note_full_snapshot("save-b")
    assert manager.requires_rearm  # autonomy does not come back by itself
    assert not manager.is_ref_current(item)


def test_a_second_sidecar_cannot_attach_until_the_first_one_is_stale(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)
    session = manager.create(mode=SessionMode.OBSERVE)
    monitor.publish(Peer.GAME, session_id=session.session_id, nonce="game-1", version="0.1.0")

    first = SidecarLock(layout, session_id=session.session_id, clock=clock, stale_after_ms=1_000)
    assert first.acquire().acquired

    second = SidecarLock(layout, session_id=session.session_id, clock=clock, stale_after_ms=1_000)
    assert not second.acquire().acquired

    # The first sidecar is killed; nothing refreshes its lock.
    clock.advance(2_000)
    outcome = second.acquire()
    assert outcome.acquired
    assert outcome.recovered_stale


def test_the_mod_side_handshake_check_uses_the_written_document(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)
    first = manager.create(mode=SessionMode.OBSERVE)
    monitor.publish(Peer.SIDECAR, session_id=first.session_id, nonce=first.nonce, version="0.1.0")

    proposal = manager.load()
    assert proposal is not None
    accepted = evaluate_handshake(
        proposal.to_dict(),
        now_ms=clock.now,
        peer=Peer.SIDECAR,
        peer_liveness=monitor.liveness(Peer.SIDECAR, clock.now),
    )
    assert accepted.accepted

    # A leftover session.json from the previous run, replayed, is refused.
    replayed = evaluate_handshake(
        proposal.to_dict(),
        now_ms=clock.now,
        peer=Peer.SIDECAR,
        peer_liveness=monitor.liveness(Peer.SIDECAR, clock.now),
        previous=proposal,
    )
    assert not replayed.accepted
    assert replayed.reason_code is ReasonCode.STALE_SESSION
