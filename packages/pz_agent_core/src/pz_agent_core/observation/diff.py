"""Tier-1 observation diffs: only what changed, and exactly enough to rebuild.

§3.6 tier 1 is "only changed scalar fields and lists of refs". A diff that is
merely *approximately* right is worse than no diff at all, because every layer
above this one reasons about a snapshot it believes is current. So the module
holds one invariant, and the round-trip test exists to keep it true::

    apply_diff(previous, diff_observations(previous, current)) == current

Two consequences shape the implementation:

* **Diffing happens on the wire representation, not on the dataclasses.** The
  optional fields of an :class:`~pz_agent_core.protocol.Observation` are omitted
  from ``to_dict`` rather than emitted as null, which makes "this field became
  None" and "this key disappeared" the same event — one that a dataclass-level
  walk would have to special-case per field and would get wrong on the next
  field somebody adds.
* **Arrays of refs are keyed by their ref, never by position.** An item that
  moves from index 3 to index 0 has not changed; encoding it positionally would
  produce a diff larger than the snapshot it replaces. The order is carried
  separately as a bare list of refs, which is what makes the reconstruction
  exact instead of merely equivalent.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ..protocol import JsonDict, Observation
from ..protocol.messages import ProtocolError

#: Key that identifies an entry inside an array of objects. Arrays whose
#: entries all carry a unique one of these are diffed by identity; anything else
#: is replaced wholesale, because a positional diff of an unkeyed array cannot
#: be reconstructed without ambiguity.
REF_KEY: Final = "ref"


class DiffError(ValueError):
    """A diff cannot be computed, or does not apply to the given snapshot."""


@dataclass(frozen=True, slots=True)
class MappingDelta:
    """Changes to one JSON object.

    ``assigned`` carries whole new values; ``nested`` recurses into sub-objects
    that changed only partially; ``lists`` holds ref-keyed arrays. A key can
    appear in exactly one of the four, which is what makes application order
    irrelevant to the result.
    """

    assigned: dict[str, Any] = field(default_factory=dict)
    removed: tuple[str, ...] = ()
    nested: dict[str, MappingDelta] = field(default_factory=dict)
    lists: dict[str, ListDelta] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.assigned or self.removed or self.nested or self.lists)

    def to_dict(self) -> JsonDict:
        out: JsonDict = {}
        if self.assigned:
            out["assigned"] = copy.deepcopy(self.assigned)
        if self.removed:
            out["removed"] = list(self.removed)
        if self.nested:
            out["nested"] = {key: sub.to_dict() for key, sub in self.nested.items()}
        if self.lists:
            out["lists"] = {key: sub.to_dict() for key, sub in self.lists.items()}
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MappingDelta:
        return cls(
            assigned=dict(_object(payload.get("assigned", {}), "assigned")),
            removed=tuple(_string_list(payload.get("removed", []), "removed")),
            nested={
                key: cls.from_dict(_object(value, f"nested.{key}"))
                for key, value in _object(payload.get("nested", {}), "nested").items()
            },
            lists={
                key: ListDelta.from_dict(_object(value, f"lists.{key}"))
                for key, value in _object(payload.get("lists", {}), "lists").items()
            },
        )


@dataclass(frozen=True, slots=True)
class ListDelta:
    """Changes to an array of ref-keyed objects.

    ``order`` is the complete ref list of the resulting array. Carrying it in
    full is what keeps reconstruction exact, and it is still compact: refs are
    short strings, and the entries themselves only travel when they are new.
    """

    order: tuple[str, ...] = ()
    added: dict[str, JsonDict] = field(default_factory=dict)
    removed: tuple[str, ...] = ()
    changed: dict[str, MappingDelta] = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        out: JsonDict = {"order": list(self.order)}
        if self.added:
            out["added"] = {ref: copy.deepcopy(entry) for ref, entry in self.added.items()}
        if self.removed:
            out["removed"] = list(self.removed)
        if self.changed:
            out["changed"] = {ref: sub.to_dict() for ref, sub in self.changed.items()}
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ListDelta:
        return cls(
            order=tuple(_string_list(payload.get("order", []), "order")),
            added={
                ref: dict(_object(entry, f"added.{ref}"))
                for ref, entry in _object(payload.get("added", {}), "added").items()
            },
            removed=tuple(_string_list(payload.get("removed", []), "removed")),
            changed={
                ref: MappingDelta.from_dict(_object(value, f"changed.{ref}"))
                for ref, value in _object(payload.get("changed", {}), "changed").items()
            },
        )


@dataclass(frozen=True, slots=True)
class ObservationDiff:
    """One tier-1 update, pinned to the snapshot it was computed against.

    ``from_seq`` is not decoration: applying a diff to the wrong baseline is the
    one way this module can silently produce a plausible-looking lie, so the
    baseline is named and checked rather than assumed.
    """

    session_id: str
    from_seq: int
    to_seq: int
    timestamp_ms: int
    delta: MappingDelta

    @property
    def is_empty(self) -> bool:
        """True when nothing observable changed between the two snapshots."""
        return self.delta.is_empty

    def to_dict(self) -> JsonDict:
        return {
            "session_id": self.session_id,
            "from_seq": self.from_seq,
            "to_seq": self.to_seq,
            "timestamp_ms": self.timestamp_ms,
            "delta": self.delta.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ObservationDiff:
        return cls(
            session_id=_text(payload, "session_id"),
            from_seq=_number(payload, "from_seq"),
            to_seq=_number(payload, "to_seq"),
            timestamp_ms=_number(payload, "timestamp_ms"),
            delta=MappingDelta.from_dict(_object(payload.get("delta", {}), "delta")),
        )


def diff_observations(previous: Observation, current: Observation) -> ObservationDiff:
    """Compute the tier-1 update that turns *previous* into *current*.

    Raises:
        DiffError: if the two snapshots belong to different sessions, or if
            *current* is older than *previous* — a diff in that direction would
            describe a world that has already been superseded.
    """
    if previous.session_id != current.session_id:
        raise DiffError("cannot diff observations minted by different sessions")
    if current.seq < previous.seq:
        raise DiffError(f"current seq {current.seq} precedes baseline seq {previous.seq}")
    return ObservationDiff(
        session_id=current.session_id,
        from_seq=previous.seq,
        to_seq=current.seq,
        timestamp_ms=current.timestamp_ms,
        delta=_diff_mapping(previous.to_dict(), current.to_dict()),
    )


def apply_diff(previous: Observation, diff: ObservationDiff) -> Observation:
    """Rebuild the newer snapshot from *previous* and *diff*.

    Raises:
        DiffError: if the diff was computed against a different baseline, or if
            the merged document is not a valid observation. Both are refused
            rather than repaired: a half-applied diff is exactly the fabricated
            state this project forbids.
    """
    if diff.session_id != previous.session_id:
        raise DiffError("diff belongs to a different session than the baseline")
    if diff.from_seq != previous.seq:
        raise DiffError(f"diff applies to seq {diff.from_seq}, baseline is seq {previous.seq}")
    merged = _apply_mapping(previous.to_dict(), diff.delta)
    try:
        return Observation.from_dict(merged)
    except ProtocolError as exc:
        raise DiffError(f"applying the diff produced an invalid observation: {exc}") from exc


# --------------------------------------------------------------------------
# document-level diff
# --------------------------------------------------------------------------


def _diff_mapping(previous: Mapping[str, Any], current: Mapping[str, Any]) -> MappingDelta:
    assigned: dict[str, Any] = {}
    nested: dict[str, MappingDelta] = {}
    lists: dict[str, ListDelta] = {}

    for key, new_value in current.items():
        if key not in previous:
            assigned[key] = copy.deepcopy(new_value)
            continue
        old_value = previous[key]
        if isinstance(old_value, Mapping) and isinstance(new_value, Mapping):
            sub = _diff_mapping(old_value, new_value)
            if not sub.is_empty:
                nested[key] = sub
            continue
        if _is_ref_array(old_value) and _is_ref_array(new_value):
            list_delta = _diff_ref_array(old_value, new_value)
            if list_delta is not None:
                lists[key] = list_delta
            continue
        if _differs(old_value, new_value):
            assigned[key] = copy.deepcopy(new_value)

    return MappingDelta(
        assigned=assigned,
        removed=tuple(key for key in previous if key not in current),
        nested=nested,
        lists=lists,
    )


def _differs(old_value: Any, new_value: Any) -> bool:
    """True when the two values are not interchangeable on the wire.

    The type check is load-bearing: ``True == 1`` in Python, so a stat that
    flipped from a boolean to an integer would otherwise diff to nothing and
    reconstruct as the wrong type.
    """
    if type(old_value) is not type(new_value):
        return True
    return bool(old_value != new_value)


def _is_ref_array(value: Any) -> bool:
    """True for a list of objects that each carry a unique string ``ref``."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return False
    refs: set[str] = set()
    for entry in value:
        if not isinstance(entry, Mapping):
            return False
        ref = entry.get(REF_KEY)
        if not isinstance(ref, str) or ref in refs:
            return False
        refs.add(ref)
    return True


