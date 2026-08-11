"""Heartbeats: two peers, two clocks, and no benefit of the doubt."""

from __future__ import annotations

from pathlib import Path

import pytest

from pz_agent_core.protocol import DangerLevel, SessionMode
from pz_agent_core.session.heartbeat import (
    Heartbeat,
    HeartbeatError,
    HeartbeatMonitor,
    Peer,
    new_nonce,
)
from tests.fixtures.ipc_builders import IPC_SESSION_ID, FakeClock, make_layout


def _heartbeat(peer: Peer = Peer.GAME, **overrides: object) -> Heartbeat:
    base: dict[str, object] = {
        "peer": peer,
        "session_id": IPC_SESSION_ID,
        "nonce": "abc123",
        "seq": 0,
        "timestamp_ms": 1_000,
        "version": "0.1.0",
    }
    base.update(overrides)
    return Heartbeat(**base)  # type: ignore[arg-type]


def test_round_trip_preserves_the_tier_zero_payload() -> None:
    original = _heartbeat(
        build="42.20",
        player_present=True,
        armed=False,
        mode=SessionMode.OBSERVE,
        danger_level=DangerLevel.LOW,
        active_action_id="act-7",
    )
    assert Heartbeat.from_dict(original.to_dict()) == original


def test_absent_tier_zero_fields_are_omitted_not_invented() -> None:
    payload = _heartbeat(peer=Peer.SIDECAR).to_dict()
    assert "armed" not in payload
    assert "danger_level" not in payload
    assert Heartbeat.from_dict(payload).armed is None


def test_staleness_is_measured_against_the_timeout() -> None:
    heartbeat = _heartbeat(timestamp_ms=1_000)
    assert not heartbeat.is_stale(1_000 + 5_000, 5_000)
    assert heartbeat.is_stale(1_000 + 5_001, 5_000)


def test_a_future_timestamp_does_not_make_a_heartbeat_immortal() -> None:
    heartbeat = _heartbeat(timestamp_ms=10_000)
    assert heartbeat.age_ms(5_000) == 0
    assert heartbeat.is_stale(20_000, 5_000)


def test_a_zero_timeout_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        _heartbeat().is_stale(2_000, 0)


def test_malformed_documents_are_rejected() -> None:
    with pytest.raises(HeartbeatError, match="peer"):
        Heartbeat.from_dict({"peer": "nobody"})
    with pytest.raises(HeartbeatError, match="seq"):
        Heartbeat.from_dict(
            {
                "peer": "game",
                "session_id": IPC_SESSION_ID,
                "nonce": "n",
                "seq": "one",
                "timestamp_ms": 1,
                "version": "0.1.0",
                "protocol_version": "1.0",
            }
        )
    with pytest.raises(HeartbeatError, match="nonce"):
        _heartbeat(nonce="")


def test_nonces_differ_between_calls() -> None:
    assert new_nonce() != new_nonce()


