"""Arbitration over the interrupt ladder, with the anti-loop brake attached.

Two rules from the blueprint meet in this module.

§7.2: an emergency may *suspend* a user's goal but must never destroy it. Only
the two rungs above every emergency — a panic stop and the player taking manual
control — abort outright; everything else suspends and is expected to resume.

§7.8: a need that keeps firing while the state it complains about does not move
is a loop, not a need. Left alone it produces the failure mode where an
assistant interrupts the same activity every few seconds forever. So each need
carries a *state signature* — whatever value proves the underlying situation
changed — and the arbiter counts consecutive triggers with an unchanged
signature. Two mean "try something else"; three mean "stop and ask the user".
After that the need is suppressed for a cooldown instead of being retried.

The counters are bounded three ways: by :attr:`AntiLoopConfig.window_ms` (a run
of triggers older than the window is not a loop, it is a coincidence), by
:attr:`AntiLoopConfig.max_tracked` needs kept at all, and by the fact that only
the need actually being served advances a counter — a need that never wins
arbitration cannot escalate itself into an interrogation.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..protocol import Priority


class Arbitration(StrEnum):
    """What to do with the plan that is currently running."""

    CONTINUE = "continue"
    SUSPEND = "suspend"
    ABORT = "abort"
    ASK_USER = "ask_user"


@dataclass(frozen=True, slots=True)
class Need:
    """One thing that wants attention.

    ``state_signature`` is the anti-loop input: a coarse, comparable rendering
    of the state that justifies the need ("thirst=0.72", "wounds=2"). When it
    changes between triggers the situation is moving and the need is doing its
    job; when it does not, the same need is being served with no effect.
    """

    key: str
    priority: Priority
    state_signature: str
    message: str
    #: False for needs that may only be queued, never used to interrupt.
    preemptive: bool = True


@dataclass(frozen=True, slots=True)
class RunningPlan:
    """The plan arbitration is being run against."""

    key: str
    priority: Priority
    #: False while a step is mid-flight in a way that cannot be safely paused.
    interruptible: bool = True


@dataclass(frozen=True, slots=True)
class ArbitrationDecision:
    """The verdict, plus everything the caller needs to explain it.

    ``serve`` is set whenever there is a need the caller should act on — which
    includes the CONTINUE case where nothing is running, because "continue" is
    about the plan, not about doing nothing.
    """

    outcome: Arbitration
    serve: Need | None
    deferred: tuple[Need, ...]
    suppressed: tuple[Need, ...]
    detail: str
    alternate_strategy: bool = False
    trigger_count: int = 0


@dataclass(frozen=True, slots=True)
class AntiLoopConfig:
    """Bounds for the loop brake. Every number is a documented window."""

    #: Triggers further apart than this are not the same loop.
    window_ms: int = 120_000
    #: Consecutive triggers on an unchanged signature before the caller is told
    #: to try a different strategy (§7.8: "2 → alternate strategy").
    alternate_after: int = 2
    #: Consecutive triggers before the need is escalated to the user
    #: (§7.8: "3 → stop/ask").
    escalate_after: int = 3
    #: How long an escalated need stays suppressed, so the answer has time to
    #: matter before the same question is asked again.
    cooldown_ms: int = 300_000
    #: Needs tracked at once; the least recently seen is evicted.
    max_tracked: int = 32
    #: Needs at or above this rung (numerically at or below) abort the running
    #: plan instead of suspending it. §7.2 keeps emergencies suspending.
    abort_at: Priority = Priority.MANUAL_INPUT

    def __post_init__(self) -> None:
        if self.window_ms <= 0 or self.cooldown_ms <= 0:
            raise ValueError("anti-loop windows must be positive")
        if not 1 <= self.alternate_after <= self.escalate_after:
            raise ValueError(
                f"alternate_after must be within 1..escalate_after, got {self.alternate_after}"
            )
        if self.max_tracked < 1:
            raise ValueError(f"max_tracked must be positive, got {self.max_tracked}")


DEFAULT_ANTI_LOOP_CONFIG: Final = AntiLoopConfig()


@dataclass(slots=True)
class _Record:
    """Bounded per-need history. Counters only; nothing accumulates."""

    signature: str
    count: int
    first_ms: int
    last_ms: int
    suppressed_until_ms: int = 0


class NeedArbiter:
    """Decides suspend / continue / abort, and refuses to thrash while doing it."""

    def __init__(self, config: AntiLoopConfig = DEFAULT_ANTI_LOOP_CONFIG) -> None:
        self._config = config
        self._records: OrderedDict[str, _Record] = OrderedDict()

    @property
    def tracked(self) -> int:
        """How many needs currently have history. Never exceeds ``max_tracked``."""
        return len(self._records)

    def note_resolved(self, key: str) -> None:
        """Forget a need's history because the underlying state was fixed.

        Serving a need successfully is the thing a loop never does, so a caller
        that observed the fix clears the counter rather than waiting out the
        window.
        """
        self._records.pop(key, None)

    def arbitrate(
        self,
        running: RunningPlan | None,
        needs: Sequence[Need],
        now_ms: int,
    ) -> ArbitrationDecision:
        """Pick the winning need and say what it does to *running*."""
        ordered = _most_urgent_per_key(needs)
        suppressed = tuple(n for n in ordered if self._is_suppressed(n, now_ms))
        candidates = [n for n in ordered if n not in suppressed]

        if not candidates:
            detail = (
                f"{len(suppressed)} need(s) suppressed after escalation"
                if suppressed
                else "nothing needs attention"
            )
            return ArbitrationDecision(
                outcome=Arbitration.CONTINUE,
                serve=None,
                deferred=(),
                suppressed=suppressed,
                detail=detail,
            )

        need = candidates[0]
        deferred = tuple(candidates[1:])

        outcome, serve, detail = self._compare(running, need)
        if serve is None:
            # The need lost to the running plan, so nothing was tried on its
            # behalf. Advancing the counter here would escalate "triggered three
            # times without its state improving" about a need the agent never
            # once acted on — a claim the arbiter has no evidence for, and the
            # cry-wolf failure mode §7.8 exists to prevent.
            return ArbitrationDecision(
                outcome=outcome,
                serve=None,
                deferred=(need, *deferred),
                suppressed=suppressed,
                detail=detail,
            )

        record = self._record_trigger(need, now_ms)
        alternate = record.count >= self._config.alternate_after

        if record.count >= self._config.escalate_after:
            record.suppressed_until_ms = now_ms + self._config.cooldown_ms
            return ArbitrationDecision(
                outcome=Arbitration.ASK_USER,
                serve=need,
                deferred=deferred,
                suppressed=suppressed,
                detail=(
                    f"{need.key} triggered {record.count} times without its state improving; "
                    "asking the user instead of retrying"
                ),
                alternate_strategy=alternate,
                trigger_count=record.count,
            )

        return ArbitrationDecision(
            outcome=outcome,
            serve=serve,
            deferred=deferred,
            suppressed=suppressed,
            detail=detail,
            alternate_strategy=alternate,
            trigger_count=record.count,
        )

    def _compare(
        self, running: RunningPlan | None, need: Need
    ) -> tuple[Arbitration, Need | None, str]:
        if running is None:
            return Arbitration.CONTINUE, need, "no plan is running; serve the need directly"
        if need.priority >= running.priority:
            return (
                Arbitration.CONTINUE,
                None,
                f"{need.key} ({need.priority.name}) does not outrank {running.key} "
                f"({running.priority.name})",
            )
        if not need.preemptive:
            return Arbitration.CONTINUE, None, f"{need.key} may not interrupt a running plan"
        if need.priority <= self._config.abort_at:
            return (
                Arbitration.ABORT,
                need,
                f"{need.key} ({need.priority.name}) ends the running plan",
            )
        if not running.interruptible:
            return (
                Arbitration.CONTINUE,
                None,
                f"{running.key} cannot be interrupted safely right now",
            )
        return (
            Arbitration.SUSPEND,
            need,
            f"{need.key} ({need.priority.name}) suspends {running.key}",
        )

    def _is_suppressed(self, need: Need, now_ms: int) -> bool:
        record = self._records.get(need.key)
        if record is None:
            return False
        if need.state_signature != record.signature:
            # The situation moved on. Whatever we escalated about is not what is
            # being reported now, so the suppression does not apply to it.
            return False
        return now_ms < record.suppressed_until_ms

    def _record_trigger(self, need: Need, now_ms: int) -> _Record:
        record = self._records.get(need.key)
        stale = record is not None and now_ms - record.first_ms > self._config.window_ms
        moved = record is not None and record.signature != need.state_signature
        if record is None or stale or moved:
            record = _Record(
                signature=need.state_signature,
                count=1,
                first_ms=now_ms,
                last_ms=now_ms,
            )
        else:
            record.count += 1
            record.last_ms = now_ms
        self._records[need.key] = record
        self._records.move_to_end(need.key)
        while len(self._records) > self._config.max_tracked:
            self._records.popitem(last=False)
        return record


def _most_urgent_per_key(needs: Sequence[Need]) -> list[Need]:
    """Deduplicate by key, keep the most urgent, and order by urgency.

    The sort is stable, so needs of equal rank keep the order the caller
    produced them in — arbitration must not depend on dictionary ordering.
    """
    best: dict[str, Need] = {}
    for need in needs:
        current = best.get(need.key)
        if current is None or need.priority < current.priority:
            best[need.key] = need
    return sorted(best.values(), key=lambda n: n.priority)
