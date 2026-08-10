"""``door.open``, ``door.close`` and ``door.unlock``: doors as observed facts.

Three actions rather than one with a mode argument, because the three fail
differently and the failures are the interesting part. A door that is merely
closed is not an error at all — closed is what doors are for. A door that is
*locked* needs a key hunt, and a door that is *barricaded* needs the planks off
or a detour, and ``DOOR_LOCKED`` / ``DOOR_BARRICADED`` exist as separate reason
codes precisely because a planner replans them differently.

Everything this side knows about a door it knows from the observation. The
mod's observer reports each nearby door with tri-state ``open``, ``locked`` and
``barricaded`` fields, and the tri-state is load-bearing: ``None`` means the
build exposed no reader for that fact, which must never be conflated with an
observed ``False`` — "the lock could not be read" and "the door is unlocked"
authorise very different plans. Every gate in this file therefore fires only on
an observed ``True`` or an observed ``False``, and an absent field passes the
command through to the mod, which resolves the engine object itself and owns
the last word.

The postcondition is the *following observation* describing the door in the
asked-for state. The mod proves its half by re-reading the ``IsoDoor`` after
the toggle (``adapters/Doors.lua``); this side proves its half by re-reading
the observation, which is the only surface it can check anything against. A
door that left the observation radius proves nothing — out of view is not open
— so ``verify`` declines and the engine reports the timeout or the mod's own
code honestly.

A door already in the asked-for state is not pre-refused. The mod answers it
as an unchanged success with identical before/after readings, and a refusal
here would turn "nothing needed doing, and that was observed" into an error
the caller has to special-case. ``door.unlock`` follows the same rule for the
same reason: an observed-unlocked door passes validation and comes back as the
mod's unchanged success, rather than being refused ``PRECONDITION_FAILED`` to
save a round trip. One semantics for all three, chosen and tested, so a
planner never has to know which of the three actions treats "already done" as
a failure — none of them does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities.probes import DOOR_TOGGLE
from ...protocol import (
    ActionName,
    Command,
    JsonDict,
    NearbyObject,
    Observation,
    ReasonCode,
    RefKind,
    RiskClass,
)
from ..adapter import Evidence, PreconditionFailed
from .common import (
    check_args,
    nearby_object,
    plane_distance,
    read_number,
    read_ref,
    require_nearby,
)
from .movement import MAX_ARRIVAL_RADIUS

__all__ = [
    "DEFAULT_DOOR_POLL_MS",
    "DEFAULT_DOOR_RADIUS",
    "DEFAULT_DOOR_TIMEOUT_MS",
    "DOOR_OBJECT_KIND",
    "MIN_DOOR_RADIUS",
    "DoorCloseAdapter",
    "DoorOpenAdapter",
    "DoorUnlockAdapter",
]

#: How the mod describes a door in ``nearby.objects``. Part of the observation
#: contract, like :data:`~.movement.SQUARE_OBJECT_KIND`: the state gates below
#: only mean anything on an object the observer itself called a door.
DOOR_OBJECT_KIND: Final = "door"

#: Reach for the toggle: close enough to push the door, the same interaction
#: range the container open uses. The floor keeps a caller from naming a radius
#: no position ever satisfies; the ceiling is movement's own, mirrored by
#: ``Doors.MAX_RADIUS`` in ``adapters/Doors.lua``.
DEFAULT_DOOR_RADIUS: Final = 1.6
MIN_DOOR_RADIUS: Final = 0.1

DEFAULT_DOOR_TIMEOUT_MS: Final = 30_000
DEFAULT_DOOR_POLL_MS: Final = 250


@dataclass(frozen=True, slots=True)
class _DoorSpec:
    """The normalised form all three door commands share.

    Parsed by ``validate``, emitted by ``build_args`` and re-parsed by
    ``verify``, which sees the *prepared* command; one parser for all three
    keeps the value verified against equal to the value sent. The shipped
    shape and the issued shape are the same here — ``door_ref`` and ``radius``
    are exactly what the mod's adapters declare — so the fixpoint is trivial
    and stays that way only while both sides keep these two names.
    """

    door_ref: str
    radius: float

    @classmethod
    def parse(cls, command: Command) -> _DoorSpec:
        check_args(command, allowed=("door_ref", "radius"), required=("door_ref",))
        return cls(
            door_ref=read_ref(command, "door_ref", kind=RefKind.OBJECT),
            radius=read_number(
                command,
                "radius",
                default=DEFAULT_DOOR_RADIUS,
                minimum=MIN_DOOR_RADIUS,
                maximum=MAX_ARRIVAL_RADIUS,
            ),
        )

    def as_args(self) -> JsonDict:
        return {"door_ref": self.door_ref, "radius": self.radius}


def _observed_door(observation: Observation, door_ref: str) -> NearbyObject:
    """The door the command names, as the observer described it, or a refusal.

    The observation is the only truth this side has: a reference the observer
    did not report is one whose state, distance and very doorness cannot be
    checked here, whatever the mod might resolve at the far end. And an object
    the observer reported as something *other* than a door is worse than
    absent — the reference format indexes the square's object list, so a
    non-door at the index means the list has shifted since the reference was
    minted, and acting on it would touch an object nobody named.
    """
    door = nearby_object(require_nearby(observation), door_ref)
    if door is None:
        raise PreconditionFailed(
            f"door {door_ref} is not in the observed surroundings",
            reason_code=ReasonCode.TARGET_NOT_LOADED,
            evidence={"door_ref": door_ref},
        )
    if door.kind != DOOR_OBJECT_KIND:
        raise PreconditionFailed(
            f"{door_ref} is observed as a {door.kind}, not a door — the square's "
            "object list has shifted since the reference was minted",
            reason_code=ReasonCode.INVALID_REF,
            evidence={"door_ref": door_ref, "observed_kind": door.kind},
        )
    return door


def _refuse_barricaded(door: NearbyObject) -> None:
    """Planks hold the door where it is, whichever way the command pushes.

    Only an observed ``True`` fires: an unreadable barricade state is the
    mod's to judge against the engine object, and refusing on ``None`` would
    turn a missing reader into a wall that is not there.
    """
    if door.barricaded is True:
        raise PreconditionFailed(
            f"door {door.ref} is barricaded; it needs the barricade removed, or a detour",
            reason_code=ReasonCode.DOOR_BARRICADED,
            evidence={"door_ref": door.ref},
        )


def _door_state_evidence(
    *,
    before: Observation,
    after: Observation,
    door_ref: str,
    door_after: NearbyObject,
) -> Evidence:
    """Package an observed door state change (or unchanged success).

    ``open_before`` is the observation's memory, so it can honestly be ``None``
    when the earlier snapshot did not expose the reader or did not include the
    door at all; ``open_after`` never is, because the caller only builds this
    once the after-state was observed. ``walked`` is whether the character's
    position moved between the two snapshots — the closest observable fact to
    the mod's own ``walked`` mark, which records whether a walk was queued.
    """
    door_before = None if before.nearby is None else nearby_object(before.nearby, door_ref)
    start = before.player.position
    now = after.player.position
    walked = now.z != start.z or plane_distance(now, start.x, start.y) > 0.0
    return Evidence(
        kind="door_state_changed",
        observation_seq=after.seq,
        observed={
            "door_ref": door_ref,
            "open_before": None if door_before is None else door_before.open,
            "open_after": door_after.open,
            "walked": walked,
            "distance": door_after.distance,
        },
    )


def _approach_progress(spec: _DoorSpec, before: Observation, latest: Observation) -> float | None:
    """Fraction of the original gap to the door that has closed.

    The same measure movement uses, off the observer's own reported distance:
    the generic stall detector watches the action queue, which keeps looking
    alive while a character is wedged against a fence, and only the distance
    stops moving. A door missing from either snapshot yields no opinion.
    """
    if before.nearby is None or latest.nearby is None:
        return None
    start = nearby_object(before.nearby, spec.door_ref)
    now = nearby_object(latest.nearby, spec.door_ref)
    if start is None or now is None:
        return None
    if start.distance <= spec.radius:
        return 1.0
    closed = start.distance - now.distance
    return min(1.0, max(0.0, closed / start.distance))


@dataclass(frozen=True, slots=True)
class DoorOpenAdapter:
    """``door.open``: the door is observed open in the following snapshot.

    Two refusals here save a round trip, and both fire only on an observed
    ``True``: a barricaded door cannot move at all, and a locked door will
    swallow the toggle — the game plays a rattle and nothing else — so sending
    the command would cost a walk to learn what the observation already says.
    A door observed already open is *not* refused: the mod answers it as an
    unchanged success, and ``verify`` reads the identical before/after states
    as the postcondition they are.
    """

    timeout_ms: int = DEFAULT_DOOR_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_DOOR_POLL_MS

    name: ClassVar[ActionName] = ActionName.DOOR_OPEN
    risk: ClassVar[RiskClass] = RiskClass.P3
    required_capability: ClassVar[str | None] = DOOR_TOGGLE

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _DoorSpec.parse(command)
        door = _observed_door(observation, spec.door_ref)
        _refuse_barricaded(door)
        # Only when the door is not already open: a lock holds a door closed,
        # so on an already-open door it forbids nothing this command wants.
        # ``locked is True`` and not merely truthy — an absent lock reader is
        # the mod's to judge, and must never read as either answer.
        if door.open is not True and door.locked is True:
            raise PreconditionFailed(
                f"door {spec.door_ref} is locked; it needs its key (door.unlock) "
                "before it will open",
                reason_code=ReasonCode.DOOR_LOCKED,
                evidence={"door_ref": spec.door_ref},
            )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _DoorSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _DoorSpec.parse(command)
        if after.nearby is None:
            return None
        door = nearby_object(after.nearby, spec.door_ref)
        # A door out of the observation radius proves nothing — the walk moved
        # the character, not the fact. And ``open is True``, never truthiness:
        # an unreadable state is not an open door.
        if door is None or door.open is not True:
            return None
        return _door_state_evidence(
            before=before, after=after, door_ref=spec.door_ref, door_after=door
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        return _approach_progress(_DoorSpec.parse(command), before, latest)


@dataclass(frozen=True, slots=True)
class DoorCloseAdapter:
    """``door.close``: the door is observed closed in the following snapshot.

    The barricade gate is the only state refusal: a lock holds a door closed,
    so it blocks opening and never blocks closing, and a door observed already
    closed comes back as the mod's unchanged success like an already-open one
    does for ``door.open``.
    """

    timeout_ms: int = DEFAULT_DOOR_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_DOOR_POLL_MS

    name: ClassVar[ActionName] = ActionName.DOOR_CLOSE
    risk: ClassVar[RiskClass] = RiskClass.P3
    required_capability: ClassVar[str | None] = DOOR_TOGGLE

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _DoorSpec.parse(command)
        _refuse_barricaded(_observed_door(observation, spec.door_ref))

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _DoorSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _DoorSpec.parse(command)
        if after.nearby is None:
            return None
        door = nearby_object(after.nearby, spec.door_ref)
        # ``open is False`` demands the observed fact. An absent reader would
        # satisfy ``not door.open`` while proving nothing, which is exactly the
        # conflation the tri-state fields exist to forbid.
        if door is None or door.open is not False:
            return None
        return _door_state_evidence(
            before=before, after=after, door_ref=spec.door_ref, door_after=door
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        return _approach_progress(_DoorSpec.parse(command), before, latest)


@dataclass(frozen=True, slots=True)
class DoorUnlockAdapter:
    """``door.unlock``: the lock is observed off in the following snapshot.

    The key search is the mod's: whether a key for this door is carried is a
    question about engine objects (``haveThisKeyForDoor``, key ids on rings)
    that the observation does not carry, so this side gates only on what it
    can see — the door exists, it is not barricaded — and the mod answers
    ``DOOR_LOCKED`` when no key is aboard.

    A door observed already unlocked is deliberately *not* pre-refused. The
    mod treats it as an unchanged success with identical before/after lock
    readings, and the one alternative — refusing ``PRECONDITION_FAILED``
    "already unlocked" to save the round trip — would make this the only door
    action that reports "already done" as an error. One semantics for all
    three, and it is the mod's.
    """

    timeout_ms: int = DEFAULT_DOOR_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_DOOR_POLL_MS

    name: ClassVar[ActionName] = ActionName.DOOR_UNLOCK
    risk: ClassVar[RiskClass] = RiskClass.P3
    required_capability: ClassVar[str | None] = DOOR_TOGGLE

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _DoorSpec.parse(command)
        _refuse_barricaded(_observed_door(observation, spec.door_ref))

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _DoorSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _DoorSpec.parse(command)
        if after.nearby is None:
            return None
        door = nearby_object(after.nearby, spec.door_ref)
        # The one fact this command exists to change, read from the following
        # observation. ``locked is False`` and nothing weaker: an absent lock
        # reader is not an unlocked door, however the toggle sounded.
        if door is None or door.locked is not False:
            return None
        door_before = None if before.nearby is None else nearby_object(before.nearby, spec.door_ref)
        return Evidence(
            kind="door_unlocked",
            observation_seq=after.seq,
            observed={
                "door_ref": spec.door_ref,
                "locked_before": None if door_before is None else door_before.locked,
                "locked_after": door.locked,
            },
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        return _approach_progress(_DoorSpec.parse(command), before, latest)
