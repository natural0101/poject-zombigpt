"""Stable, session-scoped references to game objects.

The LLM never sees a Lua table or a Java handle. It sees an opaque string that
the mod can re-resolve and re-validate immediately before acting on it
(docs/blueprint/03_GAME_STATE_AND_IPC.md §3.7).

A reference carries a *generation*. Anything that invalidates object identity —
a save/load transition, a new session — bumps the generation, so a reference
minted before the transition fails validation instead of silently pointing at a
different object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: Session ids are UUIDs; container/item segments are restricted to a safe
#: alphabet so a reference can never smuggle a path separator or a delimiter.
_SAFE_SEGMENT: Final = re.compile(r"^[A-Za-z0-9_.\-]+$")
_SESSION_SEGMENT: Final = re.compile(r"^[A-Za-z0-9\-]{1,64}$")

REF_SEPARATOR: Final = ":"


class RefKind(StrEnum):
    """Leading token of a reference string."""

    ITEM = "item"
    CONTAINER = "container"
    SQUARE = "square"
    ZOMBIE = "zombie"
    WOUND = "wound"
    OBJECT = "object"


class RefError(ValueError):
    """A reference is malformed, or belongs to a different session."""


def _check_segment(value: str, *, field: str) -> str:
    if not value or not _SAFE_SEGMENT.match(value):
        raise RefError(f"invalid {field} segment: {value!r}")
    return value


def _check_session(value: str) -> str:
    if not _SESSION_SEGMENT.match(value):
        raise RefError(f"invalid session segment: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class ItemRef:
    """``item:<session>:<container-ref-tail>:<runtime-id>:<generation>``.

    The container tail is stored joined because a container reference itself
    contains colons; splitting is done from both ends, never by a naive
    ``split(":")``.
    """

    session_id: str
    container_tail: str
    runtime_id: str
    generation: int

    def __str__(self) -> str:
        return REF_SEPARATOR.join(
            (
                RefKind.ITEM.value,
                self.session_id,
                self.container_tail,
                self.runtime_id,
                str(self.generation),
            )
        )

    @classmethod
    def parse(cls, raw: str) -> ItemRef:
        kind, _, rest = raw.partition(REF_SEPARATOR)
        if kind != RefKind.ITEM.value:
            raise RefError(f"not an item ref: {raw!r}")
        session, _, rest = rest.partition(REF_SEPARATOR)
        head, sep, generation = rest.rpartition(REF_SEPARATOR)
        if not sep:
            raise RefError(f"item ref missing generation: {raw!r}")
        container_tail, sep, runtime_id = head.rpartition(REF_SEPARATOR)
        if not sep:
            raise RefError(f"item ref missing runtime id: {raw!r}")
        if not generation.isdigit():
            raise RefError(f"item ref generation must be numeric: {raw!r}")
        return cls(
            session_id=_check_session(session),
            container_tail=container_tail,
            runtime_id=_check_segment(runtime_id, field="runtime id"),
            generation=int(generation),
        )

    @property
    def container_ref(self) -> str:
        """The owning container reference, reconstructed."""
        return REF_SEPARATOR.join((RefKind.CONTAINER.value, self.session_id, self.container_tail))


@dataclass(frozen=True, slots=True)
class ContainerRef:
    """``container:<session>:<tail>`` where the tail encodes the location.

    Recognised tails:

    * ``player-main``
    * ``worn:<slot>:<item-runtime-id>``
    * ``carried:<item-runtime-id>``
    * ``world:<x>:<y>:<z>:<object-index>:<container-index>``
    """

    session_id: str
    tail: str

    def __str__(self) -> str:
        return REF_SEPARATOR.join((RefKind.CONTAINER.value, self.session_id, self.tail))

    @classmethod
    def parse(cls, raw: str) -> ContainerRef:
        kind, _, rest = raw.partition(REF_SEPARATOR)
        if kind != RefKind.CONTAINER.value:
            raise RefError(f"not a container ref: {raw!r}")
        session, _, tail = rest.partition(REF_SEPARATOR)
        if not tail:
            raise RefError(f"container ref missing tail: {raw!r}")
        for part in tail.split(REF_SEPARATOR):
            _check_segment(part, field="container tail")
        return cls(session_id=_check_session(session), tail=tail)

    @classmethod
    def player_main(cls, session_id: str) -> ContainerRef:
        return cls(session_id=_check_session(session_id), tail="player-main")

    @classmethod
    def worn(cls, session_id: str, slot: str, item_runtime_id: str) -> ContainerRef:
        return cls(
            session_id=_check_session(session_id),
            tail=REF_SEPARATOR.join(
                (
                    "worn",
                    _check_segment(slot, field="slot"),
                    _check_segment(item_runtime_id, field="runtime id"),
                )
            ),
        )

    @classmethod
    def world(
        cls,
        session_id: str,
        *,
        x: int,
        y: int,
        z: int,
        object_index: int,
        container_index: int,
    ) -> ContainerRef:
        return cls(
            session_id=_check_session(session_id),
            tail=REF_SEPARATOR.join(
                ("world", str(x), str(y), str(z), str(object_index), str(container_index))
            ),
        )

    @property
    def is_player_main(self) -> bool:
        return self.tail == "player-main"

    @property
    def is_on_person(self) -> bool:
        """True for the main inventory and anything worn or carried."""
        return self.is_player_main or self.tail.startswith(("worn:", "carried:"))

    @property
    def is_world(self) -> bool:
        return self.tail.startswith("world:")


@dataclass(frozen=True, slots=True)
class SquareRef:
    """``square:<session>:<x>:<y>:<z>`` — a single grid cell."""

    session_id: str
    x: int
    y: int
    z: int

    def __str__(self) -> str:
        return REF_SEPARATOR.join(
            (RefKind.SQUARE.value, self.session_id, str(self.x), str(self.y), str(self.z))
        )

    @classmethod
    def parse(cls, raw: str) -> SquareRef:
        parts = raw.split(REF_SEPARATOR)
        if len(parts) != 5 or parts[0] != RefKind.SQUARE.value:
            raise RefError(f"not a square ref: {raw!r}")
        _, session, sx, sy, sz = parts
        try:
            return cls(session_id=_check_session(session), x=int(sx), y=int(sy), z=int(sz))
        except ValueError as exc:
            raise RefError(f"square ref has non-integer coordinate: {raw!r}") from exc

    def chebyshev_distance(self, other: SquareRef) -> int:
        """Grid distance ignoring floor; movement checks compare floors separately."""
        return max(abs(self.x - other.x), abs(self.y - other.y))


@dataclass(frozen=True, slots=True)
class ZombieRef:
    """``zombie:<session>:<runtime-id>:<generation>``."""

    session_id: str
    runtime_id: str
    generation: int

    def __str__(self) -> str:
        return REF_SEPARATOR.join(
            (
                RefKind.ZOMBIE.value,
                self.session_id,
                self.runtime_id,
                str(self.generation),
            )
        )

    @classmethod
    def parse(cls, raw: str) -> ZombieRef:
        parts = raw.split(REF_SEPARATOR)
        if len(parts) != 4 or parts[0] != RefKind.ZOMBIE.value:
            raise RefError(f"not a zombie ref: {raw!r}")
        _, session, runtime_id, generation = parts
        if not generation.isdigit():
            raise RefError(f"zombie ref generation must be numeric: {raw!r}")
        return cls(
            session_id=_check_session(session),
            runtime_id=_check_segment(runtime_id, field="runtime id"),
            generation=int(generation),
        )


def ref_kind(raw: str) -> RefKind:
    """Classify a reference string without fully parsing it.

    Raises:
        RefError: if the leading token is not a known kind.
    """
    head, _, _ = raw.partition(REF_SEPARATOR)
    try:
        return RefKind(head)
    except ValueError as exc:
        raise RefError(f"unknown ref kind: {raw!r}") from exc


def belongs_to_session(raw: str, session_id: str) -> bool:
    """True when *raw* was minted by *session_id*.

    A reference from an earlier session is not merely stale — its runtime ids
    may now denote entirely different objects, so callers must treat a False
    here as ``INVALID_REF`` rather than retrying.
    """
    head, _, rest = raw.partition(REF_SEPARATOR)
    if head not in {k.value for k in RefKind}:
        return False
    candidate, _, _ = rest.partition(REF_SEPARATOR)
    return candidate == session_id
