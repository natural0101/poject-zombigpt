"""The adapter contract: everything the engine needs to know about one action.

An adapter owns the meaning of a single command — what makes it valid, what the
mod receives, and what counts as proof that it happened. The engine owns the
lifecycle and nothing else.

The split exists to make one property structural rather than conventional: the
only route from a command to ``status = "succeeded"`` runs through
:meth:`ActionAdapter.verify` returning an :class:`Evidence`, and an ``Evidence``
cannot be constructed without an observed payload. An adapter that has nothing
to observe therefore *cannot* claim success, no matter what the mod reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..protocol import (
    ActionName,
    Command,
    JsonDict,
    Observation,
    ReasonCode,
    RiskClass,
)

__all__ = [
    "ActionAdapter",
    "AdapterRegistry",
    "DuplicateAdapterError",
    "Evidence",
    "PreconditionFailed",
    "ProgressReporter",
    "UnknownActionError",
]


@dataclass(frozen=True, slots=True)
class Evidence:
    """Observed proof that a postcondition held.

    ``observed`` carries the values the proof rests on — the stat that moved,
    the container that now holds the item, the world time that advanced — and
    it is rejected when empty. That check is the mechanical half of the honesty
    guarantee: :meth:`~pz_agent_core.protocol.ActionResult.succeeded` also
    refuses an empty payload, and ``Evidence`` is the only thing the engine
    ever hands it.
    """

    kind: str
    observation_seq: int
    observed: JsonDict

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("evidence kind must be a non-empty label")
        if self.observation_seq < 0:
            raise ValueError(f"observation_seq must be non-negative, got {self.observation_seq}")
        if not self.observed:
            raise ValueError("evidence requires at least one observed value")

    def to_payload(self) -> JsonDict:
        """The wire form attached to a successful :class:`ActionResult`."""
        return {
            "kind": self.kind,
            "observation_seq": self.observation_seq,
            "observed": dict(self.observed),
        }


class PreconditionFailed(Exception):
    """An adapter refused a command; nothing has been sent to the mod.

    The reason code travels with the exception because the caller's recovery
    table is keyed by code (blueprint § 17.6): an unresolvable reference, an
    unsupported capability and a policy refusal are all precondition failures
    and each is handled differently.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: ReasonCode = ReasonCode.PRECONDITION_FAILED,
        evidence: JsonDict | None = None,
    ) -> None:
        if reason_code is ReasonCode.POSTCONDITION_MET:
            raise ValueError("POSTCONDITION_MET cannot explain a refusal")
        super().__init__(message)
        self.reason_code = reason_code
        #: What was looked at while refusing — the failed-result diagnostics of
        #: blueprint § 4.15 are useless without it.
        self.evidence: JsonDict = dict(evidence or {})


class ActionAdapter(Protocol):
    """One game action, as the engine sees it.

    The five data members are declared as read-only properties so that a frozen
    dataclass — the natural shape for a stateless adapter — satisfies the
    protocol; a plain mutable attribute satisfies it too.
    """

    @property
    def name(self) -> ActionName:
        """Which command this adapter implements."""
        ...

    @property
    def risk(self) -> RiskClass:
        """Permission tier the policy layer must clear before the engine runs it."""
        ...

    @property
    def required_capability(self) -> str | None:
        """Probe name that must be usable, or None when the action needs no game API."""
        ...

    @property
    def timeout_ms(self) -> int:
        """How long the postcondition may take to appear before the attempt fails."""
        ...

    @property
    def poll_interval_ms(self) -> int:
        """Longest the engine waits on the observation source between checks."""
        ...

    def validate(self, command: Command, observation: Observation) -> None:
        """Check preconditions against a *fresh* observation.

        Raises:
            PreconditionFailed: when the command must not be sent. Returning
                normally is a promise that the command is worth shipping, not
                that it will succeed.
        """
        ...

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        """Produce the argument object the mod receives.

        Called after :meth:`validate`, so it may assume the command is sane and
        concentrate on resolving and normalising.
        """
        ...

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        """Look for the postcondition in *after*.

        Returns None while the postcondition is not (yet) observable. None is
        never "probably fine": the engine keeps polling and, when the budget
        runs out, reports failure.
        """
        ...


@runtime_checkable
class ProgressReporter(Protocol):
    """Optional adapter extension: a domain-specific progress signal.

    Stuck detection works without it — the engine falls back to what the mod
    reports about the action queue — but an adapter that can measure its own
    advance (distance closed, pages read, game time elapsed) gives a far
    sharper answer than "the queue entry looks the same".
    """

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        """Fraction of the work observed to be done, or None when unmeasurable."""
        ...


class UnknownActionError(LookupError):
    """No adapter is registered for a requested action."""

    #: Reported to the caller as-is; an unimplemented action is indistinguishable
    #: from one whose game API is missing, and both are permanent for this build.
    reason_code = ReasonCode.CAPABILITY_UNAVAILABLE

    def __init__(self, action: ActionName) -> None:
        super().__init__(f"no adapter registered for action {action.value!r}")
        self.action = action


class DuplicateAdapterError(ValueError):
    """Two adapters claim the same action."""

    def __init__(self, action: ActionName) -> None:
        super().__init__(f"an adapter for action {action.value!r} is already registered")
        self.action = action


class AdapterRegistry:
    """Maps an action to the adapter that implements it.

    Registration is single-assignment on purpose. Silently replacing an adapter
    would let an import-order change swap out the code that decides what counts
    as proof for an action, which is the last thing that should move quietly.
    """

    def __init__(self) -> None:
        self._adapters: dict[ActionName, ActionAdapter] = {}

    def register(self, adapter: ActionAdapter) -> None:
        """Add *adapter*.

        Raises:
            DuplicateAdapterError: if its action is already claimed.
        """
        if adapter.name in self._adapters:
            raise DuplicateAdapterError(adapter.name)
        self._adapters[adapter.name] = adapter

    def get(self, action: ActionName) -> ActionAdapter:
        """The adapter for *action*.

        Raises:
            UnknownActionError: when nothing is registered for it.
        """
        adapter = self._adapters.get(action)
        if adapter is None:
            raise UnknownActionError(action)
        return adapter

    def __contains__(self, action: object) -> bool:
        return action in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)

    def names(self) -> tuple[ActionName, ...]:
        """Registered actions, in a stable order for diagnostics."""
        return tuple(sorted(self._adapters, key=lambda action: action.value))