def test_publish_and_read_round_trip(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    monitor = HeartbeatMonitor(layout, clock=clock)
    first = monitor.publish(
        Peer.GAME, session_id=IPC_SESSION_ID, nonce="n1", version="0.1.0", armed=True
    )
    second = monitor.publish(Peer.GAME, session_id=IPC_SESSION_ID, nonce="n1", version="0.1.0")

    assert (first.seq, second.seq) == (0, 1)
    stored = monitor.read(Peer.GAME)
    assert stored is not None
    assert stored.seq == 1
    assert layout.game_heartbeat.exists()


def test_the_two_heartbeats_go_stale_independently(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    monitor = HeartbeatMonitor(
        layout, clock=clock, game_timeout_ms=1_000, sidecar_timeout_ms=10_000
    )
    monitor.publish(Peer.GAME, session_id=IPC_SESSION_ID, nonce="g", version="0.1.0")
    monitor.publish(Peer.SIDECAR, session_id=IPC_SESSION_ID, nonce="s", version="0.1.0")

    clock.advance(2_000)
    assert not monitor.liveness(Peer.GAME).alive
    assert monitor.liveness(Peer.SIDECAR).alive


def test_a_missing_heartbeat_is_never_treated_as_alive(tmp_path: Path) -> None:
    monitor = HeartbeatMonitor(make_layout(tmp_path), clock=FakeClock())
    liveness = monitor.liveness(Peer.GAME)
    assert not liveness.alive
    assert liveness.missing
    assert "no readable heartbeat" in liveness.detail


def test_a_corrupt_heartbeat_file_is_not_alive(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.game_heartbeat.write_text("{ truncated", encoding="utf-8")
    monitor = HeartbeatMonitor(layout, clock=FakeClock())
    assert monitor.read(Peer.GAME) is None
    liveness = monitor.liveness(Peer.GAME)
    assert not liveness.alive
    # A corrupt file and an absent one are both "not alive" but not the same
    # problem, and whoever has to diagnose a silent game needs to know which.
    assert "malformed JSON" in liveness.detail


def test_a_heartbeat_naming_the_wrong_peer_is_refused(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=FakeClock())
    monitor.publish(Peer.SIDECAR, session_id=IPC_SESSION_ID, nonce="s", version="0.1.0")
    layout.game_heartbeat.write_text(
        layout.sidecar_heartbeat.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert monitor.read(Peer.GAME) is None
    assert "belong to sidecar" in monitor.liveness(Peer.GAME).detail


def test_liveness_reports_the_silence_it_measured(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    clock = FakeClock()
    monitor = HeartbeatMonitor(layout, clock=clock, game_timeout_ms=1_000)
    monitor.publish(Peer.GAME, session_id=IPC_SESSION_ID, nonce="g", version="0.1.0")
    clock.advance(3_000)
    liveness = monitor.liveness(Peer.GAME)
    assert "silent for 3000 ms" in liveness.detail
    assert liveness.heartbeat is not None


def test_a_peer_whose_clock_runs_ahead_cannot_look_alive_after_it_stops(tmp_path: Path) -> None:
    """A heartbeat stamped in the future is not evidence about the present.

    Each peer stamps its own clock. When the game's runs an hour ahead, every
    reading of its last heartbeat clamps to an age of zero — so the file a
    stopped game left behind reads "fresh" for as long as the skew lasts, which
    is exactly what the liveness window exists to refuse. The snapshot reader
    already refuses a document stamped past its window into the future; this is
    the same rule one file over.
    """
    layout = make_layout(tmp_path)
    clock = FakeClock()
    ahead = FakeClock(now=clock.now + 3_600_000)
    HeartbeatMonitor(layout, clock=ahead).publish(
        Peer.GAME, session_id=IPC_SESSION_ID, nonce="g", version="0.1.0"
    )
    monitor = HeartbeatMonitor(layout, clock=clock, game_timeout_ms=1_000)

    clock.advance(60_000)
    liveness = monitor.liveness(Peer.GAME)

    assert not liveness.alive
    assert "future" in liveness.detail
    # The evidence still travels with the verdict: whoever has to diagnose this
    # needs the document that disagrees with their clock, not its absence.
    assert liveness.heartbeat is not None


def test_ordinary_clock_skew_is_tolerated(tmp_path: Path) -> None:
    """A second or two ahead is two machines' clocks, not a stopped peer."""
    layout = make_layout(tmp_path)
    clock = FakeClock()
    ahead = FakeClock(now=clock.now + 1_000)
    HeartbeatMonitor(layout, clock=ahead).publish(
        Peer.GAME, session_id=IPC_SESSION_ID, nonce="g", version="0.1.0"
    )

    assert HeartbeatMonitor(layout, clock=clock).liveness(Peer.GAME).alive


def test_peers_are_each_other_s_other() -> None:
    assert Peer.GAME.other is Peer.SIDECAR
    assert Peer.SIDECAR.other is Peer.GAME
