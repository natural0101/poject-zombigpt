"""The goal queue's eight load-bearing promises, one class each.

:mod:`tests.unit.test_goal_channel` covers the channel's *vocabulary* — what a
goal may say, what a refusal may quote, what a record may claim. This file
covers the queue's *liveness*: the eight properties whose absence would leave a
user with a goal that never ends, a channel that grows without limit, or a stop
button that does not stop anything.

Everything here is driven by :class:`ManualClock`. There is no :func:`time.sleep`
in this file and there must never be one: a test that waits is a test that is
slow when it passes and flaky when it fails, and a deadline the test cannot
place exactly is a deadline the test cannot pin to the millisecond it fires on.
Boundary expectations are therefore written as arithmetic the reader can do —
``started at 5_000`` plus ``2_000 ms of budget`` is ``7_000`` — rather than read
back out of :attr:`GoalRecord.deadline_ms`, which would pass against a deadline
computed any way at all.

The two regression tests worth naming, because they are the defects this file
was written to catch and did:

* ``activate_next`` promoted a goal whose cancellation had already been
  requested. The goal became ACTIVE, the executor would have dispatched a step
  for work the user had withdrawn, and only the following tick ended it.
* ``note_step`` charged the last step of a cancelled goal and reported it as
  ``EXPIRED``/``NO_PROGRESS`` — budget exhaustion — consuming the pending
  cancellation so that no tick ever reported it. The user was told the goal ran
  out of steps when what ended it was the user.
"""

from __future__ import annotations

from typing import Final

import pytest

from pz_agent_core.goals import (
    DEFAULT_MAX_OPEN,
    DEFAULT_MAX_REMEMBERED,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalQueue,
    GoalRecord,
    GoalRequest,
    GoalState,
    GoalTransition,
    TrainableSkill,
    UnknownGoalError,
)
from pz_agent_core.protocol import ActionResult, ReasonCode

# --------------------------------------------------------------------------
# the injected clock
# --------------------------------------------------------------------------


class ManualClock:
    """A wall clock in milliseconds that only the test moves.

    The queue takes a ``Clock`` callable rather than reading the system clock
    itself, which is the whole reason a deadline can be landed on exactly here.
    Call counting exists so one test can prove the queue really does read *this*
    object and not something else.
    """

    def __init__(self, start: int = 0) -> None:
        self.now = start
        self.reads = 0

    def __call__(self) -> int:
        self.reads += 1
        return self.now

    def set(self, ms: int) -> ManualClock:
        self.now = ms
        return self

    def advance(self, ms: int) -> ManualClock:
        self.now += ms
        return self


# A budget small enough that every bound in it can be reached inside one test,
# and written out here rather than taken from GOAL_SPECS so that a change to a
# shipped default does not silently change what these tests are measuring.
SHORT: Final = GoalBudget(max_wall_ms=2_000, max_steps=3, pending_ttl_ms=4_000)

# Wall clock and time to live both far past anything these tests advance to, so
# a goal carrying it ends only for the reason the test is about.
PATIENT: Final = GoalBudget(max_wall_ms=900_000, max_steps=3, pending_ttl_ms=600_000)


def hunger(key: str, *, budget: GoalBudget | None = None) -> GoalRequest:
    return GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key=key, budget=budget)


def training(key: str, *, budget: GoalBudget | None = None) -> GoalRequest:
    return GoalRequest(
        kind=GoalKind.TRAIN_SKILL,
        idempotency_key=key,
        params=GoalParams(skill=TrainableSkill.CARPENTRY),
        budget=budget,
    )


def observed() -> ActionResult:
    """A success ack, built the only way the protocol allows one to be built."""
    return ActionResult.succeeded(
        session_id="s-1",
        seq=1,
        command_id="c-1",
        action="consume.eat",
        timestamp_ms=0,
        evidence={"hunger": 0.1},
    )


def admit(queue: GoalQueue, request: GoalRequest) -> GoalRecord:
    """Submit *request*, asserting it was admitted, and return its record."""
    admission = queue.submit(request)
    assert admission.goal is not None, admission.refusal
    return admission.goal


def activate(queue: GoalQueue) -> GoalRecord:
    """Activate the next goal, asserting it started, and return its record."""
    admission = queue.activate_next()
    assert admission.goal is not None, admission.refusal
    assert admission.goal.state is GoalState.ACTIVE
    return admission.goal


def start_one(queue: GoalQueue, request: GoalRequest) -> GoalRecord:
    admit(queue, request)
    return activate(queue)


def states(queue: GoalQueue, goal_ids: list[str]) -> list[GoalState]:
    """The current state of each id, asserting none has been forgotten."""
    resolved = []
    for goal_id in goal_ids:
        record = queue.record(goal_id)
        assert record is not None, "an open goal must never be forgotten"
        resolved.append(record.state)
    return resolved


def assert_nothing_in_flight(queue: GoalQueue, goal_ids: list[str]) -> None:
    """The complete statement of "no goal is in flight"."""
    assert queue.active is None
    assert queue.pending == ()
    assert queue.open_count == 0
    for goal_id in goal_ids:
        record = queue.record(goal_id)
        assert record is not None
        assert record.state.is_terminal, record.state
        assert record.is_open is False
        assert record.reason_code is not None
        assert record.finished_at_ms is not None
    # And nothing is left queued up to fire later.
    assert queue.tick() == ()


# --------------------------------------------------------------------------
# T001 — the queue is bounded, and the bound refuses rather than drops
# --------------------------------------------------------------------------


