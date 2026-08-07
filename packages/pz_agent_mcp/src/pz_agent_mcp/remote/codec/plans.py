"""Plans across the process boundary: what was asked for, and how far it got.

Three records travel here, and only one of them is flat.

:class:`~pz_agent_mcp.ports.PlanRequest` goes *out*, from the MCP server to the
sidecar, and it is the one message in this module that causes something to
happen in the game. Every one of its five fields is written on every encode and
required on every decode: none of them is declared ``| None``, and a default on
the reading side would turn "the peer did not send it" into "the peer sent the
default". The two that would hurt most are the limits — a lost ``max_steps`` or
``max_real_seconds`` is a plan the core runs to a ceiling nobody chose, which
AGENTS.md's "bounded everything" makes a bug rather than a degradation. A lost
``mode`` would be worse still: it is the autonomy the plan may spend.

:class:`~pz_agent_mcp.ports.PlanRecord` comes *back* and carries a *sequence* of
:class:`~pz_agent_mcp.ports.PlanStepRecord`, which is the only nesting on this
link. Three properties of that list are load bearing and are each pinned by a
test rather than left to the reader:

* it decodes to a ``tuple``, because the record declares one and a list would
  make an ostensibly frozen record mutable through its own field;
* it preserves order, because ``step_index`` addresses into it — a codec that
  sorted or de-duplicated would still pass a one-step round trip;
* it may be empty, because a plan that was refused before its first step has no
  steps and that is an answer, not a failure.

``steps`` is a **required** key even though the dataclass defaults it to ``()``.
The default is what an in-process caller gets for saying nothing; it is not
licence to invent one from a payload that did not carry it. A decoder that
defaulted it would publish ``step_count: 0`` for a plan that had run six steps,
which is the shape of failure this whole package is written against — a
confident, wrong, harmless-looking answer where the honest one is that the
payload could not be read.

The optional fields — ``reason_code`` on a step, ``action_id`` on a step,
``stopped_reason`` on the record — are omitted when absent rather than written
as ``null``, matching :mod:`pz_agent_mcp.remote.codec.actions`; the decoder
accepts either spelling, since a peer that omits a null is saying the same
thing. ``reason_code`` and ``stopped_reason`` are optional *enums*, and
:func:`_optional_enum` delegates to
:func:`~pz_agent_mcp.remote.codec.require_enum` for everything except the
absence, so an unknown reason is still a refusal and never a fallback member.
Falling back would report a plan stopped for a reason this build happens to
know instead of the one it was actually stopped for.

:class:`PlanRecord` has its own invariant — a non-empty ``plan_id`` and a
non-negative ``step_index`` — and it is not restated here.
:func:`decode_plan_record` builds the real record and turns its ``ValueError``
into a :class:`CodecError`, so this codec cannot drift into accepting a plan the
record itself would reject. The chain is dropped (``from None``) because the
record's message quotes the offending ``step_index`` and the message this module
raises must not quote anything from the payload.

Nothing here bounds ``goal``
----------------------------

``PlanRequest.goal`` is free text that reached the process from a tool caller or
a voice transcript, and this module carries it verbatim. That is deliberate: the
cap belongs where the text enters, and it exists there — ``pz_plan_execute``
declares ``maxLength: MAX_GOAL_CHARS`` in :mod:`pz_agent_mcp.catalog` and
:mod:`pz_agent_mcp.validation` enforces it before a :class:`PlanRequest` is ever
constructed, while the voice path only ever puts a closed ``VoiceGoal`` token in
the field. A second, different ceiling here would either duplicate that one or
silently disagree with it, and the codec is the wrong layer to decide how long a
goal a user may state. The transport's own 64 KiB request cap is the backstop.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, TypeVar

from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    JsonDict,
    ReasonCode,
    SessionMode,
)
from pz_agent_mcp.ports import PlanRecord, PlanRequest, PlanStepRecord
from pz_agent_mcp.remote.codec import (
    CodecError,
    optional_str,
    require_enum,
    require_int,
    require_mapping,
    require_sequence,
    require_str,
)

__all__ = [
    "decode_plan_current",
    "decode_plan_record",
    "decode_plan_request",
    "decode_plan_step",
    "encode_plan_current",
    "encode_plan_record",
    "encode_plan_request",
    "encode_plan_step",
]

_REQUEST = "plan.request"
_STEP = "plan.step"
_RECORD = "plan.record"
_CURRENT = "plan.current"

#: The key the record sits under in a ``plan.current`` body. Present only when
#: a plan exists; see :func:`encode_plan_current`.
_RECORD_FIELD = "plan"

_EnumT = TypeVar("_EnumT", bound=Enum)


def _optional_enum(
    payload: Mapping[str, Any], field: str, kind: type[_EnumT], *, where: str
) -> _EnumT | None:
    """Read an optional enum member, reusing the strict reader for the value.

    Absent and explicitly null are the same answer — "no value" — because the
    encoder omits the key and a peer that writes ``null`` means the same thing.
    Anything else present goes through :func:`require_enum`, so an unknown
    reason code is refused here exactly as it would be in a required field. A
    fallback member would be a plan reporting a reason it was not stopped for.
    """
    if payload.get(field) is None:
        return None
    return require_enum(payload, field, kind, where=where)


def encode_plan_request(request: PlanRequest) -> JsonDict:
    """The params body for ``plan.execute``.

    Every field, always. ``goal`` is carried verbatim — the length cap lives at
    the tool boundary that admitted the text, not here.
    """
    return {
        "goal": request.goal,
        "mode": request.mode.value,
        "max_steps": request.max_steps,
        "max_real_seconds": request.max_real_seconds,
        "idempotency_key": request.idempotency_key,
    }


def decode_plan_request(payload: Mapping[str, Any]) -> PlanRequest:
    """Read a ``plan.execute`` params body.

    Raises:
        CodecError: a field is missing, has the wrong type, or carries a mode
            this build does not know.
    """
    return PlanRequest(
        goal=require_str(payload, "goal", where=_REQUEST),
        mode=require_enum(payload, "mode", SessionMode, where=_REQUEST),
        max_steps=require_int(payload, "max_steps", where=_REQUEST),
        max_real_seconds=require_int(payload, "max_real_seconds", where=_REQUEST),
        idempotency_key=require_str(payload, "idempotency_key", where=_REQUEST),
    )


def encode_plan_step(record: PlanStepRecord) -> JsonDict:
    """One step, as the object that sits in a record's ``steps`` array."""
    out: JsonDict = {
        "index": record.index,
        "action": record.action.value,
        "status": record.status.value,
    }
    if record.reason_code is not None:
        out["reason_code"] = record.reason_code.value
    if record.action_id is not None:
        out["action_id"] = record.action_id
    return out


