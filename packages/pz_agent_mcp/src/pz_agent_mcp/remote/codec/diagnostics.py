"""The doctor's checks and the tail of the log, across the process boundary.

These two records are what a user sees when something is already wrong, which
is what makes their codec worth arguing about. ``pz_debug_doctor`` is read when
arming was refused, and ``pz_debug_tail`` is read when the answer to "what
happened" is not in any tool's output. A field this codec drops or invents is a
field that misleads exactly the person who is already lost.

**``detail`` and ``remediation`` are required on the wire**, even though
:class:`~pz_agent_mcp.ports.DoctorCheck` defaults both to ``""``. The default
exists so a caller constructing a check in-process may leave out what it has
nothing to say about; it is not licence to invent the field from a payload that
did not carry it. An empty string and an absent key are different statements:
``""`` is "this check has no remedy to offer", and a missing key is "this
payload was not written by a build that agrees with mine". Decoded as ``""``
they become the same statement, and the one they become is the reassuring one —
a failing check would be reported with no way to fix it, and nothing downstream
could tell that from a check that genuinely had no remedy. The encoder always
writes both, so a peer that omits one is a peer this build cannot read.

**``level`` is a string and is deliberately not an enum.** The core has a
:class:`~pz_agent_core.diagnostics.LogLevel`, and pinning ``level`` to it here
would look stricter and would be worse. The port declares ``level: str``, and
the router already handles a level it does not recognise: it drops that record
and *counts* it, so the tool reports "one record omitted" rather than a short
list with no explanation. Rejecting the value in the codec would move that
decision to the wrong place and change its blast radius — one unrecognised
level would make the whole ``diagnostics.tail`` answer unreadable, and a log
tail that refuses to load is the worst possible outcome for the one tool whose
job is to explain a failure. So the level travels verbatim and the filtering
stays where it already is.

**``message`` is carried byte for byte.** It is a line the sidecar's own logger
wrote, and redaction happened at write time, upstream of here — this module
scrubs nothing, because a codec that redacted would redact a second time, on
text that a redactor has already seen, and neither half could then say what the
log actually said. Nor does it truncate: the caller bounds the *number* of
records (``MAX_LOG_RECORDS`` in the catalog, ``limit`` in the query), and a
codec that silently shortened a message would make a log line lie about what
was logged. The response cap in ``docs/CORE_RPC.md`` is the bound on size, it
is enforced on the bytes, and it fails loudly.

``code`` and ``action_id`` are the record's only optional fields — both are
declared ``| None`` — so they are encoded as an explicit ``null`` and decoded
from either the null or the absent key, since a peer that omits a null is
saying the same thing. Every other field is required.

:class:`TailQuery` is here for the same reason
:class:`~pz_agent_mcp.remote.codec.memory.MemoryQuery` is in its module:
:meth:`~pz_agent_mcp.ports.DiagnosticsPort.tail` states its parameters as a
keyword signature, and a signature is not something a wire can carry. Its
``limit`` is carried rather than clamped — the ceiling belongs to the router,
and a second ceiling here would be a second policy that drifts out of step with
the first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pz_agent_core.protocol import JsonDict
from pz_agent_mcp.ports import DoctorCheck, LogRecord
from pz_agent_mcp.remote.codec import (
    CodecError,
    optional_str,
    require_bool,
    require_int,
    require_sequence,
    require_str,
)

__all__ = [
    "TailQuery",
    "decode_doctor_check",
    "decode_doctor_checks",
    "decode_log_record",
    "decode_log_records",
    "decode_tail_query",
    "encode_doctor_check",
    "encode_doctor_checks",
    "encode_log_record",
    "encode_log_records",
    "encode_tail_query",
]

_CHECK: Final = "diagnostics.check"
_CHECKS: Final = "diagnostics.doctor"
_RECORD: Final = "diagnostics.record"
_RECORDS: Final = "diagnostics.tail"
_QUERY: Final = "diagnostics.tail params"

#: The key the checks sit under in a ``diagnostics.doctor`` result. Always
#: present: a doctor that found nothing to report answers with an empty array,
#: so a missing key is not a clean bill of health, it is an unreadable answer.
_CHECKS_FIELD: Final = "checks"

#: The key the log lines sit under in a ``diagnostics.tail`` result. Always
#: present, for the same reason: "nothing matched the filter" is an empty
#: array.
_RECORDS_FIELD: Final = "records"


@dataclass(frozen=True, slots=True)
class TailQuery:
    """The params of ``diagnostics.tail``, as one object the wire can carry.

    The three filters mirror :meth:`~pz_agent_mcp.ports.DiagnosticsPort.tail`
    exactly, including that ``None`` means "do not filter on this" rather than
    "match the empty string". ``limit`` has no default: a tail is requested by
    somebody who chose how much of it they want, and a default here would be a
    bound nobody asked for.
    """

    limit: int
    level: str | None = None
    component: str | None = None
    action_id: str | None = None


def encode_doctor_check(check: DoctorCheck) -> JsonDict:
    """*check* as the object to put in an RPC result."""
    return {
        "code": check.code,
        "ok": check.ok,
        "detail": check.detail,
        "remediation": check.remediation,
    }


def decode_doctor_check(payload: Mapping[str, Any]) -> DoctorCheck:
    """Read one environment check from *payload*, or refuse it.

    Raises:
        CodecError: a field is missing or has the wrong type. ``detail`` and
            ``remediation`` are required despite their dataclass defaults —
            see this module's docstring: decoded as ``""`` a dropped
            ``remediation`` reports a failing check as having no known fix.
    """
    return DoctorCheck(
        code=require_str(payload, "code", where=_CHECK),
        ok=require_bool(payload, "ok", where=_CHECK),
        detail=require_str(payload, "detail", where=_CHECK),
        remediation=require_str(payload, "remediation", where=_CHECK),
    )


def encode_doctor_checks(checks: Sequence[DoctorCheck]) -> JsonDict:
    """The result body for ``diagnostics.doctor``.

    Always the key, even for no checks. "The doctor reported nothing" and "the
    answer could not be read" must not have the same spelling, because the
    first one is a state a caller may reasonably act on.
    """
    return {_CHECKS_FIELD: [encode_doctor_check(check) for check in checks]}


def decode_doctor_checks(payload: Mapping[str, Any]) -> tuple[DoctorCheck, ...]:
    """Read that body back.

    Raises:
        CodecError: the key is missing, is not an array, or holds anything that
            is not a check. One unreadable check refuses the whole answer: a
            partial doctor that did not say it was partial would be read as a
            healthy environment with fewer checks in it.
    """
    raw = require_sequence(payload, _CHECKS_FIELD, where=_CHECKS)
    out: list[DoctorCheck] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise CodecError(f"{_CHECKS}: {_CHECKS_FIELD}[{index}] must be an object")
        out.append(decode_doctor_check(item))
    return tuple(out)


def encode_log_record(record: LogRecord) -> JsonDict:
    """*record* as the object to put in an RPC result.

    ``message`` is copied unchanged: it was redacted when it was written, and
    this codec neither redacts it again nor shortens it.
    """
    return {
        "timestamp_ms": record.timestamp_ms,
        "level": record.level,
        "component": record.component,
        "message": record.message,
        "code": record.code,
        "action_id": record.action_id,
    }


def decode_log_record(payload: Mapping[str, Any]) -> LogRecord:
    """Read one log line from *payload*, or refuse it.

    Raises:
        CodecError: a required field is missing or has the wrong type.
            ``timestamp_ms`` must be an integer and not a boolean, ``level``
            and ``component`` are carried verbatim as strings, and ``code`` and
            ``action_id`` may be null or absent.
    """
    return LogRecord(
        timestamp_ms=require_int(payload, "timestamp_ms", where=_RECORD),
        level=require_str(payload, "level", where=_RECORD),
        component=require_str(payload, "component", where=_RECORD),
        message=require_str(payload, "message", where=_RECORD),
        code=optional_str(payload, "code", where=_RECORD),
        action_id=optional_str(payload, "action_id", where=_RECORD),
    )


def encode_log_records(records: Sequence[LogRecord]) -> JsonDict:
    """The result body for ``diagnostics.tail``."""
    return {_RECORDS_FIELD: [encode_log_record(record) for record in records]}


def decode_log_records(payload: Mapping[str, Any]) -> tuple[LogRecord, ...]:
    """Read that body back.

    Raises:
        CodecError: the key is missing, is not an array, or holds anything that
            is not a log record. Absence is an error rather than an empty tail:
            there is no "no log yet" state — a core with nothing to show sends
            an empty array — so a missing key means the payload was unreadable.
    """
    raw = require_sequence(payload, _RECORDS_FIELD, where=_RECORDS)
    out: list[LogRecord] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise CodecError(f"{_RECORDS}: {_RECORDS_FIELD}[{index}] must be an object")
        out.append(decode_log_record(item))
    return tuple(out)


def encode_tail_query(query: TailQuery) -> JsonDict:
    """The params body for ``diagnostics.tail``.

    The three filters are written even when they are ``None``, so the object
    always carries the full field set and an absent filter is visible as such
    rather than as a key somebody forgot.
    """
    return {
        "limit": query.limit,
        "level": query.level,
        "component": query.component,
        "action_id": query.action_id,
    }


def decode_tail_query(payload: Mapping[str, Any]) -> TailQuery:
    """Read a ``diagnostics.tail`` params body.

    Raises:
        CodecError: ``limit`` is missing or is not an integer, or a filter is
            present as something other than a string. ``limit`` is not
            defaulted: a peer that did not send one is not asking for the
            catalog's default, it is a peer this build cannot read.
    """
    return TailQuery(
        limit=require_int(payload, "limit", where=_QUERY),
        level=optional_str(payload, "level", where=_QUERY),
        component=optional_str(payload, "component", where=_QUERY),
        action_id=optional_str(payload, "action_id", where=_QUERY),
    )
