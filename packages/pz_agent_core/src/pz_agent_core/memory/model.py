"""The facts memory is allowed to hold, and the shape each one has on disk.

Memory stores *derived facts*, never observations. A snapshot is a photograph of
one tick with session-scoped references all through it; replaying one later would
act on refs that now denote different objects, or nothing. So what is kept here
is the residue that outlives a session: where a container stands, what was in it
last time it was opened, which square is home, which route failed, what the user
set aside, what was tried and how it ended.

Two rules run through every record:

* **Nothing session-scoped is stored.** A container is remembered by the
  *tail* of its reference (``world:12045:3402:0:1:0``) — the half that describes
  a place rather than a session — and a reservation is keyed by item type, not by
  an item reference. A reference minted last session is not stale, it is wrong
  (§3.7), and a store that persisted one would hand it back looking valid.
* **Every string is bounded and quarantined.** Container names and task details
  come from the game, which AGENTS.md classes as untrusted data. They are
  truncated and stripped of control characters and path shapes on the way in, by
  the same function the MCP boundary uses, so a memory file cannot become a
  prompt-injection channel by being read back.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from ..observation.compact import redact_text
from ..protocol import ContainerKind, ContainerRef, RefError

__all__ = [
    "MAX_CATEGORY_LEN",
    "MAX_DETAIL_LEN",
    "MAX_FULL_TYPE_LEN",
    "MAX_LABEL_LEN",
    "MAX_PREFERENCE_KEY_LEN",
    "MAX_PREFERENCE_VALUE_LEN",
    "MAX_TAIL_LEN",
    "REASON_MIGRATED",
    "FailedPath",
    "KnownContainer",
    "MemoryValueError",
    "Preference",
    "Reservation",
    "SafeZone",
    "Square",
    "TaskOutcome",
    "TaskRecord",
    "container_tail",
    "read_int",
    "read_str",
]

#: Bounds on the free-text a record may carry. All of them are small because a
#: memory file is read back into a planner's view, and the planner's context is
#: not a place to spend on a two-hundred-character container name.
MAX_LABEL_LEN: Final = 64
MAX_DETAIL_LEN: Final = 120
MAX_TAIL_LEN: Final = 128
MAX_FULL_TYPE_LEN: Final = 96
MAX_CATEGORY_LEN: Final = 32
MAX_PREFERENCE_KEY_LEN: Final = 48
MAX_PREFERENCE_VALUE_LEN: Final = 64

#: Reason attached to a reservation recovered from a store written before
#: reservations carried one. Stating that the reason was not recorded is honest;
#: inventing one would put words in the user's mouth.
REASON_MIGRATED: Final = "migrated from an older memory file; the original reason was not recorded"


class MemoryValueError(ValueError):
    """A remembered fact is malformed, unbounded, or not save-scoped."""


@dataclass(frozen=True, slots=True)
class Square:
    """One grid cell. Integral, because a remembered place is a tile."""

    x: int
    y: int
    z: int

    def chebyshev_distance(self, other: Square) -> int:
        """Grid distance ignoring the floor; floors are compared separately."""
        return max(abs(self.x - other.x), abs(self.y - other.y))

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "z": self.z}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, field_name: str) -> Square:
        return cls(
            x=read_int(payload, "x", field_name=f"{field_name}.x"),
            y=read_int(payload, "y", field_name=f"{field_name}.y"),
            z=read_int(payload, "z", field_name=f"{field_name}.z"),
        )


class TaskOutcome(StrEnum):
    """How a remembered task ended.

    A closed vocabulary rather than free text: task history is read to decide
    whether to try something again, and "did it work" must not depend on parsing
    a sentence.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def read_str(
    payload: Mapping[str, Any],
    key: str,
    *,
    field_name: str,
    max_len: int,
    required: bool = True,
) -> str:
    """Read a bounded string out of a stored document.

    Over-long values are refused rather than truncated: truncation on *read*
    would silently change a fact that some earlier write already accepted, and
    the honest response to a document this module did not produce is to say so.
    """
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise MemoryValueError(f"{field_name} must be a string, got {type(value).__name__}")
    if required and not value:
        raise MemoryValueError(f"{field_name} must not be empty")
    if len(value) > max_len:
        raise MemoryValueError(f"{field_name} must be at most {max_len} characters")
    return value


def read_int(
    payload: Mapping[str, Any], key: str, *, field_name: str, default: int | None = None
) -> int:
    """Read an integer. ``bool`` is not an integer here."""
    if key not in payload:
        if default is None:
            raise MemoryValueError(f"missing required field {field_name}")
        return default
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise MemoryValueError(f"{field_name} must be an integer, got {type(value).__name__}")
    return value


