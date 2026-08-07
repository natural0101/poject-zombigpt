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

The other side can vanish too, and the last three sections are about that:

4. **The game dying mid-action.** Whatever the mod last said, an action nobody
   watched finish is never ``succeeded``. The engine's own loop ends it as a
   timeout or a missing postcondition; the session layer, which is the half that
   knows about heartbeats, closes it ``lost`` under ``GAME_DISCONNECTED`` — and
   caches that, so a redelivery cannot start the work a second time.
5. **A save that changed under us.** §3.12: references are invalidated, the
   generation moves, and the agent stays disarmed until a *person* re-arms it.
   Re-observing the world restores references; it never restores authority.
6. **Re-arming afterwards.** §7.7's backup is bound to the save the mod reports,
   so the backup that covered the previous world does not cover this one.

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
from pz_agent_core.ipc.queue import CommandQueue
from pz_agent_core.policy.autonomy import AutonomyGate, AutonomyOutcome, BackupEvidence
from pz_agent_core.policy.selection import CapabilitySnapshot
from pz_agent_core.protocol import (
    ActionName,
    ActionOwnership,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.safety.reflex import ReflexGuard, ReflexSignals
from pz_agent_core.session.handshake import RecoveryKind, SessionManager, evaluate_handshake
from pz_agent_core.session.heartbeat import (
    DEFAULT_TIMEOUT_MS,
    Heartbeat,
    HeartbeatMonitor,
    Peer,
)
from tests.fixtures import (
    DEFAULT_SAVE,
    DEFAULT_SESSION,
    make_action_state,
    make_game,
    make_observation,
    make_safety,
)
from tests.fixtures.action_doubles import (
    AckPlan,
    FakeCommandSink,
    FakeObservationSource,
    StubAdapter,
)
from tests.fixtures.action_doubles import (
    FakeClock as EngineClock,
)
from tests.fixtures.autonomy_worlds import autonomous_observation
from tests.fixtures.ipc_builders import IPC_SESSION_ID, FakeClock, make_command, make_layout
from tests.fixtures.policy_items import food_item, hungry_player, inventory
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


def test_a_restarted_sidecar_does_not_re_execute_the_previous_runs_commands(
    tmp_path: Path,
) -> None:
    """§3.12, and the reason a resume is safe at all.

    Every command of the dead run is still sitting in the queue journal. A
    consumer that started at the top of the file would eat the sandwich again,
    walk the route again and swing the axe again — none of which can be undone,
    which is why the restart's reader is positioned at the end instead.
    """
    layout = make_layout(tmp_path)
    died = CommandQueue(layout=layout, session_id=IPC_SESSION_ID, clock=FakeClock())
    try:
        for index in range(3):
            died.submit(
                died.build(ActionName.CONSUME_EAT, idempotency_key=f"eat-{index}", lease_ms=30_000)
            )
    finally:
        died.close()

    restarted = CommandQueue(layout=layout, session_id=IPC_SESSION_ID, clock=FakeClock())
    try:
        reader = restarted.command_reader_at_end()
        assert reader.read().payloads == (), "the previous run's commands must not be replayed"

        restarted.submit(
            restarted.build(ActionName.CONSUME_EAT, idempotency_key="eat-new", lease_ms=30_000)
        )
        keys = [payload["idempotency_key"] for payload in reader.read().payloads]
    finally:
        restarted.close()

    assert keys == ["eat-new"], "and everything written after the restart must still arrive"


# ---------------------------------------------------------------------------
# the game dying mid-action (E12-M03-T002)
# ---------------------------------------------------------------------------


def _in_flight_engine(
    *,
    acks: tuple[AckPlan, ...] = (),
    observations: tuple[Observation, ...] = (),
    max_retries: int = 0,
) -> tuple[ActionResult, FakeCommandSink]:
    """Ship one command, then let the game go silent.

    *observations* is the whole supply: once it is exhausted the source returns
    None forever, which is exactly what the engine sees when the game stops
    writing. The engine is given no panic switch and no stale flag, so nothing
    but the silence itself can end the action.
    """
    clock = EngineClock()
    sink = FakeCommandSink(clock, acks=acks)
    source = FakeObservationSource(clock)
    source.push(*observations)
    registry = AdapterRegistry()
    registry.register(StubAdapter(name=ActionName.MOVEMENT_MOVE_TO, verify_after=None))
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
            policy=CommandPolicy(allow_interrupt=True, max_retries=max_retries),
        )
    )
    return result, sink