def _diff_ref_array(previous: Sequence[Any], current: Sequence[Any]) -> ListDelta | None:
    old_by_ref = {entry[REF_KEY]: entry for entry in previous}
    new_by_ref = {entry[REF_KEY]: entry for entry in current}

    added = {
        ref: copy.deepcopy(dict(entry))
        for ref, entry in new_by_ref.items()
        if ref not in old_by_ref
    }
    removed = tuple(ref for ref in old_by_ref if ref not in new_by_ref)
    changed: dict[str, MappingDelta] = {}
    for ref, entry in new_by_ref.items():
        old_entry = old_by_ref.get(ref)
        if old_entry is None:
            continue
        sub = _diff_mapping(old_entry, entry)
        if not sub.is_empty:
            changed[ref] = sub

    order = tuple(new_by_ref)
    if not added and not removed and not changed and order == tuple(old_by_ref):
        return None
    return ListDelta(order=order, added=added, removed=removed, changed=changed)


# --------------------------------------------------------------------------
# document-level apply
# --------------------------------------------------------------------------


def _apply_mapping(previous: Mapping[str, Any], delta: MappingDelta) -> JsonDict:
    out: JsonDict = copy.deepcopy(dict(previous))
    for key in delta.removed:
        out.pop(key, None)
    for key, sub in delta.nested.items():
        base = out.get(key)
        if not isinstance(base, Mapping):
            raise DiffError(f"diff recurses into {key!r}, which is not an object in the baseline")
        out[key] = _apply_mapping(base, sub)
    for key, list_delta in delta.lists.items():
        base_list = out.get(key)
        if isinstance(base_list, (str, bytes)) or not isinstance(base_list, Sequence):
            raise DiffError(f"diff updates {key!r}, which is not an array in the baseline")
        out[key] = _apply_ref_array(base_list, list_delta)
    out.update(copy.deepcopy(delta.assigned))
    return out


