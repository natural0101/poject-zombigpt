"""``movement.move_to`` and ``movement.move_near``: walking, and proving it.

The postcondition is the whole design: the character's observed position is
inside the requested radius *and* on the requested floor. Anything short of
that — the walk action finished, the mod acked, the path result looked fine —
is a statement about the queue, not about where the character is standing.

Four refusals here are policy, not plumbing, and they are absolute rather than
tunable (blueprint §5.19, "поведенческие ограничения"):

* a square the mod has not reported as loaded is never a target, because a
  reference into an unloaded cell resolves to nothing this side can verify;
* a closed window is never traversed automatically;
* a drop from height is never taken automatically;
* a floor change without stairs is refused rather than attempted.

A single command is also capped in distance. Long journeys are the planner's
job to split into waypoints (§4.5): one thirty-square walk that fails halfway
tells the caller far less than three ten-square walks, one of which failed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import ClassVar, Final

from ...capabilities import MOVE_TO_SQUARE
from ...protocol import (
    ActionName,
    Command,
    CommandPolicy,
    JsonDict,
    NearbyObject,
    Observation,
    Position,
    ReasonCode,
    RefError,
    RefKind,
    RiskClass,
    SquareRef,
)
from ..adapter import Evidence, PreconditionFailed
from .common import (
    check_args,
    grid_distance,
    nearby_object,
    plane_distance,
    read_count,
    read_flag,
    read_number,
    read_position,
    read_ref,
    require_nearby,
    square_of,
)

__all__ = [
    "DEFAULT_ARRIVAL_RADIUS",
    "DEFAULT_MOVE_DISTANCE_SQUARES",
    "DEFAULT_MOVE_POLL_MS",
    "DEFAULT_MOVE_TIMEOUT_MS",
    "DEFAULT_NEAR_RADIUS",
    "DEFAULT_SAFE_RADIUS_SQUARES",
    "MAX_ARRIVAL_RADIUS",
    "MAX_MOVE_DISTANCE_SQUARES",
    "MOVE_RETRY_POLICY",
    "SEMANTIC_BLOCKED",
    "SEMANTIC_CLOSED_WINDOW",
    "SEMANTIC_DROP",
    "SEMANTIC_LOADED",
    "SEMANTIC_STAIRS",
    "SQUARE_OBJECT_KIND",
    "MoveNearAdapter",
    "MoveToAdapter",
]

#: Squares. §4.5's own example uses 30 and the reason it is a cap rather than a
#: default is §4.5's closing line: a long trip is a sequence of waypoints.
MAX_MOVE_DISTANCE_SQUARES: Final = 30
DEFAULT_MOVE_DISTANCE_SQUARES: Final = 20

#: World units. 0.75 is §4.5's example; the ceiling keeps "arrived" meaning
#: adjacent rather than "somewhere in the room".
DEFAULT_ARRIVAL_RADIUS: Final = 0.75
MAX_ARRIVAL_RADIUS: Final = 3.0

#: Interaction range for ``move_near``: close enough to open a container.
DEFAULT_NEAR_RADIUS: Final = 1.5

#: Beyond this many squares the move leaves what the adapter can treat as the
#: safe neighbourhood, and the permission tier rises to P3 (§17.2). It is a
#: floor, not the last word — the autonomy layer holds the real home radius.
DEFAULT_SAFE_RADIUS_SQUARES: Final = 20

DEFAULT_MOVE_TIMEOUT_MS: Final = 30_000
DEFAULT_MOVE_POLL_MS: Final = 250

#: §4.5 step 10: on a stall, cancel and rebuild the path *once*. The engine's
#: retry budget is where that lives, so the policy is a value callers pass with
#: the request rather than a number this module keeps to itself.
MOVE_RETRY_POLICY: Final = CommandPolicy(allow_interrupt=True, max_retries=1)

#: How the mod describes squares in ``nearby.objects``. Movement is verified
#: against the world description the observer already produces (§4.11), so
#: these names are part of the observation contract, not an internal detail.
SQUARE_OBJECT_KIND: Final = "square"
SEMANTIC_LOADED: Final = "loaded"
SEMANTIC_BLOCKED: Final = "blocked"
SEMANTIC_CLOSED_WINDOW: Final = "closed_window"
SEMANTIC_DROP: Final = "drop"
SEMANTIC_STAIRS: Final = "stairs"


def _check_declared_square_ref(command: Command, x: int, y: int, z: int) -> None:
    """Refuse a ``square_ref`` that names anything other than ``target``.

    ``build_args`` mints this argument, so the prepared command carries it and
    it has to be *accepted* here — but accepting it is not the same as ignoring
    it. A caller-supplied reference pointing at another square would otherwise
    be silently overwritten, and the command the mod ran would not be the
    command that was asked for.
    """
    if command.args.get("square_ref") is None:
        return
    given = read_ref(command, "square_ref", kind=RefKind.SQUARE)
    try:
        parsed = SquareRef.parse(given)
    except RefError as exc:
        raise PreconditionFailed(
            f"square_ref is not a well-formed square reference: {given!r}",
            reason_code=ReasonCode.INVALID_REF,
        ) from exc
    if (parsed.x, parsed.y, parsed.z) != (x, y, z):
        raise PreconditionFailed(
            f"square_ref names ({parsed.x}, {parsed.y}, {parsed.z}) while target "
            f"names ({x}, {y}, {z})",
            reason_code=ReasonCode.INVALID_ARGUMENT,
            evidence={"square_ref": given, "target": {"x": x, "y": y, "z": z}},
        )


@dataclass(frozen=True, slots=True)
class _MoveToSpec:
    """The normalised form of a ``movement.move_to`` command.

    Parsed by ``validate``, emitted by ``build_args`` and re-parsed by
    ``verify``, which sees the *prepared* command. One parser for all three is
    what keeps the value verified against equal to the value sent.
    """

    x: int
    y: int
    z: int
    radius: float
    max_distance: int
    allow_doors: bool
    allow_stairs: bool

    @classmethod
    def parse(cls, command: Command) -> _MoveToSpec:
        """Read either shape of this command: as issued, or as shipped.

        ``verify`` is handed the command the engine actually sent, whose args
        are :meth:`as_args` output — so a parser that accepted only the issued
        shape would refuse the adapter's own payload and fail every
        verification. The issued shape names the destination as a ``target``
        object; the shipped one names it as three scalars, because the mod's
        dispatcher accepts nothing else.
        """
        check_args(
            command,
            allowed=(
                "target",
                "x",
                "y",
                "z",
                "radius",
                "max_distance",
                "allow_doors",
                "allow_windows",
                "allow_stairs",
                "square_ref",
            ),
        )
        if read_flag(command, "allow_windows", default=False):
            raise PreconditionFailed(
                "climbing through a window is never done automatically; "
                "ask for it as a separate, explicitly permitted action",
                reason_code=ReasonCode.POLICY_DENIED,
            )
        if "target" in command.args:
            x, y, z = read_position(command.args.get("target"), field_name="target")
        else:
            x, y, z = read_position(
                {key: command.args.get(key) for key in ("x", "y", "z")},
                field_name="x/y/z",
            )
        _check_declared_square_ref(command, x, y, z)
        return cls(
            x=x,
            y=y,
            z=z,
            radius=read_number(
                command,
                "radius",
                default=DEFAULT_ARRIVAL_RADIUS,
                minimum=0.1,
                maximum=MAX_ARRIVAL_RADIUS,
            ),
            max_distance=read_count(
                command,
                "max_distance",
                default=DEFAULT_MOVE_DISTANCE_SQUARES,
                minimum=1,
                maximum=MAX_MOVE_DISTANCE_SQUARES,
            ),
            allow_doors=read_flag(command, "allow_doors", default=True),
            allow_stairs=read_flag(command, "allow_stairs", default=True),
        )

    def as_args(self, session_id: str) -> JsonDict:
        """The payload the mod receives — which is not the command's own shape.

        Three scalars rather than a ``target`` object, because
        ``PZAgent.CommandDispatcher`` accepts only scalar arguments: a nested
        table is refused before an adapter is reached, so a ``target`` object
        could never have arrived. It used to be sent anyway, together with a
        ``square_ref`` the mod recomputes and four flags the mod does not
        declare, and every one of those keys is an undeclared argument — which
        is a refusal, not an ignored field. ``movement.move_to`` was therefore
        refused outright, every time.

        ``max_distance``, ``allow_doors``, ``allow_windows`` and
        ``allow_stairs`` stay on this side deliberately. They are decisions, and
        :meth:`parse` and :func:`_check_square` have already made them against
        the observation. Sending them would ask the mod to re-derive a policy it
        has no observation to re-derive it from.
        """
        del session_id  # the mod resolves the square from the coordinates
        return {
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "radius": self.radius,
        }

    def arrived(self, position: Position) -> bool:
        return position.z == self.z and plane_distance(position, self.x, self.y) <= self.radius


def _square_object(observation: Observation, x: int, y: int, z: int) -> NearbyObject | None:
    """The mod's description of one square, or None when it did not report it."""
    nearby = require_nearby(observation)
    for candidate in nearby.objects:
        if candidate.kind != SQUARE_OBJECT_KIND or candidate.position is None:
            continue
        if square_of(candidate.position) == (x, y, z):
            return candidate
    return None