def test_nothing_is_sent_to_a_game_that_is_not_observing_at_all() -> None:
    """No fresh observation means no precondition was checked against anything.

    ``GAME_DISCONNECTED`` before the send, rather than a hopeful dispatch: a
    command handed to a mod that is not producing observations could never be
    verified, so the only honest outcomes left would be a lie or a timeout.
    """
    clock = EngineClock()
    sink = FakeCommandSink(clock)
    registry = AdapterRegistry()
    registry.register(StubAdapter(name=ActionName.MOVEMENT_MOVE_TO, verify_after=1))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=FakeObservationSource(clock),
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
    assert result.reason_code is ReasonCode.GAME_DISCONNECTED
    assert sink.sent == []


def test_an_action_the_game_stopped_reporting_on_never_succeeds() -> None:
    """The command shipped, then the world went quiet. It is not a success.

    ``ACTION_TIMEOUT`` rather than ``GAME_DISCONNECTED``: the engine is given no
    heartbeat, so all it can honestly say is that the postcondition never
    appeared within the budget. Naming the *cause* is the session layer's job
    (below), and the invariant both share is that neither invents an outcome.
    """
    result, sink = _in_flight_engine(observations=(_obs(seq=1),))

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.ACTION_TIMEOUT
    assert len(sink.sent) == 1
    assert [reason for _, reason in sink.cancelled] == [ReasonCode.ACTION_TIMEOUT], (
        "the mod must be told to abandon a command the engine has given up on"
    )


def test_a_success_ack_from_a_game_that_then_went_quiet_is_not_a_success() -> None:
    """The sharpest form of the rule: the mod said it worked, and nobody saw it.

    An engine that trusted the ack would report ``succeeded`` here — a claim
    about a world that stopped being observable one tick after the claim was
    made. What comes back instead names exactly what went wrong.
    """
    clock = EngineClock()
    sink = FakeCommandSink(clock, acks=[AckPlan(status=ActionStatus.SUCCEEDED, after_polls=1)])
    source = FakeObservationSource(clock)
    source.push(_obs(seq=1))
    registry = AdapterRegistry()
    registry.register(StubAdapter(name=ActionName.MOVEMENT_MOVE_TO, verify_after=None))
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

    assert result.status is not ActionStatus.SUCCEEDED
    assert result.reason_code is ReasonCode.POSTCONDITION_FAILED
    assert result.evidence.get("observation_seq") is not None, (
        "a refusal carries what was observed, never postcondition evidence"
    )


def test_the_engine_does_not_keep_trying_against_a_game_that_is_gone() -> None:
    """Bounded, and the retry never reaches a mod that has stopped reporting.

    ``ACTION_TIMEOUT`` is retryable — right when the game is merely slow — so a
    second attempt is begun. It gets no further than
    :meth:`ActionEngine._fresh_observation`, because a precondition cannot be
    checked against a world nobody is describing, and the caller is left holding
    ``GAME_DISCONNECTED`` rather than a second timeout.
    """
    result, sink = _in_flight_engine(observations=(_obs(seq=1),), max_retries=1)

    assert result.status is not ActionStatus.SUCCEEDED
    assert result.attempt == 2, "the retry budget was spent"
    assert result.reason_code is ReasonCode.GAME_DISCONNECTED
    assert len(sink.sent) == 1, (
        "only the first attempt reached the mod; the retry stopped at the "
        f"missing observation, but {len(sink.sent)} commands went out"
    )


def _live_session(tmp_path: Path) -> tuple[SessionManager, HeartbeatMonitor, FakeClock]:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    monitor = HeartbeatMonitor(layout, clock=clock)
    manager = SessionManager(layout, clock=clock, heartbeats=monitor)
    session = manager.create(mode=SessionMode.ASSISTED, nonce="s1", session_id=IPC_SESSION_ID)
    monitor.publish(Peer.GAME, session_id=session.session_id, nonce="g1", version="0.1.0")
    return manager, monitor, clock


def test_a_silent_game_is_reported_as_disconnected_rather_than_as_a_bad_session(
    tmp_path: Path,
) -> None:
    """Which of the two codes comes back decides what the user is told to do."""
    manager, _, clock = _live_session(tmp_path)

    assert not manager.staleness().stale
    clock.advance(EXPECTED_TIMEOUT_MS + 1)
    stale = manager.staleness()

    assert stale.stale
    assert stale.reason_code is ReasonCode.GAME_DISCONNECTED


