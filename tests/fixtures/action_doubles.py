"""Test doubles for the action engine's two ports, plus a scriptable adapter.

The engine does no IO, so the whole lifecycle can be driven from lists and a
counter. Time is the only thing that needs care: :class:`FakeClock` moves only
when the engine waits, and :class:`FakeObservationSource` consumes the full
slice it was offered, so ``N`` polls of ``interval`` reach a deadline of
``N * interval`` exactly and every timeout test is deterministic.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from pz_agent_core.actions import Dispatch, Evidence, PreconditionFailed
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    Command,
    JsonDict,
    Observation,
    ReasonCode,
    RiskClass,
)

DEFAULT_START_MS = 1_700_000_000_000


class FakeClock:
    """Wall clock in milliseconds that only advances when told to."""

    def __init__(self, start: int = DEFAULT_START_MS) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


class FakeObservationSource:
    """Scripted observations.

    ``push`` queues specific snapshots; ``repeat`` supplies an endless stream of
    one snapshot (with a fresh ``seq`` each time) for tests about what happens
    when the world reports nothing new.
    """

    def __init__(self, clock: FakeClock, *, latest: Observation | None = None) -> None:
        self._clock = clock
        self._queue: deque[Observation] = deque()
        self._latest = latest
        self._repeat: Observation | None = None
        self.waits: list[tuple[int, int]] = []

    def push(self, *observations: Observation) -> None:
        self._queue.extend(observations)

    def repeat(self, observation: Observation) -> None:
        self._repeat = observation

    def latest(self) -> Observation | None:
        return self._latest

    def wait_for_next(self, after_seq: int, timeout_ms: int) -> Observation | None:
        self.waits.append((after_seq, timeout_ms))
        self._clock.advance(timeout_ms)
        while self._queue and self._queue[0].seq <= after_seq:
            self._queue.popleft()
        if self._queue:
            nxt = self._queue.popleft()
        elif self._repeat is not None:
            nxt = replace(self._repeat, seq=max(after_seq, self._repeat.seq) + 1)
        else:
            return None
        self._latest = nxt
        return nxt


@dataclass(frozen=True, slots=True)
class AckPlan:
    """One ack the fake sink emits on the *after_polls*-th ``poll_acks`` call.

    The poll counter restarts on every send, so a plan describes what the mod
    does for each attempt rather than for the run as a whole.
    """

    status: ActionStatus
    reason: ReasonCode = ReasonCode.INTERNAL_ERROR
    after_polls: int = 1
    progress: float | None = None


class FakeCommandSink:
    """Records what was shipped and replays a scripted ack stream."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        acks: Sequence[AckPlan] = (),
        rejections: Sequence[ReasonCode] = (),
    ) -> None:
        self._clock = clock
        self._plans = list(acks)
        self._rejections = deque(rejections)
        self._polls = 0
        self._seq = 0
        self.sent: list[Command] = []
        self.cancelled: list[tuple[str, ReasonCode]] = []

    @property
    def last(self) -> Command:
        return self.sent[-1]

    def send(self, command: Command) -> Dispatch:
        shipped = replace(command, seq=self._next_seq())
        self.sent.append(shipped)
        self._polls = 0
        if self._rejections:
            reason = self._rejections.popleft()
            return Dispatch(
                command=shipped,
                rejection=ActionResult.failure(
                    session_id=shipped.session_id,
                    seq=self._next_seq(),
                    command_id=shipped.command_id,
                    action=shipped.action.value,
                    timestamp_ms=self._clock(),
                    reason_code=reason,
                    status=ActionStatus.REJECTED,
                    message="fake sink refused the command",
                ),
            )
        return Dispatch(command=shipped)

    def poll_acks(self) -> Sequence[ActionResult]:
        if not self.sent:
            return ()
        self._polls += 1
        return tuple(
            self._materialise(plan) for plan in self._plans if plan.after_polls == self._polls
        )

    def cancel(self, command: Command, reason: ReasonCode) -> None:
        self.cancelled.append((command.command_id, reason))

    def _materialise(self, plan: AckPlan) -> ActionResult:
        command = self.sent[-1]
        if plan.status is ActionStatus.SUCCEEDED:
            # The mod's own evidence: the engine must ignore it and insist on
            # what its adapter observed.
            return ActionResult.succeeded(
                session_id=command.session_id,
                seq=self._next_seq(),
                command_id=command.command_id,
                action=command.action.value,
                timestamp_ms=self._clock(),
                evidence={"mod_says": "queue entry completed"},
            )
        return ActionResult.failure(
            session_id=command.session_id,
            seq=self._next_seq(),
            command_id=command.command_id,
            action=command.action.value,
            timestamp_ms=self._clock(),
            reason_code=plan.reason,
            status=plan.status,
            message="scripted ack",
        )

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = seq + 1
        return seq


@dataclass
class StubAdapter:
    """An adapter whose every decision the test dictates.

    ``verify_after`` counts calls to :meth:`verify`; ``None`` means the
    postcondition is never observed, which is the case most of the engine's
    honesty rules are about.
    """

    name: ActionName = ActionName.MOVEMENT_MOVE_TO
    risk: RiskClass = RiskClass.P2
    required_capability: str | None = None
    timeout_ms: int = 1_000
    poll_interval_ms: int = 250
    verify_after: int | None = None
    refuse: PreconditionFailed | None = None
    crash: Exception | None = None
    #: Raised from :meth:`verify`, i.e. *after* the command has been shipped.
    verify_crash: Exception | None = None
    validate_calls: list[int] = field(default_factory=list)
    verify_calls: int = 0

    def validate(self, command: Command, observation: Observation) -> None:
        self.validate_calls.append(observation.seq)
        if self.crash is not None:
            raise self.crash
        if self.refuse is not None:
            raise self.refuse

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return {**command.args, "observed_seq": observation.seq}

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        self.verify_calls += 1
        if self.verify_crash is not None:
            raise self.verify_crash
        if self.verify_after is None or self.verify_calls < self.verify_after:
            return None
        return Evidence(
            kind="stub_postcondition",
            observation_seq=after.seq,
            observed={"verify_calls": self.verify_calls},
        )
