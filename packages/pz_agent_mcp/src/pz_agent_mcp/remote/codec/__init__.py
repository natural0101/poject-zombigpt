"""Turning the boundary's records into JSON and back, one module per port.

Every module here exports a matched pair — ``encode_x`` and ``decode_x`` — and
the pair is the unit. A record that can be encoded but not decoded is a message
the other process cannot read, and the failure surfaces on a user's machine as
a malformed answer rather than here as a failing test. So every codec is tested
by round trip, and the round trip asserts *equality of the record*, not that the
JSON looks right: a field silently dropped by both halves survives a
JSON-shape assertion and fails an equality one.

**Decoding is strict.** These bytes come from another process, and while that
process is our own, a stale executable from an earlier install is exactly the
peer this link expects to meet. Missing means missing: nothing here defaults a
field it did not receive. Defaulting is how ``armed`` becomes ``False`` because
a key was renamed, and a boundary that reports "not armed" when it means "I
could not read the answer" is worse than one that refuses.

**Enums travel as their values**, never as their names or their ordinals. An
ordinal changes when a member is inserted; a name changes when it is renamed;
the value is the thing the protocol already pins.

Errors are :class:`CodecError`, which the router turns into ``MALFORMED``. The
message names the field and never its value — this string reaches a traceback
and a bug report before any redactor sees it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, TypeVar

__all__ = [
    "CodecError",
    "optional_int",
    "optional_str",
    "require_bool",
    "require_enum",
    "require_float",
    "require_int",
    "require_mapping",
    "require_sequence",
    "require_str",
]

EnumT = TypeVar("EnumT", bound=Enum)


class CodecError(ValueError):
    """A payload could not be read as the record it claims to be.

    Carries the field name and the reason. Never the value: a decode failure on
    a memory record would otherwise put a game label into an exception, and an
    exception is written before any redactor runs.
    """


def _present(payload: Mapping[str, Any], field: str, where: str) -> Any:
    if field not in payload:
        raise CodecError(f"{where}: missing {field}")
    return payload[field]


def require_str(payload: Mapping[str, Any], field: str, *, where: str) -> str:
    value = _present(payload, field, where)
    if not isinstance(value, str):
        raise CodecError(f"{where}: {field} must be a string")
    return value


def optional_str(payload: Mapping[str, Any], field: str, *, where: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CodecError(f"{where}: {field} must be a string or null")
    return value


def require_int(payload: Mapping[str, Any], field: str, *, where: str) -> int:
    value = _present(payload, field, where)
    # `bool` is an `int` in Python, and letting `True` through as 1 turns a
    # field-name mistake into a plausible-looking number.
    if not isinstance(value, int) or isinstance(value, bool):
        raise CodecError(f"{where}: {field} must be an integer")
    return value


def optional_int(payload: Mapping[str, Any], field: str, *, where: str) -> int | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise CodecError(f"{where}: {field} must be an integer or null")
    return value


def require_bool(payload: Mapping[str, Any], field: str, *, where: str) -> bool:
    value = _present(payload, field, where)
    if not isinstance(value, bool):
        raise CodecError(f"{where}: {field} must be a boolean")
    return value


def require_float(payload: Mapping[str, Any], field: str, *, where: str) -> float:
    value = _present(payload, field, where)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodecError(f"{where}: {field} must be a number")
    return float(value)


def require_mapping(payload: Mapping[str, Any], field: str, *, where: str) -> Mapping[str, Any]:
    value = _present(payload, field, where)
    if not isinstance(value, Mapping):
        raise CodecError(f"{where}: {field} must be an object")
    return value


def require_sequence(payload: Mapping[str, Any], field: str, *, where: str) -> Sequence[Any]:
    value = _present(payload, field, where)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CodecError(f"{where}: {field} must be an array")
    return value


def require_enum(payload: Mapping[str, Any], field: str, kind: type[EnumT], *, where: str) -> EnumT:
    """Read *field* as a member of *kind*, by value.

    An unknown value is a refusal, not a fallback member. A fallback would make
    a peer that grew a new state look like one reporting the default state, and
    the default state for most of these enums is the safe-looking one.
    """
    raw = _present(payload, field, where)
    try:
        return kind(raw)
    except ValueError:
        # The value *is* named here, deliberately and safely: these are closed
        # protocol enums whose members are fixed identifiers, not user data.
        raise CodecError(f"{where}: {field}={raw!r} is not a known {kind.__name__}") from None
