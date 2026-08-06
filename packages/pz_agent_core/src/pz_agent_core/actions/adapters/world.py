"""``world.inspect``: reading the squares around the character.

A read-only action's postcondition is unusual enough to be worth stating
plainly: what a look proves is that a reading was *taken*, so the evidence is
the findings themselves. There is no before/after pair, because nothing changed
— which is precisely why the check below is that the *observation* describes the
squares the command asked about. A report that names no requested square is not
a look at the requested block; it is a look at nothing, and §4.11's "не
передавать огромный список sprites" is about what a reading contains, not about
whether one happened.

Nothing here moves the character. That absence is load-bearing rather than
incidental: ``world.inspect`` is in
:data:`~pz_agent_core.protocol.READ_ONLY_ACTIONS`, so it runs in OBSERVE mode
and without arming, and an inspect that walked round a corner to see better
would be a mutating command wearing a read-only command's permissions.

It also names no ``required_capability``, and that is deliberate. Every probe in
:mod:`pz_agent_core.capabilities.probes` resolves a *Lua* symbol on the user's
install; what a look needs is the observation's ``nearby`` tier, which the mod
either produced or did not. Declaring a probe over Java accessors the Lua
scanner can never see would report ``unsupported`` on a perfectly good install,
so the gate is the honest one: no ``nearby`` tier, no inspect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...protocol import (
    ActionName,
    Command,
    JsonDict,
    NearbyObject,
    NearbyView,
    Observation,
    ReasonCode,
    RefError,
    RefKind,
    RiskClass,
    SquareRef,
)
from ..adapter import Evidence, PreconditionFailed
from .common import check_args, read_count, read_ref, require_nearby, square_of
from .movement import SQUARE_OBJECT_KIND

__all__ = [
    "DEFAULT_INSPECT_POLL_MS",
    "DEFAULT_INSPECT_RADIUS",
    "DEFAULT_INSPECT_TIMEOUT_MS",
    "MAX_INSPECT_RADIUS",
    "MAX_REPORTED_SQUARES",
    "WorldInspectAdapter",
]

#: Squares out from the centre. Two is a five-by-five block — enough to answer
#: "what is in this room with me", small enough that the reading stays a glance
#: rather than a survey. The mod applies the same ceiling
#: (``adapters/World.lua``); stating it twice is deliberate, because a radius
#: this side waves through is one the mod refuses.
MAX_INSPECT_RADIUS: Final = 2
DEFAULT_INSPECT_RADIUS: Final = 1

#: The largest block a reading can cover, and therefore the bound on the square
#: list that ends up in the evidence.
MAX_REPORTED_SQUARES: Final = (2 * MAX_INSPECT_RADIUS + 1) ** 2

#: A look is answered by the next observation, so the budget is short: waiting
#: longer would not make the mod describe more of the world.
DEFAULT_INSPECT_TIMEOUT_MS: Final = 5_000
DEFAULT_INSPECT_POLL_MS: Final = 100


@dataclass(frozen=True, slots=True)
class _InspectSpec:
    """The normalised centre and radius of one reading.

    Parsed by ``validate``, emitted by ``build_args`` and re-parsed by
    ``verify``, which sees the *prepared* command — one parser for all three, so
    the block that is checked is the block that was asked for.
    """

    x: int
    y: int
    z: int
    radius: int

    @classmethod
    def parse(cls, command: Command, observation: Observation) -> _InspectSpec:
        check_args(command, allowed=("ref", "radius"))
        radius = read_count(
            command,
            "radius",
            default=DEFAULT_INSPECT_RADIUS,
            minimum=0,
            maximum=MAX_INSPECT_RADIUS,
        )
        raw = command.args.get("ref")
        if raw is None:
            # No centre named is "look around me", which is the only centre a
            # reading can pick for itself. The floor comes from the character's
            # own z, so it never reads the storey below.
            x, y, z = square_of(observation.player.position)
            return cls(x=x, y=y, z=z, radius=radius)
        given = read_ref(command, "ref", kind=RefKind.SQUARE)
        try:
            parsed = SquareRef.parse(given)
        except RefError as exc:
            raise PreconditionFailed(
                f"ref is not a well-formed square reference: {given!r}",
                reason_code=ReasonCode.INVALID_REF,
            ) from exc
        return cls(x=parsed.x, y=parsed.y, z=parsed.z, radius=radius)

    def as_args(self, session_id: str) -> JsonDict:
        return {
            "ref": str(SquareRef(session_id=session_id, x=self.x, y=self.y, z=self.z)),
            "radius": self.radius,
        }

    def squares(self) -> tuple[tuple[int, int, int], ...]:
        """Every square the reading covers, in a stable order."""
        return tuple(
            (self.x + dx, self.y + dy, self.z)
            for dx in range(-self.radius, self.radius + 1)
            for dy in range(-self.radius, self.radius + 1)
        )


def _reported_square(nearby: NearbyView, x: int, y: int, z: int) -> NearbyObject | None:
    """The mod's description of one square, or None when it did not report it."""
    for candidate in nearby.objects:
        if candidate.kind != SQUARE_OBJECT_KIND or candidate.position is None:
            continue
        if square_of(candidate.position) == (x, y, z):
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class WorldInspectAdapter:
    """``world.inspect``: describe the block of squares around a centre.

    An unloaded square is not a failure — a block at the edge of the loaded
    world always has some — but a block where *nothing* answered is. That is the
    same line the mod draws (``loaded == 0`` is ``TARGET_NOT_LOADED`` there),
    and it is the difference between a partial reading and no reading at all.
    """

    timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_INSPECT_POLL_MS

    name: ClassVar[ActionName] = ActionName.WORLD_INSPECT
    risk: ClassVar[RiskClass] = RiskClass.P0
    required_capability: ClassVar[str | None] = None

    def validate(self, command: Command, observation: Observation) -> None:
        _InspectSpec.parse(command, observation)
        # Refuses with CAPABILITY_UNAVAILABLE when the mod produced no
        # surroundings: a look whose findings cannot be read back is a look
        # whose result could only ever be asserted.
        require_nearby(observation)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _InspectSpec.parse(command, observation).as_args(command.session_id)

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _InspectSpec.parse(command, before)
        if after.nearby is None:
            return None
        requested = spec.squares()
        described: list[JsonDict] = []
        for x, y, z in requested:
            square = _reported_square(after.nearby, x, y, z)
            if square is None:
                continue
            described.append(
                {
                    "ref": square.ref,
                    "x": x,
                    "y": y,
                    "z": z,
                    "semantics": sorted(square.semantics),
                }
            )
        if not described:
            return None
        return Evidence(
            kind="squares_described",
            observation_seq=after.seq,
            observed={
                "centre": {"x": spec.x, "y": spec.y, "z": spec.z},
                "radius": spec.radius,
                "squares_requested": len(requested),
                "squares_described": len(described),
                "squares_not_reported": len(requested) - len(described),
                "squares": described[:MAX_REPORTED_SQUARES],
            },
        )
