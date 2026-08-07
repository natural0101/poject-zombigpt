"""Memory across the process boundary, and the split the record exists to keep.

:class:`~pz_agent_mcp.ports.MemoryRecord` is deliberately shaped so that a
memory store has nowhere to put free text that is not quarantined: ``data``
holds numbers, booleans and nulls, ``refs`` holds references, and ``label`` is
the one string a human would read — the one the router runs through
:func:`~pz_agent_mcp.scrub.scrub_text` and marks as untrusted content.

A codec is exactly where that split gets undone by accident. ``data`` is typed
``Mapping[str, float | int | bool | None]`` and Python enforces none of it, so a
store that put a sentence in a numeric field would ship it straight past the
quarantine — the router scrubs ``label`` by name, and a string arriving under
``data`` is not a label. So this module checks the values rather than trusting
the annotation, **on both sides**: encoding refuses it too, because the failure
belongs on the side that holds the offending record, not on the peer that has
only the bytes.

What ``data`` may carry, and nothing else:

* an ``int`` or a **finite** ``float`` — ``NaN`` and the infinities are Python
  floats that :func:`json.dumps` writes as bare ``NaN``/``Infinity``, which no
  strict JSON reader on the other end of a pipe has to accept;
* a ``bool``, which stays a ``bool``. ``bool`` is a subclass of ``int`` in
  Python and ``True == 1``, so a coercion here would survive every equality
  assertion in the suite and change what the record says;
* ``None``, which is in the declared type and means "known to be absent";
* nothing nested. An object or an array is refused rather than walked: a
  container is somewhere a string can hide one level down, and the record has
  no field that is a container of anything but references.

Keys are checked for being strings for a reason that looks pedantic and is not:
:func:`json.dumps` silently turns an ``int`` key into a string, so a record with
a non-string key would round-trip through JSON as a *different* record, and both
halves would agree it was fine.

**The messages name ``data`` and the constraint, never the entry.** A key or a
value in a memory payload comes from a memory store fed by the game, and a
:class:`CodecError` is written before any redactor sees it.

``label`` is encoded as an explicit ``null`` when absent so the object always
carries the full field set; the decoder accepts the null or the missing key,
since a peer that omits a null is saying the same thing. Every other field is
required — ``refs`` and ``data`` have dataclass defaults so a caller building a
record in-process may leave them out, which is not licence to invent them from a
payload that did not carry them. An absent ``refs`` decoded as ``()`` is a
record that has quietly lost its references.

:class:`MemoryQuery` is here rather than in :mod:`pz_agent_mcp.ports` because
:meth:`~pz_agent_mcp.ports.MemoryPort.query` expresses the query as a keyword
signature, and a signature is not something a wire can carry. It exists only
between the two halves of this link. Its ``limit`` is carried, not clamped: the
ceiling is :data:`~pz_agent_mcp.catalog.MAX_MEMORY_RESULTS`, the router applies
it, and a second ceiling here would be a second policy that drifts.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pz_agent_core.protocol import JsonDict
from pz_agent_mcp.ports import MemoryRecord
from pz_agent_mcp.remote.codec import (
    CodecError,
    optional_str,
    require_int,
    require_mapping,
    require_sequence,
    require_str,
)

__all__ = [
    "MemoryQuery",
    "decode_memory_query",
    "decode_memory_record",
    "decode_memory_records",
    "encode_memory_query",
    "encode_memory_record",
    "encode_memory_records",
]

_RECORD: Final = "memory.record"
_RECORDS: Final = "memory.records"
_QUERY: Final = "memory.query"

#: The key the list sits under in a ``memory.query`` result. Always present:
#: "no memories matched" is an empty array, so a missing key is not an empty
#: answer, it is an unreadable one.
_RECORDS_FIELD: Final = "records"


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """The params of ``memory.query``, as one object the wire can carry.

    ``kinds`` empty means "every kind", which is what
    :meth:`~pz_agent_mcp.ports.MemoryPort.query` already means by an empty
    sequence — the emptiness is carried, not turned into a missing key. Neither
    field has a default: a query is built to be sent, and a default here would
    be a filter or a bound nobody chose.
    """

    kinds: tuple[str, ...]
    limit: int


def _data_value(value: Any, *, where: str) -> float | int | bool | None:
    """One ``data`` entry, unchanged, or a refusal.

    Returned unchanged rather than coerced: ``float(True)`` is ``1.0`` and
    ``int(2.5)`` is ``2``, and both are a codec deciding what a record says.
    """
    if value is None:
        return None
    # Before `int`, because `bool` is a subclass of it and the isinstance check
    # below would otherwise claim every `True` as an integer.
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodecError(f"{where}: data numbers must be finite")
        return value
    # Strings land here, and this is the branch the record's shape exists for:
    # `data` is the numeric half of the split and a string in it is text that
    # never passed the quarantine. So does anything nested — an object or an
    # array is a place for one to hide a level down.
    raise CodecError(f"{where}: data values must be a number, a boolean or null")


def _encode_data(data: Mapping[str, float | int | bool | None], *, where: str) -> JsonDict:
    """Check and copy ``data`` on the way out.

    The annotation is checked rather than trusted: it is not enforced at
    runtime, this mapping comes from whichever memory store the core is running,
    and the whole point of the field's type is that text cannot travel in it.
    """
    # Widened deliberately: the declared key and value types are what this
    # function is verifying, so it must not be typed as though they held — and
    # that includes the container's own type, checked before it is walked.
    raw: Any = data
    if not isinstance(raw, Mapping):
        raise CodecError(f"{where}: data must be an object")
    out: JsonDict = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise CodecError(f"{where}: data keys must be strings")
        out[key] = _data_value(value, where=where)
    return out


def _decode_data(payload: Mapping[str, Any], *, where: str) -> dict[str, float | int | bool | None]:
    """Read ``data`` back with the same rules the encoder applied."""
    raw: Mapping[Any, Any] = require_mapping(payload, "data", where=where)
    out: dict[str, float | int | bool | None] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise CodecError(f"{where}: data keys must be strings")
        out[key] = _data_value(value, where=where)
    return out


def _encode_strings(values: Sequence[str], *, field: str, where: str) -> list[str]:
    """Check and copy a sequence of strings on the way out.

    The container is checked before its elements, and a ``str`` is refused as
    the container specifically: a string *is* a sequence of one-character
    strings, so the element loop below would accept ``refs="ref:room:2b"`` and
    emit eleven single-character references. That payload is well-formed, so
    the peer decodes it without complaint and both halves agree on a record the
    core never held — the same silent-divergence failure the non-string ``data``
    key check exists to stop. A ``Mapping`` is refused for the same reason: it
    iterates as its keys, which are strings too.
    """
    # Widened for the same reason as `data`: the element type is what is being
    # verified, so this must not be typed as though it already held.
    raw: Any = values
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise CodecError(f"{where}: {field} must be an array")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise CodecError(f"{where}: {field} must contain only strings")
        out.append(item)
    return out


def _decode_strings(payload: Mapping[str, Any], field: str, *, where: str) -> tuple[str, ...]:
    """Read a required array of strings back as a tuple.

    A tuple because the record is frozen and its references are not a list the
    caller may edit; decoding to a list would make a decoded record unequal to
    the one that was encoded, which is the one thing the round trip must catch.
    """
    raw = require_sequence(payload, field, where=where)
    for item in raw:
        if not isinstance(item, str):
            raise CodecError(f"{where}: {field} must contain only strings")
    return tuple(raw)


def encode_memory_record(record: MemoryRecord) -> JsonDict:
    """*record* as the object to put in an RPC result.

    Raises:
        CodecError: ``data`` or ``refs`` carries something the record's type
            says it cannot — a string, a container, a non-finite number or a
            non-string key — or is not the container its type says it is.
            Refused here as well as on decode so a store that smuggles text
            fails on the side that produced it.
    """
    return {
        "kind": record.kind,
        "key": record.key,
        "label": record.label,
        "refs": _encode_strings(record.refs, field="refs", where=_RECORD),
        "data": _encode_data(record.data, where=_RECORD),
    }


def decode_memory_record(payload: Mapping[str, Any]) -> MemoryRecord:
    """Read one memory record from *payload*, or refuse it.

    Raises:
        CodecError: a required field is missing or has the wrong type, or
            ``data`` carries a value the record's type does not permit.
    """
    return MemoryRecord(
        kind=require_str(payload, "kind", where=_RECORD),
        key=require_str(payload, "key", where=_RECORD),
        label=optional_str(payload, "label", where=_RECORD),
        refs=_decode_strings(payload, "refs", where=_RECORD),
        data=_decode_data(payload, where=_RECORD),
    )


def encode_memory_records(records: Sequence[MemoryRecord]) -> JsonDict:
    """The result body for ``memory.query``.

    Always the key, even for no matches. An empty result and an unreadable one
    are different answers, and a boundary that spelled them the same way would
    report "nothing remembered" for a link it could not read.
    """
    return {_RECORDS_FIELD: [encode_memory_record(record) for record in records]}


def decode_memory_records(payload: Mapping[str, Any]) -> tuple[MemoryRecord, ...]:
    """Read that body back.

    Raises:
        CodecError: the key is missing, is not an array, or holds anything that
            is not a memory record. Absence is an error here, unlike
            ``observation.latest``: there is no "no answer yet" state for a
            query, so a missing key means the payload could not be read.
    """
    raw = require_sequence(payload, _RECORDS_FIELD, where=_RECORDS)
    out: list[MemoryRecord] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise CodecError(f"{_RECORDS}: records[{index}] must be an object")
        out.append(decode_memory_record(item))
    return tuple(out)


def encode_memory_query(query: MemoryQuery) -> JsonDict:
    """The params body for ``memory.query``."""
    return {
        "kinds": _encode_strings(query.kinds, field="kinds", where=_QUERY),
        "limit": query.limit,
    }


def decode_memory_query(payload: Mapping[str, Any]) -> MemoryQuery:
    """Read a ``memory.query`` params body.

    Raises:
        CodecError: a field is missing, ``kinds`` is not an array of strings, or
            ``limit`` is not an integer. ``limit`` is not defaulted: a peer that
            did not send one is not asking for the catalog's default, it is a
            peer this build cannot read.
    """
    return MemoryQuery(
        kinds=_decode_strings(payload, "kinds", where=_QUERY),
        limit=require_int(payload, "limit", where=_QUERY),
    )
