"""``survival.rest`` and ``survival.sleep``: standing still, and going under.

Both actions ask the character to stop being useful for a while, and both are
verified against the stat they exist to move — endurance for one, fatigue for
the other. Neither infers anything from the queue: resting is largely the
*absence* of a queued action, so "the queue is idle" is exactly the observation
that proves nothing here.

Sleep carries a second, independent postcondition: game time advanced. A night
in Project Zomboid is hours of world clock, and fatigue alone would also fall
during a quiet afternoon — the pair is what distinguishes a slept night from a
character who simply stopped running. It is measured on the mod's world clock
rather than the sidecar's, for the reason ``action.wait`` gives: real seconds
pass while the game is paused, and those are not seconds the character slept
through.

Sleep is also the one action in this package rated P4. Once the character is
asleep the mod cannot wake them — there is no queue entry to cancel and no
timed action to interrupt — so it is never taken on the agent's own initiative,
and the reflex guard is consulted before it is sent at all. A danger level the
guard can see is a danger the character would sleep through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Final

from ...capabilities.probes import SURVIVAL_REST, SURVIVAL_SLEEP
from ...protocol import (
    ActionName,
    ActionOwnership,
    Command,
    DangerLevel,
    JsonDict,
    Observation,
    ReasonCode,
    RefError,
    RefKind,
    RiskClass,
    SquareRef,
)
from ..adapter import Evidence, PreconditionFailed
from .common import check_args, read_count, read_flag, read_number, read_ref

__all__ = [
    "DEFAULT_REST_POLL_MS",
    "DEFAULT_REST_TARGET",
    "DEFAULT_REST_TIMEOUT_MS",
    "DEFAULT_SLEEP_HOURS",
    "DEFAULT_SLEEP_POLL_MS",
    "DEFAULT_SLEEP_TIMEOUT_MS",
    "MAX_REST_TARGET",
    "MAX_SLEEP_HOURS",
    "MIN_REST_TARGET",
    "MIN_SLEEP_HOURS",
    "SEATED_ACTION_MARKER",
    "RestAdapter",
    "SleepAdapter",
]

#: Endurance runs 0..1 and full is 1. Nine tenths is "ready to run again"
#: without waiting out the long tail of the last tenth.
DEFAULT_REST_TARGET: Final = 0.9

#: A target under a twentieth is met by drawing breath rather than by resting,
#: and the stat never reads above 1, so a higher target could never be met.
MIN_REST_TARGET: Final = 0.05
MAX_REST_TARGET: Final = 1.0

DEFAULT_SLEEP_HOURS: Final = 8
MIN_SLEEP_HOURS: Final = 1
MAX_SLEEP_HOURS: Final = 16

#: Wall-clock bounds a caller may put on either wait. They are the mod's own
#: bounds; stating them twice is deliberate, because a value this side waves
#: through is one the mod refuses.
MIN_WAIT_MS: Final = 1_000
MAX_REST_WAIT_MS: Final = 900_000
DEFAULT_REST_WAIT_MS: Final = 300_000
MAX_SLEEP_WAIT_MS: Final = 1_800_000
DEFAULT_SLEEP_WAIT_MS: Final = 600_000

#: Endurance recovery is slow, so both budgets are long — and bounded.
DEFAULT_REST_TIMEOUT_MS: Final = MAX_REST_WAIT_MS
DEFAULT_REST_POLL_MS: Final = 1_000
DEFAULT_SLEEP_TIMEOUT_MS: Final = MAX_SLEEP_WAIT_MS
DEFAULT_SLEEP_POLL_MS: Final = 1_000

#: How a sitting action names itself in ``action.type``. The mod builds those
#: entries from ``ISSitOnChairAction`` and ``ISSitOnGround``
#: (``adapters/Rest.lua``), so the substring is what a posture reading has to go
#: on. It is only ever consulted when endurance cannot be read at all.
SEATED_ACTION_MARKER: Final = "sit"

#: Stat keys. Read by name because "the mod did not report endurance" and "the
#: character has no endurance left" are different facts, and
#: :attr:`~pz_agent_core.protocol.PlayerState.endurance` reads the first as the
#: second.
_ENDURANCE: Final = "endurance"
_FATIGUE: Final = "fatigue"


def _reported_stat(observation: Observation, key: str) -> float | None:
    """One stat as a number, or None when the mod did not report it."""
    if key not in observation.player.stats:
        return None
    value = observation.player.stats[key]
    if value is None or isinstance(value, bool):
        return None
    return float(value)


def _read_optional_square_ref(command: Command, key: str) -> str | None:
    """A square reference argument, checked, or None when it was not given.

    Parsed as well as kind-checked: a reference the mod would refuse is better
    refused here, where the caller still learns which argument was wrong.
    """
    if command.args.get(key) is None:
        return None
    given = read_ref(command, key, kind=RefKind.SQUARE)
    try:
        SquareRef.parse(given)
    except RefError as exc:
        raise PreconditionFailed(
            f"{key} is not a well-formed square reference: {given!r}",
            reason_code=ReasonCode.INVALID_REF,
        ) from exc
    return given


# --------------------------------------------------------------------------
# survival.rest
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RestSpec:
    target: float
    seat_ref: str | None
    allow_ground: bool
    max_wait_ms: int

    @classmethod
    def parse(cls, command: Command) -> _RestSpec:
        check_args(command, allowed=("target_endurance", "seat_ref", "allow_ground", "max_wait_ms"))
        return cls(
            target=read_number(
                command,
                "target_endurance",
                default=DEFAULT_REST_TARGET,
                minimum=MIN_REST_TARGET,
                maximum=MAX_REST_TARGET,
            ),
            seat_ref=_read_optional_square_ref(command, "seat_ref"),
            allow_ground=read_flag(command, "allow_ground", default=False),
            max_wait_ms=read_count(
                command,
                "max_wait_ms",
                default=DEFAULT_REST_WAIT_MS,
                minimum=MIN_WAIT_MS,
                maximum=MAX_REST_WAIT_MS,
            ),
        )

    def as_args(self) -> JsonDict:
        args: JsonDict = {
            "target_endurance": self.target,
            "allow_ground": self.allow_ground,
            "max_wait_ms": self.max_wait_ms,
        }
        if self.seat_ref is not None:
            args["seat_ref"] = self.seat_ref
        return args

    @property
    def asked_to_sit(self) -> bool:
        """True when the command asked for a posture rather than just a stat."""
        return self.seat_ref is not None or self.allow_ground


def _seated(observation: Observation) -> bool:
    """True when the mod's own queue entry says the character is sitting down."""
    if not observation.action.busy or observation.action.ownership is not ActionOwnership.MOD:
        return False
    action_type = observation.action.type or ""
    return SEATED_ACTION_MARKER in action_type.lower()


