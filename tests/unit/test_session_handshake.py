"""Handshake acceptance (§3.3) and the recovery rules (§3.12)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import ActionStatus, ReasonCode, SessionMode
from pz_agent_core.session.handshake import (
    HandshakeResult,
    RecoveryKind,
    SessionDescriptor,
    SessionError,
    SessionManager,
    evaluate_handshake,
)
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer, PeerLiveness
from tests.fixtures.ipc_builders import IPC_SESSION_ID, FakeClock, make_command, make_layout

NOW = 1_700_000_000_000


def _descriptor(**overrides: object) -> SessionDescriptor:
    base: dict[str, object] = {
        "session_id": IPC_SESSION_ID,
        "nonce": "nonce-a",
        "created_at_ms": NOW,
        "mode": SessionMode.OBSERVE,
    }
    base.update(overrides)
    return SessionDescriptor(**base)  # type: ignore[arg-type]


def _alive(peer: Peer = Peer.GAME) -> PeerLiveness:
    return PeerLiveness(peer, None, alive=True, detail="fresh")


def _dead(peer: Peer = Peer.GAME) -> PeerLiveness:
    return PeerLiveness(peer, None, alive=False, detail="silent for 9000 ms")


def _manager(tmp_path: Path, clock: FakeClock, **kwargs: object) -> SessionManager:
    return SessionManager(make_layout(tmp_path), clock=clock, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------


def test_descriptor_round_trips(tmp_path: Path) -> None:
    original = _descriptor(save_id="save-1", generation=2)
    assert SessionDescriptor.from_dict(original.to_dict()) == original


def test_a_non_uuid_session_id_is_refused() -> None:
    with pytest.raises(SessionError, match="UUID"):
        _descriptor(session_id="not-a-uuid")


def test_an_absurd_observation_rate_is_refused() -> None:
    with pytest.raises(SessionError, match="requested_observation_hz"):
        _descriptor(requested_observation_hz=1_000)


def test_an_unknown_mode_is_refused() -> None:
    payload = _descriptor().to_dict()
    payload["mode"] = "GODMODE"
    with pytest.raises(SessionError, match="mode"):
        SessionDescriptor.from_dict(payload)


# --------------------------------------------------------------------------
# acceptance rules
# --------------------------------------------------------------------------


def _evaluate(payload: dict[str, object], **kwargs: object) -> HandshakeResult:
    defaults: dict[str, object] = {
        "now_ms": NOW,
        "peer": Peer.GAME,
        "peer_liveness": _alive(),
    }
    defaults.update(kwargs)
    return evaluate_handshake(payload, **defaults)  # type: ignore[arg-type]


def test_a_well_formed_fresh_session_is_accepted() -> None:
    result = _evaluate(_descriptor().to_dict())
    assert result.accepted
    assert result.reason_code is None
    assert result.session is not None


def test_a_malformed_document_is_refused_as_invalid() -> None:
    result = _evaluate({"session_id": "nope"})
    assert not result.accepted
    assert result.reason_code is ReasonCode.INVALID_ARGUMENT


def test_an_incompatible_protocol_major_is_refused() -> None:
    payload = _descriptor().to_dict()
    payload["protocol_version"] = "2.0"
    result = _evaluate(payload)
    assert not result.accepted
    assert result.reason_code is ReasonCode.INVALID_ARGUMENT
    assert "not compatible" in result.detail


def test_a_compatible_minor_version_is_accepted() -> None:
    payload = _descriptor().to_dict()
    payload["protocol_version"] = "1.7"
    assert _evaluate(payload).accepted


def test_a_stale_timestamp_is_refused() -> None:
    result = _evaluate(_descriptor(created_at_ms=NOW - 120_000).to_dict())
    assert not result.accepted
    assert result.reason_code is ReasonCode.STALE_SESSION


def test_a_timestamp_far_in_the_future_is_refused() -> None:
    result = _evaluate(_descriptor(created_at_ms=NOW + 60_000).to_dict())
    assert not result.accepted
    assert "in the future" in result.detail


def test_a_small_clock_skew_is_tolerated() -> None:
    assert _evaluate(_descriptor(created_at_ms=NOW + 1_000).to_dict()).accepted


def test_a_reused_nonce_is_refused() -> None:
    previous = _descriptor(nonce="nonce-a")
    result = _evaluate(_descriptor(nonce="nonce-a").to_dict(), previous=previous)
    assert not result.accepted
    assert result.reason_code is ReasonCode.STALE_SESSION
    assert "nonce" in result.detail


def test_a_new_nonce_against_the_same_previous_session_is_accepted() -> None:
    previous = _descriptor(nonce="nonce-a")
    assert _evaluate(_descriptor(nonce="nonce-b").to_dict(), previous=previous).accepted


def test_a_dead_game_peer_is_refused_as_disconnected() -> None:
    result = _evaluate(_descriptor().to_dict(), peer_liveness=_dead())
    assert not result.accepted
    assert result.reason_code is ReasonCode.GAME_DISCONNECTED
    assert "silent for 9000 ms" in result.detail


def test_a_dead_sidecar_peer_is_refused_as_stale() -> None:
    result = _evaluate(
        _descriptor().to_dict(), peer=Peer.SIDECAR, peer_liveness=_dead(Peer.SIDECAR)
    )
    assert not result.accepted
    assert result.reason_code is ReasonCode.STALE_SESSION


def test_missing_liveness_evidence_is_not_a_pass() -> None:
    result = _evaluate(_descriptor().to_dict(), peer_liveness=None)
    assert not result.accepted
    assert "no peer liveness evidence" in result.detail


# --------------------------------------------------------------------------
# the manager
# --------------------------------------------------------------------------


def test_create_writes_the_session_document(tmp_path: Path) -> None:
    clock = FakeClock()
    manager = _manager(tmp_path, clock)
    session = manager.create(mode=SessionMode.OBSERVE)

    assert manager.layout.session.exists()
    assert manager.load() == session
    assert session.created_at_ms == clock.now
    assert manager.generation == 0
    assert not manager.requires_rearm


def test_creating_a_session_with_the_previous_nonce_is_refused(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeClock())
    first = manager.create(mode=SessionMode.OBSERVE)
    with pytest.raises(SessionError, match="nonce"):
        manager.create(mode=SessionMode.OBSERVE, nonce=first.nonce)


def test_a_generated_nonce_differs_from_the_previous_session(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeClock())
    first = manager.create(mode=SessionMode.OBSERVE)
    second = manager.create(mode=SessionMode.ASSISTED)
    assert first.nonce != second.nonce
    assert first.session_id != second.session_id


def test_an_unreadable_session_document_loads_as_nothing(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeClock())
    manager.layout.session.write_text("{ truncated", encoding="utf-8")
    assert manager.load() is None


def test_staleness_needs_a_session_a_live_game_and_a_matching_id(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock, game_timeout_ms=1_000)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)

    assert manager.staleness().stale  # no session.json yet
    session = manager.create(mode=SessionMode.OBSERVE)

    stale = manager.staleness()
    assert stale.stale
    assert stale.reason_code is ReasonCode.GAME_DISCONNECTED

    monitor.publish(
        Peer.GAME, session_id=session.session_id, nonce="g", version="0.1.0", build="42.20"
    )
    assert not manager.staleness().stale

    clock.advance(2_000)
    assert manager.staleness().reason_code is ReasonCode.GAME_DISCONNECTED

    monitor.publish(Peer.GAME, session_id=str(uuid.uuid4()), nonce="g2", version="0.1.0")
    other = manager.staleness()
    assert other.stale
    assert other.reason_code is ReasonCode.STALE_SESSION


def test_resume_mints_a_new_sidecar_nonce(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)
    session = manager.create(mode=SessionMode.ASSISTED)
    monitor.publish(Peer.GAME, session_id=session.session_id, nonce="g", version="0.1.0")

    restarted = SessionManager(layout, clock=clock, heartbeats=monitor)
    outcome = restarted.resume()

    assert outcome.resumed
    assert outcome.session is not None
    assert outcome.session.session_id == session.session_id
    assert outcome.sidecar_nonce is not None
    assert outcome.sidecar_nonce != session.nonce
    published = monitor.read(Peer.SIDECAR)
    assert published is not None
    assert published.nonce == outcome.sidecar_nonce


def test_resume_refuses_when_the_game_is_gone(tmp_path: Path) -> None:
    clock = FakeClock()
    manager = _manager(tmp_path, clock)
    manager.create(mode=SessionMode.OBSERVE)
    outcome = manager.resume()
    assert not outcome.resumed
    assert outcome.reason_code is ReasonCode.GAME_DISCONNECTED


def test_resume_refuses_a_session_the_game_is_not_on(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)
    manager.create(mode=SessionMode.OBSERVE)
    monitor.publish(Peer.GAME, session_id=str(uuid.uuid4()), nonce="g", version="0.1.0")

    outcome = manager.resume()
    assert not outcome.resumed
    assert outcome.reason_code is ReasonCode.STALE_SESSION


def test_resume_without_a_session_document_fails(tmp_path: Path) -> None:
    outcome = _manager(tmp_path, FakeClock()).resume()
    assert not outcome.resumed
    assert outcome.reason_code is ReasonCode.STALE_SESSION


def test_a_game_restart_bumps_the_generation_and_loses_in_flight_work(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    manager = _manager(tmp_path, clock)
    session = manager.create(mode=SessionMode.ASSISTED)
    running = make_command(session_id=session.session_id)

    outcome = manager.on_game_restart(in_flight=[running])

    assert outcome.kind is RecoveryKind.GAME_RESTART
    assert outcome.generation == 1
    assert manager.generation == 1
    assert outcome.refs_invalidated
    assert outcome.requires_rearm
    assert manager.requires_rearm
    assert not manager.refs_valid
    assert len(outcome.lost) == 1
    lost = outcome.lost[0]
    assert lost.status is ActionStatus.LOST
    assert lost.reason_code is ReasonCode.GAME_DISCONNECTED
    assert lost.evidence == {}
    # The bump is persisted, so a later reader sees the same generation.
    reloaded = manager.load()
    assert reloaded is not None and reloaded.generation == 1


def test_a_game_restart_without_a_session_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SessionError, match="without a session"):
        _manager(tmp_path, FakeClock()).on_game_restart()


def test_the_first_observed_save_is_not_a_recovery(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeClock())
    manager.create(mode=SessionMode.ASSISTED)
    assert manager.note_save_id("save-1") is None
    assert manager.refs_valid
    assert not manager.requires_rearm


def test_the_same_save_id_changes_nothing(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeClock())
    manager.create(mode=SessionMode.ASSISTED, save_id="save-1")
    assert manager.note_save_id("save-1") is None
    assert manager.generation == 0


def test_a_changed_save_id_invalidates_refs_and_forces_a_rearm(tmp_path: Path) -> None:
    clock = FakeClock()
    manager = _manager(tmp_path, clock)
    session = manager.create(mode=SessionMode.AUTONOMOUS, save_id="save-1")
    running = make_command(session_id=session.session_id)

    outcome = manager.note_save_id("save-2", in_flight=[running])

    assert outcome is not None
    assert outcome.kind is RecoveryKind.SAVE_CHANGED
    assert outcome.reason_code is ReasonCode.SAVE_CHANGED
    assert outcome.refs_invalidated
    assert outcome.requires_rearm
    assert manager.requires_rearm
    assert not manager.refs_valid
    assert outcome.lost[0].reason_code is ReasonCode.SAVE_CHANGED
    assert outcome.lost[0].status is ActionStatus.LOST


def test_a_save_change_never_silently_rearms(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeClock())
    manager.create(mode=SessionMode.AUTONOMOUS, save_id="save-1")
    manager.note_save_id("save-2")
    # Re-observing the new save restores references but not authority.
    assert manager.note_full_snapshot("save-2")
    assert manager.refs_valid
    assert manager.requires_rearm
    manager.mark_rearmed()
    assert not manager.requires_rearm


def test_a_snapshot_of_another_save_revalidates_nothing(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeClock())
    manager.create(mode=SessionMode.ASSISTED, save_id="save-1")
    manager.note_save_id("save-2")
    assert not manager.note_full_snapshot("save-1")
    assert not manager.refs_valid


def test_refs_are_scoped_to_the_session_and_the_generation(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeClock())
    session = manager.create(mode=SessionMode.ASSISTED, save_id="save-1")
    sid = session.session_id
    item = f"item:{sid}:player-main:8891:0"
    square = f"square:{sid}:1200:3400:0"

    assert manager.is_ref_current(item)
    assert manager.is_ref_current(square)
    assert not manager.is_ref_current(f"item:{uuid.uuid4()}:player-main:8891:0")
    assert not manager.is_ref_current("not a ref at all")
    assert not manager.is_ref_current(f"item:{sid}:player-main:8891")

    manager.on_game_restart()
    assert not manager.is_ref_current(item)
    assert not manager.is_ref_current(square)

    manager.note_full_snapshot("save-1")
    # The generation moved, so the old item ref stays invalid while a square
    # observed under the new generation is usable again.
    assert not manager.is_ref_current(item)
    assert manager.is_ref_current(f"item:{sid}:player-main:8891:1")
    assert manager.is_ref_current(square)


def test_refs_are_unusable_before_a_session_exists(tmp_path: Path) -> None:
    manager = SessionManager(IpcLayout(tmp_path), clock=FakeClock())
    assert not manager.is_ref_current(f"square:{IPC_SESSION_ID}:1:2:0")


def test_adopting_a_session_makes_it_current(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeClock())
    descriptor = _descriptor()
    manager.adopt(descriptor)
    assert manager.session == descriptor
    assert manager.refs_valid
