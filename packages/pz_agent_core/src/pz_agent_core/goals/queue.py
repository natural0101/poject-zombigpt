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

One qualification joined the third promise with the preemption wave: a goal may
*step aside and come back*. :meth:`GoalQueue.suspend` parks the active goal at
the front of the backlog — state ``PENDING`` again, the closed enum stays
closed, and a ``suspended_by`` marker on the record says who it stepped aside
for — and :meth:`GoalQueue.submit_front` admits the preempting goal ahead of
the backlog. Three consequences are the design rather than accidents of it:

* **The wall clock stops while suspended.** The budget promise is
  ``max_wall_ms`` of *active* time, so suspension banks the accrued active
  milliseconds on the record (``active_ms_before_suspend``) and resumption
  serves only the remainder. Steps carry across unchanged: a step spent was
  spent.
* **The pending time to live does not apply to a suspended goal.** The TTL
  exists to end a goal that was admitted and never started; a suspended goal
  already started, and expiring it for having been preempted would punish the
  interruption the user never asked for. Its bound is the channel's own
  progress instead: it sits at the front of the backlog behind goals whose
  budgets are each finite, its own re-suspensions are capped, and every stop
  lever (cancel, panic) still ends it like any pending goal. A caller that
  stops activating strands it — the same caller-controlled exception the
  paragraph above already grants to a caller that stops ticking.
* **Preemption is bounded.** :data:`~.model.MAX_SUSPENSIONS_PER_GOAL`
  suspensions per goal, then :meth:`GoalQueue.suspend` refuses and the arbiter
  must let the goal finish. And suspension frees no slot — the parked goal is
  still open — so injecting a preemptor needs one free ``max_open`` slot or
  :meth:`GoalQueue.submit_front` refuses; nothing is ever evicted to make
  room.

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
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

from ..ipc.clocks import Clock
from ..protocol import ActionResult, ActionStatus, ReasonCode
from .model import (
    MAX_SUSPENSIONS_PER_GOAL,
    GoalAdmission,
    GoalRecord,
    GoalRefusal,
    GoalRequest,
    GoalState,
    GoalTransition,
    mint_goal_id,
    normalise_evidence_keys,
)

__all__ = [
    "DEFAULT_MAX_OPEN",
    "DEFAULT_MAX_REMEMBERED",
    "RESTART_LOST_DETAIL",
    "GoalQueue",
    "GoalSnapshot",
    "UnknownGoalError",
]

#: Goals that may be open — pending plus active — at one time. Small because a
#: backlog of user goals is a backlog of stale intentions: by the time the
#: fourth is reached the world has moved.
DEFAULT_MAX_OPEN: Final = 4

#: Goals kept in the history that answers resubmissions. A finished goal has to
#: stay long enough that a retried submission returns *it* rather than starting
#: the work again, and no longer.
DEFAULT_MAX_REMEMBERED: Final = 32


