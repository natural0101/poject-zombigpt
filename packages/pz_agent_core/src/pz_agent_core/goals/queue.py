"""The bounded goal queue: one active goal, a hard cap, and no silent drops.

The channel's shape follows from four things that must all stay true at once.

* **Nothing is dropped quietly.** A submission past the cap is a
  :class:`~.model.GoalRefusal` the caller receives and can speak aloud, never a
  goal that vanishes between the microphone and the engine. The same is true of
  a resubmission: it comes back as the goal that already exists, marked
  ``duplicate``, rather than as a second goal that would eat a second sandwich.
* **One goal at a time.** Submission fills a bounded backlog; activation is
  exclusive. A second activation is refused *by name* — the refusal carries the
  active goal's id — because "busy" without saying what with is the answer that
  makes a user pull the plug.
* **Everything ends.** A pending goal ends when its time to live runs out, an
  active goal ends when its wall clock runs out or its steps run out, and both
  of those are checked on the tick the caller was going to run anyway. There is
  no path by which a goal stays open because nobody asked about it — except one
  the caller controls entirely, which is not calling :meth:`GoalQueue.tick`.
* **The stop levers win.** :meth:`GoalQueue.request_cancel` is observed on the
  next tick, :meth:`GoalQueue.disarm` ends the active goal, and
  :meth:`GoalQueue.panic_stop` leaves nothing in flight at all — no active goal,
  no backlog, no cancel request still waiting to be applied. "Observed on the
  next tick" is a bound on when the goal *ends*, not a licence to keep serving
  it in the meantime: a cancelled goal is not started by
  :meth:`GoalQueue.activate_next`, and a step that lands against one ends it as
  cancelled rather than as whatever its budget would otherwise have said.

Two details are deliberate rather than incidental.

The clock is read through :data:`~pz_agent_core.ipc.clocks.Clock` and then
*monotonised*: ``now = max(previous_now, clock())``. It is a wall clock, which
means a time sync can move it backwards, and a deadline compared against a clock
that jumped back is a deadline that grows. The queue never lets that happen, and
never orders anything by timestamp — position in the backlog is
:attr:`~.model.GoalRecord.sequence`, an integer this queue increments. Windows
is the shipping platform and its wall clock advances in ~15.6 ms granules, so
two goals submitted in the same granule genuinely do carry the same timestamp;
FIFO has to come from somewhere else, and it comes from there.

Expiry uses ``>=`` rather than ``>``. Under a coarse clock the difference is a
whole granule of overrun on every goal, which is exactly the class of defect
that a millisecond-resolution Linux test would never show.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from typing import Final

from ..ipc.clocks import Clock
from ..protocol import ActionResult, ActionStatus, ReasonCode
from .model import (
    GoalAdmission,
    GoalRecord,
    GoalRefusal,
    GoalRequest,
    GoalState,
    GoalTransition,
    mint_goal_id,
    normalise_evidence_keys,
)

__all__ = ["DEFAULT_MAX_OPEN", "DEFAULT_MAX_REMEMBERED", "GoalQueue", "UnknownGoalError"]

#: Goals that may be open — pending plus active — at one time. Small because a
#: backlog of user goals is a backlog of stale intentions: by the time the
#: fourth is reached the world has moved.
DEFAULT_MAX_OPEN: Final = 4

#: Goals kept in the history that answers resubmissions. A finished goal has to
#: stay long enough that a retried submission returns *it* rather than starting
#: the work again, and no longer.
DEFAULT_MAX_REMEMBERED: Final = 32


class UnknownGoalError(LookupError):
    """Raised for a goal id the queue has never minted.

    A distinct type because it is a caller bug rather than a runtime condition,
    and because the message must not echo the id it was given — that id came
    from outside.
    """


class GoalQueue:
    """Admission, exclusivity and expiry for the typed goal channel."""

    def __init__(
        self,
        *,
        clock: Clock,
        max_open: int = DEFAULT_MAX_OPEN,
        max_remembered: int = DEFAULT_MAX_REMEMBERED,
        armed: bool = True,
    ) -> None:
        if max_open < 1:
            raise ValueError(f"max_open must be positive, got {max_open}")
        if max_remembered < max_open:
            raise ValueError(
                f"max_remembered must be at least max_open ({max_open}), got {max_remembered}"
            )
        self._clock = clock
        self._max_open = max_open
        self._max_remembered = max_remembered
        self._armed = armed
        self._now = 0
        self._sequence = 0
        #: Every record this queue has minted and not yet evicted, oldest first.
        #: Open goals are never evicted, which is why max_remembered >= max_open.
        self._records: OrderedDict[str, GoalRecord] = OrderedDict()
        #: key digest -> goal id. The raw idempotency key is never stored.
        self._by_digest: OrderedDict[str, str] = OrderedDict()
        #: Goal ids whose cancellation has been requested and not yet applied.
        #: Bounded by construction: only an open goal's id is ever added.
        self._cancel_requested: set[str] = set()

    # -- read-only surface -------------------------------------------------

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def max_open(self) -> int:
        return self._max_open

    @property
    def active(self) -> GoalRecord | None:
        """The goal currently being served, if any."""
        for record in self._records.values():
            if record.state is GoalState.ACTIVE:
                return record
        return None

    @property
    def pending(self) -> tuple[GoalRecord, ...]:
        """The backlog, oldest first by admission sequence."""
        waiting = [r for r in self._records.values() if r.state is GoalState.PENDING]
        return tuple(sorted(waiting, key=lambda r: r.sequence))

    @property
    def open_count(self) -> int:
        """Pending plus active. Never exceeds :attr:`max_open`."""
        return sum(1 for r in self._records.values() if r.is_open)

    def record(self, goal_id: str) -> GoalRecord | None:
        """The record for *goal_id*, or None once it has been forgotten."""
        return self._records.get(goal_id)

    # -- admission ---------------------------------------------------------

    def submit(self, request: GoalRequest) -> GoalAdmission:
        """Admit *request* to the backlog, or explain why not.

        A repeat of a key already seen returns the goal that key created, so a
        retried submission — the ordinary consequence of a dropped
        acknowledgement — is never a second goal. A key reused for *different*
        content is refused instead, because that is a caller bug and silently
        serving the first goal would hide it.
        """
        now = self._tick_clock()
        digest = request.digest
        existing_id = self._by_digest.get(digest)
        existing = self._records.get(existing_id) if existing_id is not None else None
        if existing_id is not None and existing is None:
            # The record was evicted from the bounded history while its digest
            # survived. There is nothing left to hand back, so the key starts
            # over rather than resolving to a goal this queue can no longer
            # describe.
            self._by_digest.pop(digest, None)
        if existing is not None:
            if existing.kind is request.kind and existing.params == request.params:
                self._by_digest.move_to_end(digest)
                return GoalAdmission(goal=existing, duplicate=True)
            return GoalAdmission(
                refusal=GoalRefusal(
                    reason_code=ReasonCode.INVALID_ARGUMENT,
                    message=(
                        "that idempotency key was already used for a goal with different "
                        "parameters; mint a new key for a new goal."
                    ),
                )
            )

        if self.open_count >= self._max_open:
            return GoalAdmission(
                refusal=GoalRefusal(
                    reason_code=ReasonCode.QUEUE_REJECTED,
                    message=(
                        f"the goal channel already holds {self._max_open} open goal(s), which "
                        "is its cap; cancel one or wait for one to finish before submitting "
                        "another."
                    ),
                )
            )

        record = GoalRecord(
            goal_id=mint_goal_id(),
            kind=request.kind,
            params=request.params,
            budget=request.effective_budget,
            key_digest=digest,
            sequence=self._next_sequence(),
            state=GoalState.PENDING,
            submitted_at_ms=now,
        )
        self._remember(record)
        self._by_digest[digest] = record.goal_id
        self._trim_digests()
        return GoalAdmission(goal=record)

    def activate_next(self) -> GoalAdmission:
        """Promote the oldest pending goal to active, or explain why not.

        Refused while another goal is active, and the refusal names it: a user
        who is told "busy" learns nothing, and a user who is told which goal is
        running can cancel it.

        A goal whose cancellation has been requested is skipped rather than
        promoted. Cancellation is applied by :meth:`tick`, but "applied later"
        must not mean "started in the meantime": activating a withdrawn goal is
        how the executor comes to dispatch a step for work the user already
        stopped, and that is the one outcome AGENTS.md's "user input always
        wins" exists to forbid. The goal stays pending and the next tick reports
        it cancelled.
        """
        self._tick_clock()
        if not self._armed:
            return GoalAdmission(
                refusal=GoalRefusal(
                    reason_code=ReasonCode.NOT_ARMED,
                    message=(
                        "the goal channel is disarmed; arm the session before activating a goal."
                    ),
                )
            )
        running = self.active
        if running is not None:
            return GoalAdmission(
                refusal=GoalRefusal(
                    reason_code=ReasonCode.QUEUE_REJECTED,
                    message=(
                        f"goal {running.goal_id} ({running.kind.value}) is already active; "
                        "cancel it or wait for it to reach a terminal state."
                    ),
                    active_goal_id=running.goal_id,
                )
            )
        waiting = self.pending
        if not waiting:
            return GoalAdmission(
                refusal=GoalRefusal(
                    reason_code=ReasonCode.PRECONDITION_FAILED,
                    message="no goal is waiting; submit one before activating.",
                )
            )
        startable = tuple(r for r in waiting if r.goal_id not in self._cancel_requested)
        if not startable:
            return GoalAdmission(
                refusal=GoalRefusal(
                    reason_code=ReasonCode.CANCELLED_BY_REQUEST,
                    message=(
                        "every waiting goal has been cancelled; the next tick reports them "
                        "as cancelled. Submit a new goal to activate one."
                    ),
                )
            )
        started = replace(startable[0], state=GoalState.ACTIVE, started_at_ms=self._now)
        self._remember(started)
        return GoalAdmission(goal=started)

    # -- progress ----------------------------------------------------------

    def note_step(self, goal_id: str) -> GoalTransition | None:
        """Charge one step against the active goal's budget.

        Returns the terminal transition when that was the last step, and None
        while the budget still has room — or when the goal has already ended,
        which is the ordinary outcome of a step result landing after a cancel.

        A step that lands while a cancellation is outstanding still spends the
        step, because it was spent, but ends the goal as *cancelled*. Reporting
        that goal as having run out of budget would name the wrong cause to the
        one person who knows better — the user who asked for it to stop — and
        would consume the pending cancellation without any tick ever reporting
        it. ``succeed`` and ``fail`` deliberately do not do this: they carry an
        outcome that was observed, whereas budget exhaustion is inferred.
        """
        record = self._require(goal_id)
        if not record.is_open:
            return None
        if record.state is not GoalState.ACTIVE:
            raise ValueError("only an active goal spends steps; activate it first")
        now = self._tick_clock()
        spent = replace(record, steps_used=record.steps_used + 1)
        self._remember(spent)
        if goal_id in self._cancel_requested:
            return self._terminate(
                spent,
                GoalState.CANCELLED,
                ReasonCode.CANCELLED_BY_REQUEST,
                now,
                "cancelled at the user's request",
            )
        if spent.steps_left > 0:
            return None
        return self._terminate(
            spent,
            GoalState.EXPIRED,
            ReasonCode.NO_PROGRESS,
            now,
            f"the goal spent all {spent.budget.max_steps} of its steps without its "
            "postcondition being observed",
        )

    def succeed(self, goal_id: str, result: ActionResult) -> GoalTransition | None:
        """End *goal_id* successfully, on the evidence in *result*.

        The evidence has to arrive as an
        :class:`~pz_agent_core.protocol.messages.ActionResult` rather than as a
        boolean because that is the type that already refuses to say
        ``succeeded`` without an observed postcondition. Both checks below are
        about the *raw constructor*, which — unlike
        :meth:`ActionResult.succeeded` — will happily build a success ack with
        an empty evidence mapping; this queue is a second consumer of that type
        and closes the gap on its own side rather than assuming.

        Returns None when the goal has already ended, which is what a
        postcondition observed one tick after a cancel looks like.
        """
        if result.status is not ActionStatus.SUCCEEDED:
            raise ValueError(
                "a goal succeeds only on a succeeded action result; report the failure instead"
            )
        if not result.evidence:
            raise ValueError("a goal succeeds only on observed postcondition evidence")
        record = self._require(goal_id)
        if not record.is_open:
            return None
        now = self._tick_clock()
        return self._terminate(
            record,
            GoalState.SUCCEEDED,
            ReasonCode.POSTCONDITION_MET,
            now,
            "the postcondition was observed",
            evidence_keys=normalise_evidence_keys(result.evidence),
        )

    def fail(
        self, goal_id: str, reason_code: ReasonCode, detail: str = ""
    ) -> GoalTransition | None:
        """End *goal_id* unsuccessfully."""
        if reason_code is ReasonCode.POSTCONDITION_MET:
            raise ValueError("use succeed() for a goal whose postcondition was observed")
        record = self._require(goal_id)
        if not record.is_open:
            return None
        now = self._tick_clock()
        return self._terminate(record, GoalState.FAILED, reason_code, now, detail)

    # -- stop levers -------------------------------------------------------

    def request_cancel(self, goal_id: str) -> bool:
        """Ask for *goal_id* to end. Applied by the next :meth:`tick`.

        Returns False when there is nothing to cancel — the goal has already
        ended — so a caller can tell "it will stop" from "it already had".
        """
        record = self._require(goal_id)
        if not record.is_open:
            return False
        self._cancel_requested.add(goal_id)
        return True

    def tick(self) -> tuple[GoalTransition, ...]:
        """Apply everything time and the stop levers have made true.

        Cancellation first: user input outranks every deadline, so a goal that
        was cancelled in the same window as its expiry is reported as cancelled.
        """
        now = self._tick_clock()
        transitions: list[GoalTransition] = []

        for goal_id in sorted(self._cancel_requested):
            record = self._records.get(goal_id)
            if record is None or not record.is_open:
                continue
            transitions.append(
                self._terminate(
                    record,
                    GoalState.CANCELLED,
                    ReasonCode.CANCELLED_BY_REQUEST,
                    now,
                    "cancelled at the user's request",
                )
            )
        self._cancel_requested.clear()

        running = self.active
        if running is not None:
            deadline = running.deadline_ms
            if deadline is not None and now >= deadline:
                transitions.append(
                    self._terminate(
                        running,
                        GoalState.EXPIRED,
                        ReasonCode.ACTION_TIMEOUT,
                        now,
                        f"the goal ran for its whole {running.budget.max_wall_ms} ms budget "
                        "without its postcondition being observed",
                    )
                )

        for record in self.pending:
            if now >= record.pending_expiry_ms:
                transitions.append(
                    self._terminate(
                        record,
                        GoalState.EXPIRED,
                        ReasonCode.ACTION_TIMEOUT,
                        now,
                        f"the goal waited its whole {record.budget.pending_ttl_ms} ms to live "
                        "without being activated",
                    )
                )
        return tuple(transitions)

    def arm(self) -> None:
        """Allow activation again after a disarm or a panic stop."""
        self._armed = True

    def disarm(self) -> tuple[GoalTransition, ...]:
        """End the active goal and stop activating new ones.

        The backlog survives: disarming is a statement about what the agent may
        do, not about what the user wanted. Re-arming and activating resumes it,
        and a goal nobody comes back for still expires on its time to live.
        """
        now = self._tick_clock()
        self._armed = False
        running = self.active
        if running is None:
            return ()
        return (
            self._terminate(
                running,
                GoalState.CANCELLED,
                ReasonCode.NOT_ARMED,
                now,
                "the session was disarmed while this goal was running",
            ),
        )

    def panic_stop(self) -> tuple[GoalTransition, ...]:
        """Leave nothing in flight: no active goal, no backlog, no pending cancel.

        §8.1 and §8.12 scope a panic stop to the queue the mod owns, and every
        goal in this channel is mod-owned by construction — the player cannot
        put one here. So the whole channel empties, and it disarms, which is
        what stops the next tick from starting the backlog up again.
        """
        now = self._tick_clock()
        self._armed = False
        self._cancel_requested.clear()
        open_goals = sorted(
            (r for r in self._records.values() if r.is_open), key=lambda r: r.sequence
        )
        return tuple(
            self._terminate(
                record,
                GoalState.CANCELLED,
                ReasonCode.PANIC_STOP,
                now,
                "a panic stop cleared the goal channel",
            )
            for record in open_goals
        )

    # -- internals ---------------------------------------------------------

    def _terminate(
        self,
        record: GoalRecord,
        state: GoalState,
        reason_code: ReasonCode,
        now: int,
        detail: str,
        *,
        evidence_keys: tuple[str, ...] = (),
    ) -> GoalTransition:
        ended = replace(
            record,
            state=state,
            reason_code=reason_code,
            finished_at_ms=now,
            evidence_keys=evidence_keys,
            detail=detail,
        )
        self._remember(ended)
        self._cancel_requested.discard(record.goal_id)
        return GoalTransition(
            goal_id=ended.goal_id,
            kind=ended.kind,
            previous=record.state,
            state=state,
            reason_code=reason_code,
            at_ms=now,
            detail=detail,
        )

    def _remember(self, record: GoalRecord) -> None:
        self._records[record.goal_id] = record
        self._records.move_to_end(record.goal_id)
        self._evict()

    def _evict(self) -> None:
        """Drop the oldest finished records until the history fits its cap.

        Open goals are skipped rather than evicted: forgetting a goal that is
        still running would lose the only handle anyone has on cancelling it.
        The loop is bounded by the record count, and it terminates because
        ``max_remembered >= max_open`` guarantees at least one finished record
        exists whenever the cap is exceeded.
        """
        while len(self._records) > self._max_remembered:
            victim = next(
                (gid for gid, rec in self._records.items() if not rec.is_open),
                None,
            )
            if victim is None:
                return
            self._records.pop(victim)

    def _trim_digests(self) -> None:
        """Keep the resubmission index no larger than the history behind it.

        Only a digest whose goal has finished (or has already been forgotten) is
        dropped: losing the key of a goal that is still open would let the same
        key be admitted twice, which is the one thing an idempotency key exists
        to prevent. At most :attr:`max_open` digests can be open at once and
        ``max_remembered >= max_open``, so a victim always exists.
        """
        while len(self._by_digest) > self._max_remembered:
            victim = next(
                (
                    digest
                    for digest, goal_id in self._by_digest.items()
                    if goal_id not in self._records or not self._records[goal_id].is_open
                ),
                None,
            )
            if victim is None:
                return
            self._by_digest.pop(victim)

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _tick_clock(self) -> int:
        """Read the clock, and never let it go backwards.

        A wall clock is shared with another process, which is why this subsystem
        uses one at all; the price is that a time sync can step it back, and a
        deadline measured against a clock that stepped back is a deadline that
        moved away.
        """
        self._now = max(self._now, self._clock())
        return self._now

    def _require(self, goal_id: str) -> GoalRecord:
        record = self._records.get(goal_id)
        if record is None:
            # The id came from outside, so it is not quoted back.
            raise UnknownGoalError(
                "no goal with that id is known to this channel; it may have been forgotten "
                "after finishing"
            )
        return record