def _check_timestamp(value: int, *, field_name: str) -> int:
    if value < 0:
        raise MemoryValueError(f"{field_name} must be non-negative, got {value}")
    return value


def _bounded_label(raw: str, *, max_len: int = MAX_LABEL_LEN) -> str:
    """Quarantine and truncate a game-authored string."""
    return redact_text(raw)[:max_len]


def container_tail(container_ref: str) -> str:
    """The session-independent half of a container reference.

    Raises:
        MemoryValueError: when the reference is malformed, or names a container
            that travels with the player. A worn or carried container's tail
            embeds a runtime id that does not survive a reload, so remembering
            one across sessions would produce a reference that resolves to
            whatever object inherited that id — the exact mis-resolve §3.7 is
            built to prevent.
    """
    try:
        parsed = ContainerRef.parse(container_ref)
    except RefError as exc:
        raise MemoryValueError(f"not a container reference: {container_ref!r}") from exc
    if not parsed.is_world:
        raise MemoryValueError(
            f"{container_ref} is not a world container; only a container with a fixed "
            "place in the world can be remembered across sessions"
        )
    if len(parsed.tail) > MAX_TAIL_LEN:
        raise MemoryValueError(f"container tail must be at most {MAX_TAIL_LEN} characters")
    return parsed.tail


def _square_from_tail(tail: str) -> Square | None:
    """The square a world container tail names, or None when it names none."""
    parts = tail.split(":")
    if len(parts) != 6 or parts[0] != "world":
        return None
    try:
        return Square(x=int(parts[1]), y=int(parts[2]), z=int(parts[3]))
    except ValueError:
        return None


def _bounded_categories(categories: Iterable[str], *, limit: int) -> tuple[str, ...]:
    """Deduplicate, sort and cap the category list of one container.

    Sorted because two observations of the same shelf in a different order are
    the same fact, and a memory that changed on every look would defeat the
    "has this container changed?" question it exists to answer.
    """
    cleaned = {
        redact_text(category)[:MAX_CATEGORY_LEN] for category in categories if category.strip()
    }
    return tuple(sorted(cleaned))[:limit]


@dataclass(frozen=True, slots=True)
class KnownContainer:
    """A container the agent has seen, where it stands and what was in it.

    ``last_seen_ms`` is when it was last observed at all; ``last_inspected_ms``
    is when its contents were last enumerated. They are separate because
    "I walked past it" and "I know what is in it" justify very different plans,
    and collapsing them would let a glance masquerade as a search.
    """

    tail: str
    kind: ContainerKind
    label: str
    square: Square | None
    last_seen_ms: int
    last_inspected_ms: int = 0
    categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.tail or len(self.tail) > MAX_TAIL_LEN:
            raise MemoryValueError(f"container tail must be 1..{MAX_TAIL_LEN} characters")
        if len(self.label) > MAX_LABEL_LEN:
            raise MemoryValueError(f"container label must be at most {MAX_LABEL_LEN} characters")
        _check_timestamp(self.last_seen_ms, field_name="last_seen_ms")
        _check_timestamp(self.last_inspected_ms, field_name="last_inspected_ms")
        if self.last_inspected_ms > self.last_seen_ms:
            raise MemoryValueError(
                "a container cannot have been inspected after the last time it was seen"
            )

    @classmethod
    def observed(
        cls,
        *,
        container_ref: str,
        kind: ContainerKind,
        name: str,
        seen_at_ms: int,
        inspected_at_ms: int = 0,
        categories: Iterable[str] = (),
        category_limit: int,
    ) -> KnownContainer:
        """Build a record from what the mod reported this session."""
        tail = container_tail(container_ref)
        return cls(
            tail=tail,
            kind=kind,
            label=_bounded_label(name),
            square=_square_from_tail(tail),
            last_seen_ms=seen_at_ms,
            last_inspected_ms=inspected_at_ms,
            categories=_bounded_categories(categories, limit=category_limit),
        )

    def ref_in_session(self, session_id: str) -> str:
        """This container's reference *for the current session*.

        Built rather than stored: the session id is the one part of a reference
        that must never be carried across a restart.
        """
        return str(ContainerRef(session_id=session_id, tail=self.tail))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "tail": self.tail,
            "kind": self.kind.value,
            "label": self.label,
            "last_seen_ms": self.last_seen_ms,
            "last_inspected_ms": self.last_inspected_ms,
        }
        if self.square is not None:
            out["square"] = self.square.to_dict()
        if self.categories:
            out["categories"] = list(self.categories)
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, category_limit: int) -> KnownContainer:
        square = payload.get("square")
        return cls(
            tail=read_str(payload, "tail", field_name="container.tail", max_len=MAX_TAIL_LEN),
            kind=_read_container_kind(payload),
            label=read_str(
                payload,
                "label",
                field_name="container.label",
                max_len=MAX_LABEL_LEN,
                required=False,
            ),
            square=(
                None
                if square is None
                else Square.from_dict(
                    _as_mapping(square, field_name="container.square"),
                    field_name="container.square",
                )
            ),
            last_seen_ms=read_int(payload, "last_seen_ms", field_name="container.last_seen_ms"),
            last_inspected_ms=read_int(
                payload, "last_inspected_ms", field_name="container.last_inspected_ms", default=0
            ),
            categories=_bounded_categories(
                _as_str_sequence(payload.get("categories", ()), field_name="container.categories"),
                limit=category_limit,
            ),
        )


