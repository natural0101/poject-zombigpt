"""The two building actions: read a square, then raise one structure on it.

Crafting is the first thing the agent does that cannot be undone. Building is
the first thing it does that cannot be undone *and stays in the world*. Two
planks spent on a spear are gone, but the world is where it was; a wall is a
new fact on a square, and this project ships no action that takes one down —
removing what somebody put there is a different authority, and asking for it is
not part of this wave. Everything below follows from that:

* **``building.build`` is P4, flat.** No ``risk_for``, no ladder, no argument
  that makes it cheaper. ``crafting.craft`` escalates P3 → P4 because "does
  this recipe need a workbench" genuinely changes what the command does;
  placing a permanent object has no such axis. There is no tier above P4 and
  no version of putting a wall in the world that is less than the top of the
  ladder, so the tier is a constant, and the permission machinery does the
  rest: :data:`~pz_agent_core.policy.permissions.MODE_LIMITS` names no P4
  ceiling in any mode, and ``_p4_gate`` demands the user's own initiative plus
  an explicit per-call grant. The agent may never raise a wall on its own
  initiative, in any mode, ever.
* **One command builds ONE structure once.** There is no ``count`` argument at
  all, which is a deliberate difference from ``crafting.craft``: that action
  puts its one on the wire so a future batch would be a visible protocol
  change, and a count argument is exactly what a loop in the mod would read.
  Building has no number to read. A second structure is a second command,
  through the policy, the permission gate and the safety stop again.
* **Nothing here chooses.** Whether the square is free, whether the materials
  may be spent, and whether the placement would seal the character in are
  :mod:`pz_agent_core.policy.building`'s answers, re-asked in ``validate``
  against a fresh observation so every refusal happens before a command exists
  to send. A refused wall costs a sentence; a wall that traps the character
  costs the save.
* **Success is the structure, observed on the square.** ``verify`` answers only
  to the square reading back as taken when it read clear before — a new object
  standing on it, or the observer's own assessment turning to blocked. A queued
  build is not a wall, and an ack that says the action finished is a statement
  about the queue.

Which of the two a clean install actually publishes, stated plainly because it
is the kind of thing a release note should not have to be read twice to learn:
**``building.inspect`` is published; ``building.build`` is not.**

``building.inspect`` is the read half. It is P0, it sits in
:data:`~pz_agent_core.protocol.READ_ONLY_ACTIONS`, it names a square and answers
from the readout and the window the observer already produces, and it names no
capability — for the reason the other four reads name none, and because
``adapters/Building.lua`` declares ``capability = nil`` for the same action and
the two halves of the wire owe each other one answer. Withholding it would not
buy safety anyway: it is what a user consults *before* granting the P4, so
taking it away makes that decision less informed rather than more.

``building.build`` rides :data:`~pz_agent_core.capabilities.probes.BUILDING`,
which resolves ``experimental``, and an experimental capability is not usable —
so the action that actually changes the world is withheld on every install until
a live run promotes it. That is the whole of the difference between the halves:
the one that only reads is available, the one that cannot be undone waits for
evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities.probes import BUILDING
from ...policy.building import (
    BUILDING_KEY,
    SQUARE_OBJECT_KIND,
    BuildingDecision,
    BuildingRefusal,
    Square,
    StructureView,
    assess_placement,
    enclosure_after,
    observed_structures,
    read_window,
    square_of,
    structure_named,
)
from ...protocol import (
    ActionName,
    Command,
    InventoryView,
    JsonDict,
    NearbyObject,
    Observation,
    ReasonCode,
    RefError,
    RefKind,
    RiskClass,
    SquareRef,
)
from ..adapter import Evidence, PreconditionFailed
from .common import check_args, read_count, read_ref, require_inventory, require_nearby

__all__ = [
    "DEFAULT_BUILD_TIMEOUT_MS",
    "DEFAULT_INSPECT_TIMEOUT_MS",
    "MAX_BLUEPRINT_NAME_LEN",
    "MAX_LISTED_STRUCTURES",
    "BuildingBuildAdapter",
    "BuildingInspectAdapter",
]

#: Longest blueprint name accepted, mirroring ``Building.lua``'s own
#: ``max_bytes`` and ``crafting``'s recipe bound. The value arrives from a model
#: and is matched against game data, so it is bounded and alphabet-checked here
#: exactly as the recipe name is.
MAX_BLUEPRINT_NAME_LEN: Final = 64

#: Structures one reading reports on, mirroring ``Building.MAX_LISTED``. A
#: listing that reaches a user is bounded like every other diagnostic, and the
#: bound is on the wire as well as in the reader so a truncated answer cannot
#: read as a complete one.
MAX_LISTED_STRUCTURES: Final = 16

#: A reading is answered by the next observation, so the budget is short:
#: waiting longer would not make the mod describe more of the square. The pair
#: mirrors ``Building.INSPECT_TIMEOUT_MS`` and ``Building.POLL_MS``.
DEFAULT_INSPECT_TIMEOUT_MS: Final = 5_000
DEFAULT_INSPECT_POLL_MS: Final = 250

#: One structure, and the ceiling on the whole of it. The number is the mod's
#: ``Building.BUILD_WINDOW_MS`` rather than a guess of this side's: the mod
#: closes the command on that clock, so a sidecar that waited longer would be
#: polling a command nobody is still running.
DEFAULT_BUILD_TIMEOUT_MS: Final = 30_000
DEFAULT_BUILD_POLL_MS: Final = 250

#: BuildingRefusal → the protocol reason a validation refusal reports. The
#: policy's vocabulary is the mission's; the wire speaks reason codes, and this
#: table is the one place the two are joined so they cannot drift per-adapter.
#: Total over the enum — pinned by a test. Three codes are the crafting rung's,
#: reused rather than duplicated: a missing plank is a missing plank whatever
#: it was going to become.
_REFUSAL_REASONS: Final[dict[BuildingRefusal, ReasonCode]] = {
    BuildingRefusal.STRUCTURE_UNKNOWN: ReasonCode.RECIPE_UNKNOWN,
    BuildingRefusal.SQUARE_UNREADABLE: ReasonCode.TARGET_NOT_LOADED,
    BuildingRefusal.SQUARE_OCCUPIED: ReasonCode.SQUARE_OCCUPIED,
    BuildingRefusal.WOULD_TRAP_PLAYER: ReasonCode.WOULD_TRAP_PLAYER,
    BuildingRefusal.MATERIALS_MISSING: ReasonCode.RECIPE_MATERIALS_MISSING,
    BuildingRefusal.MATERIALS_RESERVED: ReasonCode.RESOURCE_RESERVED,
}


def _read_square(command: Command) -> Square:
    """The ``square`` argument, as a reference this session minted.

    A reference rather than three integers, which is the mod's own declaration
    (``Building.lua`` types the argument ``ARG.REF`` of kind ``square``) and the
    better discipline besides: a square reference exists because the observer
    described that square and named it, so a placement can only ever target
    ground this session has actually seen. Coordinates would let a caller aim at
    a square nobody has looked at, which is precisely the picture the enclosure
    check has to refuse anyway.

    The argument is required and has no default. That absence is the safety
    property it looks like: the only square a command could pick for itself is
    the one the character is standing on, and walling in the square somebody is
    standing on is the exact mistake this wave exists to refuse.
    """
    given = read_ref(command, "square", kind=RefKind.SQUARE)
    try:
        parsed = SquareRef.parse(given)
    except RefError as exc:
        raise PreconditionFailed(
            f"square is not a well-formed square reference: {given!r}",
            reason_code=ReasonCode.INVALID_REF,
        ) from exc
    return (parsed.x, parsed.y, parsed.z)


def _square_arg(session_id: str, square: Square) -> str:
    """The square, re-minted from the parsed coordinates.

    Emitted rather than echoed so the string the mod resolves is the string this
    side reasoned about, normalised through the one constructor — the same round
    trip ``world.inspect`` does with its centre.
    """
    return str(SquareRef(session_id=session_id, x=square[0], y=square[1], z=square[2]))


def _read_blueprint_name(command: Command) -> str:
    """Read the ``blueprint`` argument, bounded and alphabet-checked.

    A blueprint name is a game identifier (``WoodenWall``-shaped). Anything
    carrying a space, a colon or a wildcard is a pattern this adapter does not
    implement rather than a blueprint it failed to find, and refusing it here
    keeps a model-authored string from reaching the mod as a lookup key.
    """
    raw = command.args.get("blueprint")
    if not isinstance(raw, str) or not raw:
        raise PreconditionFailed(
            "blueprint must be a non-empty string", reason_code=ReasonCode.INVALID_ARGUMENT
        )
    if len(raw) > MAX_BLUEPRINT_NAME_LEN:
        raise PreconditionFailed(
            f"blueprint must be at most {MAX_BLUEPRINT_NAME_LEN} characters",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    if not all(character.isalnum() or character in "._-" for character in raw):
        raise PreconditionFailed(
            "blueprint may only hold letters, digits, '.', '_' and '-'",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    return raw


@dataclass(frozen=True, slots=True)
class _BuildSpec:
    """``blueprint`` and ``square`` — the whole of a build command.

    Parsed by ``validate``, emitted by ``build_args`` and re-parsed by
    ``verify``, so the square that is proven is the square that was asked
    about: the one-parser rule ``world.inspect`` and ``crafting.craft`` follow
    for the same reason.

    Two arguments and no third. There is no ``count``, no orientation and no
    "and then the next one": every extra degree of freedom here is another thing
    a P4 approval would have to cover, and a count argument in particular is
    exactly what a loop in the mod would read.
    """

    blueprint: str
    square: Square

    @classmethod
    def parse(cls, command: Command) -> _BuildSpec:
        check_args(command, allowed=("blueprint", "square"), required=("blueprint", "square"))
        return cls(blueprint=_read_blueprint_name(command), square=_read_square(command))

    @property
    def floor(self) -> int:
        return self.square[2]

    def as_args(self, session_id: str) -> JsonDict:
        return {"blueprint": self.blueprint, "square": _square_arg(session_id, self.square)}


@dataclass(frozen=True, slots=True)
class _InspectSpec:
    """``square`` and ``limit`` — the whole of a reading.

    The mod accepts an inspect with no square at all, and answers it with the
    bare catalogue of what the character could build anywhere. This side never
    sends that one: the question worth asking before a P4 is *this blueprint,
    this square*, and a catalogue with no ground under it cannot answer the two
    refusals that matter. Being the stricter half is safe — every key this
    emits is one the mod declared — and it keeps the reading and the build
    talking about the same square.
    """

    square: Square
    limit: int

    @classmethod
    def parse(cls, command: Command) -> _InspectSpec:
        check_args(command, allowed=("square", "limit"), required=("square",))
        return cls(
            square=_read_square(command),
            limit=read_count(
                command,
                "limit",
                default=MAX_LISTED_STRUCTURES,
                minimum=1,
                maximum=MAX_LISTED_STRUCTURES,
            ),
        )

    @property
    def floor(self) -> int:
        return self.square[2]

    def as_args(self, session_id: str) -> JsonDict:
        return {"square": _square_arg(session_id, self.square), "limit": self.limit}


def _resolve_structure(inventory: InventoryView, name: str) -> StructureView:
    """The observed readout for this structure, or a refusal naming it.

    ``RECIPE_UNKNOWN`` rather than ``INVALID_ARGUMENT``: a structure no
    observed item participates in is one this side cannot say the character
    knows how to build, which is what that reason code is for.
    """
    structure = structure_named(inventory, name)
    if structure is None:
        raise PreconditionFailed(
            f"no observed item participates in a structure called {name}",
            reason_code=ReasonCode.RECIPE_UNKNOWN,
            evidence={"structure": name},
        )
    return structure


def _refuse_from(decision: BuildingDecision) -> PreconditionFailed:
    """The policy's refusal as the sidecar-side precondition failure it is.

    Raised in ``validate`` so the command never leaves this process: nothing is
    queued, nothing is placed, and the refusal carries the policy's own token,
    what was found standing on the square, and — for the refusal this wave is
    about — the enclosure answer including the claim it is entitled to make.
    """
    refusal = decision.refusal
    assert refusal is not None  # only ever called on a refused decision
    evidence: JsonDict = {
        "refusal": refusal.value,
        "structure": None if decision.structure is None else decision.structure.name,
        "square": None if decision.square is None else _square_dict(decision.square),
        "shortfalls": [s.as_dict() for s in decision.shortfalls],
    }
    if decision.occupied_by:
        evidence["occupied_by"] = list(decision.occupied_by)
    if decision.enclosure is not None:
        evidence["enclosure"] = decision.enclosure.as_dict()
    return PreconditionFailed(
        f"building policy refused: {decision.detail}",
        reason_code=_REFUSAL_REASONS[refusal],
        evidence=evidence,
    )


def _carries_readout(inventory: InventoryView) -> bool:
    """Whether any observed item carries a building readout at all.

    The inspect's own postcondition. An observation where nothing carries one
    answers no building question — which is a failed reading, not the finding
    that a structure is unknown. It is the same line ``container.inspect``
    draws between an empty crate and a crate nobody described.
    """
    return any(BUILDING_KEY in item.extra for item in inventory.items)


def _objects_on(observation: Observation, square: Square) -> dict[str, NearbyObject]:
    """Every non-square object the observation puts on *square*, by reference.

    Read straight off the observation rather than through
    :class:`~pz_agent_core.policy.building.ObservedWindow`, because ``verify``
    asks a different question from the policy's: not "may something be built
    here" but "is something here now that was not here before".
    """
    found: dict[str, NearbyObject] = {}
    nearby = observation.nearby
    if nearby is None:
        return found
    for seen in nearby.objects:
        if seen.kind == SQUARE_OBJECT_KIND or seen.position is None:
            continue
        if square_of(seen.position) == square:
            found[seen.ref] = seen
    return found


def _square_dict(square: Square) -> JsonDict:
    return {"x": square[0], "y": square[1], "z": square[2]}


@dataclass(frozen=True, slots=True)
class BuildingInspectAdapter:
    """``building.inspect``: what would happen if this stood on that square.

    Read-only in the protocol's sense and in fact: the character does not move
    and touches nothing, and the answer comes off the building readout and the
    square descriptions the observer already produces. It names no capability
    for the reason the other four reads name none — a probe over the Java
    accessors behind a blueprint table would report ``unsupported`` on a
    perfectly healthy install — and ``adapters/Building.lua`` declares
    ``capability = nil`` for the same action, which is the agreement the two
    halves owe each other.

    That makes this the published half of the rung: on a clean install
    ``building.inspect`` is available and ``building.build`` is not. The split
    is deliberate rather than tolerated. The reading is what a user consults
    *before* granting the P4 that puts something permanent in the world, and
    withholding it would leave that decision less informed, not more — while
    the action that actually changes the world stays behind an experimental
    capability until a live run promotes it.

    "Nothing may stand here" is a finding, not a failure. The failure is an
    observation that answers no building question at all: no readout on
    anything the character carries, or no window to read the square out of.
    """

    timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_INSPECT_POLL_MS

    name: ClassVar[ActionName] = ActionName.BUILDING_INSPECT
    risk: ClassVar[RiskClass] = RiskClass.P0
    required_capability: ClassVar[str | None] = None

    def validate(self, command: Command, observation: Observation) -> None:
        _InspectSpec.parse(command)
        # Both tiers, both refusing with CAPABILITY_UNAVAILABLE: a reading
        # whose findings cannot be read back is a reading whose result could
        # only ever be asserted.
        require_inventory(observation)
        require_nearby(observation)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _InspectSpec.parse(command).as_args(command.session_id)

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        """The square, and what of the observed blueprints could stand on it.

        One entry per readable structure, bounded by the command's own
        ``limit``, each carrying the verdict the *build* would reach for this
        square — so a caller sees the refusal coming instead of discovering it
        one P4 grant later. The enclosure answer is reported once, for a solid
        placement, because that is the question the listing exists to answer:
        would a wall here seal the character in.
        """
        spec = _InspectSpec.parse(command)
        inventory = after.inventory
        if inventory is None or not _carries_readout(inventory):
            return None
        window = read_window(after, spec.floor)
        if window is None:
            return None
        structures = observed_structures(inventory)
        listed = structures[: spec.limit]
        entries: list[JsonDict] = []
        for structure in listed:
            decision = assess_placement(after, structure, spec.square)
            entries.append(
                {
                    "blueprint": structure.name,
                    "known": structure.known,
                    "blocks_when_placed": structure.blocks_when_placed,
                    "buildable": not decision.refused,
                    "refusal": None if decision.refusal is None else decision.refusal.value,
                    "shortfalls": [s.as_dict() for s in decision.shortfalls],
                }
            )
        solid = enclosure_after(after, window, spec.square, blocks=True)
        return Evidence(
            kind="placement_described",
            observation_seq=after.seq,
            observed={
                "square": _square_dict(spec.square),
                "square_described": window.describes(spec.square),
                "square_occupied": window.is_occupied(spec.square),
                "occupied_by": list(window.blockers_on(spec.square)),
                "blueprints": entries,
                "listed": len(entries),
                "truncated": len(structures) > len(listed),
                "enclosure_if_solid": solid.as_dict(),
            },
        )


@dataclass(frozen=True, slots=True)
class BuildingBuildAdapter:
    """``building.build``: raise one structure on one square, prove it by the square.

    Terminal after the one placement. There is no "keep building along", no
    second attempt behind this command's id, and no fallback to a different
    square when this one turns out to be taken — a mission that wants another
    structure issues another command, and every gate between the user and the
    world gets its turn again in between.

    The tier is P4 and it is a class constant, not a computation. See the
    module docstring: there is no argument that makes placing something
    permanent cheaper, and the absence of a ``risk_for`` here is the statement
    that there is no such argument.
    """

    timeout_ms: int = DEFAULT_BUILD_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_BUILD_POLL_MS

    name: ClassVar[ActionName] = ActionName.BUILDING_BUILD
    risk: ClassVar[RiskClass] = RiskClass.P4
    required_capability: ClassVar[str | None] = BUILDING

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _BuildSpec.parse(command)
        inventory = require_inventory(observation)
        require_nearby(observation)
        structure = _resolve_structure(inventory, spec.blueprint)
        decision = assess_placement(observation, structure, spec.square)
        if decision.refused:
            raise _refuse_from(decision)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _BuildSpec.parse(command).as_args(command.session_id)

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        """Something stands on the square now that did not stand there before.

        Two readings count and they are the same fact seen from two sides: a
        world object on the square that the previous observation did not carry,
        or the observer's own assessment of the square turning to blocked. The
        first is what a wall, a door frame or a fence post looks like; the
        second is what a build that produced no separately described object
        looks like. Neither is the mod saying it finished, which is the one
        thing that could not be evidence.

        Both readings are *differences*, so both need a picture of the square
        from before. Without one there is no change to observe — only a square
        with something on it, which is as true of a square that always had it —
        so an unreadable before is an unproven postcondition rather than a
        lenient one. ``validate`` already refuses a command whose observation
        carried no window; this is the same rule stated where the claim is made.
        """
        spec = _BuildSpec.parse(command)
        before_window = read_window(before, spec.floor)
        after_window = read_window(after, spec.floor)
        if before_window is None or after_window is None:
            return None
        if not after_window.describes(spec.square):
            return None

        objects = _objects_on(after, spec.square)
        appeared = sorted(set(objects) - set(_objects_on(before, spec.square)))
        blocked_before = before_window.is_occupied(spec.square)
        blocked_after = after_window.is_occupied(spec.square)
        if not appeared and not (blocked_after and not blocked_before):
            # Not yet, or not at all. The square reads exactly as it did, and a
            # square that reads unchanged is a square nothing was built on.
            return None
        return Evidence(
            kind="structure_on_square",
            observation_seq=after.seq,
            observed={
                # ``blueprint`` and ``square`` are the two keys the ``building``
                # probe's confirmation demands, and the two the mod's own ack
                # carries flat beside them, so one live build promotes the
                # capability from either side of the wire.
                "blueprint": spec.blueprint,
                "square": _square_dict(spec.square),
                "object_refs": appeared,
                "object_kinds": sorted({objects[ref].kind for ref in appeared}),
                "occupied_before": blocked_before,
                "occupied_after": blocked_after,
            },
        )