@dataclass(frozen=True, slots=True)
class RestAdapter:
    """``survival.rest``: recover endurance to a target.

    Two postconditions, and the second one is narrow on purpose. Normally the
    proof is the endurance reading: higher than it was, and at or above the
    target that was asked for. When the mod reports no endurance at all the
    first is impossible, and the only observable thing left is the posture — so
    a command that asked the character to *sit* may be proven by the character
    being observed sitting. A standing rest with no readable endurance has
    nothing to show for itself and stays a timeout, which is the honest outcome
    for an action whose whole effect is invisible on this install.
    """

    timeout_ms: int = DEFAULT_REST_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_REST_POLL_MS

    name: ClassVar[ActionName] = ActionName.SURVIVAL_REST
    risk: ClassVar[RiskClass] = RiskClass.P2
    required_capability: ClassVar[str | None] = SURVIVAL_REST

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _RestSpec.parse(command)
        endurance = _reported_stat(observation, _ENDURANCE)
        if endurance is None:
            if not spec.asked_to_sit:
                raise PreconditionFailed(
                    "the mod reported no endurance, and a rest that neither sits down "
                    "nor moves a readable stat cannot be verified",
                    reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                    evidence={"observation_seq": observation.seq},
                )
            return
        if endurance >= spec.target:
            raise PreconditionFailed(
                f"endurance is already {endurance:.2f}, at or above the "
                f"{spec.target:.2f} asked for",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"endurance": endurance, "target_endurance": spec.target},
            )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _RestSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _RestSpec.parse(command)
        was = _reported_stat(before, _ENDURANCE)
        now = _reported_stat(after, _ENDURANCE)
        if was is not None and now is not None:
            if now <= was or now < spec.target:
                return None
            return Evidence(
                kind="endurance_recovered",
                observation_seq=after.seq,
                observed={
                    "endurance_before": was,
                    "endurance_after": now,
                    "target_endurance": spec.target,
                    "seat_ref": spec.seat_ref,
                    "seated": _seated(after),
                },
            )
        if not spec.asked_to_sit or not _seated(after):
            return None
        return Evidence(
            kind="rest_posture_taken",
            observation_seq=after.seq,
            observed={
                "endurance_before": was,
                "endurance_after": now,
                "target_endurance": spec.target,
                "seat_ref": spec.seat_ref,
                "seated": True,
                "action_type": after.action.type,
            },
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        """Fraction of the endurance gap that has closed, or None when unreadable."""
        spec = _RestSpec.parse(command)
        was = _reported_stat(before, _ENDURANCE)
        now = _reported_stat(latest, _ENDURANCE)
        if was is None or now is None:
            return None
        gap = spec.target - was
        if gap <= 0:
            return 1.0
        return min(1.0, max(0.0, (now - was) / gap))


# --------------------------------------------------------------------------
# survival.sleep
# --------------------------------------------------------------------------


def _world_time(observation: Observation) -> datetime | None:
    """The mod's world clock, or None when it is absent or unreadable."""
    raw = observation.game.world_time
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _elapsed_game_seconds(before: Observation, after: Observation) -> float | None:
    """World seconds between two observations, or None when unmeasurable.

    A negative span means the clock went backwards — a reload, not a night — and
    is unmeasurable rather than zero, so it can never accumulate towards a
    success.
    """
    start = _world_time(before)
    end = _world_time(after)
    if start is None or end is None:
        return None
    if (start.tzinfo is None) != (end.tzinfo is None):
        # The mod changed how it stamps the clock mid-action; subtracting these
        # would mean inventing the offset this check exists to measure.
        return None
    elapsed = (end - start).total_seconds()
    return None if elapsed < 0 else elapsed


@dataclass(frozen=True, slots=True)
class _SleepSpec:
    bed_ref: str | None
    hours: int
    allow_vehicle_seat: bool
    max_wait_ms: int

    @classmethod
    def parse(cls, command: Command) -> _SleepSpec:
        check_args(command, allowed=("bed_ref", "hours", "allow_vehicle_seat", "max_wait_ms"))
        return cls(
            bed_ref=_read_optional_square_ref(command, "bed_ref"),
            hours=read_count(
                command,
                "hours",
                default=DEFAULT_SLEEP_HOURS,
                minimum=MIN_SLEEP_HOURS,
                maximum=MAX_SLEEP_HOURS,
            ),
            allow_vehicle_seat=read_flag(command, "allow_vehicle_seat", default=False),
            max_wait_ms=read_count(
                command,
                "max_wait_ms",
                default=DEFAULT_SLEEP_WAIT_MS,
                minimum=MIN_WAIT_MS,
                maximum=MAX_SLEEP_WAIT_MS,
            ),
        )

    def as_args(self) -> JsonDict:
        args: JsonDict = {
            "hours": self.hours,
            "allow_vehicle_seat": self.allow_vehicle_seat,
            "max_wait_ms": self.max_wait_ms,
        }
        if self.bed_ref is not None:
            args["bed_ref"] = self.bed_ref
        return args

    @property
    def required_game_seconds(self) -> float:
        """The world time a night of this length has to cover.

        A quarter of the request, not all of it: a sleep ends early for reasons
        the game owns — noise, pain, a full night's rest reached sooner — and
        those are still nights slept. What the fraction rules out is a "sleep"
        that advanced the clock by a minute.
        """
        return self.hours * 3600.0 * 0.25


@dataclass(frozen=True, slots=True)
class SleepAdapter:
    """``survival.sleep``: sleep a night off, verified against clock and fatigue.

    Refused outright while the guard reports any danger at all — a stricter bar
    than the engine's threat threshold, because every other action can be
    cancelled mid-flight and this one cannot.
    """

    timeout_ms: int = DEFAULT_SLEEP_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_SLEEP_POLL_MS

    name: ClassVar[ActionName] = ActionName.SURVIVAL_SLEEP
    risk: ClassVar[RiskClass] = RiskClass.P4
    required_capability: ClassVar[str | None] = SURVIVAL_SLEEP

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _SleepSpec.parse(command)
        if observation.safety.danger_level > DangerLevel.NONE:
            raise PreconditionFailed(
                f"danger level {observation.safety.danger_level.value}: a sleeping "
                "character cannot be woken by this mod, so sleep is never started "
                "with a threat on the board",
                reason_code=ReasonCode.POLICY_DENIED,
                evidence={"danger_level": observation.safety.danger_level.value},
            )
        if _reported_stat(observation, _FATIGUE) is None:
            raise PreconditionFailed(
                "the mod reported no fatigue, so a night's sleep could not be verified",
                reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                evidence={"observation_seq": observation.seq},
            )
        if _world_time(observation) is None:
            raise PreconditionFailed(
                "the mod reported no readable world_time, so the hours slept could not be verified",
                reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                evidence={"world_time": observation.game.world_time},
            )
        if spec.bed_ref is None and not spec.allow_vehicle_seat:
            raise PreconditionFailed(
                "name the bed to sleep on, or allow a vehicle seat explicitly",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _SleepSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _SleepSpec.parse(command)
        was = _reported_stat(before, _FATIGUE)
        now = _reported_stat(after, _FATIGUE)
        if was is None or now is None or now >= was:
            return None
        elapsed = _elapsed_game_seconds(before, after)
        if elapsed is None or elapsed < spec.required_game_seconds:
            # Fatigue falls a little whenever the character is idle. Without the
            # world clock moving with it, this is a rest at best.
            return None
        return Evidence(
            kind="fatigue_fell_over_slept_hours",
            observation_seq=after.seq,
            observed={
                "fatigue_before": was,
                "fatigue_after": now,
                "world_time_before": before.game.world_time,
                "world_time_after": after.game.world_time,
                "elapsed_game_seconds": elapsed,
                "required_game_seconds": spec.required_game_seconds,
                "hours_requested": spec.hours,
                "bed_ref": spec.bed_ref,
            },
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        """Fraction of the required world time that has passed."""
        spec = _SleepSpec.parse(command)
        elapsed = _elapsed_game_seconds(before, latest)
        if elapsed is None or spec.required_game_seconds <= 0:
            return None
        return min(1.0, elapsed / spec.required_game_seconds)
