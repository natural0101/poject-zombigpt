"""A sidecar that dies mid-session, from detection to recovery (E12-M03-T001).

The sidecar is the side that can vanish without anybody noticing: the game keeps
running, the character keeps standing there, and the last commands it was sent
are still in the queue. §3.6 tier 0 makes that decidable — each peer writes only
its own heartbeat, so a file that stops moving *is* the evidence — and this file
follows that evidence through the three places it has to land.

1. **Detection.** :meth:`HeartbeatMonitor.liveness` judges the sidecar on its own
   timeout and never gives absence the benefit of the doubt: a missing file, a
   malformed one and a file claiming to be the other peer are all "not alive".
   The mod runs the same rule in Lua against the same number, so that number is
   compared across the two languages here rather than assumed to have stayed in
   step.
2. **Stopping.** Once the mod reports ``sidecar_stale``, the deterministic guard
   says start nothing new and the action engine refuses every command before it
   is sent. Neither of them touches the player's queue on the way: losing the
   link is not evidence about who owns what is running.
3. **Recovery.** A restarted sidecar reattaches to the session already on disk
   with a *new* nonce and replays nothing, and it refuses to reattach at all
   while the game is the silent one.

The mod-side stop is Lua and is exercised by ``tests/lua/test_safety.lua``; what
this file asserts about it is only that its threshold and its refusals have not
drifted away from the Python they mirror.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_core.actions import ActionEngine, ActionRequest, AdapterRegistry
from pz_agent_core.protocol import (
    ActionName,
    ActionOwnership,
    ActionStatus,
    CommandPolicy,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.safety.reflex import ReflexGuard, ReflexSignals
from pz_agent_core.session.handshake import SessionManager, evaluate_handshake
from pz_agent_core.session.heartbeat import (
    DEFAULT_TIMEOUT_MS,
    Heartbeat,
    HeartbeatMonitor,
    Peer,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    make_action_state,
    make_observation,
    make_safety,
)
from tests.fixtures.action_doubles import (
    FakeClock as EngineClock,
)
from tests.fixtures.action_doubles import (
    FakeCommandSink,
    FakeObservationSource,
    StubAdapter,
)
from tests.fixtures.ipc_builders import IPC_SESSION_ID, FakeClock, make_layout
from tests.fixtures.safety_builders import NOW_MS

GUARD: Final = ReflexGuard()

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SAFETY_LUA: Final = (
    REPO_ROOT / "pz-mod" / "42" / "media" / "lua" / "client" / "PZAgent" / "Safety.lua"
)

#: The timeout written out again, independently of the constant under test: an
#: assertion that imports the number it is checking proves only that Python can
#: read its own source.
EXPECTED_TIMEOUT_MS: Final = 5_000


def _obs(**overrides: Any) -> Observation:
    base: dict[str, Any] = {"seq": 1, "timestamp_ms": NOW_MS}
    base.update(overrides)
    return make_observation(**base)


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


def _monitor(tmp_path: Path, clock: FakeClock) -> HeartbeatMonitor:
    return HeartbeatMonitor(make_layout(tmp_path), clock=clock)


def test_a_sidecar_that_stops_writing_is_dead_one_millisecond_past_the_timeout(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    monitor = _monitor(tmp_path, clock)
    monitor.publish(Peer.SIDECAR, session_id=IPC_SESSION_ID, nonce="s1", version="0.1.0")
    written_at = clock.now

    assert monitor.liveness(Peer.SIDECAR, written_at + EXPECTED_TIMEOUT_MS).alive
    dead = monitor.liveness(Peer.SIDECAR, written_at + EXPECTED_TIMEOUT_MS + 1)
    assert not dead.alive
    assert "silent for" in dead.detail
    assert dead.heartbeat is not None, "the evidence travels with the verdict"


def test_the_sidecar_timeout_is_the_one_the_mod_uses() -> None:
    """Two implementations of one rule, so the number is compared, not trusted.

    The mod cannot ask the sidecar whether the sidecar is alive, so it applies
    §8.4's threshold itself. A change on one side that is not made on the other
    is a window in which one of them is still obeying commands from a process
    the other has already buried.
    """
    assert DEFAULT_TIMEOUT_MS == EXPECTED_TIMEOUT_MS
    source = SAFETY_LUA.read_text(encoding="utf-8")
    match = re.search(r"^Safety\.SIDECAR_MAX_AGE_MS\s*=\s*([0-9 */+]+)$", source, re.MULTILINE)
    assert match is not None, f"{SAFETY_LUA.name} no longer declares SIDECAR_MAX_AGE_MS"
    factors = [int(part.strip()) for part in match.group(1).split("*")]
    lua_timeout = 1
    for factor in factors:
        lua_timeout *= factor
    assert lua_timeout == EXPECTED_TIMEOUT_MS


@pytest.mark.parametrize("function", ["Safety.arm", "Safety.mayStart"])
def test_the_mod_refuses_to_start_anything_while_the_sidecar_is_stale(function: str) -> None:
    """The mod's two gates both consult the heartbeat and both name the code."""
    body = _lua_function(SAFETY_LUA.read_text(encoding="utf-8"), function)
    assert "Safety.sidecarStale(state, nowMs)" in body, (
        f"{function} no longer checks whether the sidecar is still there"
    )
    assert "STALE_SESSION" in body, f"{function} refuses without saying it is a stale session"