@dataclass(frozen=True, slots=True)
class SafeZone:
    """A square the user or the agent marked as somewhere to fall back to."""

    key: str
    centre: Square
    radius: int
    label: str
    recorded_at_ms: int

    def __post_init__(self) -> None:
        if not self.key or len(self.key) > MAX_LABEL_LEN:
            raise MemoryValueError(f"a safe zone key must be 1..{MAX_LABEL_LEN} characters")
        if self.radius < 0:
            raise MemoryValueError(f"a safe zone radius must be non-negative, got {self.radius}")
        _check_timestamp(self.recorded_at_ms, field_name="recorded_at_ms")

    def contains(self, square: Square) -> bool:
        return square.z == self.centre.z and self.centre.chebyshev_distance(square) <= self.radius

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "centre": self.centre.to_dict(),
            "radius": self.radius,
            "label": self.label,
            "recorded_at_ms": self.recorded_at_ms,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SafeZone:
        return cls(
            key=read_str(payload, "key", field_name="safe_zone.key", max_len=MAX_LABEL_LEN),
            centre=Square.from_dict(
                _as_mapping(payload.get("centre", {}), field_name="safe_zone.centre"),
                field_name="safe_zone.centre",
            ),
            radius=read_int(payload, "radius", field_name="safe_zone.radius"),
            label=read_str(
                payload,
                "label",
                field_name="safe_zone.label",
                max_len=MAX_LABEL_LEN,
                required=False,
            ),
            recorded_at_ms=read_int(
                payload, "recorded_at_ms", field_name="safe_zone.recorded_at_ms"
            ),
        )


