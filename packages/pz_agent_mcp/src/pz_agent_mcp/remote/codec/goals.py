"""The typed goal channel across the process boundary.

A goal is the widest thing anything outside the deterministic core is allowed to
express — AGENTS.md puts it as "the model may express a *goal*; it never picks
the sandwich" — and :mod:`pz_agent_core.goals` is where that promise is a type
rather than a convention. This module carries that type over a pipe, and the
whole difficulty is that a pipe is exactly where a type stops being enforced:
what arrives is bytes, and the closed enum on the far side is a string until
something refuses it.

:class:`~pz_agent_core.goals.GoalRequest` goes *out*, from the MCP server to the
sidecar. It is the only message on this link whose content originated with a
language model or a microphone, so it is the one that decides how wide the
opening actually is.

:class:`~pz_agent_core.goals.GoalRecord` and
:class:`~pz_agent_core.goals.GoalRefusal` come *back*, wrapped in a
:class:`~pz_agent_core.goals.GoalAdmission` for a submission, in a
:class:`~pz_agent_mcp.ports.GoalChannelStatus` for a status and in a
:class:`~pz_agent_mcp.ports.GoalCancellation` for a cancel. The record carries
the channel's honesty rule — ``SUCCEEDED`` requires ``POSTCONDITION_MET`` and
observed evidence keys — and that rule is deliberately *not* restated here:
:func:`decode_goal_record` builds the real record and lets it refuse, exactly as
:mod:`pz_agent_mcp.remote.codec.actions` does one layer down. A remote core
cannot report a success it cannot prove merely by being on the other side of a
pipe. The two wrappers do the same with their own invariants — a cancellation
that was accepted must name the goal it was accepted for, and that check stays
in :class:`~pz_agent_mcp.ports.GoalCancellation`.

Why ``kind`` does not go through ``require_enum``
-------------------------------------------------

Every other closed enum on this link is read with
:func:`~pz_agent_mcp.remote.codec.require_enum`, which names the offending value
in its message on the grounds that a protocol identifier is not user data. That
grounds does not hold for a goal. ``kind`` and ``skill`` are the two fields on
this link that a transcript or a model's output reaches *unfiltered* — the whole
point of the channel is that the caller proposes a token — and
:mod:`pz_agent_core.goals.model` is explicit that "the only route from a string
to either of them is :func:`~pz_agent_core.goals.parse_kind` /
:func:`~pz_agent_core.goals.parse_skill`", whose ``None`` case "callers report
with a refusal that does not quote *raw*".

So both fields are read through those two functions and refused without being
echoed. That buys three things at once: the string is bounded before it is
folded (``parse_kind`` truncates first), there is exactly one string-to-enum door
in the process rather than two that can disagree, and an invented kind produces a
:class:`~pz_agent_mcp.remote.codec.CodecError` naming the field and a count —
never a member of the enum, and never a byte the caller wrote into a traceback a
bug report will quote.

That last part is the property ``test_rpc_codec_goals.py`` exists for. A decoder
that fell back to a member would turn an unknown token into
``satisfy_hunger``, and the caller would be told its goal was accepted while the
agent went to eat.

The full field set travels
--------------------------

:func:`encode_goal_record` writes every field of the record on every encode,
including the three that may be absent (``started_at_ms``, ``finished_at_ms``,
``reason_code``), which travel as an explicit ``null``. Nothing here defaults a
field it did not receive: defaulting ``steps_used`` would report a goal that had
spent its budget as one that had just started, and defaulting ``state`` would
report a cancelled goal as pending. The decoder accepts either the null or the
missing key for the three optional fields, because a peer that omits a null is
saying the same thing.

:class:`~pz_agent_core.goals.GoalParams` is the one object encoded sparsely, and
that is not a style choice: :meth:`~pz_agent_core.goals.GoalParams.present`
distinguishes "this parameter was supplied" from "it was not", and the kind's
spec makes each parameter required or forbidden on that basis. A key written as
``null`` and a key omitted must therefore decode identically, which they do.
``satisfy_to`` is why the encoder tests ``is not None`` rather than truthiness:
``0.0`` is a parameter a caller chose, and dropping it would silently widen a
goal that asked to stop at nothing.

``budget`` is optional on a request and absent means "use the kind's declared
default" — a statement, not a gap. It is carried as an absent key and decodes
back to ``None`` rather than to the default it resolves to, so a round trip is
equality of the request and not merely of its behaviour. Materialising the
default here would be this layer answering a question the caller left open, and
the two would then disagree the moment :data:`~pz_agent_core.goals.GOAL_SPECS`
moved.

Nothing carries the raw idempotency key back
--------------------------------------------

The request carries it because the core needs it; the record carries only
:attr:`~pz_agent_core.goals.GoalRecord.key_digest`, because that is all the
record has. No message this module raises quotes the key either — the failures
that could are re-raised with :class:`CodecError` and ``from None``, since the
constructor's own text quotes the value it rejected and an exception is written
before any redactor runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Final, TypeVar

from pz_agent_core.goals import (
    MAX_EVIDENCE_KEYS,
    GoalAdmission,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalRecord,
    GoalRefusal,
    GoalRequest,
    GoalState,
    TrainableSkill,
    parse_kind,
    parse_skill,
)
from pz_agent_core.protocol import JsonDict, ReasonCode
from pz_agent_mcp.remote.codec import (
    CodecError,
    optional_int,
    require_bool,
    require_enum,
    require_float,
    require_int,
    require_mapping,
    require_sequence,
    require_str,
)

__all__ = [
    "decode_goal_admission",
    "decode_goal_budget",
    "decode_goal_cancellation",
    "decode_goal_id",
    "decode_goal_params",
    "decode_goal_record",
    "decode_goal_refusal",
    "decode_goal_request",
    "decode_goal_status",
    "encode_goal_admission",
    "encode_goal_budget",
    "encode_goal_cancellation",
    "encode_goal_id",
    "encode_goal_params",
    "encode_goal_record",
    "encode_goal_refusal",
    "encode_goal_request",
    "encode_goal_status",
]

_REQUEST: Final = "goal.request"
_PARAMS: Final = "goal.params"
_BUDGET: Final = "goal.budget"
_RECORD: Final = "goal.record"
_REFUSAL: Final = "goal.refusal"
_ADMISSION: Final = "goal.admission"
_STATUS: Final = "goal.status body"
_CANCEL: Final = "goal.cancel body"
_ID_PARAMS: Final = "goal id params"

#: The key a record sits under, in a status body and in an admission. Present
#: only when there is a record; see :func:`encode_goal_status`.
_GOAL_FIELD: Final = "goal"

#: The key a refusal sits under in an admission. An admission carries exactly
#: one of the two, which is the admission's own invariant and not restated here.
_REFUSAL_FIELD: Final = "refusal"

#: Whether the admitted record is one the channel already held.
_DUPLICATE_FIELD: Final = "duplicate"

#: The one key of a ``goal.cancel`` answer. ``True`` means an open goal's
#: cancellation was recorded and the channel will apply it; ``False`` means
#: there was nothing open under that id. Both are answers — neither is a
#: failure — so they travel as a value rather than as an error code.
_REQUESTED_FIELD: Final = "requested"

#: The one param of ``goal.status`` and of ``goal.cancel``. Named once, here,
#: because both halves of the link read it from this module rather than
#: spelling it twice.
_GOAL_ID_FIELD: Final = "goal_id"

_EnumT = TypeVar("_EnumT", bound=Enum)


def _optional_enum(
    payload: Mapping[str, Any], field: str, kind: type[_EnumT], *, where: str
) -> _EnumT | None:
    """Read an optional enum member, reusing the strict reader for the value.

    Absent and explicitly null are the same answer — "no value" — and anything
    else present goes through :func:`require_enum`, so an unknown reason code is
    refused here exactly as it would be in a required field. A fallback member
    would be a goal reporting a reason it did not end for.
    """
    if payload.get(field) is None:
        return None
    return require_enum(payload, field, kind, where=where)


def _optional_float(payload: Mapping[str, Any], field: str, *, where: str) -> float | None:
    """Read an optional number, reusing the strict reader for the type check.

    Absent and explicitly null are the same answer. Anything else present is
    checked by :func:`require_float`, so a string in a numeric field is refused
    here exactly as it would be in a required one.
    """
    if payload.get(field) is None:
        return None
    return require_float(payload, field, where=where)


def _require_kind(payload: Mapping[str, Any], *, where: str) -> GoalKind:
    """Read ``kind`` through the channel's own door, and never quote it back.

    :func:`~pz_agent_core.goals.parse_kind` bounds the string before it folds it
    and answers a member or nothing. Nothing is a refusal here — never a
    fallback, and never the string itself in the message: this field is what a
    model or a transcript proposed, so it is the one value on this link that a
    traceback must not carry.

    Raises:
        CodecError: ``kind`` is missing, is not a string, or is not a goal this
            build carries.
    """
    kind = parse_kind(require_str(payload, "kind", where=where))
    if kind is None:
        raise CodecError(
            f"{where}: kind is not one of the {len(GoalKind)} goals this build carries"
        )
    return kind


def _optional_skill(payload: Mapping[str, Any], *, where: str) -> TrainableSkill | None:
    """Read ``skill`` through :func:`~pz_agent_core.goals.parse_skill`, or not at all.

    Same reasoning as :func:`_require_kind`, and the same silence about the
    value. Absent and explicitly null are both "no skill", which is what every
    kind but ``train_skill`` requires and what ``train_skill`` refuses — the
    request type decides that, not this function.

    Raises:
        CodecError: ``skill`` is present and is not a string, or is not a skill
            this build trains.
    """
    raw = payload.get("skill")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CodecError(f"{where}: skill must be a string or null")
    skill = parse_skill(raw)
    if skill is None:
        raise CodecError(
            f"{where}: skill is not one of the {len(TrainableSkill)} skills this build trains"
        )
    return skill


def _decode_evidence_keys(payload: Mapping[str, Any], *, where: str) -> tuple[str, ...]:
    """Read the bounded, value-free record of what was observed.

    The length is checked *before* anything is copied, so a peer that answered
    with a million-element array is refused rather than materialised. The cap is
    the channel's own :data:`~pz_agent_core.goals.MAX_EVIDENCE_KEYS`; the record
    would refuse a longer tuple anyway, and this check is what stops the work
    from being done first.

    Raises:
        CodecError: the field is missing, is not an array, carries more keys
            than a goal keeps, or carries something that is not a string.
    """
    raw = require_sequence(payload, "evidence_keys", where=where)
    if len(raw) > MAX_EVIDENCE_KEYS:
        raise CodecError(
            f"{where}: evidence_keys carries more than the {MAX_EVIDENCE_KEYS} keys a goal keeps"
        )
    keys: list[str] = []
    for position, key in enumerate(raw):
        if not isinstance(key, str):
            # The position is structural — it comes from the array and not from
            # anything the payload said — so naming it leaks nothing.
            raise CodecError(f"{where}: evidence_keys[{position}] must be a string")
        keys.append(key)
    return tuple(keys)


def encode_goal_params(params: GoalParams) -> JsonDict:
    """*params* as an object, with the supplied parameters and no others.

    Sparse on purpose: :meth:`~pz_agent_core.goals.GoalParams.present` is what
    the kind's spec checks against, so "supplied" and "not supplied" must remain
    distinguishable on the wire. ``is not None`` rather than truthiness —
    ``satisfy_to=0.0`` is a parameter the caller chose.
    """
    out: JsonDict = {}
    if params.skill is not None:
        out["skill"] = params.skill.value
    if params.target_level is not None:
        out["target_level"] = params.target_level
    if params.satisfy_to is not None:
        out["satisfy_to"] = params.satisfy_to
    if params.pages is not None:
        out["pages"] = params.pages
    return out


def decode_goal_params(payload: Mapping[str, Any]) -> GoalParams:
    """Read a parameter object back, ranges and all.

    The ranges are :data:`~pz_agent_core.goals.NUMERIC_RANGES`, enforced by
    :class:`~pz_agent_core.goals.GoalParams` itself. This function calls the
    constructor rather than restating them, so the codec cannot drift into
    accepting a level the channel would reject.

    Raises:
        CodecError: a value has the wrong type, names a skill this build does
            not train, or lies outside the range its parameter declares.
    """
    skill = _optional_skill(payload, where=_PARAMS)
    target_level = optional_int(payload, "target_level", where=_PARAMS)
    satisfy_to = _optional_float(payload, "satisfy_to", where=_PARAMS)
    pages = optional_int(payload, "pages", where=_PARAMS)
    try:
        return GoalParams(
            skill=skill,
            target_level=target_level,
            satisfy_to=satisfy_to,
            pages=pages,
        )
    except ValueError:
        # The constructor's message quotes the number it rejected. That number
        # came from the caller, so the chain is dropped rather than repeated.
        raise CodecError(
            f"{_PARAMS}: target_level, satisfy_to or pages lies outside the range "
            f"the channel declares for it"
        ) from None


def encode_goal_budget(budget: GoalBudget) -> JsonDict:
    """*budget* as an object. All three bounds, always.

    None of them is optional, and none of them may be inferred: wall clock alone
    lets a goal whose every step fails hold the channel for its full deadline,
    step count alone never advances for a single step that hangs, and the
    pending time to live is the only thing that ends a goal nobody activated.
    A field lost on the way is one of those three guarantees lost with it.
    """
    return {
        "max_wall_ms": budget.max_wall_ms,
        "max_steps": budget.max_steps,
        "pending_ttl_ms": budget.pending_ttl_ms,
    }


def decode_goal_budget(payload: Mapping[str, Any]) -> GoalBudget:
    """Read a budget back, or refuse it.

    Raises:
        CodecError: a bound is missing, is not an integer, or lies outside the
            range :class:`~pz_agent_core.goals.GoalBudget` enforces.
    """
    max_wall_ms = require_int(payload, "max_wall_ms", where=_BUDGET)
    max_steps = require_int(payload, "max_steps", where=_BUDGET)
    pending_ttl_ms = require_int(payload, "pending_ttl_ms", where=_BUDGET)
    try:
        return GoalBudget(
            max_wall_ms=max_wall_ms,
            max_steps=max_steps,
            pending_ttl_ms=pending_ttl_ms,
        )
    except ValueError:
        raise CodecError(
            f"{_BUDGET}: max_wall_ms, max_steps and pending_ttl_ms must each lie inside "
            f"the bounds the goal channel enforces"
        ) from None


def encode_goal_request(request: GoalRequest) -> JsonDict:
    """The params body for ``goal.submit``.

    ``budget`` is written only when the caller chose one. Its absence is the
    caller saying "use the kind's default", which is a statement the wire
    carries as a missing key and the decoder reads back as ``None``.
    """
    out: JsonDict = {
        "kind": request.kind.value,
        "idempotency_key": request.idempotency_key,
        "params": encode_goal_params(request.params),
    }
    if request.budget is not None:
        out["budget"] = encode_goal_budget(request.budget)
    return out


def decode_goal_request(payload: Mapping[str, Any]) -> GoalRequest:
    """Read a ``goal.submit`` params body.

    ``budget`` decodes to ``None`` when it is absent — not to
    ``GOAL_SPECS[kind].budget``. The two behave identically through
    :attr:`~pz_agent_core.goals.GoalRequest.effective_budget` and are not the
    same request, and resolving the default here would move a decision out of
    the module that owns the table.

    Raises:
        CodecError: a field is missing or ill-typed, ``kind`` is not a goal this
            build carries, ``skill`` is not one it trains, a number is out of
            range, the idempotency key is not shaped like one, or the parameters
            are not the set this kind requires.
    """
    kind = _require_kind(payload, where=_REQUEST)
    idempotency_key = require_str(payload, "idempotency_key", where=_REQUEST)
    params = decode_goal_params(require_mapping(payload, "params", where=_REQUEST))
    budget: GoalBudget | None = None
    if payload.get("budget") is not None:
        budget = decode_goal_budget(require_mapping(payload, "budget", where=_REQUEST))
    try:
        return GoalRequest(
            kind=kind,
            idempotency_key=idempotency_key,
            params=params,
            budget=budget,
        )
    except ValueError:
        # Reported without the offending value. The idempotency key is minted by
        # the caller and this text reaches a traceback and a bug report.
        raise CodecError(
            f"{_REQUEST}: the idempotency key's shape, or the set of parameters this "
            f"kind requires, is not what the goal channel accepts"
        ) from None


def encode_goal_record(record: GoalRecord) -> JsonDict:
    """A goal as the object to put in an RPC result.

    Every field, always. The three that may be absent are written as an explicit
    ``null`` so the object always carries the full field set — a reader that met
    a missing ``reason_code`` and a missing ``state`` would have no way to tell
    "still running" from "the peer dropped two keys".

    The raw idempotency key is not here because it is not on the record: the
    channel keeps only :attr:`~pz_agent_core.goals.GoalRecord.key_digest`, so no
    encoding of a goal can carry the caller's bytes back out.
    """
    return {
        "goal_id": record.goal_id,
        "kind": record.kind.value,
        "params": encode_goal_params(record.params),
        "budget": encode_goal_budget(record.budget),
        "key_digest": record.key_digest,
        "sequence": record.sequence,
        "state": record.state.value,
        "submitted_at_ms": record.submitted_at_ms,
        "started_at_ms": record.started_at_ms,
        "finished_at_ms": record.finished_at_ms,
        "steps_used": record.steps_used,
        "reason_code": None if record.reason_code is None else record.reason_code.value,
        "evidence_keys": list(record.evidence_keys),
        "detail": record.detail,
    }


def decode_goal_record(payload: Mapping[str, Any]) -> GoalRecord:
    """Read a goal back, invariant and all.

    The honesty rule is enforced by constructing the real
    :class:`~pz_agent_core.goals.GoalRecord`: a payload claiming ``succeeded``
    without ``POSTCONDITION_MET`` and without observed evidence keys raises
    rather than decoding, and so does one whose clocks run backwards or whose
    steps exceed the budget it carries. Checking that here would be a second
    copy of the rule, and the copy that drifts is always the one on the side
    that reports success.

    Raises:
        CodecError: a field is missing or ill-typed, ``kind`` is not a goal this
            build carries, ``state`` or ``reason_code`` is not a value it knows,
            the evidence keys are more than a goal keeps, or the record's own
            invariant refuses the combination.
    """
    goal_id = require_str(payload, "goal_id", where=_RECORD)
    kind = _require_kind(payload, where=_RECORD)
    params = decode_goal_params(require_mapping(payload, "params", where=_RECORD))
    budget = decode_goal_budget(require_mapping(payload, "budget", where=_RECORD))
    key_digest = require_str(payload, "key_digest", where=_RECORD)
    sequence = require_int(payload, "sequence", where=_RECORD)
    state = require_enum(payload, "state", GoalState, where=_RECORD)
    submitted_at_ms = require_int(payload, "submitted_at_ms", where=_RECORD)
    started_at_ms = optional_int(payload, "started_at_ms", where=_RECORD)
    finished_at_ms = optional_int(payload, "finished_at_ms", where=_RECORD)
    steps_used = require_int(payload, "steps_used", where=_RECORD)
    reason_code = _optional_enum(payload, "reason_code", ReasonCode, where=_RECORD)
    evidence_keys = _decode_evidence_keys(payload, where=_RECORD)
    detail = require_str(payload, "detail", where=_RECORD)

    try:
        return GoalRecord(
            goal_id=goal_id,
            kind=kind,
            params=params,
            budget=budget,
            key_digest=key_digest,
            sequence=sequence,
            state=state,
            submitted_at_ms=submitted_at_ms,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            steps_used=steps_used,
            reason_code=reason_code,
            evidence_keys=evidence_keys,
            detail=detail,
        )
    except ValueError:
        # The record's message quotes the numbers it rejected; this one does
        # not, and states the whole rule instead so a reader can find which
        # part was broken without the payload being echoed.
        raise CodecError(
            f"{_RECORD}: state, reason_code, evidence_keys, steps_used and the three "
            f"timestamps are not a combination the channel permits — a succeeded goal "
            f"requires POSTCONDITION_MET and observed evidence, a terminal goal must "
            f"say why and when it ended, an open one must carry no reason code, an "
            f"active one must record when it started, no clock may run backwards, and "
            f"steps_used must lie within the budget the goal carries"
        ) from None


def encode_goal_refusal(refusal: GoalRefusal) -> JsonDict:
    """A refusal as the object to put in an RPC result.

    ``message`` is assembled by the channel from constants, numbers and
    identifiers the core minted; it carries no caller-supplied string by
    construction, which is why it can be relayed verbatim.
    """
    return {
        "reason_code": refusal.reason_code.value,
        "message": refusal.message,
        "active_goal_id": refusal.active_goal_id,
    }


def decode_goal_refusal(payload: Mapping[str, Any]) -> GoalRefusal:
    """Read a refusal back, or refuse it.

    ``active_goal_id`` is required rather than defaulted to ``""``. The default
    exists so a caller constructing a refusal in-process may leave it out; it is
    not permission to invent "no goal is active" from a payload that dropped the
    key, which is precisely the answer that stops a user cancelling the goal
    that is blocking theirs.

    Raises:
        CodecError: a field is missing or ill-typed, the reason code is not one
            this build knows, or the refusal says nothing.
    """
    reason_code = require_enum(payload, "reason_code", ReasonCode, where=_REFUSAL)
    message = require_str(payload, "message", where=_REFUSAL)
    active_goal_id = require_str(payload, "active_goal_id", where=_REFUSAL)
    try:
        return GoalRefusal(
            reason_code=reason_code,
            message=message,
            active_goal_id=active_goal_id,
        )
    except ValueError:
        raise CodecError(
            f"{_REFUSAL}: a refusal must say what failed and what to do, and "
            f"POSTCONDITION_MET is not a refusal"
        ) from None


def encode_goal_admission(admission: GoalAdmission) -> JsonDict:
    """The result body for ``goal.submit``.

    Exactly one of ``goal`` and ``refusal`` is written, because exactly one of
    them exists — the admission's own invariant. ``duplicate`` is always
    written: it is the difference between "your goal was admitted" and "you
    already submitted this one", and a caller that could not tell them apart
    would count a retried submission as a second goal.
    """
    out: JsonDict = {_DUPLICATE_FIELD: admission.duplicate}
    if admission.goal is not None:
        out[_GOAL_FIELD] = encode_goal_record(admission.goal)
    if admission.refusal is not None:
        out[_REFUSAL_FIELD] = encode_goal_refusal(admission.refusal)
    return out


def decode_goal_admission(payload: Mapping[str, Any]) -> GoalAdmission:
    """Read a ``goal.submit`` answer back.

    A body carrying both a goal and a refusal, or neither, is refused by
    :class:`~pz_agent_core.goals.GoalAdmission` itself rather than by a check
    written here — the same reason the record's honesty rule is not restated.

    Raises:
        CodecError: a key is present and is not the record it claims to be,
            ``duplicate`` is missing or is not a boolean, or the combination is
            not one an admission may hold.
    """
    goal: GoalRecord | None = None
    if payload.get(_GOAL_FIELD) is not None:
        goal = decode_goal_record(require_mapping(payload, _GOAL_FIELD, where=_ADMISSION))
    refusal: GoalRefusal | None = None
    if payload.get(_REFUSAL_FIELD) is not None:
        refusal = decode_goal_refusal(require_mapping(payload, _REFUSAL_FIELD, where=_ADMISSION))
    duplicate = require_bool(payload, _DUPLICATE_FIELD, where=_ADMISSION)
    try:
        return GoalAdmission(goal=goal, refusal=refusal, duplicate=duplicate)
    except ValueError:
        raise CodecError(
            f"{_ADMISSION}: an admission carries exactly one of a goal or a refusal, "
            f"and only an admitted goal can be a duplicate"
        ) from None


def encode_goal_status(record: GoalRecord | None) -> JsonDict:
    """The result body for ``goal.status``.

    The channel answers ``None`` for an id it has no record of — a goal that
    finished long enough ago to have been evicted from the bounded history.
    That is encoded as a missing key rather than as ``{}``, because an empty
    object decodes as "a record with every field missing", which is an error,
    and the caller would learn "the answer was malformed" when the truth is
    "there is no such goal".
    """
    if record is None:
        return {}
    return {_GOAL_FIELD: encode_goal_record(record)}


def decode_goal_status(payload: Mapping[str, Any]) -> GoalRecord | None:
    """Read that body back, distinguishing "no such goal" from "unreadable".

    Raises:
        CodecError: the key is present but is not a goal record. Deliberately
            not raised when the key is absent: that is the documented "the
            channel has no goal with this id" answer.
    """
    if _GOAL_FIELD not in payload:
        return None
    return decode_goal_record(require_mapping(payload, _GOAL_FIELD, where=_STATUS))


def encode_goal_cancellation(requested: bool) -> JsonDict:
    """The result body for ``goal.cancel``.

    ``True`` means an open goal's cancellation was recorded and the channel will
    apply it on its next tick; ``False`` means nothing open was found under that
    id, which is the ordinary answer for a goal that had already ended. Both are
    answers, and a caller needs to tell them apart: one says "it will stop", the
    other says "it already had".
    """
    return {_REQUESTED_FIELD: requested}


def decode_goal_cancellation(payload: Mapping[str, Any]) -> bool:
    """Read that body back.

    Required rather than defaulted. ``False`` is the safe-looking default and it
    is the wrong one: a peer that dropped the key would be reported as "there
    was nothing to cancel", and a user who was told that about a goal that is
    still running has been told the opposite of the truth.

    Raises:
        CodecError: the field is missing or is not a boolean.
    """
    return require_bool(payload, _REQUESTED_FIELD, where=_CANCEL)


def encode_goal_id(goal_id: str) -> JsonDict:
    """The params body for ``goal.status`` and for ``goal.cancel``.

    One function for both because it is one body. A second spelling in the
    client half is a key that can drift, and the drift arrives on a user's
    machine as a refusal nobody can read.
    """
    return {_GOAL_ID_FIELD: goal_id}


def decode_goal_id(payload: Mapping[str, Any]) -> str:
    """Read that params body, or refuse it.

    Raises:
        CodecError: the id is missing or is not a string. An empty string is
            carried rather than refused — "the channel has no goal with this id"
            is the core's own answer and an empty id gets it, whereas refusing
            here would report an unreadable link instead.
    """
    return require_str(payload, _GOAL_ID_FIELD, where=_ID_PARAMS)