def _check_square(square: NearbyObject, *, allow_stairs: bool, changes_floor: bool) -> None:
    """Refuse a destination square the agent must not walk onto."""
    semantics = frozenset(square.semantics)
    if SEMANTIC_LOADED not in semantics:
        raise PreconditionFailed(
            f"square {square.ref} is described but not loaded",
            reason_code=ReasonCode.TARGET_NOT_LOADED,
            evidence={"square_ref": square.ref, "semantics": sorted(semantics)},
        )
    if SEMANTIC_BLOCKED in semantics:
        raise PreconditionFailed(
            f"square {square.ref} is blocked",
            reason_code=ReasonCode.PATH_NOT_FOUND,
            evidence={"square_ref": square.ref, "semantics": sorted(semantics)},
        )
    if SEMANTIC_CLOSED_WINDOW in semantics:
        raise PreconditionFailed(
            f"reaching square {square.ref} means going through a closed window",
            reason_code=ReasonCode.POLICY_DENIED,
            evidence={"square_ref": square.ref, "semantics": sorted(semantics)},
        )
    if SEMANTIC_DROP in semantics:
        raise PreconditionFailed(
            f"reaching square {square.ref} means dropping from height",
            reason_code=ReasonCode.POLICY_DENIED,
            evidence={"square_ref": square.ref, "semantics": sorted(semantics)},
        )
    if changes_floor and not (allow_stairs and SEMANTIC_STAIRS in semantics):
        raise PreconditionFailed(
            f"square {square.ref} is on another floor and no permitted stairs reach it",
            reason_code=ReasonCode.PATH_NOT_FOUND,
            evidence={"square_ref": square.ref, "semantics": sorted(semantics)},
        )