#: The detail a previous session's ACTIVE goal ends with when a fresh queue
#: restores it. One constant rather than an f-string at the call site, because
#: it is the sentence a client polling the old goal id will read, and a test
#: pins it verbatim: the goal was not cancelled and did not run out of budget —
#: the process serving it died under it, and the record says exactly that.
RESTART_LOST_DETAIL: Final = "the sidecar restarted while this goal was active"


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    """Everything the queue holds, frozen, in the queue's own retention order.

    The unit persistence works in. ``records`` shares the queue's immutable
    :class:`~.model.GoalRecord` objects — nothing is copied and nothing can be
    mutated through it — and ``revision`` is the mutation count the records
    were read at, so a writer can tell "the channel moved since my last write"
    from "this tick changed nothing" without comparing documents.
    """

    records: tuple[GoalRecord, ...]
    revision: int


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
        new_id: Callable[[], str] = mint_goal_id,
    ) -> None:
        if max_open < 1:
            raise ValueError(f"max_open must be positive, got {max_open}")
        if max_remembered < max_open:
            raise ValueError(
                f"max_remembered must be at least max_open ({max_open}), got {max_remembered}"
            )
        self._clock = clock
        # Injected for the same reason the clock is: a goal id reaches a peer
        # inside a result body, and a test that pins that body against a
        # hand-written object cannot do it against a fresh uuid4. Still minted
        # HERE rather than accepted from the submitter — the rule that matters
        # is that a *caller* cannot choose an id, because an id a caller chose
        # is an id a caller could fill with text, and it comes back in refusals.
        # Wiring is not calling: nothing on the request path reaches this.
        self._new_id = new_id
        self._max_open = max_open
        self._max_remembered = max_remembered
        self._armed = armed
        self._now = 0
        self._sequence = 0
        #: Counts every record mutation — admission, activation, a spent step,
        #: a terminal transition, a restore. Read by the persistence seam to
        #: skip the write on a tick that changed nothing; never reset.
        self._revision = 0
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
        """The backlog: front-inserted goals first, then oldest by admission.

        The key is ``(front_rank, sequence)``. Ordinary admissions all carry
        rank 0, so between themselves this is the same FIFO by admission
        sequence it always was; a suspended or front-injected goal carries a
        negative rank minted below every rank already waiting, so the most
        recently fronted goal is served first — which is what makes a
        preemptor run next and a suspended goal resume before the backlog.
        """
        waiting = [r for r in self._records.values() if r.state is GoalState.PENDING]
        return tuple(sorted(waiting, key=lambda r: (r.front_rank, r.sequence)))

    @property
    def open_count(self) -> int:
        """Pending plus active. Never exceeds :attr:`max_open`."""
        return sum(1 for r in self._records.values() if r.is_open)

    def record(self, goal_id: str) -> GoalRecord | None:
        """The record for *goal_id*, or None once it has been forgotten."""
        return self._records.get(goal_id)

    @property
    def revision(self) -> int:
        """How many record mutations this queue has made. Monotonic, never reset."""
        return self._revision

    def snapshot(self) -> GoalSnapshot:
        """A frozen view of every held record, for persistence.

        Called under the same lock discipline as every other method — the queue
        has no lock of its own, the loop's ``goal_lock`` governs — and cheap
        enough to take on every tick: one tuple over at most ``max_remembered``
        immutable records, no copying, no IO.
        """
        return GoalSnapshot(records=tuple(self._records.values()), revision=self._revision)

    def restore(self, snapshot: GoalSnapshot, *, now_ms: int) -> tuple[GoalRecord, ...]:
        """Refill a fresh queue from a previous process's snapshot, honestly.

        The three cases, each the truthful reading of what the restart did:

        * A ``PENDING`` record comes back pending, with its original id,
          submission time and idempotency digest — so a client resubmitting the
          same key resolves to the *same* goal across the restart, and the
          pending time to live keeps counting from the original submission: a
          goal whose TTL ran out while the sidecar was down expires on the
          first tick rather than being granted a second life.
        * An ``ACTIVE`` record is recorded terminal ``FAILED`` with
          ``SESSION_TERMINATED`` and :data:`RESTART_LOST_DETAIL`. The process
          serving it died under it and nobody observed how far it got;
          ``GoalState`` stays closed — no ``LOST`` member — because "failed,
          because the session ended" is that fact in the vocabulary every
          client already reads. The returned tuple is these records, so the
          caller can log what the restart cost.
        * A terminal record comes back as terminal history, so a client
          polling a recently finished goal id still gets its answer.

        All-or-nothing: the new state is built aside and committed at the end,
        so a snapshot that is refused leaves the queue exactly as fresh as it
        was. Refusals are ``ValueError`` because every one of them means the
        snapshot is not something this queue could ever have written — the
        caller's honest move is to set the file aside, not to guess.

        Raises:
            ValueError: the queue already holds goals, the snapshot repeats a
                goal id or an idempotency digest, or it carries more pending
                goals than this queue's ``max_open`` admits.
        """
        if self._records or self._by_digest:
            raise ValueError("restore only fills a fresh queue; this one already holds goals")
        self._now = max(self._now, now_ms)
        pending = sum(1 for record in snapshot.records if record.state is GoalState.PENDING)
        if pending > self._max_open:
            raise ValueError(
                f"the snapshot holds {pending} pending goal(s), over this channel's cap of "
                f"{self._max_open}; refusing to guess which to drop"
            )
        records: OrderedDict[str, GoalRecord] = OrderedDict()
        by_digest: OrderedDict[str, str] = OrderedDict()
        lost: list[GoalRecord] = []
        sequence = self._sequence
        for record in snapshot.records:
            if record.goal_id in records:
                raise ValueError("the snapshot names one goal id twice; refusing to pick one")
            if record.key_digest in by_digest:
                raise ValueError(
                    "the snapshot reuses one idempotency digest across goals; refusing to pick one"
                )
            adopted = record
            if record.state is GoalState.ACTIVE:
                started = (
                    record.started_at_ms
                    if record.started_at_ms is not None
                    else record.submitted_at_ms
                )
                adopted = replace(
                    record,
                    state=GoalState.FAILED,
                    reason_code=ReasonCode.SESSION_TERMINATED,
                    # Clamped so a wall clock that stepped back across the
                    # restart cannot mint a goal that finished before it began.
                    finished_at_ms=max(self._now, started, record.submitted_at_ms),
                    evidence_keys=(),
                    detail=RESTART_LOST_DETAIL,
                )
                lost.append(adopted)
            records[adopted.goal_id] = adopted
            by_digest[adopted.key_digest] = adopted.goal_id
            sequence = max(sequence, adopted.sequence)
        self._records = records
        self._by_digest = by_digest
        self._sequence = sequence
        self._revision += 1
        # Overflow falls to the ordinary retention rules: terminal history past
        # the cap is evicted oldest-first, exactly as it would have been had
        # this queue lived through the records itself. Open goals never are —
        # the pending check above is what guarantees a victim exists.
        self._evict()
        self._trim_digests()
        return tuple(lost)

    # -- admission ---------------------------------------------------------

    def submit(self, request: GoalRequest) -> GoalAdmission:
        """Admit *request* to the backlog, or explain why not.

        A repeat of a key already seen returns the goal that key created, so a
        retried submission — the ordinary consequence of a dropped
        acknowledgement — is never a second goal. A key reused for *different*
        content is refused instead, because that is a caller bug and silently
        serving the first goal would hide it.
        """
        self._tick_clock()
        return self._admit(request, front_rank=0)

    def submit_front(self, request: GoalRequest, *, now_ms: int) -> GoalAdmission:
        """Admit *request* ahead of the whole backlog, or explain why not.

        The priority-injection half of preemption: a preempting goal must
        activate *next*, ahead of everything waiting — including the goal it
        just suspended — so it is admitted with a front rank minted below every
        rank in the backlog. Nothing is evicted to make room: the suspended
        goal still occupies its open slot, so injection needs one free slot of
        its own and is refused with the ordinary over-cap ``QUEUE_REJECTED``
        when there is none. With the shipped :data:`DEFAULT_MAX_OPEN` of 4
        that arithmetic reads: preemption fits while at most 3 goals are open.

        A duplicate key resolves to the goal it already names *where it
        already is* — repositioning on a retry would let a dropped
        acknowledgement reorder the backlog.

        ``now_ms`` is the arbiter's clock reading for the tick that decided to
        preempt, monotonised exactly as :meth:`restore` treats its own — this
        queue's clock never goes backwards, whoever reports the time.
        """
        self._now = max(self._now, now_ms)
        return self._admit(request, front_rank=self._next_front_rank())

    def _admit(self, request: GoalRequest, *, front_rank: int) -> GoalAdmission:
        """The shared admission path; ``self._now`` is already current."""
        now = self._now
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
            goal_id=self._new_id(),
            kind=request.kind,
            params=request.params,
            budget=request.effective_budget,
            key_digest=digest,
            sequence=self._next_sequence(),
            state=GoalState.PENDING,
            submitted_at_ms=now,
            front_rank=front_rank,
        )
        self._remember(record)
        self._by_digest[digest] = record.goal_id
        self._trim_digests()
        return GoalAdmission(goal=record)

    def activate_next(self) -> GoalAdmission:
        """Promote the front pending goal to active, or explain why not.

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

        This is also the whole of *resuming* a suspended goal — there is no
        separate resume call, verified by the suspension tests rather than
        asserted here. A suspended goal sits at the front of :attr:`pending`,
        so the ordinary promotion below picks it first; activation consumes
        the marker (``suspended_by`` cleared, ``front_rank`` reset, the
        parked-reason detail wiped) and starts a fresh activation window whose
        deadline is the goal's *remaining* wall budget, because
        ``active_ms_before_suspend`` — which survives — is subtracted by
        :attr:`~.model.GoalRecord.deadline_ms`. Steps carry across untouched.
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
        started = replace(
            startable[0],
            state=GoalState.ACTIVE,
            started_at_ms=self._now,
            # Resuming consumes the suspension marker and the front position:
            # the goal is running again, and a second preemption earns it a
            # fresh place at the front then. The detail is wiped with them —
            # it held the parked reason, which is no longer the truth.
            suspended_by=None,
            front_rank=0,
            detail="",
        )
        self._remember(started)
        return GoalAdmission(goal=started)

    def suspend(self, goal_id: str, *, by_goal_id: str, reason: str, now_ms: int) -> GoalAdmission:
        """Park the active goal at the front of the backlog so another may run.

        The interrupt half of preemption («прервать текущую цель … продолжить
        исходную»): the goal returns to ``PENDING`` — the closed
        :class:`~.model.GoalState` gains no ``SUSPENDED`` member — carrying a
        ``suspended_by`` marker naming *by_goal_id* and a front rank that puts
        it ahead of the whole backlog, so the ordinary activation path resumes
        it as soon as the preemptor is done. Its wall clock stops: the active
        milliseconds accrued so far are banked on the record (clamped at the
        budget itself — a goal suspended past its deadline resumes only to
        expire, honestly), and its steps carry across untouched. *reason* is a
        bounded single line assembled by the caller from constants — it lands
        in the parked record's ``detail`` for status readers and is wiped on
        resume.

        Typed refusals, each one the arbiter can act on:

        * ``PRECONDITION_FAILED`` — the goal already ended, or is waiting
          rather than active; there is nothing running to interrupt.
        * ``CANCELLED_BY_REQUEST`` — the user already cancelled it; user input
          outranks preemption, the next tick ends the goal, and parking it
          would only delay that.
        * ``QUEUE_REJECTED`` — the goal has been suspended
          :data:`~.model.MAX_SUSPENSIONS_PER_GOAL` times already. The arbiter
          must read this as "do not preempt": the goal runs to its own end.

        An unknown *goal_id* raises :class:`UnknownGoalError` — a caller bug,
        exactly as everywhere else on this surface. ``now_ms`` is monotonised
        like :meth:`restore`'s; a stale reading cannot rewind the clock.

        Suspension frees no ``max_open`` slot — the parked goal is still open.
        The preemptor needs a free slot of its own via :meth:`submit_front`,
        so the arbiter's order is: check, suspend, inject, activate.
        """
        record = self._require(goal_id)
        self._now = max(self._now, now_ms)
        if not record.is_open or record.state is not GoalState.ACTIVE:
            return GoalAdmission(
                refusal=GoalRefusal(
                    reason_code=ReasonCode.PRECONDITION_FAILED,
                    message=("only the active goal can be suspended; this one is not running."),
                )
            )
        if goal_id in self._cancel_requested:
            return GoalAdmission(
                refusal=GoalRefusal(
                    reason_code=ReasonCode.CANCELLED_BY_REQUEST,
                    message=(
                        "the user already cancelled that goal and the next tick ends it; "
                        "there is nothing to suspend."
                    ),
                )
            )
        if record.suspensions >= MAX_SUSPENSIONS_PER_GOAL:
            return GoalAdmission(
                refusal=GoalRefusal(
                    reason_code=ReasonCode.QUEUE_REJECTED,
                    message=(
                        f"goal {record.goal_id} has already been suspended "
                        f"{MAX_SUSPENSIONS_PER_GOAL} times, which is its cap; let it "
                        "finish instead of preempting it again."
                    ),
                    active_goal_id=record.goal_id,
                )
            )
        started_at = record.started_at_ms
        if started_at is None:
            # Unreachable: an ACTIVE record cannot be constructed without its
            # start time. Kept as a typed failure rather than an assert so a
            # broken invariant surfaces as the bug it is, not as bad arithmetic.
            raise ValueError("an active goal must record when it started")
        accrued = self._now - started_at
        parked = replace(
            record,
            state=GoalState.PENDING,
            started_at_ms=None,
            suspended_by=by_goal_id,
            suspensions=record.suspensions + 1,
            # The budget promise is max_wall_ms of ACTIVE time; the clamp keeps
            # the bank inside the budget so a goal suspended at (or past) its
            # deadline stores "the whole budget is spent" and nothing weirder.
            active_ms_before_suspend=min(
                record.active_ms_before_suspend + accrued, record.budget.max_wall_ms
            ),
            front_rank=self._next_front_rank(),
            detail=reason,
        )
        self._remember(parked)
        return GoalAdmission(goal=parked)

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
            if record.suspended_by is not None:
                # The pending TTL bounds a goal that was admitted and never
                # started; a suspended goal already started — expiring it for
                # having been preempted would punish the interruption. Its
                # bound is the channel's progress instead (documented at the
                # top of this module), and the stop levers still apply.
                continue
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
            # A terminal record answers "why did it end"; who once paused it is
            # not part of that answer, and the model refuses a marker on
            # anything but a pending record. The suspensions *count* survives —
            # it is history, not state.
            suspended_by=None,
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
        self._revision += 1
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

    def _next_front_rank(self) -> int:
        """A rank strictly ahead of every goal now waiting.

        One below the minimum rank in the pending backlog (ordinary admissions
        sit at 0), so each front insertion — a suspension or an injection —
        lands ahead of everything already fronted: last suspended, first
        resumed. Bounded without a counter to persist: a new minimum can only
        be minted while the goal holding the old minimum is still open, so at
        most ``max_open`` distinct negative ranks exist at once and no open
        rank is ever below ``-max_open``; when the fronted goals drain, the
        minimum relaxes back towards 0 by this same arithmetic. A test pins
        the bound under a preemption storm.
        """
        waiting = [
            record.front_rank
            for record in self._records.values()
            if record.state is GoalState.PENDING
        ]
        return min(waiting, default=0) - 1

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
