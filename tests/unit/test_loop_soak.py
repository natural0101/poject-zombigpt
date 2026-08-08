"""H3 — the loop soak: thousands of real ticks under a deterministic barrage.

The assembly is the contract tests' (``test_remote_actions_served.py``,
``test_sidecar_serves_the_core.py``), deliberately: a real
:class:`~pz_agent_cli.runtime.SidecarLoop` built by the same
:func:`~pz_agent_cli.app.build_loop` the CLI runs, over a
:func:`~tests.fixtures.cli_worlds.make_world` machine, the mod faked at the
exchange-directory files and nowhere else — a heartbeat keeper and an
observation feeder whose world clock advances a minute per record. What this
file adds is *duration and churn*: while the loop ticks on its own thread, a
schedule seeded with a literal interleaves observation appends, goal
submissions (valid, duplicate, and past the cap), action submissions through
the channel (disarmed and armed), disarms, re-arms, a panic sentinel, and a
resume — and asserts, from the outside, the boundedness the design promises:

* the action channel's record store never exceeds its documented cap, and an
  id is never forgotten before its record was seen terminal — a submitter's
  only handle on in-flight work is never evicted;
* the goal queue's open set (pending plus active) never exceeds its cap;
* the tick thread never dies mid-soak — progress is observed between
  checkpoints through the loop's tick counter and its observation store;
* nothing submitted while disarmed ever reads ``succeeded``;
* shutdown releases the lock and reports its cause by name, and every thread
  this test started is gone afterwards (compared by name against the set that
  existed before, so pytest's own threads are tolerated).

Everything is bounded: the loop by an explicit tick budget and a stop flag,
the schedule by a fixed operation list, every wait by a deadline and a poll
cap, both feeder threads by iteration caps and stop events, the joins by
timeouts. Nothing here decides an outcome; the loop's own levers do, and this
file only reads what they published.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.app import build_loop
from pz_agent_cli.context import resolve_workspace
from pz_agent_cli.runtime import (
    DEFAULT_MAX_REMEMBERED_ACTIONS,
    MAX_RETAINED_EVENTS,
    LoopError,
    LoopLimits,
    RunSummary,
    StopCause,
)
from pz_agent_cli.supervisor import ControlKind
from pz_agent_core.actions import ActionRequest
from pz_agent_core.goals import GoalKind, GoalParams, GoalRequest, GoalState
from pz_agent_core.ipc.clocks import system_clock_ms
from pz_agent_core.ipc.journal import JournalWriter
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import PRODUCT_VERSION
from tests.fixtures import make_game, make_observation
from tests.fixtures.cli_worlds import make_world

#: The schedule seed. A literal, so a failure report names the exact schedule
#: that produced it and a re-run replays the same operation sequence.
SEED: Final = 0xB5EED

#: Outer bound on each individual wait. Long enough for a loaded CI runner,
#: short enough that the whole soak stays far under the suite's 300 s cap.
GRACE: Final = 8.0

#: Ticks the loop must have served before the soak may end — "a few thousand",
#: made a number the summary is checked against.
TICK_GOAL: Final = 2_500

#: The one wait allowed to be longer than :data:`GRACE`: topping the tick
#: count up to :data:`TICK_GOAL` on a slow runner.
TICK_GOAL_DEADLINE_S: Final = 90.0

#: Operations in the disarmed soak phase. Fixed, so the schedule is a fixed
#: list once the seed is fixed.
STEPS: Final = 120

#: Where the panic pulses land in the schedule: after these step indices the
#: sentinel is written, the channels are watched until empty, and it is
#: removed again. Deterministic positions, not sampled ones.
PULSE_AFTER: Final = frozenset({29, 59, 89})

#: Progress checkpoints: at these step indices the tick counter and the
#: observation store must both have moved since the previous checkpoint.
CHECKPOINT_AT: Final = frozenset({0, 19, 39, 79, 119})

#: The fake mod's world clock, advancing far past the one game second the
#: armed waits ask for — the world itself is the proof, exactly as in
#: ``test_remote_actions_served.py``.
WORLD_EPOCH: Final = datetime(1993, 7, 9, 14, 20, 0)
GAME_SECONDS_PER_OBSERVATION: Final = 60


class NoInitiativeGoalPlanner:
    """Goal-capable and idle: goals activate for real, nothing dispatches.

    ``propose_for_goal`` makes it a :class:`~pz_agent_cli.runtime.GoalPlanner`
    so the loop's ``AUTONOMOUS`` tick activates submitted goals; answering
    ``None`` to everything keeps every dispatch below attributable to the
    action channel. Two counters instead of a list, because this planner is
    asked every tick for the length of the soak and a list would be the
    unbounded growth this file exists to refuse.
    """

    def __init__(self) -> None:
        self.goals_asked = 0
        self.last_goal_id: str | None = None

    def propose(self, observation: Observation) -> ActionRequest | None:
        return None

    def propose_for_goal(self, goal: PlannerGoal, observation: Observation) -> ActionRequest | None:
        self.goals_asked += 1
        self.last_goal_id = goal.goal_id
        return None


def _until(predicate: Callable[[], bool], *, message: str, deadline_s: float = GRACE) -> None:
    """Poll *predicate* with a deadline and a poll bound."""
    deadline = time.monotonic() + deadline_s
    for _ in range(int(deadline_s / 0.02) + 1):
        if predicate():
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    pytest.fail(f"{message} (schedule seed {SEED:#x})")


def _wait_request(session_id: str, key: str) -> ActionRequest:
    return ActionRequest(
        action=ActionName.ACTION_WAIT,
        session_id=session_id,
        idempotency_key=key,
        args={"game_seconds": 1.0},
    )


def _goal_request(kind: GoalKind, key: str, satisfy_to: float | None) -> GoalRequest:
    params = GoalParams() if satisfy_to is None else GoalParams(satisfy_to=satisfy_to)
    return GoalRequest(kind=kind, idempotency_key=key, params=params)


def test_loop_soak_stays_bounded(tmp_path: Path) -> None:
    # -- who was here before us: the baseline for the thread-leak check ------
    threads_before = {thread.name for thread in threading.enumerate()}

    # -- the machine and the real loop, assembled as `pz-agent start` does ---
    world = make_world(tmp_path, username="u")
    ctx = replace(world.ctx, clock_ms=system_clock_ms, state_dir_override=tmp_path / "s")
    workspace = resolve_workspace(ctx)
    assert workspace.ipc_root is not None

    # The budget is explicit and the outer bound; the stop flag is how the
    # soak actually ends. A 1 ms interval reaches a few thousand ticks in a
    # handful of seconds without turning the loop into a spin.
    limits = LoopLimits(tick_interval_ms=1, tick_budget=200_000)
    loop = build_loop(ctx, workspace, limits=limits)
    attach = loop.attach()
    assert attach.attached, attach.detail
    session = attach.session
    assert session is not None
    session_id = session.session_id

    planner = NoInitiativeGoalPlanner()
    loop.planner = planner

    channel = loop.actions
    assert channel is not None
    goals = loop.goals
    assert goals is not None
    control = loop.control
    assert control is not None
    sentinel = loop.layout.panic_stop

    # -- the outside-view invariant sweep -----------------------------------
    #: action id -> whether a terminal record for it has been observed. The
    #: eviction-honesty rule is asserted through this map: an id may only stop
    #: answering `status` after a terminal record for it was seen, because the
    #: channel documents that a waiting or running record is never the victim.
    seen_terminal: dict[str, bool] = {}
    #: ids of everything submitted while the loop was never armed (phase A):
    #: none of them may ever read `succeeded`.
    disarmed_ids: set[str] = set()
    refused_full = 0

    def note(record_id: str, terminal: bool) -> None:
        seen_terminal[record_id] = seen_terminal.get(record_id, False) or terminal

    def sweep_and_assert_bounds() -> None:
        assert channel.pending_count <= channel.max_pending, (
            f"the action queue exceeded its cap (schedule seed {SEED:#x})"
        )
        assert channel.remembered_count <= DEFAULT_MAX_REMEMBERED_ACTIONS, (
            f"the action record store exceeded its cap (schedule seed {SEED:#x})"
        )
        with loop.goal_lock:
            open_count = goals.open_count
            open_cap = goals.max_open
        assert open_count <= open_cap, (
            f"the goal queue's open set exceeded its cap (schedule seed {SEED:#x})"
        )
        assert len(loop.recent_events) <= MAX_RETAINED_EVENTS
        assert len(loop.state_problems) <= MAX_RETAINED_EVENTS
        for action_id in list(seen_terminal):
            record = channel.status(action_id)
            if record is None:
                assert seen_terminal[action_id], (
                    f"action {action_id} was evicted before any terminal record was "
                    f"observed — an in-flight record was forgotten (schedule seed {SEED:#x})"
                )
                continue
            note(action_id, record.terminal)
            if action_id in disarmed_ids:
                assert record.status is not ActionStatus.SUCCEEDED, (
                    f"a submission made while disarmed reads succeeded (schedule seed {SEED:#x})"
                )

    def submit_action(request: ActionRequest, *, while_disarmed: bool) -> str | None:
        """One admission through the channel; a full queue is a named refusal."""
        nonlocal refused_full
        try:
            record = channel.submit(request)
        except LoopError as refusal:
            assert "cap" in str(refusal), refusal
            refused_full += 1
            return None
        note(record.action_id, record.terminal)
        if while_disarmed:
            disarmed_ids.add(record.action_id)
        return record.action_id

    # -- phase 0, before the tick thread: both caps refuse deterministically -
    pre_requests = [_wait_request(session_id, f"soak-pre-{n}") for n in range(4)]
    for request in pre_requests:
        assert submit_action(request, while_disarmed=True) is not None
    with pytest.raises(LoopError, match="cap"):
        channel.submit(_wait_request(session_id, "soak-pre-overflow"))
    refused_full += 1
    assert channel.pending_count == channel.max_pending

    goal_requests: list[GoalRequest] = []
    goal_ids: list[str] = []
    for n in range(4):
        request_g = _goal_request(GoalKind.SATISFY_THIRST, f"soak-pre-g-{n}", None)
        with loop.goal_lock:
            admission = goals.submit(request_g)
        assert admission.refusal is None and admission.goal is not None
        goal_requests.append(request_g)
        goal_ids.append(admission.goal.goal_id)
    with loop.goal_lock:
        overflow = goals.submit(_goal_request(GoalKind.SATISFY_THIRST, "soak-pre-g-over", None))
    assert overflow.refusal is not None
    assert overflow.refusal.reason_code is ReasonCode.QUEUE_REJECTED
    sweep_and_assert_bounds()

    # -- the loop on its thread, and the fake mod at the files ---------------
    summaries: list[RunSummary] = []
    stop = threading.Event()
    ticking = threading.Thread(
        target=lambda: summaries.append(loop.run(should_stop=stop.is_set)),
        name="soak-loop",
        daemon=True,
    )

    feeder_stop = threading.Event()
    monitor = HeartbeatMonitor(loop.layout, clock=system_clock_ms)

    def keep_beating() -> None:
        for _ in range(1_200):  # 0.25 s period: bounded at five minutes
            if feeder_stop.is_set():
                return
            monitor.publish(
                Peer.GAME,
                session_id=session_id,
                nonce="fake-mod-nonce",
                version=PRODUCT_VERSION,
                build="42.20",
                player_present=True,
            )
            feeder_stop.wait(0.25)

    def keep_observing() -> None:
        writer = JournalWriter(loop.layout, loop.layout.observation_events)
        try:
            for seq in range(1, 30_000):  # 10 ms period: bounded at five minutes
                if feeder_stop.is_set():
                    return
                stamp = WORLD_EPOCH + timedelta(seconds=seq * GAME_SECONDS_PER_OBSERVATION)
                observation = make_observation(
                    session_id=session_id,
                    seq=seq,
                    timestamp_ms=system_clock_ms(),
                    game=make_game(world_time=stamp.isoformat()),
                )
                writer.append(observation.to_dict())
                feeder_stop.wait(0.01)
        finally:
            writer.close()

    beating = threading.Thread(target=keep_beating, name="soak-heartbeat", daemon=True)
    observing = threading.Thread(target=keep_observing, name="soak-observations", daemon=True)
    ticking.start()
    beating.start()
    observing.start()

    def store_seq() -> int:
        latest = loop.store.latest()
        return -1 if latest is None else latest.seq

    def checkpoint(previous: tuple[int, int]) -> tuple[int, int]:
        """The tick thread is alive and moving: ticks and store both advanced."""
        ticks_then, seq_then = previous
        assert ticking.is_alive(), f"the tick thread died mid-soak (schedule seed {SEED:#x})"
        _until(
            lambda: loop.ticks > ticks_then and store_seq() > seq_then,
            message="the loop stopped making progress between checkpoints",
        )
        return loop.ticks, store_seq()

    def panic_pulse() -> None:
        """Write the sentinel, watch both channels empty, then clear it."""
        sentinel.write_text("panic", encoding="utf-8")

        def levelled() -> bool:
            with loop.goal_lock:
                open_now = goals.open_count
            return not loop.armed and channel.pending_count == 0 and open_now == 0

        _until(levelled, message="the panic sentinel did not empty both channels")
        sentinel.unlink()

    def arm_via_control(mode: SessionMode) -> None:
        """Ask through the shipped channel until the loop's own judgement arms.

        Retried because an arm can be honestly refused in a running world — a
        journal caught mid-append, a guard event on the same tick — and each
        refusal says to ask again. Bounded by the attempt count and the
        per-attempt deadline.
        """
        for _ in range(8):
            request = control.write(ControlKind.ARM, mode=mode)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if loop.armed and loop.mode is mode:
                    return
                decision = loop.control_decision
                if decision is not None and decision.nonce == request.nonce:
                    if decision.applied:
                        break  # applied but not visible yet: keep polling
                    break  # judged and refused: write a fresh request
                time.sleep(0.02)
            if loop.armed and loop.mode is mode:
                return
        pytest.fail(
            f"the loop refused to arm into {mode.value}; last decision: "
            f"{loop.control_decision} (schedule seed {SEED:#x})"
        )

    try:
        # -- phase A: the disarmed soak, on the seeded schedule --------------
        rng = random.Random(SEED)
        kinds = (
            "act_new",
            "act_new",
            "act_new",
            "act_new",
            "act_dup",
            "act_dup",
            "act_burst",
            "goal_new",
            "goal_new",
            "goal_dup",
            "goal_cancel",
            "disarm_req",
        )
        schedule = [rng.choice(kinds) for _ in range(STEPS)]
        action_requests: list[ActionRequest] = list(pre_requests)
        mark = (loop.ticks, store_seq())
        serial = 0
        for step, op in enumerate(schedule):
            if op == "act_new":
                serial += 1
                request = _wait_request(session_id, f"soak-a-{serial}")
                if submit_action(request, while_disarmed=True) is not None:
                    action_requests.append(request)
            elif op == "act_dup":
                # The same request object again: a retried submission resolves
                # to the record its key already created — or starts the key
                # over once that record has been honestly evicted.
                submit_action(rng.choice(action_requests), while_disarmed=True)
            elif op == "act_burst":
                for _ in range(6):
                    serial += 1
                    request = _wait_request(session_id, f"soak-a-{serial}")
                    if submit_action(request, while_disarmed=True) is not None:
                        action_requests.append(request)
            elif op == "goal_new":
                serial += 1
                request_g = _goal_request(
                    GoalKind.SATISFY_HUNGER, f"soak-g-{serial}", rng.choice((0.4, 0.6, 0.8))
                )
                with loop.goal_lock:
                    admission = goals.submit(request_g)
                if admission.goal is not None:
                    goal_requests.append(request_g)
                    goal_ids.append(admission.goal.goal_id)
                else:
                    assert admission.refusal is not None  # past the cap, by name
            elif op == "goal_dup":
                with loop.goal_lock:
                    goals.submit(rng.choice(goal_requests))
            elif op == "goal_cancel":
                target = rng.choice(goal_ids)
                with loop.goal_lock:
                    record_g = goals.record(target)
                    if record_g is not None and record_g.is_open:
                        goals.request_cancel(target)
            else:  # disarm_req: a no-op lever is still a lever the loop judges
                control.write(ControlKind.DISARM)
            sweep_and_assert_bounds()
            assert loop.armed is False, (
                f"the loop armed itself during the disarmed soak (schedule seed {SEED:#x})"
            )
            if step in CHECKPOINT_AT:
                mark = checkpoint(mark)
            if step in PULSE_AFTER:
                panic_pulse()

        # How many of the schedule's submissions were admitted rather than
        # cap-refused races the tick thread's own clearing, so the minted
        # total varies run to run. Eviction must be exercised every run, so
        # mint past the record cap here — bounded twice, by the iteration cap
        # and by the deadline inside the wait.
        for _ in range(600):
            if len(seen_terminal) > DEFAULT_MAX_REMEMBERED_ACTIONS + 4:
                break
            serial += 1
            if submit_action(_wait_request(session_id, f"soak-t-{serial}"), while_disarmed=True):
                sweep_and_assert_bounds()
            else:
                time.sleep(0.02)  # the queue is at its cap; the next tick clears it
        else:
            pytest.fail(f"could not mint past the action record cap (schedule seed {SEED:#x})")

        # -- phase B: armed for real, one dispatch to the engine's own proof -
        # The leftover disarmed submissions must be ended by their own lever
        # BEFORE authority arrives: one still waiting on the arming tick would
        # be served through the funnel — the arm outranks it — and this file's
        # "disarmed never succeeds" reading is about submissions whose whole
        # life was disarmed.
        _until(
            lambda: channel.pending_count == 0,
            message="leftover disarmed submissions were never ended",
        )
        arm_via_control(SessionMode.ASSISTED)
        armed_request = _wait_request(session_id, "soak-armed-1")
        maybe_armed_id = submit_action(armed_request, while_disarmed=False)
        assert maybe_armed_id is not None
        armed_id: str = maybe_armed_id

        def armed_ended() -> bool:
            record = channel.status(armed_id)
            return record is not None and record.terminal

        _until(armed_ended, message="the armed submission never reached a terminal record")
        final = channel.status(armed_id)
        assert final is not None
        assert final.status is ActionStatus.SUCCEEDED, final
        result = final.result
        assert result is not None
        assert result.reason_code is ReasonCode.POSTCONDITION_MET
        sweep_and_assert_bounds()

        # -- phase C: panic while armed, and while the sentinel stays --------
        armed_burst = [
            submit_action(_wait_request(session_id, f"soak-c-{n}"), while_disarmed=False)
            for n in range(3)
        ]
        sentinel.write_text("panic", encoding="utf-8")

        def panic_settled() -> bool:
            with loop.goal_lock:
                open_now = goals.open_count
            if loop.armed or channel.pending_count != 0 or open_now != 0:
                return False
            return all(
                aid is None or ((rec := channel.status(aid)) is not None and rec.terminal)
                for aid in armed_burst
            )

        _until(panic_settled, message="the panic sentinel left work in flight")

        # A level, not an edge: a submission that arrives while the sentinel
        # stays is ended by the next tick with a stop lever's own reason.
        maybe_during_id = submit_action(
            _wait_request(session_id, "soak-c-panic"), while_disarmed=True
        )
        assert maybe_during_id is not None
        during_id: str = maybe_during_id

        def during_ended() -> bool:
            record = channel.status(during_id)
            return record is not None and record.terminal

        _until(during_ended, message="a submission made under panic was never ended")
        during = channel.status(during_id)
        assert during is not None
        assert during.status is not ActionStatus.SUCCEEDED
        assert during.result is not None
        assert during.result.reason_code in (ReasonCode.PANIC_STOP, ReasonCode.NOT_ARMED)
        sweep_and_assert_bounds()

        # -- phase D: resume. Clearing the sentinel re-arms nothing ----------
        sentinel.unlink()
        mark = checkpoint(mark)
        mark = checkpoint(mark)  # two checkpoints of ticking, still disarmed
        assert loop.armed is False, (
            f"clearing the sentinel re-armed the loop (schedule seed {SEED:#x})"
        )

        arm_via_control(SessionMode.AUTONOMOUS)
        with loop.goal_lock:
            admission = goals.submit(_goal_request(GoalKind.SATISFY_THIRST, "soak-d-goal", None))
        assert admission.refusal is None and admission.goal is not None
        resumed_goal = admission.goal.goal_id

        def activated() -> bool:
            with loop.goal_lock:
                active = goals.active
            return (
                active is not None
                and active.goal_id == resumed_goal
                and planner.last_goal_id == resumed_goal
            )

        _until(activated, message="the resumed goal was never activated and served")
        assert planner.goals_asked > 0

        control.write(ControlKind.DISARM)

        def goal_disarmed() -> bool:
            with loop.goal_lock:
                record_g = goals.record(resumed_goal)
                active = goals.active
            return (
                not loop.armed
                and active is None
                and record_g is not None
                and record_g.state is GoalState.CANCELLED
            )

        _until(goal_disarmed, message="disarm left the resumed goal in flight")
        with loop.goal_lock:
            ended_goal = goals.record(resumed_goal)
        assert ended_goal is not None
        assert ended_goal.reason_code is ReasonCode.NOT_ARMED
        sweep_and_assert_bounds()

        # -- phase E: top the soak up to its promised tick count -------------
        _until(
            lambda: loop.ticks >= TICK_GOAL,
            message=f"the loop never reached {TICK_GOAL} ticks",
            deadline_s=TICK_GOAL_DEADLINE_S,
        )
        assert ticking.is_alive(), (
            f"the tick thread died before the stop was requested (schedule seed {SEED:#x})"
        )
    finally:
        stop.set()
        feeder_stop.set()
        ticking.join(timeout=GRACE)
        beating.join(timeout=GRACE)
        observing.join(timeout=GRACE)
        shutdown = loop.shutdown(reason="soak finished")

    # -- shutdown reports clean and releases the lock ------------------------
    assert not ticking.is_alive(), "the loop did not stop inside the bound"
    assert shutdown.lock_released is True
    assert len(summaries) == 1, "the run never returned a summary"
    summary = summaries[0]
    assert summary.cause is StopCause.CALLER_ASKED, summary
    assert summary.ticks >= TICK_GOAL
    assert summary.ticks == loop.ticks

    # -- the final outside view: every bound held to the end -----------------
    sweep_and_assert_bounds()
    assert channel.pending_count == 0, "shutdown left submissions waiting"
    assert len(seen_terminal) > DEFAULT_MAX_REMEMBERED_ACTIONS, (
        "the soak minted too few actions to exercise eviction at all"
    )
    assert all(seen_terminal.values()), (
        f"some submissions never reached an observed terminal record (schedule seed {SEED:#x})"
    )
    assert refused_full >= 1, "the full-queue refusal was never exercised"

    # -- every thread this test started is gone; pytest's own are tolerated --
    assert not beating.is_alive() and not observing.is_alive()
    leftover = {thread.name for thread in threading.enumerate()} - threads_before
    assert not {name for name in leftover if name.startswith("soak-")}, (
        f"soak threads outlived the test: {sorted(leftover)} (schedule seed {SEED:#x})"
    )