def test_the_mod_starts_out_believing_the_sidecar_is_gone() -> None:
    """Absence of a heartbeat is staleness, not a grace period."""
    source = SAFETY_LUA.read_text(encoding="utf-8")
    assert "sidecar_last_seen_ms = nil" in _lua_function(source, "Safety.newState")
    stale = _lua_function(source, "Safety.sidecarStale")
    assert "if state.sidecar_last_seen_ms == nil then" in stale
    assert "return true" in stale


def _lua_function(source: str, name: str) -> str:
    """The body of one Lua function, from its ``function`` line to the next."""
    start = source.find(f"function {name}(")
    assert start >= 0, f"{name} is not defined in {SAFETY_LUA.name}"
    end = source.find("\nfunction ", start + 1)
    return source[start:] if end < 0 else source[start:end]


def test_a_heartbeat_from_the_other_peer_is_not_evidence_this_one_is_alive(
    tmp_path: Path,
) -> None:
    """A shared directory must not let the game's heartbeat vouch for us."""
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock)
    impostor = Heartbeat(
        peer=Peer.GAME,
        session_id=IPC_SESSION_ID,
        nonce="g1",
        seq=0,
        timestamp_ms=clock.now,
        version="0.1.0",
    )
    layout.sidecar_heartbeat.write_text(json.dumps(impostor.to_dict()), encoding="utf-8")

    liveness = monitor.liveness(Peer.SIDECAR, clock.now)

    assert not liveness.alive
    assert liveness.missing
    assert "claims to belong to game" in liveness.detail


def test_a_handshake_is_refused_while_the_sidecars_heartbeat_is_dead(tmp_path: Path) -> None:
    """The mod's acceptance rule, expressed once so both sides agree on it.

    ``evaluate_handshake`` is what the mod runs against a ``session.json`` a
    sidecar left behind: a proposal from a process that is no longer writing a
    heartbeat is refused, however well-formed it is.
    """
    clock = FakeClock()
    monitor = _monitor(tmp_path, clock)
    manager = SessionManager(make_layout(tmp_path), clock=clock, heartbeats=monitor)
    proposal = manager.create(mode=SessionMode.OBSERVE, nonce="s1").to_dict()
    monitor.publish(Peer.SIDECAR, session_id=IPC_SESSION_ID, nonce="s1", version="0.1.0")
    written_at = clock.now

    alive = evaluate_handshake(
        proposal,
        now_ms=written_at,
        peer=Peer.SIDECAR,
        peer_liveness=monitor.liveness(Peer.SIDECAR, written_at),
    )
    assert alive.accepted, alive.detail

    clock.advance(EXPECTED_TIMEOUT_MS + 1)
    refused = evaluate_handshake(
        proposal,
        now_ms=clock.now,
        peer=Peer.SIDECAR,
        peer_liveness=monitor.liveness(Peer.SIDECAR, clock.now),
    )
    assert not refused.accepted
    assert refused.reason_code is ReasonCode.STALE_SESSION
    assert refused.session is None, "a refused handshake hands back no session to use"