def test_work_in_flight_when_the_game_went_is_closed_lost_and_never_succeeded(
    tmp_path: Path,
) -> None:
    """§3.12: ``lost`` is the honest status for an outcome nobody observed.

    Not ``failed`` — the eat may well have completed in the last frame before
    the game vanished — and emphatically not ``succeeded``. Both of those are
    statements about the world; ``lost`` is a statement about the evidence.
    """
    manager, _, clock = _live_session(tmp_path)
    clock.advance(EXPECTED_TIMEOUT_MS + 1)
    running: tuple[Command, ...] = (
        make_command(ActionName.CONSUME_EAT, session_id=IPC_SESSION_ID),
        make_command(ActionName.MOVEMENT_MOVE_TO, session_id=IPC_SESSION_ID),
    )

    outcome = manager.on_game_restart(in_flight=running)

    assert outcome.kind is RecoveryKind.GAME_RESTART
    assert outcome.reason_code is ReasonCode.GAME_DISCONNECTED
    assert outcome.generation == 1, "every reference minted before this is now void"
    assert outcome.refs_invalidated
    assert outcome.requires_rearm
    assert len(outcome.lost) == len(running)
    assert {result.status for result in outcome.lost} == {ActionStatus.LOST}
    assert {result.reason_code for result in outcome.lost} == {ReasonCode.GAME_DISCONNECTED}
    assert all(result.is_terminal for result in outcome.lost)
    assert all(not result.evidence for result in outcome.lost), (
        "a lost action has no postcondition evidence, because none was observed"
    )
    assert [result.command_id for result in outcome.lost] == [c.command_id for c in running]


def test_only_a_person_re_arms_after_the_game_came_back(tmp_path: Path) -> None:
    """Autonomy never resumes by itself, and a resume does not resume it.

    A restarted sidecar reattaching is a technical event; the user deciding the
    world is in a state worth acting on is not, and only :meth:`mark_rearmed`
    represents the second one.
    """
    manager, monitor, _ = _live_session(tmp_path)
    manager.on_game_restart()
    assert manager.requires_rearm

    monitor.publish(Peer.GAME, session_id=IPC_SESSION_ID, nonce="g2", version="0.1.0")
    outcome = manager.resume()

    assert outcome.resumed, outcome.detail
    assert outcome.requires_rearm, "reattaching is not consent to start acting again"
    assert manager.requires_rearm

    manager.mark_rearmed()
    assert not manager.requires_rearm


def test_a_command_closed_by_a_game_restart_is_never_executed_again(tmp_path: Path) -> None:
    """``lost`` is terminal, so it is cached — and a redelivery replays it.

    The mod redelivers on its own timers, and a redelivery that got through
    would re-run a command against a world that has since reloaded.
    """
    queue = CommandQueue(layout=make_layout(tmp_path), session_id=IPC_SESSION_ID, clock=FakeClock())
    try:
        first = queue.submit(
            queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=30_000)
        )
        assert first.accepted

        lost = queue.close_in_flight(ReasonCode.GAME_DISCONNECTED)
        assert lost is not None
        assert lost.status is ActionStatus.LOST
        assert queue.in_flight is None

        redelivered = queue.submit(
            queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=30_000)
        )
    finally:
        queue.close()

    assert not redelivered.accepted
    assert redelivered.duplicate
    assert redelivered.replayed is not None
    assert redelivered.replayed.status is ActionStatus.LOST
    assert redelivered.replayed.reason_code is ReasonCode.GAME_DISCONNECTED


def test_a_fresh_idempotency_key_is_still_accepted_after_the_restart(tmp_path: Path) -> None:
    """The control: the replay above is the cache, not a queue that shut down."""
    queue = CommandQueue(layout=make_layout(tmp_path), session_id=IPC_SESSION_ID, clock=FakeClock())
    try:
        queue.submit(queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=30_000))
        queue.close_in_flight(ReasonCode.GAME_DISCONNECTED)
        fresh = queue.submit(
            queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-2", lease_ms=30_000)
        )
    finally:
        queue.close()

    assert fresh.accepted
    assert fresh.replayed is None


# ---------------------------------------------------------------------------
# a save that changed under us (E12-M03-T003)
# ---------------------------------------------------------------------------

#: The world the session started on and the one the player loaded instead. Two
#: literals rather than one derived from the other: the whole rule is exact
#: equality of the id the mod computed, so the test must not be able to satisfy
#: it by construction.
FIRST_SAVE: Final = "save-3ac91f70"
SECOND_SAVE: Final = "save-01b7ee42"


