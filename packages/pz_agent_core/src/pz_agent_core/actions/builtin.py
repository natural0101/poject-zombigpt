"""The adapters that need no game-specific API.

Both of them exist to be exercised for real, today: ``action.wait`` proves the
verify loop against a value the game reports, and ``plan.cancel`` proves the
lifecycle against the action queue's ownership. Neither touches an API whose
presence in Build 42.20 has not been observed.

Movement, transfer, consumption and reading live in their own modules — this
one stays deliberately small so the registry is the extension point.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Final

from ..protocol import (
    ActionName,
    ActionOwnership,
    Command,
    JsonDict,
    Observation,
    ReasonCode,
    RiskClass,
)
from .adapter import AdapterRegistry, Evidence, PreconditionFailed

__all__ = ["CancelAdapter", "WaitAdapter", "register_builtins"]

#: One in-game hour. A longer wait is a plan step, not a single action: the
#: engine would be holding a lease open across a stretch of world where the
#: reasons for waiting have almost certainly changed.
MAX_WAIT_GAME_SECONDS: Final = 3_600.0

DEFAULT_WAIT_TIMEOUT_MS: Final = 60_000
DEFAULT_WAIT_POLL_MS: Final = 250

DEFAULT_CANCEL_TIMEOUT_MS: Final = 5_000
DEFAULT_CANCEL_POLL_MS: Final = 100

#: Ownership values that leave a cancel unproven. ``ambiguous`` counts because
#: the mod is telling us it cannot say whether it authored the entry, and
#: "cannot say" is not "it is gone".
_UNCLEARED_OWNERSHIP: Final[frozenset[ActionOwnership]] = frozenset(
    {ActionOwnership.MOD, ActionOwnership.AMBIGUOUS}
)


def _parse_world_time(raw: str | None) -> datetime | None:
    """Read the mod's world clock, or None when it is absent or unreadable."""
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class WaitAdapter:
    """``action.wait``: hold still until the world clock has advanced.

    Verified against observed game time, never against the sidecar's wall
    clock. Real seconds pass while the game is paused, in a menu, or running at
    a different speed, so a wall-clock wait would report success for a wait
    that never happened in the world the player is in.
    """

    timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_WAIT_POLL_MS

    name: ClassVar[ActionName] = ActionName.ACTION_WAIT
    risk: ClassVar[RiskClass] = RiskClass.P0
    required_capability: ClassVar[str | None] = None

    def validate(self, command: Command, observation: Observation) -> None:
        unknown = set(command.args) - {"game_seconds"}
        if unknown:
            raise PreconditionFailed(
                f"unsupported argument(s): {sorted(unknown)}",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        seconds = command.args.get("game_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise PreconditionFailed(
                "game_seconds must be a number",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        if not 0 < seconds <= MAX_WAIT_GAME_SECONDS:
            raise PreconditionFailed(
                f"game_seconds must be within 0..{MAX_WAIT_GAME_SECONDS}, got {seconds}",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        if _parse_world_time(observation.game.world_time) is None:
            raise PreconditionFailed(
                "the mod did not report a readable world_time, so a wait cannot be verified",
                reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                evidence={"world_time": observation.game.world_time},
            )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return {"game_seconds": float(command.args["game_seconds"])}

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        elapsed = self._elapsed_game_seconds(before, after)
        required = float(command.args["game_seconds"])
        if elapsed is None or elapsed < required:
            return None
        return Evidence(
            kind="world_time_advanced",
            observation_seq=after.seq,
            observed={
                "world_time_before": before.game.world_time,
                "world_time_after": after.game.world_time,
                "elapsed_game_seconds": elapsed,
                "required_game_seconds": required,
            },
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        elapsed = self._elapsed_game_seconds(before, latest)
        required = float(command.args["game_seconds"])
        if elapsed is None or required <= 0:
            return None
        return min(1.0, elapsed / required)

    def _elapsed_game_seconds(self, before: Observation, after: Observation) -> float | None:
        """Game seconds between two observations, or None when unmeasurable.

        A negative span means the world clock went backwards — a reload, not a
        wait — and is reported as unmeasurable rather than as zero progress, so
        it can never accumulate towards success.
        """
        start = _parse_world_time(before.game.world_time)
        end = _parse_world_time(after.game.world_time)
        if start is None or end is None:
            return None
        if (start.tzinfo is None) != (end.tzinfo is None):
            # The mod changed how it stamps the world clock mid-action. Python
            # refuses to subtract these two, and guessing an offset would
            # invent the very number this adapter exists to measure.
            return None
        elapsed = (end - start).total_seconds()
        return None if elapsed < 0 else elapsed


@dataclass(frozen=True, slots=True)
class CancelAdapter:
    """``plan.cancel``: leave no mod-owned entry in the action queue.

    The postcondition is stated negatively, which is what keeps the player's
    own work safe (§ 4.3): a manual action running alongside satisfies it
    immediately, because "no mod-owned entry remains" is already true and there
    is nothing for the engine to clear.
    """

    timeout_ms: int = DEFAULT_CANCEL_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_CANCEL_POLL_MS

    name: ClassVar[ActionName] = ActionName.PLAN_CANCEL
    risk: ClassVar[RiskClass] = RiskClass.P1
    required_capability: ClassVar[str | None] = None

    def validate(self, command: Command, observation: Observation) -> None:
        unknown = set(command.args) - {"command_id"}
        if unknown:
            raise PreconditionFailed(
                f"unsupported argument(s): {sorted(unknown)}",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        target = command.args.get("command_id")
        if target is None:
            return
        if not isinstance(target, str):
            raise PreconditionFailed(
                "command_id must be a string",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        try:
            uuid.UUID(target)
        except ValueError as exc:
            raise PreconditionFailed(
                f"command_id must be a UUID, got {target!r}",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            ) from exc

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        target = command.args.get("command_id")
        return {} if target is None else {"command_id": target}

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        state = after.action
        if state.busy and state.ownership in _UNCLEARED_OWNERSHIP:
            target = command.args.get("command_id")
            # An unidentified entry could be the targeted one, so it counts as
            # still present; only a positive mismatch clears the cancel.
            if target is None or state.action_id is None or state.action_id == target:
                return None
        return Evidence(
            kind="action_queue_free_of_mod_entries",
            observation_seq=after.seq,
            observed={
                "busy": state.busy,
                "ownership": state.ownership.value,
                "action_id": state.action_id,
                "action_type": state.type,
            },
        )


def register_builtins(registry: AdapterRegistry) -> AdapterRegistry:
    """Add the API-free adapters to *registry* and return it.

    Kept as a function rather than a module-level singleton so a session can
    choose what it publishes: the registry is the extension point the
    game-specific adapters plug into.
    """
    registry.register(WaitAdapter())
    registry.register(CancelAdapter())
    return registry