def _decode_plan_step(payload: Mapping[str, Any], *, where: str) -> PlanStepRecord:
    """Read one step, reporting failures against *where*.

    Private so that the nested call can say *which* step failed — the position
    is structural, derived from the array and not from anything the payload
    said, so naming it leaks nothing.
    """
    return PlanStepRecord(
        index=require_int(payload, "index", where=where),
        action=require_enum(payload, "action", ActionName, where=where),
        status=require_enum(payload, "status", ActionStatus, where=where),
        reason_code=_optional_enum(payload, "reason_code", ReasonCode, where=where),
        action_id=optional_str(payload, "action_id", where=where),
    )


def decode_plan_step(payload: Mapping[str, Any]) -> PlanStepRecord:
    """Read one step back.

    Raises:
        CodecError: a required field is missing or ill-typed, or an enum carries
            a value this build does not know.
    """
    return _decode_plan_step(payload, where=_STEP)


def encode_plan_record(record: PlanRecord) -> JsonDict:
    """The result body for ``plan.execute``, and the inner body of a status.

    ``steps`` is always written, as an array, in the record's own order. An
    empty plan is written as an empty array rather than omitted: "the plan ran
    no steps" is a fact about the plan, and a missing key would decode as a
    refusal.
    """
    out: JsonDict = {
        "plan_id": record.plan_id,
        "status": record.status.value,
        "step_index": record.step_index,
        "steps": [encode_plan_step(step) for step in record.steps],
    }
    if record.stopped_reason is not None:
        out["stopped_reason"] = record.stopped_reason.value
    return out


def decode_plan_record(payload: Mapping[str, Any]) -> PlanRecord:
    """Read a plan back, steps and invariant and all.

    ``steps`` becomes a ``tuple`` in the order it arrived — not sorted by
    ``index`` and not de-duplicated. The core owns the ordering; a codec that
    imposed one would silently repair a peer that had it wrong, and the repair
    would be indistinguishable from the peer being right.

    Raises:
        CodecError: a required field is missing or ill-typed, ``steps`` is not
            an array of step objects, an enum carries a value this build does
            not know, or the record's own invariant refuses the combination.
    """
    plan_id = require_str(payload, "plan_id", where=_RECORD)
    status = require_enum(payload, "status", ActionStatus, where=_RECORD)
    step_index = require_int(payload, "step_index", where=_RECORD)
    stopped_reason = _optional_enum(payload, "stopped_reason", ReasonCode, where=_RECORD)

    raw_steps = require_sequence(payload, "steps", where=_RECORD)
    steps: list[PlanStepRecord] = []
    for position, raw_step in enumerate(raw_steps):
        where = f"{_RECORD}: steps[{position}]"
        if not isinstance(raw_step, Mapping):
            raise CodecError(f"{where} must be an object")
        steps.append(_decode_plan_step(raw_step, where=where))

    try:
        return PlanRecord(
            plan_id=plan_id,
            status=status,
            step_index=step_index,
            steps=tuple(steps),
            stopped_reason=stopped_reason,
        )
    except ValueError:
        # The record's own bounds. Its message quotes the offending
        # `step_index`, so it is replaced rather than chained — an exception is
        # written before any redactor runs.
        raise CodecError(
            f"{_RECORD}: plan_id must not be empty and step_index must be non-negative"
        ) from None


def encode_plan_current(record: PlanRecord | None) -> JsonDict:
    """The result body for ``plan.current``.

    ``PlanPort.current`` answers ``None`` when no plan has run, and ``None`` is
    not something :func:`encode_plan_record` can produce. It is encoded as a
    missing key rather than as ``{}``, because an empty object decodes as "a
    plan with every field missing" — an error — and the caller would learn "the
    answer was malformed" when the truth is "no plan is running".
    """
    if record is None:
        return {}
    return {_RECORD_FIELD: encode_plan_record(record)}


def decode_plan_current(payload: Mapping[str, Any]) -> PlanRecord | None:
    """Read that body back, distinguishing "no plan" from "unreadable".

    Raises:
        CodecError: the key is present but is not a plan record. Deliberately
            *not* raised when the key is absent: that is the documented "nothing
            is running" answer, and turning it into an error would make an idle
            session look like a broken link.
    """
    if _RECORD_FIELD not in payload:
        return None
    return decode_plan_record(require_mapping(payload, _RECORD_FIELD, where=_CURRENT))