def test_the_first_sight_of_a_save_is_not_a_recovery(tmp_path: Path) -> None:
    """The control that stops "always report SAVE_CHANGED" from passing.

    Nothing was ever minted against a *different* world, so recording the save
    for the first time must not invalidate references or bump the generation —
    doing so would void every ref already handed out for this very save while
    reporting that nothing happened.
    """
    manager, _, _ = _live_session(tmp_path)

    assert manager.note_save_id(FIRST_SAVE) is None
    assert manager.generation == 0
    assert manager.refs_valid
    assert not manager.requires_rearm


def _ref_on(manager: SessionManager, generation: int) -> str:
    session = manager.session
    assert session is not None
    return f"item:{session.session_id}:player-main:9001:{generation}"


def test_a_changed_save_ends_the_session_rather_than_continuing(tmp_path: Path) -> None:
    """Every reference dies with the world it described.

    The generation moves so a ref minted on the old save can be *recognised* as
    stale rather than silently resolving against whatever now occupies that
    runtime id — which, in a game that reuses them, is how the agent would end
    up acting on the wrong object.
    """
    manager, _, _ = _live_session(tmp_path)
    manager.note_save_id(FIRST_SAVE)
    old_ref = _ref_on(manager, 0)
    assert manager.is_ref_current(old_ref)
    running = (make_command(ActionName.CONSUME_EAT, session_id=IPC_SESSION_ID),)

    outcome = manager.note_save_id(SECOND_SAVE, in_flight=running)

    assert outcome is not None
    assert outcome.kind is RecoveryKind.SAVE_CHANGED
    assert outcome.reason_code is ReasonCode.SAVE_CHANGED
    assert outcome.generation == 1
    assert outcome.refs_invalidated
    assert outcome.requires_rearm
    assert manager.requires_rearm, (
        "the manager's own flag has to agree with what the outcome reported; a "
        "recovery that announces a re-arm without recording one leaves the next "
        "caller believing it may carry on"
    )
    assert [result.status for result in outcome.lost] == [ActionStatus.LOST]
    assert [result.reason_code for result in outcome.lost] == [ReasonCode.SAVE_CHANGED]
    assert not manager.refs_valid
    assert not manager.is_ref_current(old_ref)
    assert not manager.is_ref_current(_ref_on(manager, 1)), (
        "not even a ref carrying the new generation is usable until the world "
        "has actually been re-observed"
    )


def test_re_observing_the_world_restores_references_and_nothing_else(tmp_path: Path) -> None:
    """A full snapshot is evidence about objects, never about permission."""
    manager, _, _ = _live_session(tmp_path)
    manager.note_save_id(FIRST_SAVE)
    manager.note_save_id(SECOND_SAVE)

    stale_world = manager.note_full_snapshot(FIRST_SAVE)
    still_invalid = manager.refs_valid
    this_world = manager.note_full_snapshot(SECOND_SAVE)
    valid_again = manager.refs_valid

    assert stale_world is False, (
        "a snapshot of the world we left says nothing about the one we are in"
    )
    assert still_invalid is False
    assert this_world is True
    assert valid_again is True
    assert manager.is_ref_current(_ref_on(manager, 1))
    assert manager.requires_rearm, "re-observing is not re-arming"


def _save_change_engine(
    *observations: Observation,
) -> tuple[ActionResult, FakeCommandSink]:
    """The engine latched to :data:`FIRST_SAVE`, fed *observations* in order."""
    clock = EngineClock()
    sink = FakeCommandSink(clock)
    source = FakeObservationSource(clock)
    source.push(*observations)
    source.repeat(observations[-1])
    registry = AdapterRegistry()
    registry.register(StubAdapter(name=ActionName.MOVEMENT_MOVE_TO, verify_after=None))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        expected_save_id=FIRST_SAVE,
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
    return result, sink


def test_nothing_is_sent_once_the_engine_sees_a_different_save() -> None:
    """The engine latches the save it started on and checks it before the send.

    An arm-time check alone would be blind to a world swapped underneath an
    already-authorised session, which is precisely the case this covers.
    """
    result, sink = _save_change_engine(_obs(seq=1, game=make_game(save_id=SECOND_SAVE)))

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code is ReasonCode.SAVE_CHANGED
    assert sink.sent == [], "nothing may be sent against a world we have not looked at"


def test_a_save_that_changed_under_a_running_command_ends_it() -> None:
    """Shipped against one world, and the player loaded another mid-action.

    The command is not left running against a world where its target no longer
    exists: the mod is told to abandon it, and the outcome is ``cancelled`` —
    someone else is driving now — rather than a failure of the action itself.
    """
    result, sink = _save_change_engine(
        _obs(seq=1, game=make_game(save_id=FIRST_SAVE)),
        _obs(seq=2, game=make_game(save_id=SECOND_SAVE)),
    )

    assert len(sink.sent) == 1, "it did ship before the world changed"
    assert result.status is ActionStatus.CANCELLED
    assert result.reason_code is ReasonCode.SAVE_CHANGED
    assert [reason for _, reason in sink.cancelled] == [ReasonCode.SAVE_CHANGED]


