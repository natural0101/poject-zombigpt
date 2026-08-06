"""``plan.cancel``: leaving nothing of this session's in flight.

The postcondition is stated negatively, and that is what keeps the player's own
work safe (§4.3): "no entry this session owns is still running" is already true
when the character is doing something the *player* queued, so a cancel issued
during a manual action is satisfied immediately and clears nothing. A cancel
that waited for the queue to be empty would be a cancel that fights the player.

This is the game-adapter half of an action the API-free
:class:`~pz_agent_core.actions.builtin.CancelAdapter` also implements, and the
two are deliberately not the same adapter:

* the builtin one needs no probe and is what a session gets before anything has
  been scanned, because stopping must work in the worst case;
* this one adds the check that only makes sense once the mod is actually
  driving a session — that the observation being read belongs to *this* session,
  so the ``mod`` ownership tag in it means "ours" rather than "some other
  sidecar's".

:func:`~pz_agent_core.actions.adapters.register_game_adapters` therefore claims
``plan.cancel`` only when nothing else has, and never replaces an adapter that
is already registered. A capability-free cancel being displaced by a stricter
one would make the mechanism that stops the agent the first thing a missing
probe takes away.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import ClassVar, Final

from ...protocol import (
    ActionName,
    ActionOwnership,
    Command,
    JsonDict,
    Observation,
    ReasonCode,
    RiskClass,
)
from ..adapter import Evidence, PreconditionFailed
from .common import check_args

__all__ = [
    "DEFAULT_CANCEL_POLL_MS",
    "DEFAULT_CANCEL_TIMEOUT_MS",
    "UNCLEARED_OWNERSHIP",
    "PlanCancelAdapter",
]

DEFAULT_CANCEL_TIMEOUT_MS: Final = 5_000
DEFAULT_CANCEL_POLL_MS: Final = 100

#: Ownership values that leave a cancel unproven. ``ambiguous`` counts because
#: the mod is telling us it cannot say whether it authored the entry, and
#: "cannot say" is not "it is gone".
UNCLEARED_OWNERSHIP: Final[frozenset[ActionOwnership]] = frozenset(
    {ActionOwnership.MOD, ActionOwnership.AMBIGUOUS}
)


@dataclass(frozen=True, slots=True)
class _CancelSpec:
    """The one optional argument: which command to cancel.

    Without it the command means "clear everything of ours", which is what the
    reflex guard and a user's stop button both want. With it, the cancel is
    aimed at one entry and the postcondition is correspondingly narrower.
    """

    command_id: str | None

    @classmethod
    def parse(cls, command: Command) -> _CancelSpec:
        check_args(command, allowed=("command_id",))
        target = command.args.get("command_id")
        if target is None:
            return cls(command_id=None)
        if not isinstance(target, str):
            raise PreconditionFailed(
                "command_id must be a string", reason_code=ReasonCode.INVALID_ARGUMENT
            )
        try:
            uuid.UUID(target)
        except ValueError as exc:
            raise PreconditionFailed(
                f"command_id must be a UUID, got {target!r}",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            ) from exc
        return cls(command_id=target)

    def as_args(self) -> JsonDict:
        return {} if self.command_id is None else {"command_id": self.command_id}


@dataclass(frozen=True, slots=True)
class PlanCancelAdapter:
    """``plan.cancel``: clear this session's queue entries and prove it.

    Names no ``required_capability``. Every other adapter in this package is
    gated on a probe, and this one is the exception on purpose: a stop that
    could be refused because a scan has not run is not a stop.
    """

    timeout_ms: int = DEFAULT_CANCEL_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_CANCEL_POLL_MS

    name: ClassVar[ActionName] = ActionName.PLAN_CANCEL
    risk: ClassVar[RiskClass] = RiskClass.P1
    required_capability: ClassVar[str | None] = None

    def validate(self, command: Command, observation: Observation) -> None:
        _CancelSpec.parse(command)
        if observation.session_id != command.session_id:
            # The ``mod`` tag in someone else's observation is someone else's
            # ownership. Cancelling against it would either clear nothing and
            # report success, or claim authority over another sidecar's work.
            raise PreconditionFailed(
                f"the observation belongs to session {observation.session_id}, "
                "so it cannot say what this session still has in flight",
                reason_code=ReasonCode.STALE_SESSION,
                evidence={"observation_session_id": observation.session_id},
            )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _CancelSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _CancelSpec.parse(command)
        if after.session_id != command.session_id:
            return None
        state = after.action
        if state.busy and state.ownership in UNCLEARED_OWNERSHIP:
            # An unidentified entry could be the targeted one, so it counts as
            # still present; only a positive mismatch clears the cancel.
            if spec.command_id is None or state.action_id is None:
                return None
            if state.action_id == spec.command_id:
                return None
        return Evidence(
            kind="no_session_owned_action_in_flight",
            observation_seq=after.seq,
            observed={
                "session_id": after.session_id,
                "busy": state.busy,
                "ownership": state.ownership.value,
                "action_id": state.action_id,
                "action_type": state.type,
                "cancelled_command_id": spec.command_id,
            },
        )
