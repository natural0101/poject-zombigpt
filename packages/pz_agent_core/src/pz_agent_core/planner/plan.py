"""The typed plan: an ordered list of actions, and nothing that could be code.

§7.13 lists what a plan must be validated against — strict schema, max length,
known action names, no unknown args, coordinate bounds, no raw code. Every one
of those is enforced here, but the first and last are enforced *structurally*
rather than by a check that could be forgotten:

* :class:`PlanStep` names its action as an :class:`~pz_agent_core.protocol.ActionName`
  and its arguments as one of the closed set of frozen dataclasses in this
  module. There is no ``dict[str, Any]`` anywhere in the plan type, so a field
  called ``lua``, ``script``, ``path`` or ``keys`` cannot be *constructed* — not
  "is rejected by a filter", cannot exist. A filter is something a future
  refactor can route around; a type is not.
* The only free text a plan carries is :attr:`Plan.summary`, which is displayed
  and never parsed, and which is stripped of control characters so it cannot
  forge structure in a log line or a transcript.

Parsing is total and strict, which is what a plan arriving from outside this
process needs. :meth:`Plan.from_payload` either returns a whole valid plan or
raises :class:`PlanRejected` naming the fault, the field and the step — never a
partially-accepted plan with the bad step dropped, because a plan with a step
silently removed is a different plan than the one that was reviewed.

The bounds come from ``schemas/plan.schema.json``, which is the wire source of
truth for this document; :data:`MAX_PLAN_STEPS` and :data:`MAX_SUMMARY_CHARS`
restate them so validation and serialisation cannot disagree.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Protocol, TypeVar, runtime_checkable

from ..protocol import (
    ActionName,
    ContainerRef,
    ItemRef,
    JsonDict,
    Observation,
    ReasonCode,
    RefError,
    RefKind,
    RiskClass,
    SquareRef,
    ZombieRef,
    ref_kind,
)

__all__ = [
    "ACTION_RISK",
    "DEFAULT_ARRIVAL_RADIUS",
    "DEFAULT_MOVE_DISTANCE",
    "DEFAULT_NEAR_RADIUS",
    "DEFAULT_READ_PAGES",
    "MAX_ARRIVAL_RADIUS",
    "MAX_CONSUME_FRACTION",
    "MAX_MOVE_DISTANCE",
    "MAX_PLAN_STEPS",
    "MAX_READ_PAGES",
    "MAX_STEP_ID_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_WAIT_GAME_SECONDS",
    "MIN_CONSUME_FRACTION",
    "PLANNABLE_ACTIONS",
    "PLAN_COORDINATE_LIMIT",
    "PLAN_FLOOR_LIMIT",
    "PLAN_SCHEMA_VERSION",
    "ConsumeArgs",
    "FailureMode",
    "ItemArgs",
    "MoveNearArgs",
    "MoveToArgs",
    "Plan",
    "PlanFault",
    "PlanRef",
    "PlanRejected",
    "PlanStep",
    "ReadArgs",
    "StepArgs",
    "StepFailure",
    "SuccessCriterion",
    "SuccessKind",
    "TransferArgs",
    "WaitArgs",
    "step_signature",
]

#: ``schemas/plan.schema.json`` pins this; a plan claiming any other version is
#: a document this build cannot reason about.
PLAN_SCHEMA_VERSION: Final = "1.0"

#: ``steps.maxItems`` in the schema. §7.4 and the master prompt both say the
#: same thing in prose: a plan is the next few steps, not a program.
MAX_PLAN_STEPS: Final = 5

MAX_SUMMARY_CHARS: Final = 500
MAX_STEP_ID_CHARS: Final = 32

#: Argument bounds, mirrored from the adapters that will receive them. They are
#: restated rather than imported so a plan can be validated with no action layer
#: wired up; :mod:`tests.unit.test_planner_plan` asserts the two agree.
MIN_CONSUME_FRACTION: Final = 0.05
MAX_CONSUME_FRACTION: Final = 1.0
DEFAULT_READ_PAGES: Final = 20
MAX_READ_PAGES: Final = 200
MAX_WAIT_GAME_SECONDS: Final = 3_600.0
DEFAULT_ARRIVAL_RADIUS: Final = 0.75
MAX_ARRIVAL_RADIUS: Final = 3.0
DEFAULT_NEAR_RADIUS: Final = 1.5
DEFAULT_MOVE_DISTANCE: Final = 20
MAX_MOVE_DISTANCE: Final = 30

#: Not the extent of the map — nobody on this project has measured that, and
#: guessing it would be the kind of unverified claim the capability rules exist
#: to prevent. It is a ceiling that stops an absurd coordinate from ever
#: reaching the mod; the mod still refuses any square it has not loaded.
PLAN_COORDINATE_LIMIT: Final = 100_000
PLAN_FLOOR_LIMIT: Final = 32

#: Both patterns are applied with :meth:`re.Pattern.fullmatch`, never ``match``:
#: ``$`` also matches immediately *before* a trailing newline, so ``match``
#: would admit a step id of ``"s1\n"`` and an object reference ending in one.
#: The mod's reference parser anchors its alphabets with Lua patterns, where
#: ``$`` is a true end-of-string anchor, so a reference this side accepted would
#: be refused there after the step had already been spent.
_STEP_ID: Final = re.compile(r"[A-Za-z0-9_-]+")

#: References whose kind has no typed parser are checked against this instead:
#: ``<kind>:<session>:<tail>`` with a safe alphabet throughout.
_UNTYPED_REF: Final = re.compile(r"[a-z]+:[A-Za-z0-9\-]{1,64}(?::[A-Za-z0-9_.\-]+)+")

_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class PlanFault(StrEnum):
    """Why a payload is not a plan.

    Distinct from :class:`~pz_agent_core.protocol.ReasonCode` because a caller
    fixing a malformed plan needs to know *which* rule it broke, while the wire
    only needs to know that the plan was refused. :attr:`PlanRejected.reason_code`
    maps one onto the other.
    """

    SCHEMA_VERSION = "SCHEMA_VERSION"
    MISSING_FIELD = "MISSING_FIELD"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    BAD_TYPE = "BAD_TYPE"
    BAD_VALUE = "BAD_VALUE"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    UNPLANNABLE_ACTION = "UNPLANNABLE_ACTION"
    BAD_REF = "BAD_REF"
    EMPTY_PLAN = "EMPTY_PLAN"
    STEP_CAP_EXCEEDED = "STEP_CAP_EXCEEDED"
    DUPLICATE_STEP_ID = "DUPLICATE_STEP_ID"
    UNSUPPORTED_CRITERION = "UNSUPPORTED_CRITERION"


#: Which wire code each fault is reported as. A reference that does not parse is
#: ``INVALID_REF`` and everything else is ``INVALID_ARGUMENT``: both are terminal
#: for the plan, and neither is in ``RETRYABLE_CODES``, so a caller cannot burn a
#: retry budget re-submitting the same malformed document.
_FAULT_CODES: Final[Mapping[PlanFault, ReasonCode]] = MappingProxyType(
    {
        PlanFault.BAD_REF: ReasonCode.INVALID_REF,
        PlanFault.UNPLANNABLE_ACTION: ReasonCode.POLICY_DENIED,
    }
)


class PlanRejected(ValueError):
    """A payload was refused; no partial plan was built.

    Carries the fault, the offending field and — when the fault is inside one —
    the step id, because "the plan is invalid" is not something a user or a
    provider can act on.
    """

    def __init__(
        self,
        fault: PlanFault,
        detail: str,
        *,
        field: str = "",
        step_id: str | None = None,
    ) -> None:
        where = f" at {field}" if field else ""
        in_step = f" in step {step_id!r}" if step_id else ""
        super().__init__(f"{fault.value}{in_step}{where}: {detail}")
        self.fault = fault
        self.detail = detail
        self.field = field
        self.step_id = step_id

    @property
    def reason_code(self) -> ReasonCode:
        """The wire code this refusal is reported as."""
        return _FAULT_CODES.get(self.fault, ReasonCode.INVALID_ARGUMENT)


class FailureMode(StrEnum):
    """What the executor does when a step fails. ``on_failure`` in the schema."""

    STOP = "stop"
    ASK_USER = "ask_user"
    REPLAN_ONCE = "replan_once"
    SKIP = "skip"


class SuccessKind(StrEnum):
    """The postcondition a step declares, in the plan's own vocabulary.

    None of these is what makes a step *succeed*: that is always an
    :class:`~pz_agent_core.actions.Evidence` the adapter observed. What a
    criterion adds is a check the plan layer can run against a fresh
    observation before spending an action — a step whose goal already holds is
    skipped rather than performed.

    :attr:`ADAPTER_EVIDENCE` states plainly that the plan has no independent
    check and the adapter's observation is the whole proof. It exists so that
    "there is nothing to verify here" is something a plan can *say*, instead of
    being expressed by an omitted field that reads like an oversight.
    """

    HUNGER_AT_MOST = "hunger_at_most"
    THIRST_AT_MOST = "thirst_at_most"
    ITEM_IN_MAIN_INVENTORY = "item_in_main_inventory"
    ITEM_CONSUMED = "item_consumed"
    POSITION_REACHED = "position_reached"
    ADAPTER_EVIDENCE = "adapter_evidence"


#: Criteria that need a threshold, and criteria that must not carry one.
_THRESHOLD_KINDS: Final[frozenset[SuccessKind]] = frozenset(
    {SuccessKind.HUNGER_AT_MOST, SuccessKind.THIRST_AT_MOST}
)


@dataclass(frozen=True, slots=True)
class PlanRef:
    """One reference a step names, with the kind it was validated as."""

    value: str
    kind: RefKind


@dataclass(frozen=True, slots=True)
class SuccessCriterion:
    """A step's declared postcondition.

    ``holds`` is deliberately three-valued. ``None`` means "this observation
    cannot answer the question" — the mod sent no inventory tier, the criterion
    is :attr:`SuccessKind.ADAPTER_EVIDENCE` — and a caller must not read it as
    either yes or no. Collapsing it to ``False`` would make a missing inventory
    look like proof that the item is still there.
    """

    kind: SuccessKind
    value: float | None = None

    def __post_init__(self) -> None:
        needs_value = self.kind in _THRESHOLD_KINDS
        if needs_value and self.value is None:
            raise ValueError(f"{self.kind.value} needs the level it is satisfied at")
        if not needs_value and self.value is not None:
            raise ValueError(f"{self.kind.value} takes no value, got {self.value}")
        if self.value is not None and not 0.0 <= self.value <= 1.0:
            raise ValueError(f"{self.kind.value} value must be within 0..1, got {self.value}")

    def to_payload(self) -> JsonDict:
        out: JsonDict = {"type": self.kind.value}
        if self.value is not None:
            out["value"] = self.value
        return out

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, step_id: str) -> SuccessCriterion:
        _closed(payload, allowed=("type", "value"), field="success", step_id=step_id)
        raw = _string(_require(payload, "type", field="success.type", step_id=step_id))
        try:
            kind = SuccessKind(raw)
        except ValueError as exc:
            raise PlanRejected(
                PlanFault.UNSUPPORTED_CRITERION,
                f"unknown success type {raw!r}",
                field="success.type",
                step_id=step_id,
            ) from exc
        value = payload.get("value")
        if value is None:
            parsed: float | None = None
        else:
            parsed = _number(
                value, field="success.value", step_id=step_id, minimum=0.0, maximum=1.0
            )
        try:
            return cls(kind=kind, value=parsed)
        except ValueError as exc:
            raise PlanRejected(
                PlanFault.BAD_VALUE, str(exc), field="success", step_id=step_id
            ) from exc

    def holds(self, args: StepArgs, observation: Observation) -> bool | None:
        """Whether the postcondition is already true, or None when unanswerable."""
        if self.kind is SuccessKind.HUNGER_AT_MOST:
            return observation.player.hunger <= _threshold(self.value)
        if self.kind is SuccessKind.THIRST_AT_MOST:
            return observation.player.thirst <= _threshold(self.value)
        if self.kind is SuccessKind.POSITION_REACHED:
            return _position_reached(args, observation)
        if self.kind in (SuccessKind.ITEM_IN_MAIN_INVENTORY, SuccessKind.ITEM_CONSUMED):
            return _item_criterion(self.kind, args, observation)
        return None


def _threshold(value: float | None) -> float:
    """The declared level. ``__post_init__`` guarantees it is present here."""
    return 0.0 if value is None else value


def _position_reached(args: StepArgs, observation: Observation) -> bool | None:
    """Euclidean, because that is the metric the move adapter's radius is in.

    A criterion that says a step's goal already holds spends no command and
    still lets the run finish ``completed``, so anything looser than
    ``MoveToAdapter.arrived`` reports an arrival nobody observed. Chebyshev
    makes the radius a *box*: (0.7, 0.7) from the target is inside a 0.75 box
    and 0.99 away from the point the adapter measures to.
    """
    if not isinstance(args, MoveToArgs):
        return None
    position = observation.player.position
    if position.z != args.z:
        return False
    return math.hypot(position.x - args.x, position.y - args.y) <= args.radius


def _item_criterion(kind: SuccessKind, args: StepArgs, observation: Observation) -> bool | None:
    """Follow the *object*, not the string that named it.

    An item reference embeds the container holding it, so ``inventory.ensure_main``
    — the step that precedes every meal and every read — re-mints the reference
    of the item it moved. Looking the old string up then finds nothing, which
    under ``item_consumed`` reads as "already eaten" and skips the meal, and
    under ``item_in_main_inventory`` reads as "not there" for an item that is.
    What survives a move is the runtime id and the generation, which is the
    same identity the adapters verify a transfer with.
    """
    inventory = observation.inventory
    if inventory is None:
        return None
    item_ref = next((r.value for r in args.refs() if r.kind is RefKind.ITEM), None)
    if item_ref is None:
        return None
    try:
        named = ItemRef.parse(item_ref)
    except RefError:
        # Unanswerable rather than false: a reference this side cannot read is
        # not evidence about where the object is, or whether it still exists.
        return None
    found = [
        item
        for item in inventory.items
        if _same_object(item.ref, named.runtime_id, named.generation)
    ]
    if kind is SuccessKind.ITEM_CONSUMED:
        return not found
    main = inventory.main_container()
    # Two entries for one runtime id is a duplication, not a completed move.
    if len(found) != 1 or main is None:
        return False
    return found[0].container_ref == main.ref


def _same_object(observed_ref: str, runtime_id: str, generation: int) -> bool:
    """Whether *observed_ref* names the same object as this id and generation.

    A reference the mod sent that this side cannot parse is dropped rather than
    raised on: it can only ever fail to match, and one malformed entry must not
    blind the search for the others.
    """
    try:
        parsed = ItemRef.parse(observed_ref)
    except RefError:
        return False
    return parsed.runtime_id == runtime_id and parsed.generation == generation


# --------------------------------------------------------------------------
# step arguments — the closed set
# --------------------------------------------------------------------------


@runtime_checkable
class StepArgs(Protocol):
    """What every step's argument object can do.

    The set of types satisfying this is closed by :data:`_ARG_PARSERS`: a plan
    may only be built from an argument object this module defines, and every one
    of them holds numbers, booleans and validated references. That is the whole
    mechanism behind "the planner cannot emit code" — there is no field for it.
    """

    def to_payload(self) -> JsonDict:
        """The ``args`` object the action engine receives."""
        ...

    def refs(self) -> tuple[PlanRef, ...]:
        """Every reference this step names, for session and freshness checks."""
        ...


@dataclass(frozen=True, slots=True)
class WaitArgs:
    """``action.wait``: hold still for a stretch of *game* time."""

    game_seconds: float

    def __post_init__(self) -> None:
        if not 0.0 < self.game_seconds <= MAX_WAIT_GAME_SECONDS:
            raise ValueError(
                f"game_seconds must be within 0..{MAX_WAIT_GAME_SECONDS}, got {self.game_seconds}"
            )

    def to_payload(self) -> JsonDict:
        return {"game_seconds": self.game_seconds}

    def refs(self) -> tuple[PlanRef, ...]:
        return ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], step_id: str) -> WaitArgs:
        _closed(payload, allowed=("game_seconds",), field="args", step_id=step_id)
        return _built(
            cls,
            step_id,
            game_seconds=_number(
                _require(payload, "game_seconds", field="args.game_seconds", step_id=step_id),
                field="args.game_seconds",
                step_id=step_id,
                minimum=0.0,
                maximum=MAX_WAIT_GAME_SECONDS,
            ),
        )


@dataclass(frozen=True, slots=True)
class MoveToArgs:
    """``movement.move_to``: walk to a square.

    ``allow_windows`` is absent by design, not defaulted to False. §5.19 forbids
    climbing through a window automatically, and the movement adapter refuses the
    argument outright — so a plan that cannot express it cannot ask for it, and
    the refusal never has to fire.
    """

    x: int
    y: int
    z: int
    radius: float = DEFAULT_ARRIVAL_RADIUS
    max_distance: int = DEFAULT_MOVE_DISTANCE
    allow_doors: bool = True
    allow_stairs: bool = False

    def __post_init__(self) -> None:
        for axis, value in (("x", self.x), ("y", self.y)):
            if abs(value) > PLAN_COORDINATE_LIMIT:
                raise ValueError(f"{axis} must be within ±{PLAN_COORDINATE_LIMIT}, got {value}")
        if abs(self.z) > PLAN_FLOOR_LIMIT:
            raise ValueError(f"z must be within ±{PLAN_FLOOR_LIMIT}, got {self.z}")
        if not 0.1 <= self.radius <= MAX_ARRIVAL_RADIUS:
            raise ValueError(f"radius must be within 0.1..{MAX_ARRIVAL_RADIUS}, got {self.radius}")
        if not 1 <= self.max_distance <= MAX_MOVE_DISTANCE:
            raise ValueError(
                f"max_distance must be within 1..{MAX_MOVE_DISTANCE}, got {self.max_distance}"
            )

    def to_payload(self) -> JsonDict:
        return {
            "target": {"x": self.x, "y": self.y, "z": self.z},
            "radius": self.radius,
            "max_distance": self.max_distance,
            "allow_doors": self.allow_doors,
            "allow_stairs": self.allow_stairs,
        }

    def refs(self) -> tuple[PlanRef, ...]:
        return ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], step_id: str) -> MoveToArgs:
        _closed(
            payload,
            allowed=("target", "radius", "max_distance", "allow_doors", "allow_stairs"),
            field="args",
            step_id=step_id,
        )
        target = _require(payload, "target", field="args.target", step_id=step_id)
        if not isinstance(target, Mapping):
            raise PlanRejected(
                PlanFault.BAD_TYPE,
                "target must be an object with x, y and z",
                field="args.target",
                step_id=step_id,
            )
        _closed(target, allowed=("x", "y", "z"), field="args.target", step_id=step_id)
        return _built(
            cls,
            step_id,
            x=_integer(
                _require(target, "x", field="args.target.x", step_id=step_id),
                field="args.target.x",
                step_id=step_id,
            ),
            y=_integer(
                _require(target, "y", field="args.target.y", step_id=step_id),
                field="args.target.y",
                step_id=step_id,
            ),
            z=_integer(
                _require(target, "z", field="args.target.z", step_id=step_id),
                field="args.target.z",
                step_id=step_id,
            ),
            radius=_optional_number(
                payload,
                "radius",
                default=DEFAULT_ARRIVAL_RADIUS,
                field="args.radius",
                step_id=step_id,
                minimum=0.1,
                maximum=MAX_ARRIVAL_RADIUS,
            ),
            max_distance=_optional_integer(
                payload,
                "max_distance",
                default=DEFAULT_MOVE_DISTANCE,
                field="args.max_distance",
                step_id=step_id,
            ),
            allow_doors=_optional_flag(payload, "allow_doors", default=True, step_id=step_id),
            allow_stairs=_optional_flag(payload, "allow_stairs", default=False, step_id=step_id),
        )


@dataclass(frozen=True, slots=True)
class MoveNearArgs:
    """``movement.move_near``: get within interaction range of a world object."""

    object_ref: str
    radius: float = DEFAULT_NEAR_RADIUS
    max_distance: int = DEFAULT_MOVE_DISTANCE

    def __post_init__(self) -> None:
        if not 0.1 <= self.radius <= MAX_ARRIVAL_RADIUS:
            raise ValueError(f"radius must be within 0.1..{MAX_ARRIVAL_RADIUS}, got {self.radius}")
        if not 1 <= self.max_distance <= MAX_MOVE_DISTANCE:
            raise ValueError(
                f"max_distance must be within 1..{MAX_MOVE_DISTANCE}, got {self.max_distance}"
            )

    def to_payload(self) -> JsonDict:
        return {
            "object_ref": self.object_ref,
            "radius": self.radius,
            "max_distance": self.max_distance,
        }

    def refs(self) -> tuple[PlanRef, ...]:
        # The kind is read back off the reference rather than declared, because
        # this argument accepts whichever of them the mod minted for the thing.
        return (PlanRef(value=self.object_ref, kind=ref_kind(self.object_ref)),)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], step_id: str) -> MoveNearArgs:
        _closed(
            payload,
            allowed=("object_ref", "radius", "max_distance"),
            field="args",
            step_id=step_id,
        )
        return _built(
            cls,
            step_id,
            object_ref=_ref(
                _require(payload, "object_ref", field="args.object_ref", step_id=step_id),
                kind=(RefKind.CONTAINER, RefKind.SQUARE, RefKind.ITEM),
                field="args.object_ref",
                step_id=step_id,
            ),
            radius=_optional_number(
                payload,
                "radius",
                default=DEFAULT_NEAR_RADIUS,
                field="args.radius",
                step_id=step_id,
                minimum=0.1,
                maximum=MAX_ARRIVAL_RADIUS,
            ),
            max_distance=_optional_integer(
                payload,
                "max_distance",
                default=DEFAULT_MOVE_DISTANCE,
                field="args.max_distance",
                step_id=step_id,
            ),
        )


@dataclass(frozen=True, slots=True)
class ItemArgs:
    """``inventory.ensure_main``: bring one item into the main inventory."""

    item_ref: str

    def to_payload(self) -> JsonDict:
        return {"item_ref": self.item_ref}

    def refs(self) -> tuple[PlanRef, ...]:
        return (PlanRef(value=self.item_ref, kind=RefKind.ITEM),)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], step_id: str) -> ItemArgs:
        _closed(payload, allowed=("item_ref",), field="args", step_id=step_id)
        return _built(
            cls,
            step_id,
            item_ref=_ref(
                _require(payload, "item_ref", field="args.item_ref", step_id=step_id),
                kind=RefKind.ITEM,
                field="args.item_ref",
                step_id=step_id,
            ),
        )


@dataclass(frozen=True, slots=True)
class TransferArgs:
    """``inventory.transfer``: move one item between two containers."""

    item_ref: str
    destination_container_ref: str
    source_container_ref: str | None = None

    def to_payload(self) -> JsonDict:
        out: JsonDict = {
            "item_ref": self.item_ref,
            "destination_container_ref": self.destination_container_ref,
        }
        if self.source_container_ref is not None:
            out["source_container_ref"] = self.source_container_ref
        return out

    def refs(self) -> tuple[PlanRef, ...]:
        found = [
            PlanRef(value=self.item_ref, kind=RefKind.ITEM),
            PlanRef(value=self.destination_container_ref, kind=RefKind.CONTAINER),
        ]
        if self.source_container_ref is not None:
            found.append(PlanRef(value=self.source_container_ref, kind=RefKind.CONTAINER))
        return tuple(found)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], step_id: str) -> TransferArgs:
        _closed(
            payload,
            allowed=("item_ref", "destination_container_ref", "source_container_ref"),
            field="args",
            step_id=step_id,
        )
        source = payload.get("source_container_ref")
        return _built(
            cls,
            step_id,
            item_ref=_ref(
                _require(payload, "item_ref", field="args.item_ref", step_id=step_id),
                kind=RefKind.ITEM,
                field="args.item_ref",
                step_id=step_id,
            ),
            destination_container_ref=_ref(
                _require(
                    payload,
                    "destination_container_ref",
                    field="args.destination_container_ref",
                    step_id=step_id,
                ),
                kind=RefKind.CONTAINER,
                field="args.destination_container_ref",
                step_id=step_id,
            ),
            source_container_ref=(
                None
                if source is None
                else _ref(
                    source,
                    kind=RefKind.CONTAINER,
                    field="args.source_container_ref",
                    step_id=step_id,
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ConsumeArgs:
    """``consume.eat`` and ``consume.drink``: spend part of one item."""

    item_ref: str
    fraction: float = MAX_CONSUME_FRACTION

    def __post_init__(self) -> None:
        if not MIN_CONSUME_FRACTION <= self.fraction <= MAX_CONSUME_FRACTION:
            raise ValueError(
                f"fraction must be within {MIN_CONSUME_FRACTION}..{MAX_CONSUME_FRACTION}, "
                f"got {self.fraction}"
            )

    def to_payload(self) -> JsonDict:
        return {"item_ref": self.item_ref, "fraction": self.fraction}

    def refs(self) -> tuple[PlanRef, ...]:
        return (PlanRef(value=self.item_ref, kind=RefKind.ITEM),)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], step_id: str) -> ConsumeArgs:
        _closed(payload, allowed=("item_ref", "fraction"), field="args", step_id=step_id)
        return _built(
            cls,
            step_id,
            item_ref=_ref(
                _require(payload, "item_ref", field="args.item_ref", step_id=step_id),
                kind=RefKind.ITEM,
                field="args.item_ref",
                step_id=step_id,
            ),
            fraction=_optional_number(
                payload,
                "fraction",
                default=MAX_CONSUME_FRACTION,
                field="args.fraction",
                step_id=step_id,
                minimum=MIN_CONSUME_FRACTION,
                maximum=MAX_CONSUME_FRACTION,
            ),
        )


@dataclass(frozen=True, slots=True)
class ReadArgs:
    """``literature.read``: read a bounded number of pages of one item."""

    item_ref: str
    pages: int = DEFAULT_READ_PAGES

    def __post_init__(self) -> None:
        if not 1 <= self.pages <= MAX_READ_PAGES:
            raise ValueError(f"pages must be within 1..{MAX_READ_PAGES}, got {self.pages}")

    def to_payload(self) -> JsonDict:
        return {"item_ref": self.item_ref, "pages": self.pages}

    def refs(self) -> tuple[PlanRef, ...]:
        return (PlanRef(value=self.item_ref, kind=RefKind.ITEM),)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], step_id: str) -> ReadArgs:
        _closed(payload, allowed=("item_ref", "pages"), field="args", step_id=step_id)
        return _built(
            cls,
            step_id,
            item_ref=_ref(
                _require(payload, "item_ref", field="args.item_ref", step_id=step_id),
                kind=RefKind.ITEM,
                field="args.item_ref",
                step_id=step_id,
            ),
            pages=_optional_integer(
                payload,
                "pages",
                default=DEFAULT_READ_PAGES,
                field="args.pages",
                step_id=step_id,
                minimum=1,
                maximum=MAX_READ_PAGES,
            ),
        )


_ArgParser = Callable[[Mapping[str, Any], str], StepArgs]

#: The actions a plan may name, each with the one argument type it takes.
#:
#: Three groups of :class:`~pz_agent_core.protocol.ActionName` are deliberately
#: absent. ``session.arm``/``session.disarm``/``safety.stop``/``plan.cancel`` are
#: the levers by which a human or the reflex guard takes control, and a plan that
#: could schedule its own disarm — or its own cancel — would be a plan that can
#: reach around the thing meant to stop it. ``inventory.equip``,
#: ``inventory.unequip`` and ``world.inspect`` have no adapter in this build, so
#: planning one would be planning an action nothing can execute or verify.
_ARG_PARSERS: Final[Mapping[ActionName, _ArgParser]] = MappingProxyType(
    {
        ActionName.ACTION_WAIT: WaitArgs.from_payload,
        ActionName.MOVEMENT_MOVE_TO: MoveToArgs.from_payload,
        ActionName.MOVEMENT_MOVE_NEAR: MoveNearArgs.from_payload,
        ActionName.INVENTORY_TRANSFER: TransferArgs.from_payload,
        ActionName.INVENTORY_ENSURE_MAIN: ItemArgs.from_payload,
        ActionName.CONSUME_EAT: ConsumeArgs.from_payload,
        ActionName.CONSUME_DRINK: ConsumeArgs.from_payload,
        ActionName.LITERATURE_READ: ReadArgs.from_payload,
    }
)

PLANNABLE_ACTIONS: Final[frozenset[ActionName]] = frozenset(_ARG_PARSERS)

#: The argument types each action accepts, so a step cannot be *constructed*
#: in-process with arguments belonging to a different action.
_ARG_TYPES: Final[Mapping[ActionName, tuple[type[object], ...]]] = MappingProxyType(
    {
        ActionName.ACTION_WAIT: (WaitArgs,),
        ActionName.MOVEMENT_MOVE_TO: (MoveToArgs,),
        ActionName.MOVEMENT_MOVE_NEAR: (MoveNearArgs,),
        ActionName.INVENTORY_TRANSFER: (TransferArgs,),
        ActionName.INVENTORY_ENSURE_MAIN: (ItemArgs,),
        ActionName.CONSUME_EAT: (ConsumeArgs,),
        ActionName.CONSUME_DRINK: (ConsumeArgs,),
        ActionName.LITERATURE_READ: (ReadArgs,),
    }
)

#: Success criteria that make sense for each action. A plan claiming that eating
#: a tin will put an item in the main inventory is not a plan with an optimistic
#: label on it — it is a plan the executor would evaluate against the wrong
#: thing, so it is refused.
_ALLOWED_SUCCESS: Final[Mapping[ActionName, frozenset[SuccessKind]]] = MappingProxyType(
    {
        ActionName.ACTION_WAIT: frozenset({SuccessKind.ADAPTER_EVIDENCE}),
        ActionName.MOVEMENT_MOVE_TO: frozenset(
            {SuccessKind.POSITION_REACHED, SuccessKind.ADAPTER_EVIDENCE}
        ),
        ActionName.MOVEMENT_MOVE_NEAR: frozenset({SuccessKind.ADAPTER_EVIDENCE}),
        ActionName.INVENTORY_TRANSFER: frozenset(
            {SuccessKind.ITEM_IN_MAIN_INVENTORY, SuccessKind.ADAPTER_EVIDENCE}
        ),
        ActionName.INVENTORY_ENSURE_MAIN: frozenset(
            {SuccessKind.ITEM_IN_MAIN_INVENTORY, SuccessKind.ADAPTER_EVIDENCE}
        ),
        ActionName.CONSUME_EAT: frozenset(
            {
                SuccessKind.HUNGER_AT_MOST,
                SuccessKind.ITEM_CONSUMED,
                SuccessKind.ADAPTER_EVIDENCE,
            }
        ),
        ActionName.CONSUME_DRINK: frozenset(
            {
                SuccessKind.THIRST_AT_MOST,
                SuccessKind.ITEM_CONSUMED,
                SuccessKind.ADAPTER_EVIDENCE,
            }
        ),
        ActionName.LITERATURE_READ: frozenset({SuccessKind.ADAPTER_EVIDENCE}),
    }
)

#: The risk tier an action carries on its name alone. It is a *floor*: three
#: adapters raise it from their arguments (a transfer that reaches into a world
#: container, a walk that leaves the neighbourhood or changes floor), which is
#: why :mod:`pz_agent_core.planner.critic` asks the adapter rather than trusting
#: this table for the final answer.
ACTION_RISK: Final[Mapping[ActionName, RiskClass]] = MappingProxyType(
    {
        ActionName.ACTION_WAIT: RiskClass.P0,
        ActionName.MOVEMENT_MOVE_TO: RiskClass.P2,
        ActionName.MOVEMENT_MOVE_NEAR: RiskClass.P3,
        ActionName.INVENTORY_TRANSFER: RiskClass.P1,
        ActionName.INVENTORY_ENSURE_MAIN: RiskClass.P1,
        ActionName.CONSUME_EAT: RiskClass.P2,
        ActionName.CONSUME_DRINK: RiskClass.P2,
        ActionName.LITERATURE_READ: RiskClass.P2,
    }
)


def step_signature(action: ActionName, args: StepArgs) -> str:
    """A stable identity for "this action with these arguments".

    Used to recognise a step the agent has just tried and failed at
    non-retryably. Sorted keys, so two structurally identical steps produce the
    same signature whatever order their payload was built in.
    """
    return f"{action.value}|{json.dumps(args.to_payload(), sort_keys=True, default=str)}"


@dataclass(frozen=True, slots=True)
class StepFailure:
    """One step that ended badly, kept so a replan cannot propose it again."""

    signature: str
    reason_code: ReasonCode
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One action, its arguments, and what proves it worked."""

    step_id: str
    action: ActionName
    args: StepArgs
    success: SuccessCriterion
    on_failure: FailureMode = FailureMode.STOP
    requires_confirmation: bool = False

    def __post_init__(self) -> None:
        if not _STEP_ID.fullmatch(self.step_id) or len(self.step_id) > MAX_STEP_ID_CHARS:
            raise ValueError(
                f"step_id must be 1..{MAX_STEP_ID_CHARS} characters of [A-Za-z0-9_-], "
                f"got {self.step_id!r}"
            )
        if self.action not in PLANNABLE_ACTIONS:
            raise ValueError(f"{self.action.value} is not an action a plan may name")
        expected = _ARG_TYPES[self.action]
        if not isinstance(self.args, expected):
            names = ", ".join(t.__name__ for t in expected)
            raise ValueError(f"{self.action.value} takes {names}, got {type(self.args).__name__}")
        if self.success.kind not in _ALLOWED_SUCCESS[self.action]:
            raise ValueError(
                f"{self.success.kind.value} is not a postcondition {self.action.value} can produce"
            )

    @property
    def risk(self) -> RiskClass:
        """The tier this action carries on its name; the adapter may raise it."""
        return ACTION_RISK[self.action]

    @property
    def signature(self) -> str:
        return step_signature(self.action, self.args)

    def to_payload(self) -> JsonDict:
        return {
            "step_id": self.step_id,
            "action": self.action.value,
            "args": self.args.to_payload(),
            "success": self.success.to_payload(),
            "on_failure": self.on_failure.value,
            "requires_confirmation": self.requires_confirmation,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> PlanStep:
        if not isinstance(payload, Mapping):
            raise PlanRejected(PlanFault.BAD_TYPE, "a step must be an object", field="steps[]")
        step_id = _step_id(payload.get("step_id"))
        _closed(
            payload,
            allowed=(
                "step_id",
                "action",
                "args",
                "success",
                "on_failure",
                "requires_confirmation",
            ),
            field="step",
            step_id=step_id,
        )
        action = _action(
            _string(_require(payload, "action", field="action", step_id=step_id)), step_id
        )
        args_payload = _require(payload, "args", field="args", step_id=step_id)
        if not isinstance(args_payload, Mapping):
            raise PlanRejected(
                PlanFault.BAD_TYPE, "args must be an object", field="args", step_id=step_id
            )
        success_payload = _require(payload, "success", field="success", step_id=step_id)
        if not isinstance(success_payload, Mapping):
            raise PlanRejected(
                PlanFault.BAD_TYPE, "success must be an object", field="success", step_id=step_id
            )
        on_failure = payload.get("on_failure", FailureMode.STOP.value)
        confirm = payload.get("requires_confirmation", False)
        if not isinstance(confirm, bool):
            raise PlanRejected(
                PlanFault.BAD_TYPE,
                "requires_confirmation must be a boolean",
                field="requires_confirmation",
                step_id=step_id,
            )
        return _built(
            cls,
            where=step_id,
            step_id=step_id,
            action=action,
            args=_ARG_PARSERS[action](args_payload, step_id),
            success=SuccessCriterion.from_payload(success_payload, step_id=step_id),
            on_failure=_failure_mode(on_failure, step_id),
            requires_confirmation=confirm,
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """An ordered, bounded list of steps serving one goal.

    :attr:`risk_class` is *derived* from the steps rather than stored. A provider
    that could declare it would be a provider that could understate it, and the
    permission ladder is keyed on that number.
    """

    goal_id: str
    summary: str
    steps: tuple[PlanStep, ...]
    confidence: float | None = None
    schema_version: str = PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLAN_SCHEMA_VERSION:
            raise ValueError(
                f"plan schema_version must be {PLAN_SCHEMA_VERSION}, got {self.schema_version!r}"
            )
        try:
            uuid.UUID(self.goal_id)
        except ValueError as exc:
            raise ValueError(f"goal_id must be a UUID, got {self.goal_id!r}") from exc
        cleaned = _clean(self.summary)
        if not cleaned:
            raise ValueError("a plan must summarise itself in a sentence a user can read")
        if len(cleaned) > MAX_SUMMARY_CHARS:
            raise ValueError(
                f"summary must be at most {MAX_SUMMARY_CHARS} characters, got {len(cleaned)}"
            )
        object.__setattr__(self, "summary", cleaned)
        if not self.steps:
            raise ValueError("a plan with no steps is not a plan")
        if len(self.steps) > MAX_PLAN_STEPS:
            raise ValueError(
                f"a plan may hold at most {MAX_PLAN_STEPS} steps, got {len(self.steps)}"
            )
        ids = [step.step_id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError(f"step ids must be unique within a plan: {sorted(ids)}")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within 0..1, got {self.confidence}")

    @property
    def risk_class(self) -> RiskClass:
        """The highest tier any step names. Derived, never declared."""
        return max((step.risk for step in self.steps), key=lambda risk: risk.rank)

    @property
    def requires_confirmation(self) -> bool:
        return any(step.requires_confirmation for step in self.steps)

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def to_payload(self) -> JsonDict:
        out: JsonDict = {
            "schema_version": self.schema_version,
            "goal_id": self.goal_id,
            "summary": self.summary,
            "risk_class": self.risk_class.value,
            "steps": [step.to_payload() for step in self.steps],
        }
        if self.confidence is not None:
            out["confidence"] = self.confidence
        return out

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Plan:
        """Parse a plan document, or refuse the whole thing.

        Raises:
            PlanRejected: on anything at all — an unknown key, an action this
                build cannot plan, a step count over the cap, a reference that
                does not parse. There is no partial-acceptance path: a plan with
                one step quietly dropped is a different plan from the one the
                critic would review.
        """
        if not isinstance(payload, Mapping):
            raise PlanRejected(PlanFault.BAD_TYPE, "a plan must be an object")
        _closed(
            payload,
            allowed=("schema_version", "goal_id", "summary", "risk_class", "confidence", "steps"),
            field="plan",
            step_id=None,
        )
        version = _string(_require(payload, "schema_version", field="schema_version"))
        if version != PLAN_SCHEMA_VERSION:
            raise PlanRejected(
                PlanFault.SCHEMA_VERSION,
                f"this build reads plan schema {PLAN_SCHEMA_VERSION}, got {version!r}",
                field="schema_version",
            )
        raw_steps = _require(payload, "steps", field="steps")
        if isinstance(raw_steps, (str, bytes)) or not isinstance(raw_steps, Sequence):
            raise PlanRejected(PlanFault.BAD_TYPE, "steps must be an array", field="steps")
        if not raw_steps:
            raise PlanRejected(PlanFault.EMPTY_PLAN, "a plan with no steps is not a plan")
        if len(raw_steps) > MAX_PLAN_STEPS:
            raise PlanRejected(
                PlanFault.STEP_CAP_EXCEEDED,
                f"a plan may hold at most {MAX_PLAN_STEPS} steps, got {len(raw_steps)}",
                field="steps",
            )
        steps = tuple(PlanStep.from_payload(entry) for entry in raw_steps)
        ids = [step.step_id for step in steps]
        duplicate = next((sid for sid in ids if ids.count(sid) > 1), None)
        if duplicate is not None:
            raise PlanRejected(
                PlanFault.DUPLICATE_STEP_ID,
                f"step id {duplicate!r} appears more than once",
                field="steps",
            )
        confidence = payload.get("confidence")
        plan = _built(
            cls,
            None,
            goal_id=_string(_require(payload, "goal_id", field="goal_id")),
            summary=_string(_require(payload, "summary", field="summary")),
            steps=steps,
            confidence=(
                None
                if confidence is None
                else _number(confidence, field="confidence", step_id=None, minimum=0.0, maximum=1.0)
            ),
            schema_version=version,
        )
        declared = payload.get("risk_class")
        if declared is not None and declared != plan.risk_class.value:
            # A declared tier below the derived one is how a plan gets past a
            # ladder it should not; a tier above it is a plan describing itself
            # as something other than what it does. Neither is accepted.
            raise PlanRejected(
                PlanFault.BAD_VALUE,
                f"risk_class {declared!r} does not match the {plan.risk_class.value} its "
                "steps add up to",
                field="risk_class",
            )
        return plan


# --------------------------------------------------------------------------
# strict readers
# --------------------------------------------------------------------------


def _clean(raw: str) -> str:
    """Strip control characters and collapse whitespace in displayed text."""
    return " ".join(_CONTROL.sub(" ", raw).split())


def _require(
    payload: Mapping[str, Any], key: str, *, field: str, step_id: str | None = None
) -> Any:
    if key not in payload or payload[key] is None:
        raise PlanRejected(
            PlanFault.MISSING_FIELD, f"{key} is required", field=field, step_id=step_id
        )
    return payload[key]


def _closed(
    payload: Mapping[str, Any],
    *,
    allowed: Sequence[str],
    field: str,
    step_id: str | None,
) -> None:
    """Refuse unknown keys rather than dropping them.

    Dropping is the dangerous option: a payload carrying ``{"item_ref": …,
    "lua": …}`` would be accepted as a valid step and the extra field would
    vanish silently, leaving nobody aware that something tried.
    """
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise PlanRejected(
            PlanFault.UNKNOWN_FIELD,
            f"unsupported field(s): {unknown}",
            field=field,
            step_id=step_id,
        )


def _string(value: Any, *, field: str = "", step_id: str | None = None) -> str:
    if not isinstance(value, str):
        raise PlanRejected(
            PlanFault.BAD_TYPE,
            f"expected a string, got {type(value).__name__}",
            field=field,
            step_id=step_id,
        )
    return value


def _number(
    value: Any, *, field: str, step_id: str | None, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanRejected(
            PlanFault.BAD_TYPE,
            f"expected a number, got {type(value).__name__}",
            field=field,
            step_id=step_id,
        )
    if not minimum <= value <= maximum:
        raise PlanRejected(
            PlanFault.BAD_VALUE,
            f"must be within {minimum}..{maximum}, got {value}",
            field=field,
            step_id=step_id,
        )
    return float(value)


def _integer(value: Any, *, field: str, step_id: str | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanRejected(
            PlanFault.BAD_TYPE,
            f"expected an integer, got {type(value).__name__}",
            field=field,
            step_id=step_id,
        )
    return value


def _optional_number(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: float,
    field: str,
    step_id: str,
    minimum: float,
    maximum: float,
) -> float:
    value = payload.get(key)
    if value is None:
        return default
    return _number(value, field=field, step_id=step_id, minimum=minimum, maximum=maximum)


def _optional_integer(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: int,
    field: str,
    step_id: str,
    minimum: int = 1,
    maximum: int = MAX_MOVE_DISTANCE,
) -> int:
    value = payload.get(key)
    if value is None:
        return default
    parsed = _integer(value, field=field, step_id=step_id)
    if not minimum <= parsed <= maximum:
        raise PlanRejected(
            PlanFault.BAD_VALUE,
            f"must be within {minimum}..{maximum}, got {parsed}",
            field=field,
            step_id=step_id,
        )
    return parsed


def _optional_flag(payload: Mapping[str, Any], key: str, *, default: bool, step_id: str) -> bool:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PlanRejected(
            PlanFault.BAD_TYPE,
            f"expected a boolean, got {type(value).__name__}",
            field=f"args.{key}",
            step_id=step_id,
        )
    return value


#: Reference kinds with a typed parser. ``object`` has none in the protocol, so
#: it is checked against the shared reference grammar instead of being waved
#: through — an unparsed kind is not an unvalidated one.
_REF_PARSERS: Final[Mapping[RefKind, Callable[[str], object]]] = MappingProxyType(
    {
        RefKind.ITEM: ItemRef.parse,
        RefKind.CONTAINER: ContainerRef.parse,
        RefKind.SQUARE: SquareRef.parse,
        RefKind.ZOMBIE: ZombieRef.parse,
    }
)


def _ref(value: Any, *, kind: RefKind | tuple[RefKind, ...], field: str, step_id: str) -> str:
    """Validate one reference in a plan, against one acceptable kind or several.

    Several is not a loosening. ``PZAgent.ObserveModel`` describes a nearby
    thing as a ``container`` reference when it holds one and a ``square``
    reference otherwise — it never mints an ``object`` reference at all — so a
    step naming something to walk up to has to accept the kinds the mod is
    capable of producing, or it refuses every plan built from a real
    observation.
    """
    accepted = (kind,) if isinstance(kind, RefKind) else kind
    raw = _string(value, field=field, step_id=step_id)
    try:
        actual = ref_kind(raw)
    except RefError as exc:
        raise PlanRejected(
            PlanFault.BAD_REF, f"{raw!r} is not a reference", field=field, step_id=step_id
        ) from exc
    if actual not in accepted:
        wanted = " or ".join(sorted(k.value for k in accepted))
        raise PlanRejected(
            PlanFault.BAD_REF,
            f"expected a {wanted} reference, got a {actual.value} one",
            field=field,
            step_id=step_id,
        )
    parser = _REF_PARSERS.get(actual)
    if parser is None:
        if not _UNTYPED_REF.fullmatch(raw):
            raise PlanRejected(
                PlanFault.BAD_REF,
                f"{raw!r} is not a well-formed {actual.value} reference",
                field=field,
                step_id=step_id,
            )
        return raw
    try:
        parser(raw)
    except RefError as exc:
        raise PlanRejected(PlanFault.BAD_REF, str(exc), field=field, step_id=step_id) from exc
    return raw


def _step_id(value: Any) -> str:
    raw = _string(value, field="step_id")
    if not _STEP_ID.fullmatch(raw) or len(raw) > MAX_STEP_ID_CHARS:
        raise PlanRejected(
            PlanFault.BAD_VALUE,
            f"step_id must be 1..{MAX_STEP_ID_CHARS} characters of [A-Za-z0-9_-], got {raw!r}",
            field="step_id",
        )
    return raw


def _failure_mode(value: Any, step_id: str) -> FailureMode:
    raw = _string(value, field="on_failure", step_id=step_id)
    try:
        return FailureMode(raw)
    except ValueError as exc:
        raise PlanRejected(
            PlanFault.BAD_VALUE,
            f"{raw!r} is not one of {sorted(mode.value for mode in FailureMode)}",
            field="on_failure",
            step_id=step_id,
        ) from exc


def _action(raw: str, step_id: str) -> ActionName:
    try:
        action = ActionName(raw)
    except ValueError as exc:
        raise PlanRejected(
            PlanFault.UNKNOWN_ACTION,
            f"{raw!r} is not an action this protocol knows",
            field="action",
            step_id=step_id,
        ) from exc
    if action not in PLANNABLE_ACTIONS:
        raise PlanRejected(
            PlanFault.UNPLANNABLE_ACTION,
            f"{action.value} exists but is not an action a plan may name",
            field="action",
            step_id=step_id,
        )
    return action


_Built = TypeVar("_Built")


def _built(factory: Callable[..., _Built], where: str | None, **kwargs: Any) -> _Built:
    """Construct a plan object, reporting its own invariants as plan faults.

    The dataclasses validate themselves so that an in-process caller gets a
    ``ValueError`` at the call site; a payload that trips the same check has to
    surface as a rejected plan instead, and translating here keeps each rule
    written exactly once.
    """
    try:
        return factory(**kwargs)
    except ValueError as exc:
        raise PlanRejected(PlanFault.BAD_VALUE, str(exc), step_id=where) from exc