def _apply_ref_array(previous: Sequence[Any], delta: ListDelta) -> list[JsonDict]:
    by_ref: dict[str, JsonDict] = {}
    for entry in previous:
        if not isinstance(entry, Mapping):
            raise DiffError("ref-keyed array contains a non-object entry")
        ref = entry.get(REF_KEY)
        if not isinstance(ref, str):
            raise DiffError("ref-keyed array contains an entry without a ref")
        by_ref[ref] = copy.deepcopy(dict(entry))

    for ref in delta.removed:
        by_ref.pop(ref, None)
    for ref, sub in delta.changed.items():
        base = by_ref.get(ref)
        if base is None:
            raise DiffError(f"diff changes {ref!r}, which is absent from the baseline array")
        by_ref[ref] = _apply_mapping(base, sub)
    for ref, entry in delta.added.items():
        by_ref[ref] = copy.deepcopy(entry)

    rebuilt: list[JsonDict] = []
    for ref in delta.order:
        entry_out = by_ref.pop(ref, None)
        if entry_out is None:
            raise DiffError(f"diff order names {ref!r}, which the delta never provides")
        rebuilt.append(entry_out)
    if by_ref:
        raise DiffError(f"diff leaves {len(by_ref)} entr(ies) unplaced: {sorted(by_ref)}")
    return rebuilt


# --------------------------------------------------------------------------
# strict readers for the serialised form
# --------------------------------------------------------------------------


def _object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DiffError(f"{field_name} must be an object, got {type(value).__name__}")
    return value


def _string_list(value: Any, field_name: str) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise DiffError(f"{field_name} must be an array, got {type(value).__name__}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise DiffError(f"{field_name} must contain only strings")
        out.append(item)
    return out


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DiffError(f"diff field {key!r} must be a non-empty string")
    return value


def _number(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiffError(f"diff field {key!r} must be an integer")
    return value