class TestT001TheQueueIsBounded:
    def test_the_shipped_cap_is_small(self) -> None:
        # Hand-written literals. A cap read back from the module would agree
        # with itself however large it had become, and the point of the cap is
        # that a backlog of four stale intentions is already too many.
        assert DEFAULT_MAX_OPEN == 4
        assert DEFAULT_MAX_REMEMBERED == 32
        assert GoalQueue(clock=ManualClock()).max_open == 4

    def test_the_submission_past_the_cap_is_refused_with_a_named_reason(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=3)
        for key in ("a", "b", "c"):
            admit(queue, hunger(key))

        over = queue.submit(hunger("d"))

        assert over.accepted is False
        assert over.goal is None
        assert over.refusal is not None
        # A *named* reason: a machine-readable code and a message that says both
        # what the limit was and what the caller can do about it.
        assert over.refusal.reason_code is ReasonCode.QUEUE_REJECTED
        assert "3 open goal(s)" in over.refusal.message
        assert "cancel one or wait" in over.refusal.message

    def test_the_refusal_dropped_nothing_and_created_nothing(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=2)
        kept = [admit(queue, hunger("a")).goal_id, admit(queue, hunger("b")).goal_id]

        assert queue.submit(hunger("over")).refusal is not None

        # Dropped nothing: both admitted goals are exactly where they were.
        assert states(queue, kept) == [GoalState.PENDING, GoalState.PENDING]
        assert [r.goal_id for r in queue.pending] == kept
        assert queue.open_count == 2
        # Created nothing: the refused goal has no record anywhere, which shows
        # up as the next admitted goal taking the sequence number the refused
        # one would have burned.
        assert [r.sequence for r in queue.pending] == [1, 2]
        queue.request_cancel(kept[0])
        queue.tick()
        assert admit(queue, hunger("over")).sequence == 3

    def test_a_flood_never_pushes_the_channel_past_its_cap(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=2, max_remembered=8)
        accepted = 0
        refused = 0
        for index in range(200):
            admission = queue.submit(hunger(f"flood-{index}"))
            if admission.accepted:
                accepted += 1
            else:
                assert admission.refusal is not None
                assert admission.refusal.reason_code is ReasonCode.QUEUE_REJECTED
                assert admission.refusal.message.strip()
                refused += 1
            assert queue.open_count <= 2, "the cap was exceeded mid-flood"
        assert accepted == 2
        assert refused == 198

    def test_the_cap_counts_the_active_goal_as_well_as_the_backlog(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=2)
        start_one(queue, hunger("running"))
        admit(queue, hunger("waiting"))

        assert queue.active is not None
        assert len(queue.pending) == 1
        assert queue.open_count == 2
        assert queue.submit(hunger("third")).refusal is not None

    def test_a_terminal_goal_hands_its_slot_back(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=1)
        running = start_one(queue, hunger("first"))
        assert queue.submit(hunger("second")).refusal is not None

        queue.request_cancel(running.goal_id)
        queue.tick()

        assert queue.open_count == 0
        assert queue.submit(hunger("second")).accepted is True

    def test_the_remembered_history_is_bounded_too(self) -> None:
        # The cap on open goals bounds the channel; the cap on remembered goals
        # bounds the memory the channel needs to answer a resubmission.
        queue = GoalQueue(clock=ManualClock(), max_open=1, max_remembered=4)
        seen: list[str] = []
        for index in range(20):
            record = start_one(queue, hunger(f"g-{index}", budget=PATIENT))
            queue.fail(record.goal_id, ReasonCode.NO_SAFE_FOOD)
            seen.append(record.goal_id)
        remembered = [goal_id for goal_id in seen if queue.record(goal_id) is not None]
        assert len(remembered) == 4
        assert remembered == seen[-4:], "the history should forget the oldest first"

    @pytest.mark.parametrize(
        ("max_open", "max_remembered", "message"),
        [
            (0, 32, "max_open must be positive"),
            (-1, 32, "max_open must be positive"),
            (4, 3, "max_remembered must be at least"),
        ],
    )
    def test_a_channel_that_could_not_be_bounded_is_refused_at_construction(
        self, max_open: int, max_remembered: int, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            GoalQueue(clock=ManualClock(), max_open=max_open, max_remembered=max_remembered)


# --------------------------------------------------------------------------
# T002 — one goal at a time, and the refusal names the one that is running
# --------------------------------------------------------------------------


class TestT002OneGoalAtATime:
    def test_a_second_activation_is_refused_and_names_the_active_goal(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        running = start_one(queue, training("first"))
        admit(queue, hunger("second"))

        blocked = queue.activate_next()

        assert blocked.accepted is False
        assert blocked.refusal is not None
        assert blocked.refusal.reason_code is ReasonCode.QUEUE_REJECTED
        # Named twice over: in the structured field a caller can act on, and in
        # the sentence a user hears.
        assert blocked.refusal.active_goal_id == running.goal_id
        assert running.goal_id in blocked.refusal.message
        assert "train_skill" in blocked.refusal.message

    def test_the_named_goal_is_the_one_the_user_can_then_cancel(self) -> None:
        # "Busy" is useless; "busy with X" is only useful if X is a handle. This
        # takes the id straight out of the refusal and stops that goal with it.
        queue = GoalQueue(clock=ManualClock())
        running = start_one(queue, hunger("first"))
        admit(queue, hunger("second"))
        blocked = queue.activate_next()
        assert blocked.refusal is not None

        assert queue.request_cancel(blocked.refusal.active_goal_id) is True
        transitions = queue.tick()

        assert [(t.goal_id, t.state) for t in transitions] == [
            (running.goal_id, GoalState.CANCELLED)
        ]

    def test_the_refusal_changes_nothing(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        running = start_one(queue, hunger("first"))
        waiting = admit(queue, hunger("second"))

        queue.activate_next()

        assert queue.active is not None
        assert queue.active.goal_id == running.goal_id
        assert [r.goal_id for r in queue.pending] == [waiting.goal_id]
        assert queue.open_count == 2

    def test_at_most_one_goal_is_active_at_any_point(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=4)
        goal_ids = [
            admit(queue, hunger(f"g-{index}", budget=PATIENT)).goal_id for index in range(4)
        ]

        for _ in range(4):
            started = activate(queue)
            active = [gid for gid in goal_ids if states(queue, [gid]) == [GoalState.ACTIVE]]
            assert active == [started.goal_id]
            assert queue.activate_next().refusal is not None
            queue.succeed(started.goal_id, observed())
        assert states(queue, goal_ids) == [GoalState.SUCCEEDED] * 4

    def test_the_backlog_is_served_in_submission_order(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=3)
        submitted = [admit(queue, hunger(key, budget=PATIENT)).goal_id for key in ("a", "b", "c")]

        served = []
        while queue.pending:
            started = activate(queue)
            served.append(started.goal_id)
            queue.fail(started.goal_id, ReasonCode.NO_SAFE_FOOD)
        assert served == submitted

    def test_activating_an_empty_channel_is_refused_with_a_reason(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        empty = queue.activate_next()
        assert empty.refusal is not None
        assert empty.refusal.reason_code is ReasonCode.PRECONDITION_FAILED
        assert empty.refusal.active_goal_id == ""


# --------------------------------------------------------------------------
# T003 — everything ends
# --------------------------------------------------------------------------


def cancel_and_tick(queue: GoalQueue, record: GoalRecord) -> None:
    queue.request_cancel(record.goal_id)
    queue.tick()


def run_out_of_steps(queue: GoalQueue, record: GoalRecord) -> None:
    for _ in range(record.budget.max_steps):
        queue.note_step(record.goal_id)


def run_out_of_time(queue: GoalQueue, record: GoalRecord, clock: ManualClock) -> None:
    clock.set(1_000_000)
    queue.tick()


class TestT003EverythingEnds:
    @pytest.mark.parametrize(
        ("ending", "expected_state", "expected_reason"),
        [
            ("succeed", GoalState.SUCCEEDED, ReasonCode.POSTCONDITION_MET),
            ("fail", GoalState.FAILED, ReasonCode.NO_SAFE_FOOD),
            ("cancel", GoalState.CANCELLED, ReasonCode.CANCELLED_BY_REQUEST),
            ("steps", GoalState.EXPIRED, ReasonCode.NO_PROGRESS),
            ("time", GoalState.EXPIRED, ReasonCode.ACTION_TIMEOUT),
            ("disarm", GoalState.CANCELLED, ReasonCode.NOT_ARMED),
            ("panic", GoalState.CANCELLED, ReasonCode.PANIC_STOP),
        ],
    )
    def test_each_way_a_goal_can_end_actually_ends_it(
        self, ending: str, expected_state: GoalState, expected_reason: ReasonCode
    ) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        record = start_one(queue, hunger("k", budget=SHORT))

        if ending == "succeed":
            queue.succeed(record.goal_id, observed())
        elif ending == "fail":
            queue.fail(record.goal_id, ReasonCode.NO_SAFE_FOOD)
        elif ending == "cancel":
            cancel_and_tick(queue, record)
        elif ending == "steps":
            run_out_of_steps(queue, record)
        elif ending == "time":
            run_out_of_time(queue, record, clock)
        elif ending == "disarm":
            queue.disarm()
        else:
            queue.panic_stop()

        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is expected_state
        assert ended.reason_code is expected_reason
        assert ended.finished_at_ms is not None
        assert_nothing_in_flight(queue, [record.goal_id])

    def test_a_goal_nobody_ever_activates_still_ends(self) -> None:
        # The case that makes "everything ends" a property of the *channel*
        # rather than of a diligent caller: a goal is admitted and then nothing
        # ever happens to it.
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=3)
        goal_ids = [admit(queue, hunger(key, budget=SHORT)).goal_id for key in ("a", "b", "c")]

        clock.set(3_999)
        assert queue.tick() == (), "the time to live had not run out yet"
        assert states(queue, goal_ids) == [GoalState.PENDING] * 3

        clock.set(4_000)
        transitions = queue.tick()

        assert [t.state for t in transitions] == [GoalState.EXPIRED] * 3
        assert {t.reason_code for t in transitions} == {ReasonCode.ACTION_TIMEOUT}
        assert_nothing_in_flight(queue, goal_ids)

    def test_a_goal_nobody_ever_steps_still_ends(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        record = start_one(queue, hunger("k", budget=SHORT))

        clock.set(2_000)
        transitions = queue.tick()

        assert [t.state for t in transitions] == [GoalState.EXPIRED]
        assert transitions[0].previous is GoalState.ACTIVE
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.steps_used == 0, "it ended without a single step being charged"
        assert_nothing_in_flight(queue, [record.goal_id])

    def test_a_full_channel_empties_within_a_bounded_number_of_ticks(self) -> None:
        # Nothing here calls succeed, fail, cancel or panic. The only thing that
        # happens is time passing and the caller ticking, and that has to be
        # enough for every goal to reach a terminal state.
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=4)
        goal_ids = [admit(queue, hunger(f"g-{i}", budget=SHORT)).goal_id for i in range(4)]
        activate(queue)

        ticks = 0
        while queue.open_count and ticks < 10:
            clock.advance(SHORT.pending_ttl_ms + SHORT.max_wall_ms)
            queue.tick()
            ticks += 1

        assert ticks == 1, "one tick past every bound should have emptied the channel"
        assert_nothing_in_flight(queue, goal_ids)

    def test_a_terminal_goal_cannot_come_back(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        record = start_one(queue, hunger("k", budget=PATIENT))
        queue.fail(record.goal_id, ReasonCode.NO_SAFE_FOOD, "nothing edible")

        # Every late arrival an executor can produce, after the goal ended.
        assert queue.note_step(record.goal_id) is None
        assert queue.succeed(record.goal_id, observed()) is None
        assert queue.fail(record.goal_id, ReasonCode.THREAT_INTERRUPTED) is None
        assert queue.request_cancel(record.goal_id) is False
        assert queue.tick() == ()

        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.NO_SAFE_FOOD
        assert ended.detail == "nothing edible"

    def test_an_unknown_goal_id_is_a_lookup_error_that_does_not_echo_it(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        poison = "C:/Users/bob/AppData/Roaming/token"
        with pytest.raises(UnknownGoalError) as caught:
            queue.note_step(poison)
        assert poison not in str(caught.value)
        assert "Users" not in str(caught.value)


# --------------------------------------------------------------------------
# T004 — the wall clock
# --------------------------------------------------------------------------


class TestT004TheWallClockDeadline:
    def test_a_goal_past_its_deadline_ends_as_expired(self) -> None:
        # Arithmetic written out: activated at 5_000 with 2_000 ms of budget is
        # dead at 7_000. Reading the number back off the record instead would
        # agree with a deadline computed any way at all.
        clock = ManualClock(start=5_000)
        queue = GoalQueue(clock=clock)
        record = start_one(queue, hunger("k", budget=SHORT))
        assert record.started_at_ms == 5_000

        clock.set(6_999)
        assert queue.tick() == ()
        assert queue.active is not None

        clock.set(7_000)
        transitions = queue.tick()

        assert len(transitions) == 1
        assert transitions[0].previous is GoalState.ACTIVE
        assert transitions[0].state is GoalState.EXPIRED
        assert transitions[0].reason_code is ReasonCode.ACTION_TIMEOUT
        assert transitions[0].at_ms == 7_000
        assert "2000 ms" in transitions[0].detail

    def test_the_deadline_runs_from_activation_not_from_submission(self) -> None:
        # A deadline measured from submission would already have fired at 3_000
        # here, ending a goal that had run for no time at all.
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        admit(queue, hunger("k", budget=SHORT))
        clock.set(3_000)
        record = activate(queue)
        assert record.started_at_ms == 3_000

        clock.set(4_999)
        assert queue.tick() == ()
        assert queue.active is not None

        clock.set(5_000)
        assert [t.state for t in queue.tick()] == [GoalState.EXPIRED]

    def test_expiry_is_recorded_on_the_goal_not_only_announced(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        record = start_one(queue, hunger("k", budget=SHORT))

        clock.set(2_000)
        queue.tick()

        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.state is GoalState.EXPIRED
        assert ended.reason_code is ReasonCode.ACTION_TIMEOUT
        assert ended.finished_at_ms == 2_000
        assert ended.evidence_keys == (), "an expiry observed nothing"

    def test_an_expiry_frees_the_channel_for_the_next_goal(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=2)
        start_one(queue, hunger("first", budget=SHORT))
        waiting = admit(queue, hunger("second", budget=PATIENT))

        clock.set(2_000)
        queue.tick()

        assert queue.active is None
        assert queue.open_count == 1
        assert activate(queue).goal_id == waiting.goal_id

    def test_only_the_active_goal_is_measured_against_the_wall_clock(self) -> None:
        # A pending goal has no deadline to be past — it has a time to live,
        # which is a different and longer bound.
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=2)
        start_one(queue, hunger("running", budget=SHORT))
        waiting = admit(queue, hunger("waiting", budget=SHORT))
        assert waiting.deadline_ms is None

        clock.set(2_000)
        transitions = queue.tick()

        assert [t.goal_id for t in transitions] != [waiting.goal_id]
        assert len(transitions) == 1
        assert states(queue, [waiting.goal_id]) == [GoalState.PENDING]


# --------------------------------------------------------------------------
# T005 — the step budget
# --------------------------------------------------------------------------


class TestT005TheStepBudget:
    def test_a_goal_past_its_step_budget_ends_exhausted(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        record = start_one(queue, hunger("k", budget=PATIENT))

        assert queue.note_step(record.goal_id) is None
        assert queue.note_step(record.goal_id) is None
        exhausted = queue.note_step(record.goal_id)

        assert exhausted is not None
        assert exhausted.previous is GoalState.ACTIVE
        assert exhausted.state is GoalState.EXPIRED
        assert exhausted.reason_code is ReasonCode.NO_PROGRESS
        assert "all 3 of its steps" in exhausted.detail
        # The clock never moved, so nothing but the steps can have ended it.
        assert clock.now == 0
        assert_nothing_in_flight(queue, [record.goal_id])

    def test_exhaustion_is_reported_as_no_progress_not_as_a_timeout(self) -> None:
        # The two expiries share a state and must not share a reason: "it kept
        # trying and got nowhere" is not "it ran out of clock". Both are built
        # here so the comparison is between two goals, not against a constant.
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=2)

        by_steps = start_one(queue, hunger("steps", budget=PATIENT))
        run_out_of_steps(queue, by_steps)
        by_clock = start_one(queue, hunger("clock", budget=SHORT))
        clock.set(2_000)
        queue.tick()

        ended_steps = queue.record(by_steps.goal_id)
        ended_clock = queue.record(by_clock.goal_id)
        assert ended_steps is not None
        assert ended_clock is not None
        assert ended_steps.state is GoalState.EXPIRED
        assert ended_clock.state is GoalState.EXPIRED
        assert (ended_steps.reason_code, ended_clock.reason_code) == (
            ReasonCode.NO_PROGRESS,
            ReasonCode.ACTION_TIMEOUT,
        )

    def test_steps_are_charged_one_at_a_time(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        record = start_one(queue, hunger("k", budget=PATIENT))

        charged = []
        for _ in range(3):
            queue.note_step(record.goal_id)
            current = queue.record(record.goal_id)
            assert current is not None
            charged.append((current.steps_used, current.steps_left))
        assert charged == [(1, 2), (2, 1), (3, 0)]

    def test_a_one_step_budget_ends_on_its_first_step(self) -> None:
        tight = GoalBudget(max_wall_ms=900_000, max_steps=1, pending_ttl_ms=600_000)
        queue = GoalQueue(clock=ManualClock())
        record = start_one(queue, hunger("k", budget=tight))

        transition = queue.note_step(record.goal_id)

        assert transition is not None
        assert transition.state is GoalState.EXPIRED
        assert "all 1 of its steps" in transition.detail

    def test_a_step_cannot_be_charged_before_the_goal_is_active(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        waiting = admit(queue, hunger("k", budget=PATIENT))
        with pytest.raises(ValueError, match="only an active goal spends steps"):
            queue.note_step(waiting.goal_id)
        assert states(queue, [waiting.goal_id]) == [GoalState.PENDING]

    def test_exhaustion_frees_the_channel_for_the_next_goal(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=2)
        record = start_one(queue, hunger("first", budget=PATIENT))
        waiting = admit(queue, hunger("second", budget=PATIENT))

        run_out_of_steps(queue, record)

        assert queue.active is None
        assert activate(queue).goal_id == waiting.goal_id


# --------------------------------------------------------------------------
# T006 — cancel is observed within one tick
# --------------------------------------------------------------------------


class TestT006Cancel:
    def test_the_first_tick_after_a_cancel_ends_the_goal(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        record = start_one(queue, hunger("k", budget=PATIENT))

        assert queue.request_cancel(record.goal_id) is True
        assert queue.active is not None, "the tick is the observation point"

        first = queue.tick()

        assert [(t.goal_id, t.previous, t.state) for t in first] == [
            (record.goal_id, GoalState.ACTIVE, GoalState.CANCELLED)
        ]
        assert first[0].reason_code is ReasonCode.CANCELLED_BY_REQUEST
        assert queue.tick() == (), "the second tick has nothing left to report"
        assert_nothing_in_flight(queue, [record.goal_id])

    def test_the_cancel_does_not_wait_for_the_step_budget(self) -> None:
        # A step is in flight and two more are paid for. The cancel must not
        # wait for any of them.
        queue = GoalQueue(clock=ManualClock())
        record = start_one(queue, hunger("k", budget=PATIENT))
        assert queue.note_step(record.goal_id) is None

        queue.request_cancel(record.goal_id)
        transitions = queue.tick()

        assert [t.state for t in transitions] == [GoalState.CANCELLED]
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.steps_left == 2, "it still had budget when the user stopped it"

    def test_a_step_landing_after_the_cancel_ends_the_goal_as_cancelled(self) -> None:
        # Regression. The step exhausted the budget and the goal was reported
        # EXPIRED/NO_PROGRESS, which named the wrong cause and swallowed the
        # pending cancellation so that no tick ever reported it.
        tight = GoalBudget(max_wall_ms=900_000, max_steps=1, pending_ttl_ms=600_000)
        queue = GoalQueue(clock=ManualClock())
        record = start_one(queue, hunger("k", budget=tight))
        queue.request_cancel(record.goal_id)

        landed = queue.note_step(record.goal_id)

        assert landed is not None
        assert landed.state is GoalState.CANCELLED
        assert landed.reason_code is ReasonCode.CANCELLED_BY_REQUEST
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.reason_code is ReasonCode.CANCELLED_BY_REQUEST
        assert ended.steps_used == 1, "the step was spent, so it is charged"
        assert_nothing_in_flight(queue, [record.goal_id])

    def test_a_cancelled_goal_is_never_started(self) -> None:
        # Regression. activate_next promoted the goal to ACTIVE, so the executor
        # would have dispatched a step for work the user had already withdrawn.
        queue = GoalQueue(clock=ManualClock())
        record = admit(queue, hunger("k", budget=PATIENT))
        assert queue.request_cancel(record.goal_id) is True

        refused = queue.activate_next()

        assert refused.goal is None
        assert refused.refusal is not None
        assert refused.refusal.reason_code is ReasonCode.CANCELLED_BY_REQUEST
        assert queue.active is None
        assert states(queue, [record.goal_id]) == [GoalState.PENDING]
        # And it still ends, on the tick that was always going to report it.
        assert [t.state for t in queue.tick()] == [GoalState.CANCELLED]

    def test_a_cancelled_goal_is_skipped_and_the_one_behind_it_runs(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=2)
        first = admit(queue, hunger("first", budget=PATIENT))
        second = admit(queue, hunger("second", budget=PATIENT))
        queue.request_cancel(first.goal_id)

        started = activate(queue)

        assert started.goal_id == second.goal_id
        assert states(queue, [first.goal_id]) == [GoalState.PENDING]
        assert [t.goal_id for t in queue.tick()] == [first.goal_id]

    def test_a_pending_goal_can_be_cancelled_too(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        record = admit(queue, hunger("k", budget=PATIENT))
        assert queue.request_cancel(record.goal_id) is True

        transitions = queue.tick()

        assert [(t.previous, t.state) for t in transitions] == [
            (GoalState.PENDING, GoalState.CANCELLED)
        ]

    def test_a_cancel_outranks_an_expiry_that_fell_in_the_same_tick(self) -> None:
        # User input outranks a deadline: the goal is reported cancelled, once.
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        record = start_one(queue, hunger("k", budget=SHORT))
        queue.request_cancel(record.goal_id)

        clock.set(50_000)
        transitions = queue.tick()

        assert len(transitions) == 1
        assert transitions[0].state is GoalState.CANCELLED
        assert transitions[0].reason_code is ReasonCode.CANCELLED_BY_REQUEST

    def test_cancelling_a_goal_that_already_ended_says_there_was_nothing_to_do(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        record = start_one(queue, hunger("k", budget=PATIENT))
        queue.succeed(record.goal_id, observed())

        assert queue.request_cancel(record.goal_id) is False
        assert queue.tick() == ()
        assert states(queue, [record.goal_id]) == [GoalState.SUCCEEDED]


# --------------------------------------------------------------------------
# T007 — disarming the session
# --------------------------------------------------------------------------


class TestT007Disarm:
    def test_disarming_terminates_the_active_goal(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        record = start_one(queue, hunger("k", budget=PATIENT))
        armed_before = queue.armed

        transitions = queue.disarm()

        assert armed_before is True, "a fresh channel is armed"
        assert [(t.goal_id, t.previous, t.state) for t in transitions] == [
            (record.goal_id, GoalState.ACTIVE, GoalState.CANCELLED)
        ]
        assert transitions[0].reason_code is ReasonCode.NOT_ARMED
        assert queue.armed is False
        assert queue.active is None
        assert_nothing_in_flight(queue, [record.goal_id])

    def test_a_disarmed_channel_refuses_to_activate_and_says_why(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=2)
        start_one(queue, hunger("running", budget=PATIENT))
        admit(queue, hunger("waiting", budget=PATIENT))
        queue.disarm()

        blocked = queue.activate_next()

        assert blocked.goal is None
        assert blocked.refusal is not None
        assert blocked.refusal.reason_code is ReasonCode.NOT_ARMED
        assert "arm the session" in blocked.refusal.message
        assert queue.active is None

    def test_the_backlog_survives_a_disarm_but_still_ends(self) -> None:
        # Disarming says what the agent may do, not what the user wanted. The
        # backlog is kept — and a session that never comes back must not leave
        # those goals in flight for ever.
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=2)
        start_one(queue, hunger("running", budget=SHORT))
        waiting = admit(queue, hunger("waiting", budget=SHORT))

        queue.disarm()
        assert [r.goal_id for r in queue.pending] == [waiting.goal_id]

        clock.set(4_000)
        assert [t.state for t in queue.tick()] == [GoalState.EXPIRED]
        assert_nothing_in_flight(queue, [waiting.goal_id])

    def test_rearming_lets_the_backlog_resume(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=2)
        start_one(queue, hunger("running", budget=PATIENT))
        waiting = admit(queue, hunger("waiting", budget=PATIENT))
        queue.disarm()

        queue.arm()

        assert queue.armed is True
        assert activate(queue).goal_id == waiting.goal_id

    def test_disarming_an_idle_channel_reports_nothing_but_still_disarms(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        assert queue.disarm() == ()
        assert queue.armed is False
        assert queue.disarm() == (), "disarming twice ends nothing twice"

    def test_a_disarm_reports_the_goal_it_ended_exactly_once(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        start_one(queue, hunger("k", budget=PATIENT))

        assert len(queue.disarm()) == 1
        assert queue.disarm() == ()
        assert queue.tick() == ()


# --------------------------------------------------------------------------
# T008 — the panic stop
# --------------------------------------------------------------------------


class TestT008PanicStop:
    def test_a_panic_stop_leaves_no_goal_in_flight(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=4)
        goal_ids = [admit(queue, hunger(f"g-{i}", budget=PATIENT)).goal_id for i in range(4)]
        activate(queue)

        transitions = queue.panic_stop()

        assert len(transitions) == 4
        assert {t.state for t in transitions} == {GoalState.CANCELLED}
        assert {t.reason_code for t in transitions} == {ReasonCode.PANIC_STOP}
        assert queue.armed is False
        assert_nothing_in_flight(queue, goal_ids)

    def test_every_goal_is_reported_exactly_once_and_in_order(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=3)
        goal_ids = [admit(queue, hunger(key, budget=PATIENT)).goal_id for key in ("a", "b", "c")]
        activate(queue)

        reported = [t.goal_id for t in queue.panic_stop()]

        assert reported == goal_ids
        assert len(set(reported)) == 3
        assert queue.panic_stop() == ()

    def test_the_active_goal_and_the_backlog_go_together(self) -> None:
        queue = GoalQueue(clock=ManualClock(), max_open=3)
        running = start_one(queue, hunger("running", budget=PATIENT))
        waiting = [admit(queue, hunger(key, budget=PATIENT)).goal_id for key in ("b", "c")]

        transitions = queue.panic_stop()

        by_previous = {t.goal_id: t.previous for t in transitions}
        assert by_previous[running.goal_id] is GoalState.ACTIVE
        assert [by_previous[goal_id] for goal_id in waiting] == [
            GoalState.PENDING,
            GoalState.PENDING,
        ]

    def test_a_panic_stop_discards_a_cancel_that_had_not_been_applied(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        record = start_one(queue, hunger("k", budget=PATIENT))
        queue.request_cancel(record.goal_id)

        assert len(queue.panic_stop()) == 1

        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.reason_code is ReasonCode.PANIC_STOP
        assert queue.tick() == (), "no cancellation was left waiting to fire"

    def test_nothing_starts_again_until_the_channel_is_armed(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        start_one(queue, hunger("before", budget=PATIENT))
        queue.panic_stop()

        admit(queue, hunger("after", budget=PATIENT))
        blocked = queue.activate_next()

        assert blocked.refusal is not None
        assert blocked.refusal.reason_code is ReasonCode.NOT_ARMED
        assert queue.active is None

    def test_a_panic_stop_on_an_idle_channel_is_quiet_and_still_disarms(self) -> None:
        queue = GoalQueue(clock=ManualClock())
        assert queue.panic_stop() == ()
        assert queue.armed is False


# --------------------------------------------------------------------------
# the clock is injected, and nothing here sleeps
# --------------------------------------------------------------------------


class TestTheInjectedClock:
    def test_every_timestamp_comes_from_the_injected_clock(self) -> None:
        clock = ManualClock(start=1_700_000_000_000)
        queue = GoalQueue(clock=clock)

        submitted = admit(queue, hunger("k", budget=PATIENT))
        assert submitted.submitted_at_ms == 1_700_000_000_000

        clock.set(1_700_000_005_000)
        started = activate(queue)
        assert started.started_at_ms == 1_700_000_005_000

        clock.set(1_700_000_009_000)
        transition = queue.fail(started.goal_id, ReasonCode.THREAT_INTERRUPTED)
        assert transition is not None
        assert transition.at_ms == 1_700_000_009_000
        assert clock.reads > 0, "the queue must read the clock it was given"

    def test_a_clock_that_steps_backwards_cannot_move_a_deadline_away(self) -> None:
        # Wall clocks are shared with the game, so a time sync can step this one
        # back. A deadline compared against a rewound clock would grow.
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        record = start_one(queue, hunger("k", budget=SHORT))

        clock.set(2_000)
        clock.set(500)  # the step back arrives before the queue looks
        assert queue.tick() == ()

        clock.set(2_000)
        assert [t.state for t in queue.tick()] == [GoalState.EXPIRED]
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.finished_at_ms == 2_000

    def test_a_backward_step_cannot_make_a_goal_finish_before_it_started(self) -> None:
        clock = ManualClock(start=10_000)
        queue = GoalQueue(clock=clock)
        record = start_one(queue, hunger("k", budget=PATIENT))

        clock.set(400)
        transition = queue.fail(record.goal_id, ReasonCode.NO_SAFE_FOOD)

        assert isinstance(transition, GoalTransition)
        assert transition.at_ms == 10_000
        ended = queue.record(record.goal_id)
        assert ended is not None
        assert ended.finished_at_ms == 10_000


# --------------------------------------------------------------------------
# T009 — a goal can step aside and come back
# --------------------------------------------------------------------------


def suspend_ok(
    queue: GoalQueue, goal_id: str, *, by: str = "preemptor", now_ms: int, reason: str = "a threat"
) -> GoalRecord:
    """Suspend *goal_id*, asserting the queue accepted it, and return the record."""
    parked = queue.suspend(goal_id, by_goal_id=by, reason=reason, now_ms=now_ms)
    assert parked.goal is not None, parked.refusal
    return parked.goal


class TestT009SuspendAndResume:
    def test_suspension_parks_the_active_goal_at_the_front_with_a_marker(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=3)
        running = start_one(queue, hunger("original", budget=PATIENT))
        behind = admit(queue, hunger("backlog", budget=PATIENT))

        parked = suspend_ok(queue, running.goal_id, by="preemptor-id", now_ms=clock.now)

        # PENDING again — the closed enum stays closed — with the marker on the
        # record saying who it stepped aside for, ahead of the whole backlog.
        assert parked.state is GoalState.PENDING
        assert parked.suspended_by == "preemptor-id"
        assert parked.suspensions == 1
        assert parked.started_at_ms is None
        assert queue.active is None
        assert [r.goal_id for r in queue.pending] == [running.goal_id, behind.goal_id]
        # Suspension freed no slot: the parked goal is still open.
        assert queue.open_count == 2

    def test_the_wall_clock_stops_while_suspended(self) -> None:
        # Arithmetic written out. Started at 5_000 with 2_000 ms of budget;
        # suspended at 5_500 having burned 500. Resumed at 100_000, so the
        # remaining 1_500 ms run out at 101_500 — not at 7_000, which is where
        # a deadline that kept burning through the suspension would have died.
        clock = ManualClock(start=5_000)
        queue = GoalQueue(clock=clock)
        running = start_one(queue, hunger("k", budget=SHORT))
        assert running.started_at_ms == 5_000

        clock.set(5_500)
        parked = suspend_ok(queue, running.goal_id, now_ms=5_500)
        assert parked.active_ms_before_suspend == 500

        clock.set(100_000)
        assert queue.tick() == (), "nothing expires while the goal waits"
        resumed = activate(queue)
        assert resumed.goal_id == running.goal_id
        assert resumed.started_at_ms == 100_000
        assert resumed.deadline_ms == 101_500

        clock.set(101_499)
        assert queue.tick() == ()
        clock.set(101_500)
        transitions = queue.tick()
        assert [(t.goal_id, t.state) for t in transitions] == [(running.goal_id, GoalState.EXPIRED)]

    def test_resuming_through_the_ordinary_activation_clears_the_marker(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        running = start_one(queue, hunger("k", budget=PATIENT))
        suspend_ok(queue, running.goal_id, now_ms=clock.now, reason="threat within reach")

        resumed = activate(queue)

        assert resumed.goal_id == running.goal_id
        assert resumed.state is GoalState.ACTIVE
        assert resumed.suspended_by is None
        assert resumed.front_rank == 0
        assert resumed.detail == "", "the parked reason is no longer the truth"
        assert resumed.suspensions == 1, "the count is history and survives"

    def test_the_parked_reason_is_readable_while_parked(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        running = start_one(queue, hunger("k", budget=PATIENT))

        suspend_ok(queue, running.goal_id, now_ms=clock.now, reason="threat within reach")

        parked = queue.record(running.goal_id)
        assert parked is not None
        assert parked.detail == "threat within reach"

    def test_steps_carry_across_a_suspension(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        running = start_one(queue, hunger("k", budget=PATIENT))  # 3 steps
        assert queue.note_step(running.goal_id) is None

        suspend_ok(queue, running.goal_id, now_ms=clock.now)
        resumed = activate(queue)

        assert resumed.steps_used == 1, "a step spent before the suspension stays spent"
        assert queue.note_step(running.goal_id) is None
        exhausted = queue.note_step(running.goal_id)
        assert exhausted is not None
        assert exhausted.state is GoalState.EXPIRED
        assert exhausted.reason_code is ReasonCode.NO_PROGRESS

    def test_the_pending_ttl_does_not_apply_to_a_suspended_goal(self) -> None:
        # Both goals share the SHORT budget, whose time to live is 4_000 ms
        # from submission at 0. At 50_000 the ordinary pending goal is long
        # dead — and the suspended one is not, because it already started, and
        # expiring it for having been preempted would punish the interruption.
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=2)
        running = start_one(queue, hunger("preempted", budget=SHORT))
        waiting = admit(queue, hunger("ordinary", budget=SHORT))
        suspend_ok(queue, running.goal_id, now_ms=clock.now)

        clock.set(50_000)
        transitions = queue.tick()

        assert [(t.goal_id, t.state) for t in transitions] == [(waiting.goal_id, GoalState.EXPIRED)]
        assert states(queue, [running.goal_id]) == [GoalState.PENDING]
        # And it still resumes, with its stopped clock intact.
        assert activate(queue).goal_id == running.goal_id

    def test_the_fourth_suspension_is_refused_and_names_the_cap(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        running = start_one(queue, hunger("k", budget=PATIENT))
        for _ in range(3):
            suspend_ok(queue, running.goal_id, now_ms=clock.now)
            assert activate(queue).goal_id == running.goal_id

        refused = queue.suspend(
            running.goal_id, by_goal_id="preemptor", reason="again", now_ms=clock.now
        )

        assert refused.goal is None
        assert refused.refusal is not None
        assert refused.refusal.reason_code is ReasonCode.QUEUE_REJECTED
        assert refused.refusal.active_goal_id == running.goal_id
        assert "3 times" in refused.refusal.message
        assert "let it finish" in refused.refusal.message
        # The refusal changed nothing: the goal is still running.
        assert queue.active is not None
        assert queue.active.goal_id == running.goal_id
        assert queue.active.suspensions == 3

    def test_suspending_what_is_not_running_is_refused_typed(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=2)
        waiting = admit(queue, hunger("waiting", budget=PATIENT))

        pending_refusal = queue.suspend(
            waiting.goal_id, by_goal_id="p", reason="r", now_ms=clock.now
        )
        assert pending_refusal.refusal is not None
        assert pending_refusal.refusal.reason_code is ReasonCode.PRECONDITION_FAILED

        started = activate(queue)
        queue.succeed(started.goal_id, observed())
        ended_refusal = queue.suspend(started.goal_id, by_goal_id="p", reason="r", now_ms=clock.now)
        assert ended_refusal.refusal is not None
        assert ended_refusal.refusal.reason_code is ReasonCode.PRECONDITION_FAILED

        with pytest.raises(UnknownGoalError):
            queue.suspend("never-minted", by_goal_id="p", reason="r", now_ms=clock.now)

    def test_a_goal_the_user_already_cancelled_is_not_suspended(self) -> None:
        # User input outranks preemption: the next tick ends this goal, and
        # parking it would only delay what the user asked for.
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        running = start_one(queue, hunger("k", budget=PATIENT))
        assert queue.request_cancel(running.goal_id) is True

        refused = queue.suspend(running.goal_id, by_goal_id="p", reason="r", now_ms=clock.now)

        assert refused.refusal is not None
        assert refused.refusal.reason_code is ReasonCode.CANCELLED_BY_REQUEST
        assert [t.state for t in queue.tick()] == [GoalState.CANCELLED]

    def test_cancelling_a_suspended_goal_ends_it_and_drops_the_marker(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        running = start_one(queue, hunger("k", budget=PATIENT))
        suspend_ok(queue, running.goal_id, now_ms=clock.now)

        assert queue.request_cancel(running.goal_id) is True
        transitions = queue.tick()

        assert [(t.goal_id, t.previous, t.state) for t in transitions] == [
            (running.goal_id, GoalState.PENDING, GoalState.CANCELLED)
        ]
        ended = queue.record(running.goal_id)
        assert ended is not None
        assert ended.suspended_by is None, (
            "a terminal record answers why it ended, not who paused it"
        )
        assert ended.suspensions == 1, "the count is history and survives the end"
        assert_nothing_in_flight(queue, [running.goal_id])

    def test_a_panic_stop_clears_a_suspended_goal_like_any_open_goal(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        running = start_one(queue, hunger("k", budget=PATIENT))
        suspend_ok(queue, running.goal_id, now_ms=clock.now)

        transitions = queue.panic_stop()

        assert [(t.goal_id, t.state) for t in transitions] == [
            (running.goal_id, GoalState.CANCELLED)
        ]
        assert_nothing_in_flight(queue, [running.goal_id])

    def test_a_suspension_at_the_deadline_banks_the_whole_budget_and_no_more(self) -> None:
        # Suspended exactly when its wall clock ran out (the tick that would
        # have expired it had not landed yet): the bank clamps at the budget,
        # so it resumes only to expire honestly on the next tick.
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        running = start_one(queue, hunger("k", budget=SHORT))

        clock.set(9_000)  # 7_000 past a 2_000 ms budget
        parked = suspend_ok(queue, running.goal_id, now_ms=9_000)

        assert parked.active_ms_before_suspend == SHORT.max_wall_ms
        resumed = activate(queue)
        assert resumed.deadline_ms == clock.now, "no budget is left; the deadline is now"
        assert [t.state for t in queue.tick()] == [GoalState.EXPIRED]


class TestT010PriorityInjection:
    def test_the_injected_goal_activates_next_and_evicts_nothing(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=4)
        original = start_one(queue, hunger("original", budget=PATIENT))
        backlog = admit(queue, hunger("backlog", budget=PATIENT))
        suspend_ok(queue, original.goal_id, by="will-be-preemptor", now_ms=clock.now)

        injected = queue.submit_front(hunger("preemptor", budget=PATIENT), now_ms=clock.now)

        assert injected.goal is not None, injected.refusal
        # Ahead of everything — including the goal it displaced — and nothing
        # was evicted to make room.
        assert [r.goal_id for r in queue.pending] == [
            injected.goal.goal_id,
            original.goal_id,
            backlog.goal_id,
        ]
        assert queue.open_count == 3
        assert activate(queue).goal_id == injected.goal.goal_id

    def test_the_original_resumes_after_the_preemptor_finishes(self) -> None:
        # The directive's whole sentence in one test: прервать текущую цель,
        # выполнить срочную, продолжить исходную.
        clock = ManualClock()
        queue = GoalQueue(clock=clock)
        original = start_one(queue, hunger("original", budget=PATIENT))
        suspend_ok(queue, original.goal_id, now_ms=clock.now)
        injected = queue.submit_front(hunger("preemptor", budget=PATIENT), now_ms=clock.now)
        assert injected.goal is not None
        assert activate(queue).goal_id == injected.goal.goal_id

        queue.succeed(injected.goal.goal_id, observed())
        resumed = activate(queue)

        assert resumed.goal_id == original.goal_id
        assert resumed.suspended_by is None

    def test_a_full_channel_refuses_the_injection_rather_than_evicting(self) -> None:
        # The pinned arithmetic: the suspended goal still counts as open, so
        # injection needs one free slot of its own. max_open=2 with an active
        # goal and a pending one is full; suspension does not change the count,
        # and the injection is refused with the ordinary over-cap reason.
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=2)
        original = start_one(queue, hunger("original", budget=PATIENT))
        backlog = admit(queue, hunger("backlog", budget=PATIENT))
        assert queue.open_count == 2
        suspend_ok(queue, original.goal_id, now_ms=clock.now)
        assert queue.open_count == 2, "suspension frees no slot"

        refused = queue.submit_front(hunger("preemptor", budget=PATIENT), now_ms=clock.now)

        assert refused.goal is None
        assert refused.refusal is not None
        assert refused.refusal.reason_code is ReasonCode.QUEUE_REJECTED
        # Refused, not evicted: both open goals are exactly where they were.
        assert [r.goal_id for r in queue.pending] == [original.goal_id, backlog.goal_id]

    def test_with_one_free_slot_the_whole_preemption_fits(self) -> None:
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=2)
        original = start_one(queue, hunger("original", budget=PATIENT))
        assert queue.open_count == 1

        suspend_ok(queue, original.goal_id, now_ms=clock.now)
        injected = queue.submit_front(hunger("preemptor", budget=PATIENT), now_ms=clock.now)

        assert injected.goal is not None, injected.refusal
        assert queue.open_count == 2
        assert activate(queue).goal_id == injected.goal.goal_id

    def test_a_duplicate_key_is_not_repositioned_by_submit_front(self) -> None:
        # A retry through the front door resolves to the goal it already
        # names, where it already is: repositioning on a dropped
        # acknowledgement would let a retry reorder the backlog.
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=3)
        first = admit(queue, hunger("a", budget=PATIENT))
        second = admit(queue, hunger("b", budget=PATIENT))

        again = queue.submit_front(hunger("b", budget=PATIENT), now_ms=clock.now)

        assert again.duplicate is True
        assert again.goal is not None
        assert again.goal.goal_id == second.goal_id
        assert [r.goal_id for r in queue.pending] == [first.goal_id, second.goal_id]

    def test_front_ranks_stay_within_the_channel_cap_under_a_preemption_storm(self) -> None:
        # The bound the rank comment claims, exercised: a chain of preemptions
        # deep enough to fill the channel never mints a rank below -max_open,
        # because minting a new minimum needs the old minimum's goal still
        # open and open goals are capped.
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=4)
        chain = [start_one(queue, hunger("g-0", budget=PATIENT))]
        for index in range(1, 4):
            suspend_ok(queue, chain[-1].goal_id, by=f"g-{index}", now_ms=clock.now)
            injected = queue.submit_front(hunger(f"g-{index}", budget=PATIENT), now_ms=clock.now)
            assert injected.goal is not None, injected.refusal
            chain.append(injected.goal)
            assert activate(queue).goal_id == injected.goal.goal_id
        # The channel is full: three parked goals plus the running preemptor.
        assert queue.open_count == 4
        ranks = [r.front_rank for r in queue.pending]
        assert ranks == sorted(ranks), "the backlog serves most recently fronted first"
        assert all(rank >= -queue.max_open for rank in ranks)
        # A fifth preemption cannot fit and is refused, not squeezed in.
        suspended = queue.suspend(
            chain[-1].goal_id, by_goal_id="g-4", reason="storm", now_ms=clock.now
        )
        assert suspended.goal is not None
        overflow = queue.submit_front(hunger("g-4", budget=PATIENT), now_ms=clock.now)
        assert overflow.refusal is not None
        assert overflow.refusal.reason_code is ReasonCode.QUEUE_REJECTED
        assert min(r.front_rank for r in queue.pending) >= -queue.max_open

    def test_nested_preemption_resumes_innermost_first(self) -> None:
        # A preempts nothing, B preempts A, C preempts B: the unwinding order
        # is C's end -> B resumes -> B's end -> A resumes. Last suspended,
        # first resumed — a stack of interrupted work, not a queue of it.
        clock = ManualClock()
        queue = GoalQueue(clock=clock, max_open=3)
        a = start_one(queue, hunger("a", budget=PATIENT))
        suspend_ok(queue, a.goal_id, by="b", now_ms=clock.now)
        b_admission = queue.submit_front(hunger("b", budget=PATIENT), now_ms=clock.now)
        assert b_admission.goal is not None
        b = b_admission.goal
        assert activate(queue).goal_id == b.goal_id
        suspend_ok(queue, b.goal_id, by="c", now_ms=clock.now)
        c_admission = queue.submit_front(hunger("c", budget=PATIENT), now_ms=clock.now)
        assert c_admission.goal is not None
        assert activate(queue).goal_id == c_admission.goal.goal_id

        queue.succeed(c_admission.goal.goal_id, observed())
        assert activate(queue).goal_id == b.goal_id
        queue.succeed(b.goal_id, observed())
        assert activate(queue).goal_id == a.goal_id

    def test_submit_front_reads_time_like_restore_does(self) -> None:
        # now_ms is monotonised: a stale reading cannot rewind the clock, and
        # a fresh one advances it for the record's own timestamps.
        clock = ManualClock(start=10_000)
        queue = GoalQueue(clock=clock)
        start_one(queue, hunger("running", budget=PATIENT))

        injected = queue.submit_front(hunger("preemptor", budget=PATIENT), now_ms=400)

        assert injected.goal is not None
        assert injected.goal.submitted_at_ms == 10_000, "the stale reading did not rewind"