# ---------------------------------------------------------------------------
# stopping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("world", "signals"),
    [
        ({"safety": make_safety(sidecar_stale=True)}, {}),
        ({}, {"sidecar_alive": False}),
    ],
    ids=["reported_by_the_mod", "observed_by_this_process"],
)
def test_a_dead_link_makes_the_guard_start_nothing_new(
    world: dict[str, Any], signals: dict[str, Any]
) -> None:
    """Either side noticing is enough, and neither touches the queue.

    A returned event means "start no new task" — that is the guard's contract —
    so the assertion that matters is that an event comes back at all, and that
    it claims no authority over an action queue nobody has said anything about.
    """
    current = _obs(
        action=make_action_state(ownership=ActionOwnership.MOD, busy=True, type="consume.eat"),
        **world,
    )
    events = GUARD.evaluate(_obs(), current, ReflexSignals(now_ms=NOW_MS, **signals))

    stale = [event for event in events if event.reason_code is ReasonCode.STALE_SESSION]
    assert len(stale) == 1, f"expected a stale-session event, got {[e.reason_code for e in events]}"
    assert not stale[0].cancels_mod_owned_queue
    assert not stale[0].cancels_running_action


def test_a_live_link_produces_no_event_at_all() -> None:
    """The control the test above needs: the guard is not simply always noisy."""
    assert GUARD.evaluate(_obs(), _obs(seq=2), ReflexSignals(now_ms=NOW_MS)) == []


def test_no_command_is_sent_once_the_mod_reports_the_sidecar_stale() -> None:
    """The engine refuses before the send, which is the only useful moment.

    Refusing afterwards would leave a command with a mod that has already
    decided to stop obeying this process, and nothing would ever close it.
    """
    clock = EngineClock()
    sink = FakeCommandSink(clock)
    source = FakeObservationSource(clock)
    source.repeat(_obs(safety=make_safety(sidecar_stale=True)))
    registry = AdapterRegistry()
    registry.register(StubAdapter(name=ActionName.MOVEMENT_MOVE_TO, verify_after=1))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        capability_check=lambda _: True,
    )

    result = engine.execute(
        ActionRequest(
            action=ActionName.MOVEMENT_MOVE_TO,
            session_id=DEFAULT_SESSION,
            idempotency_key="move-1",
            args={"target": {"x": 1, "y": 2, "z": 0}},
            policy=CommandPolicy(allow_interrupt=True, max_retries=0),
        )
    )

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code is ReasonCode.STALE_SESSION
    assert sink.sent == []


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------


def test_a_restarted_sidecar_reattaches_with_a_new_nonce_and_the_same_session(
    tmp_path: Path,
) -> None:
    """§3.12: the session survives, the process identity does not.

    The nonce is how the mod tells a new sidecar from a replayed ``session.json``,
    so a resume that reused it would be indistinguishable from the corpse of the
    previous run reattaching.
    """
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock)
    died = SessionManager(layout, clock=clock, heartbeats=monitor)
    original = died.create(mode=SessionMode.OBSERVE, nonce="s1")
    monitor.publish(Peer.GAME, session_id=original.session_id, nonce="g1", version="0.1.0")

    restarted = SessionManager(layout, clock=clock, heartbeats=monitor)
    outcome = restarted.resume()

    assert outcome.resumed, outcome.detail
    assert outcome.session is not None
    assert outcome.session.session_id == original.session_id
    assert outcome.session.generation == original.generation
    assert outcome.sidecar_nonce is not None
    assert outcome.sidecar_nonce != original.nonce
    published = monitor.read(Peer.SIDECAR)
    assert published is not None
    assert published.nonce == outcome.sidecar_nonce


def test_a_restarted_sidecar_does_not_reattach_to_a_game_that_has_gone_quiet(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)
    session = manager.create(mode=SessionMode.OBSERVE, nonce="s1")
    monitor.publish(Peer.GAME, session_id=session.session_id, nonce="g1", version="0.1.0")
    clock.advance(EXPECTED_TIMEOUT_MS + 1)

    outcome = SessionManager(layout, clock=clock, heartbeats=monitor).resume()

    assert not outcome.resumed
    assert outcome.reason_code is ReasonCode.GAME_DISCONNECTED
    assert monitor.read(Peer.SIDECAR) is None, "a refused resume publishes no heartbeat"