@dataclass(frozen=True, slots=True)
class FailedPath:
    """A route that did not work, so it is not tried blind a third time.

    ``attempts`` is a counter and not a list of timestamps: the question this
    record answers is "have I already failed at this?", and a list would grow
    with every retry — which is precisely the unbounded thing a memory must not
    hold.
    """

    origin: Square
    target: Square
    reason: str
    attempts: int
    last_failed_ms: int

    def __post_init__(self) -> None:
        if not self.reason or len(self.reason) > MAX_DETAIL_LEN:
            raise MemoryValueError(f"a failed path reason must be 1..{MAX_DETAIL_LEN} characters")
        if self.attempts < 1:
            raise MemoryValueError(f"attempts must be at least 1, got {self.attempts}")
        _check_timestamp(self.last_failed_ms, field_name="last_failed_ms")

    @property
    def key(self) -> str:
        """Identity of the route, used to merge repeated failures."""
        return (
            f"{self.origin.x}:{self.origin.y}:{self.origin.z}"
            f"->{self.target.x}:{self.target.y}:{self.target.z}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin.to_dict(),
            "target": self.target.to_dict(),
            "reason": self.reason,
            "attempts": self.attempts,
            "last_failed_ms": self.last_failed_ms,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FailedPath:
        return cls(
            origin=Square.from_dict(
                _as_mapping(payload.get("origin", {}), field_name="failed_path.origin"),
                field_name="failed_path.origin",
            ),
            target=Square.from_dict(
                _as_mapping(payload.get("target", {}), field_name="failed_path.target"),
                field_name="failed_path.target",
            ),
            reason=read_str(
                payload, "reason", field_name="failed_path.reason", max_len=MAX_DETAIL_LEN
            ),
            attempts=read_int(payload, "attempts", field_name="failed_path.attempts", default=1),
            last_failed_ms=read_int(
                payload, "last_failed_ms", field_name="failed_path.last_failed_ms"
            ),
        )


@dataclass(frozen=True, slots=True)
class Reservation:
    """An item type the user set aside.

    Keyed by ``full_type`` rather than by an item reference on purpose: a
    reference is session-scoped, so a reservation naming one would stop matching
    the moment the game reloaded — and the agent would eat the thing it was told
    not to touch, having "honoured" a reservation that no longer applied to
    anything.
    """

    full_type: str
    reason: str
    created_at_ms: int

    def __post_init__(self) -> None:
        if not self.full_type or len(self.full_type) > MAX_FULL_TYPE_LEN:
            raise MemoryValueError(f"full_type must be 1..{MAX_FULL_TYPE_LEN} characters")
        if len(self.reason) > MAX_DETAIL_LEN:
            raise MemoryValueError(f"a reservation reason must be at most {MAX_DETAIL_LEN} chars")
        _check_timestamp(self.created_at_ms, field_name="created_at_ms")

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_type": self.full_type,
            "reason": self.reason,
            "created_at_ms": self.created_at_ms,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Reservation:
        return cls(
            full_type=read_str(
                payload, "full_type", field_name="reservation.full_type", max_len=MAX_FULL_TYPE_LEN
            ),
            reason=read_str(
                payload,
                "reason",
                field_name="reservation.reason",
                max_len=MAX_DETAIL_LEN,
                required=False,
            ),
            created_at_ms=read_int(
                payload, "created_at_ms", field_name="reservation.created_at_ms", default=0
            ),
        )


@dataclass(frozen=True, slots=True)
class Preference:
    """One user setting that belongs to this save rather than to the install."""

    key: str
    value: str
    set_at_ms: int

    def __post_init__(self) -> None:
        if not self.key or len(self.key) > MAX_PREFERENCE_KEY_LEN:
            raise MemoryValueError(f"a preference key must be 1..{MAX_PREFERENCE_KEY_LEN} chars")
        if len(self.value) > MAX_PREFERENCE_VALUE_LEN:
            raise MemoryValueError(
                f"a preference value must be at most {MAX_PREFERENCE_VALUE_LEN} characters"
            )
        _check_timestamp(self.set_at_ms, field_name="set_at_ms")

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "set_at_ms": self.set_at_ms}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Preference:
        return cls(
            key=read_str(
                payload, "key", field_name="preference.key", max_len=MAX_PREFERENCE_KEY_LEN
            ),
            value=read_str(
                payload,
                "value",
                field_name="preference.value",
                max_len=MAX_PREFERENCE_VALUE_LEN,
                required=False,
            ),
            set_at_ms=read_int(payload, "set_at_ms", field_name="preference.set_at_ms", default=0),
        )


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """One thing that was attempted, and how it ended."""

    key: str
    outcome: TaskOutcome
    finished_at_ms: int
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.key or len(self.key) > MAX_LABEL_LEN:
            raise MemoryValueError(f"a task key must be 1..{MAX_LABEL_LEN} characters")
        if len(self.detail) > MAX_DETAIL_LEN:
            raise MemoryValueError(f"a task detail must be at most {MAX_DETAIL_LEN} characters")
        _check_timestamp(self.finished_at_ms, field_name="finished_at_ms")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "outcome": self.outcome.value,
            "finished_at_ms": self.finished_at_ms,
        }
        if self.detail:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TaskRecord:
        raw = read_str(payload, "outcome", field_name="task.outcome", max_len=MAX_LABEL_LEN)
        try:
            outcome = TaskOutcome(raw)
        except ValueError as exc:
            raise MemoryValueError(f"unknown task outcome {raw!r}") from exc
        return cls(
            key=read_str(payload, "key", field_name="task.key", max_len=MAX_LABEL_LEN),
            outcome=outcome,
            finished_at_ms=read_int(payload, "finished_at_ms", field_name="task.finished_at_ms"),
            detail=read_str(
                payload,
                "detail",
                field_name="task.detail",
                max_len=MAX_DETAIL_LEN,
                required=False,
            ),
        )


def _read_container_kind(payload: Mapping[str, Any]) -> ContainerKind:
    raw = read_str(payload, "kind", field_name="container.kind", max_len=MAX_CATEGORY_LEN)
    try:
        return ContainerKind(raw)
    except ValueError as exc:
        raise MemoryValueError(f"unknown container kind {raw!r}") from exc


def _as_mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoryValueError(f"{field_name} must be an object, got {type(value).__name__}")
    return value


def _as_str_sequence(value: Any, *, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MemoryValueError(f"{field_name} must be an array, got {type(value).__name__}")
    return tuple(item for item in value if isinstance(item, str))