def test_the_same_save_throughout_lets_the_command_run() -> None:
    """The control: the two refusals above are the save id, not the harness."""
    result, sink = _save_change_engine(
        _obs(seq=1, game=make_game(save_id=FIRST_SAVE)),
        _obs(seq=2, game=make_game(save_id=FIRST_SAVE)),
    )

    assert len(sink.sent) == 1
    assert result.reason_code is not ReasonCode.SAVE_CHANGED


def test_the_guard_calls_a_changed_save_a_disarm(tmp_path: Path) -> None:
    """One tick either side of the load screen, and the control beside it."""
    before = _obs(seq=1, game=make_game(save_id=FIRST_SAVE))
    after = _obs(seq=2, game=make_game(save_id=SECOND_SAVE))

    events = GUARD.evaluate(before, after, ReflexSignals(now_ms=NOW_MS))

    changed = [event for event in events if event.reason_code is ReasonCode.SAVE_CHANGED]
    assert len(changed) == 1, f"expected one save-changed event, got {events}"
    assert changed[0].forces_disarm
    assert not changed[0].cancels_running_action, (
        "who owns the action running on the new world is not something the "
        "previous world's observation can say"
    )
    assert GUARD.evaluate(before, _obs(seq=2, game=make_game(save_id=FIRST_SAVE))) == []


# ---------------------------------------------------------------------------
# re-arming afterwards: the backup has to cover *this* save (E12-M03-T003)
# ---------------------------------------------------------------------------

#: A hungry, armed, AUTONOMOUS tick with something to eat within reach, so the
#: only thing that can refuse below is the backup rule.
CAPABILITIES: Final = CapabilitySnapshot.verified("eat_percentage")


def _world_on(save_id: str) -> Observation:
    return autonomous_observation(
        game=make_game(save_id=save_id),
        player=hungry_player(0.75),
        inventory=inventory(food_item("f1")),
    )


def _decide(save_id: str, backup: BackupEvidence | None) -> Any:
    return AutonomyGate().evaluate(
        _world_on(save_id), now_ms=NOW_MS, backup=backup, capabilities=CAPABILITIES
    )


def test_the_backup_that_covered_the_old_save_does_not_arm_the_new_one() -> None:
    """A backup of another world is not a safety net for this one.

    The failure this prevents is quiet: the agent had a perfectly good, freshly
    verified backup a minute ago, and everything about the session other than
    ``save_id`` still looks the same.
    """
    decision = _decide(
        SECOND_SAVE, BackupEvidence(save_id=FIRST_SAVE, created_at_ms=NOW_MS, verified=True)
    )

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.SAVE_CHANGED
    assert SECOND_SAVE not in decision.detail and FIRST_SAVE not in decision.detail, (
        "a refusal names what failed and what to do, never a payload value"
    )


def test_an_unverified_backup_of_the_right_save_is_not_a_confirmed_one() -> None:
    """ "A file exists" is not "a restore would work"."""
    decision = _decide(
        SECOND_SAVE, BackupEvidence(save_id=SECOND_SAVE, created_at_ms=NOW_MS, verified=False)
    )

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.PRECONDITION_FAILED


def test_no_backup_at_all_is_refused_the_same_way() -> None:
    decision = _decide(SECOND_SAVE, None)

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_confirmed_backup_of_this_very_save_is_what_it_takes() -> None:
    """The control: without it the three refusals above prove nothing.

    Every other input is identical, so this is the one that shows the gate is
    reading ``save_id`` and ``verified`` rather than refusing autonomy outright.
    """
    decision = _decide(
        SECOND_SAVE, BackupEvidence(save_id=SECOND_SAVE, created_at_ms=NOW_MS, verified=True)
    )

    assert decision.outcome is AutonomyOutcome.ACT
    assert decision.reason_code is None


def test_neither_save_id_here_is_the_one_the_builders_default_to() -> None:
    """So a fixture default cannot be what makes any of the above agree.

    Every observation in this section names its save explicitly. If one of these
    two ids happened to equal the builders' default, a test that forgot to pass
    ``save_id`` would still line up — and would stop lining up the day the
    default changed, for a reason unrelated to anything it asserts.
    """
    assert len({FIRST_SAVE, SECOND_SAVE, DEFAULT_SAVE}) == 3