def _arrival_evidence(
    kind: str,
    *,
    before: Observation,
    after: Observation,
    target: tuple[int, int, int],
    radius: float,
    measured_from: tuple[float, float] | None = None,
    extra: JsonDict | None = None,
) -> Evidence:
    """Package an observed arrival.

    ``measured_from`` is the point the radius test actually used. It exists
    because ``move_near`` measures against the object's own position while it
    names the *square* underneath it: recording the distance to the square
    instead would put a number in the evidence that can exceed the radius the
    check just passed, and evidence that contradicts its own criterion is worse
    than no evidence at all.
    """
    origin = (float(target[0]), float(target[1])) if measured_from is None else measured_from
    observed: JsonDict = {
        "position": after.player.position.to_dict(),
        "position_before": before.player.position.to_dict(),
        "target": {"x": target[0], "y": target[1], "z": target[2]},
        "radius": radius,
        "distance": plane_distance(after.player.position, origin[0], origin[1]),
        "floor": after.player.position.z,
    }
    observed.update(extra or {})
    return Evidence(kind=kind, observation_seq=after.seq, observed=observed)


@dataclass(frozen=True, slots=True)
class MoveToAdapter:
    """``movement.move_to``: stand on a named square.

    Progress is reported as the fraction of the original gap that has closed,
    which is what turns the engine's generic "nothing changed" stall detector
    into a real stuck check: a character shoved against a fence keeps its walk
    action busy and its queue entry alive, and only the distance stops moving.
    """

    timeout_ms: int = DEFAULT_MOVE_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_MOVE_POLL_MS
    safe_radius_squares: int = DEFAULT_SAFE_RADIUS_SQUARES

    name: ClassVar[ActionName] = ActionName.MOVEMENT_MOVE_TO
    risk: ClassVar[RiskClass] = RiskClass.P2
    required_capability: ClassVar[str | None] = MOVE_TO_SQUARE

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _MoveToSpec.parse(command)
        position = observation.player.position
        distance = grid_distance(position, spec.x, spec.y)
        if distance > spec.max_distance:
            raise PreconditionFailed(
                f"the target is {distance} squares away, past the {spec.max_distance} "
                "a single move command covers",
                reason_code=ReasonCode.TARGET_OUT_OF_RANGE,
                evidence={"distance_squares": distance, "max_distance": spec.max_distance},
            )
        square = _square_object(observation, spec.x, spec.y, spec.z)
        if square is None:
            raise PreconditionFailed(
                f"no loaded square was reported at ({spec.x}, {spec.y}, {spec.z})",
                reason_code=ReasonCode.TARGET_NOT_LOADED,
                evidence={"target": {"x": spec.x, "y": spec.y, "z": spec.z}},
            )
        _check_square(
            square,
            allow_stairs=spec.allow_stairs,
            changes_floor=spec.z != position.z,
        )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _MoveToSpec.parse(command).as_args(command.session_id)

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _MoveToSpec.parse(command)
        if not spec.arrived(after.player.position):
            return None
        return _arrival_evidence(
            "position_within_radius",
            before=before,
            after=after,
            target=(spec.x, spec.y, spec.z),
            radius=spec.radius,
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        spec = _MoveToSpec.parse(command)
        start = plane_distance(before.player.position, spec.x, spec.y)
        if start <= spec.radius:
            return 1.0
        remaining = plane_distance(latest.player.position, spec.x, spec.y)
        return min(1.0, max(0.0, (start - remaining) / start))

    def risk_for(self, command: Command, observation: Observation) -> RiskClass:
        """The tier this particular move needs, never below :attr:`risk`.

        Distance and a floor change are the two things that turn a step across
        the room into leaving the area the agent was trusted with, and neither
        is visible from the action name alone.
        """
        spec = _MoveToSpec.parse(command)
        position = observation.player.position
        if spec.z != position.z:
            return RiskClass.P3
        if grid_distance(position, spec.x, spec.y) > self.safe_radius_squares:
            return RiskClass.P3
        return self.risk


@dataclass(frozen=True, slots=True)
class _MoveNearSpec:
    object_ref: str
    radius: float
    max_distance: int

    @classmethod
    def parse(cls, command: Command) -> _MoveNearSpec:
        # ``ref`` and ``reach`` are the shipped spellings; see _MoveToSpec.parse
        # for why both shapes have to be readable here.
        check_args(
            command,
            allowed=("object_ref", "ref", "radius", "reach", "max_distance", "allow_windows"),
        )
        if "object_ref" not in command.args and "ref" in command.args:
            command = replace(command, args={**command.args, "object_ref": command.args["ref"]})
        if "radius" not in command.args and "reach" in command.args:
            command = replace(command, args={**command.args, "radius": command.args["reach"]})
        if "object_ref" not in command.args:
            raise PreconditionFailed(
                "missing required argument(s): ['object_ref']",
                reason_code=ReasonCode.INVALID_ARGUMENT,
            )
        if read_flag(command, "allow_windows", default=False):
            raise PreconditionFailed(
                "approaching something through a window is never done automatically",
                reason_code=ReasonCode.POLICY_DENIED,
            )
        return cls(
            # Not RefKind.OBJECT, though the argument is named object_ref and the
            # protocol defines that kind. PZAgent.ObserveModel never mints one:
            # it describes a nearby thing as a container reference when it holds
            # a container and as a square reference otherwise, "because that
            # reference is the one an inventory transfer can actually name". So
            # insisting on an object reference here rejected every reference the
            # mod is capable of producing, and move_near could not be reached
            # from a real observation at all.
            object_ref=read_ref(
                command,
                "object_ref",
                kind=(RefKind.CONTAINER, RefKind.SQUARE, RefKind.ITEM),
            ),
            radius=read_number(
                command,
                "radius",
                default=DEFAULT_NEAR_RADIUS,
                minimum=0.1,
                maximum=MAX_ARRIVAL_RADIUS,
            ),
            max_distance=read_count(
                command,
                "max_distance",
                default=DEFAULT_MOVE_DISTANCE_SQUARES,
                minimum=1,
                maximum=MAX_MOVE_DISTANCE_SQUARES,
            ),
        )

    def as_args(self) -> JsonDict:
        """``ref`` and ``reach``, which is what the mod's adapter declares.

        The command-side names (``object_ref``, ``radius``) are the planner's
        and the MCP surface's; the mod's are these. Translating here is this
        method's whole job, and sending the command's own names instead meant
        every argument was undeclared and the command refused.
        """
        return {
            "ref": self.object_ref,
            "reach": self.radius,
        }


@dataclass(frozen=True, slots=True)
class MoveNearAdapter:
    """``movement.move_near``: get within interaction range of a world object.

    Verified against the object's *re-observed* position rather than the one it
    had when the command was issued (§5.11): after moving, references and
    object indices are re-resolved, and an object that is no longer in view
    cannot be proven to be within arm's reach.
    """

    timeout_ms: int = DEFAULT_MOVE_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_MOVE_POLL_MS
    safe_radius_squares: int = DEFAULT_SAFE_RADIUS_SQUARES

    name: ClassVar[ActionName] = ActionName.MOVEMENT_MOVE_NEAR
    risk: ClassVar[RiskClass] = RiskClass.P3
    required_capability: ClassVar[str | None] = MOVE_TO_SQUARE

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _MoveNearSpec.parse(command)
        target = nearby_object(require_nearby(observation), spec.object_ref)
        if target is None:
            raise PreconditionFailed(
                f"object {spec.object_ref} is not in the observed surroundings",
                reason_code=ReasonCode.TARGET_NOT_LOADED,
                evidence={"object_ref": spec.object_ref},
            )
        if target.position is None:
            raise PreconditionFailed(
                f"object {spec.object_ref} was reported without a position, "
                "so arrival on its floor cannot be verified",
                reason_code=ReasonCode.TARGET_NOT_LOADED,
                evidence={"object_ref": spec.object_ref},
            )
        x, y, z = square_of(target.position)
        distance = grid_distance(observation.player.position, x, y)
        if distance > spec.max_distance:
            raise PreconditionFailed(
                f"{spec.object_ref} is {distance} squares away, past the {spec.max_distance} "
                "a single move command covers",
                reason_code=ReasonCode.TARGET_OUT_OF_RANGE,
                evidence={"distance_squares": distance, "max_distance": spec.max_distance},
            )
        square = _square_object(observation, x, y, z)
        if square is None:
            raise PreconditionFailed(
                f"no loaded square was reported under {spec.object_ref}",
                reason_code=ReasonCode.TARGET_NOT_LOADED,
                evidence={"object_ref": spec.object_ref, "square": {"x": x, "y": y, "z": z}},
            )
        _check_square(
            square,
            allow_stairs=True,
            changes_floor=z != observation.player.position.z,
        )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _MoveNearSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _MoveNearSpec.parse(command)
        if after.nearby is None:
            return None
        target = nearby_object(after.nearby, spec.object_ref)
        if target is None or target.position is None:
            return None
        x, y, z = square_of(target.position)
        if after.player.position.z != z:
            return None
        if (
            plane_distance(after.player.position, target.position.x, target.position.y)
            > spec.radius
        ):
            return None
        return _arrival_evidence(
            "position_near_object",
            before=before,
            after=after,
            target=(x, y, z),
            radius=spec.radius,
            measured_from=(target.position.x, target.position.y),
            extra={"object_ref": spec.object_ref, "reported_distance": target.distance},
        )

    def progress(self, command: Command, before: Observation, latest: Observation) -> float | None:
        spec = _MoveNearSpec.parse(command)
        if before.nearby is None or latest.nearby is None:
            return None
        start_object = nearby_object(before.nearby, spec.object_ref)
        now_object = nearby_object(latest.nearby, spec.object_ref)
        if start_object is None or now_object is None:
            return None
        if start_object.distance <= spec.radius:
            return 1.0
        closed = start_object.distance - now_object.distance
        return min(1.0, max(0.0, closed / start_object.distance))

    def risk_for(self, command: Command, observation: Observation) -> RiskClass:
        """Always P3: approaching a world object is touching the world (§17.2)."""
        return self.risk
