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

:func:`encode_goal_record` writes every long-standing field of the record on
every encode, including the three that may be absent (``started_at_ms``,
``finished_at_ms``, ``reason_code``), which travel as an explicit ``null``.
Nothing here defaults a field it did not receive: defaulting ``steps_used``
would report a goal that had spent its budget as one that had just started, and
defaulting ``state`` would report a cancelled goal as pending. The decoder
accepts either the null or the missing key for the three optional fields,
because a peer that omits a null is saying the same thing.

The additive tail, and the one place a default is honest
--------------------------------------------------------

Four keys on the record (``suspended_by``, ``suspensions``,
``active_ms_before_suspend``, ``front_rank``) and three on the channel status
(``progress``, ``paused``, ``report``) arrived after the link did. A peer built
before them writes none of them, so they are the one group this module decodes
from an absent key — and the value it decodes to is the *declared default of
the object being built*, never a number, a marker or a phase chosen here.

That is not the defaulting the paragraph above refuses, and the difference is
worth stating rather than trusting to memory. ``steps_used`` absent means "a
peer dropped a field it writes", and there is no honest value for it. These
seven absent mean "there is no such field here", and a build with no preemption
has suspended nothing, ranked nothing to the front and has no deterministic
drive to report a phase from — which is exactly ``suspensions=0``,
``front_rank=0``, ``suspended_by=None`` and three ``None`` tails. Absence
restores what that peer's channel actually held; it never invents state, and it
never manufactures the *positive* claim: a ``None`` progress is "nothing was
reported", which is a different sentence from "the goal is not progressing",
and :mod:`pz_agent_cli.goal_cli` prints it as the first.

The record's four are written *sparsely* for a reason of its own, spelled out
on :func:`encode_goal_record`: the wire schema declares the record's fields
with ``additionalProperties: false`` and this module does not get to grow that
set on the way past. A goal that has never been suspended therefore encodes the
document the schema declares and nothing more, and one that has carries the
bookkeeping — which is sound precisely because absence and default say the same
thing here. The three status tails have no such schema and are written as an
explicit ``null``, the way the rest of that body is.

A present value is read strictly all the same. ``suspensions`` present and not
an integer is refused exactly as ``steps_used`` would be, and the record's own
constructor still decides whether the combination is one a goal may hold — a
marker on a terminal record, or accrued active time with no recorded
suspension, is refused here because it is refused there.

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
    DEFAULT_MAX_REMEMBERED,
    MAX_EVIDENCE_KEYS,
    GoalAdmission,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalRecord,
    GoalRefusal,
    GoalRequest,
    GoalState,
    LootScope,
    TrainableSkill,
    parse_kind,
    parse_scope,
    parse_skill,
)
from pz_agent_core.protocol import JsonDict, ReasonCode
from pz_agent_mcp.ports import (
    MAX_PROGRESS_COUNTERS,
    GoalCancellation,
    GoalChannelStatus,
    GoalProgress,
    PausedGoalRecord,
)
from pz_agent_mcp.remote.codec import (
    CodecError,
    optional_int,
    optional_str,
    require_bool,
    require_enum,
    require_float,
    require_int,
    require_mapping,
    require_sequence,
    require_str,
)

__all__ = [
    "MAX_GOALS_ON_THE_WIRE",
    "decode_goal_admission",
    "decode_goal_budget",
    "decode_goal_cancellation",
    "decode_goal_channel_status",
    "decode_goal_id",
    "decode_goal_params",
    "decode_goal_progress",
    "decode_goal_query",
    "decode_goal_record",
    "decode_goal_refusal",
    "decode_goal_request",
    "decode_paused_goal",
    "encode_goal_admission",
    "encode_goal_budget",
    "encode_goal_cancellation",
    "encode_goal_channel_status",
    "encode_goal_id",
    "encode_goal_params",
    "encode_goal_progress",
    "encode_goal_query",
    "encode_goal_record",
    "encode_goal_refusal",
    "encode_goal_request",
    "encode_paused_goal",
]

_REQUEST: Final = "goal.request"
_PARAMS: Final = "goal.params"
_BUDGET: Final = "goal.budget"
_RECORD: Final = "goal.record"
_REFUSAL: Final = "goal.refusal"
_ADMISSION: Final = "goal.admission"
_CHANNEL: Final = "goal channel status"
_CANCEL: Final = "goal cancellation"
_QUERY_PARAMS: Final = "goal query params"
_ID_PARAMS: Final = "goal id params"

#: How many records one ``goal.status`` answer may carry in its backlog.
#:
#: Taken from :data:`~pz_agent_core.goals.DEFAULT_MAX_REMEMBERED` rather than
#: restated, and from *that* rather than from the smaller open-goal cap:
#: :class:`~pz_agent_core.goals.GoalQueue` refuses to be built unless it
#: remembers at least as many records as it may hold open, so no honest channel
#: can offer a backlog longer than the history behind it. The length is checked
#: before a single record is decoded, so a peer answering with a million-element
#: array costs this process one ``len``.
#:
#: A sidecar configured above the default would be refused here rather than
#: silently truncated. That is the intended direction: losing part of a backlog
#: without saying so is how a user is told a goal is not queued when it is.
MAX_GOALS_ON_THE_WIRE: Final = DEFAULT_MAX_REMEMBERED

#: The key a single record sits under, in an admission and in a cancellation.
_GOAL_FIELD: Final = "goal"

#: The key a refusal sits under in an admission. An admission carries exactly
#: one of the two, which is the admission's own invariant and not restated here.
_REFUSAL_FIELD: Final = "refusal"

#: Whether the admitted record is one the channel already held.
_DUPLICATE_FIELD: Final = "duplicate"

#: Whether a cancel request was accepted against an open goal. ``False`` means
#: the goal had already ended — an answer, not a failure — so it travels as a
#: value rather than as an error code.
_REQUESTED_FIELD: Final = "requested"

#: The three goal-carrying keys of a channel status.
_ACTIVE_FIELD: Final = "active"
_PENDING_FIELD: Final = "pending"
_NAMED_FIELD: Final = "named"

#: Its three additive tails, each of which decodes to the dataclass's own
#: ``None`` when a peer does not carry it. They describe the goal the answer is
#: *about* rather than the channel, which is why they sit beside the records
#: instead of inside them: ``progress`` and ``report`` come from a
#: deterministic drive and its ledger, and ``paused`` from the loop's takeover
#: marker — three things a :class:`~pz_agent_core.goals.GoalRecord` has no
#: field for and must not grow one for, since the queue does not own them.
_PROGRESS_FIELD: Final = "progress"
_PAUSED_FIELD: Final = "paused"
_REPORT_FIELD: Final = "report"

#: Where a progress answer and a takeover marker are refused from.
_PROGRESS: Final = "goal progress"
_PAUSED: Final = "goal paused marker"

#: The one param of ``goal.status`` and of ``goal.cancel``. Named once, here,
#: because both halves of the link read it from this module rather than
#: spelling it twice — and because the two bodies differ only in whether the id
#: may be null, which is a distinction worth having in one place.
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


def _optional_bool(payload: Mapping[str, Any], field: str, *, where: str) -> bool | None:
    """Read an optional boolean, reusing the strict reader for the type check.

    Absent and explicitly null are the same answer — "the caller did not
    choose" — which for ``take_all`` is a different statement from ``False``
    ("the caller chose the useful-only default in as many words"), so neither
    may collapse into the other.
    """
    if payload.get(field) is None:
        return None
    return require_bool(payload, field, where=where)


def _defaulted_int(payload: Mapping[str, Any], field: str, *, default: int, where: str) -> int:
    """Read an additive integer, or restore the default the record declares.

    Absent and explicitly null are the same answer — "this peer has no such
    field" — and the answer is *default*, which the caller takes from the
    dataclass rather than choosing. Anything else present goes through
    :func:`require_int`, so a string in a numeric field is refused here exactly
    as it would be in a required one.

    The one use is the suspension bookkeeping the module docstring sets apart:
    a peer that predates preemption has suspended nothing, and zero is what its
    channel held. This is not licence to default a field the peer does write.
    """
    if payload.get(field) is None:
        return default
    return require_int(payload, field, where=where)


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


def _optional_scope(payload: Mapping[str, Any], *, where: str) -> LootScope | None:
    """Read ``scope`` through :func:`~pz_agent_core.goals.parse_scope`, or not at all.

    Same reasoning as :func:`_optional_skill`, and the same silence about the
    value: the scope decides how far the character may wander unattended, it is
    proposed by the caller, and an unknown token is refused without being
    echoed — never approximated to a scope the caller did not name.

    Raises:
        CodecError: ``scope`` is present and is not a string, or is not a
            scope this build loots by.
    """
    raw = payload.get("scope")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CodecError(f"{where}: scope must be a string or null")
    scope = parse_scope(raw)
    if scope is None:
        raise CodecError(
            f"{where}: scope is not one of the {len(LootScope)} loot scopes this build carries"
        )
    return scope


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
    if params.target_x is not None:
        out["target_x"] = params.target_x
    if params.target_y is not None:
        out["target_y"] = params.target_y
    if params.target_z is not None:
        out["target_z"] = params.target_z
    if params.scope is not None:
        out["scope"] = params.scope.value
    if params.radius is not None:
        out["radius"] = params.radius
    # ``is not None``, not truthiness, for the same reason as ``satisfy_to``
    # above: ``take_all=False`` is the caller choosing the useful-only default
    # in as many words, and dropping it would erase the choice.
    if params.take_all is not None:
        out["take_all"] = params.take_all
    if params.categories is not None:
        out["categories"] = params.categories
    # The care parameters. Carried since the wave that gave ``rest_until`` and
    # ``sleep_until_rested`` a terminal: the first is *required* by its kind, so
    # an encoder that dropped it produced a request the decoder could only
    # refuse, and the second is the difference between the night the caller
    # asked for and the adapter's own default.
    if params.target_endurance is not None:
        out["target_endurance"] = params.target_endurance
    if params.hours is not None:
        out["hours"] = params.hours
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
    target_x = optional_int(payload, "target_x", where=_PARAMS)
    target_y = optional_int(payload, "target_y", where=_PARAMS)
    target_z = optional_int(payload, "target_z", where=_PARAMS)
    scope = _optional_scope(payload, where=_PARAMS)
    radius = optional_int(payload, "radius", where=_PARAMS)
    take_all = _optional_bool(payload, "take_all", where=_PARAMS)
    categories = optional_str(payload, "categories", where=_PARAMS)
    target_endurance = _optional_float(payload, "target_endurance", where=_PARAMS)
    hours = optional_int(payload, "hours", where=_PARAMS)
    try:
        return GoalParams(
            skill=skill,
            target_level=target_level,
            satisfy_to=satisfy_to,
            pages=pages,
            target_x=target_x,
            target_y=target_y,
            target_z=target_z,
            scope=scope,
            radius=radius,
            take_all=take_all,
            categories=categories,
            target_endurance=target_endurance,
            hours=hours,
        )
    except ValueError:
        # The constructor's message quotes the number it rejected, and for
        # `categories` its input is the caller's own string. Both came from the
        # caller, so the chain is dropped rather than repeated.
        raise CodecError(
            f"{_PARAMS}: a numeric parameter lies outside the range the channel declares "
            f"for it, or categories is not a comma-joined list of distinct loot "
            f"category tokens within the channel's length bound"
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

    Every field the record has always had, always. The three that may be absent
    are written as an explicit ``null`` so the object carries the whole of that
    field set — a reader that met a missing ``reason_code`` and a missing
    ``state`` would have no way to tell "still running" from "the peer dropped
    two keys".

    The suspension bookkeeping is the exception, and the reason is not style:
    ``schemas/goal.schema.json`` declares the record's fields with
    ``additionalProperties: false``, growing it is a protocol change with its
    own version bump and sync test, and this module does not get to make one on
    the way past. So the four keys are written only when the goal has
    bookkeeping to report — a goal that has never stepped aside encodes exactly
    the document the schema declares, byte for byte, and a goal that has says
    who for and how often. Their absence and their default are the same
    statement either way, which is what makes the sparse form honest:
    :func:`decode_goal_record` restores the record's own defaults from a missing
    key, and "nothing has been suspended" is what those defaults say.

    The raw idempotency key is not here because it is not on the record: the
    channel keeps only :attr:`~pz_agent_core.goals.GoalRecord.key_digest`, so no
    encoding of a goal can carry the caller's bytes back out.
    """
    out: JsonDict = {
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
    if record.suspended_by is not None:
        out["suspended_by"] = record.suspended_by
    if record.suspensions:
        out["suspensions"] = record.suspensions
    if record.active_ms_before_suspend:
        out["active_ms_before_suspend"] = record.active_ms_before_suspend
    if record.front_rank:
        out["front_rank"] = record.front_rank
    return out


def decode_goal_record(payload: Mapping[str, Any]) -> GoalRecord:
    """Read a goal back, invariant and all.

    The honesty rule is enforced by constructing the real
    :class:`~pz_agent_core.goals.GoalRecord`: a payload claiming ``succeeded``
    without ``POSTCONDITION_MET`` and without observed evidence keys raises
    rather than decoding, and so does one whose clocks run backwards or whose
    steps exceed the budget it carries. Checking that here would be a second
    copy of the rule, and the copy that drifts is always the one on the side
    that reports success.

    The suspension bookkeeping is the exception to "missing means missing", for
    the reason the module docstring gives: a peer built before preemption never
    writes those four keys, and their absence restores the record's own
    defaults — which is what that peer's channel held, not a state invented
    here. Present, they are read as strictly as everything else.

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
    suspended_by = optional_str(payload, "suspended_by", where=_RECORD)
    suspensions = _defaulted_int(payload, "suspensions", default=0, where=_RECORD)
    active_ms_before_suspend = _defaulted_int(
        payload, "active_ms_before_suspend", default=0, where=_RECORD
    )
    front_rank = _defaulted_int(payload, "front_rank", default=0, where=_RECORD)

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
            suspended_by=suspended_by,
            suspensions=suspensions,
            active_ms_before_suspend=active_ms_before_suspend,
            front_rank=front_rank,
        )
    except ValueError:
        # The record's message quotes the numbers it rejected; this one does
        # not, and states the whole rule instead so a reader can find which
        # part was broken without the payload being echoed.
        raise CodecError(
            f"{_RECORD}: state, reason_code, evidence_keys, steps_used, the three "
            f"timestamps and the suspension bookkeeping are not a combination the "
            f"channel permits — a succeeded goal requires POSTCONDITION_MET and "
            f"observed evidence, a terminal goal must say why and when it ended, an "
            f"open one must carry no reason code, an active one must record when it "
            f"started, no clock may run backwards, steps_used must lie within the "
            f"budget the goal carries, only a pending goal may carry a suspension "
            f"marker, a marker or accrued active time requires a recorded suspension, "
            f"and front_rank is zero or negative because the queue mints it"
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


def _optional_record(payload: Mapping[str, Any], field: str, *, where: str) -> GoalRecord | None:
    """Read an optional nested record. Absent and null are the same answer."""
    if payload.get(field) is None:
        return None
    return decode_goal_record(require_mapping(payload, field, where=where))


def encode_goal_progress(progress: GoalProgress) -> JsonDict:
    """One deterministic drive's phase and counters.

    Both fields, always: an empty ``counters`` is a drive that counts nothing,
    which is a different answer from a drive that was not asked. Neither is
    re-checked on the way out — :class:`~pz_agent_mcp.ports.GoalProgress`
    refuses a phase or a counter name that is not a closed token at
    construction, and a second copy of that rule here is a second thing to
    drift.
    """
    return {"phase": progress.phase, "counters": dict(progress.counters)}


def decode_goal_progress(payload: Mapping[str, Any]) -> GoalProgress:
    """Read a progress answer back, closed tokens and all.

    The token rule and the counter cap belong to
    :class:`~pz_agent_mcp.ports.GoalProgress` and are enforced by constructing
    it. The cap is *also* checked here, before the map is copied, for the reason
    :func:`_decode_evidence_keys` checks its own: a peer answering with ten
    thousand counters would otherwise be materialised in full and then refused.

    Raises:
        CodecError: ``phase`` or ``counters`` is missing or ill-typed, there are
            more counters than a progress answer carries, or the record refuses
            the phase or a counter. The message names the constraint and not the
            offending token — a phase is minted by a drive, but this string is
            written before any redactor sees it, like every other in the file.
    """
    phase = require_str(payload, "phase", where=_PROGRESS)
    raw = require_mapping(payload, "counters", where=_PROGRESS)
    if len(raw) > MAX_PROGRESS_COUNTERS:
        raise CodecError(
            f"{_PROGRESS}: counters carries more than the {MAX_PROGRESS_COUNTERS} a "
            f"progress answer holds"
        )
    try:
        return GoalProgress(phase=phase, counters=dict(raw))
    except ValueError:
        raise CodecError(
            f"{_PROGRESS}: phase and every counter name must be a short lower-case "
            f"token, and every counter must be a non-negative whole number"
        ) from None


def encode_paused_goal(paused: PausedGoalRecord) -> JsonDict:
    """The loop's takeover marker, as an object.

    ``reason`` travels as the sentence it is. It is the loop's own text rather
    than the game's, and quarantining it is
    :class:`~pz_agent_mcp.router.ToolRouter`'s job on the way to a client — this
    link carries it to a process that has the same rule, and scrubbing it twice
    in two places is how the two come to scrub it differently.
    """
    return {
        "goal_id": paused.goal_id,
        "kind": paused.kind,
        "reason": paused.reason,
        "paused_at_ms": paused.paused_at_ms,
    }


def decode_paused_goal(payload: Mapping[str, Any]) -> PausedGoalRecord:
    """Read a takeover marker back.

    ``kind`` goes through :func:`~pz_agent_core.goals.parse_kind` although the
    marker's own field is a plain string: it *is* a goal kind, and this link has
    one string-to-enum door rather than one per field that happens to hold a
    kind. What is stored is the member's own value, so a marker cannot name a
    goal by a spelling the channel does not use, and an unknown token is refused
    without being echoed — the same way the record beside it would be.

    Raises:
        CodecError: a field is missing or ill-typed, ``kind`` is not a goal this
            build carries, or the marker names no goal.
    """
    goal_id = require_str(payload, "goal_id", where=_PAUSED)
    kind = _require_kind(payload, where=_PAUSED)
    reason = require_str(payload, "reason", where=_PAUSED)
    paused_at_ms = require_int(payload, "paused_at_ms", where=_PAUSED)
    try:
        return PausedGoalRecord(
            goal_id=goal_id, kind=kind.value, reason=reason, paused_at_ms=paused_at_ms
        )
    except ValueError:
        raise CodecError(
            f"{_PAUSED}: a paused marker must name the goal it parked, carry that "
            f"goal's kind, and record a non-negative time it was parked at"
        ) from None


def encode_goal_channel_status(status: GoalChannelStatus) -> JsonDict:
    """The result body for ``goal.status``.

    Every field, always. ``active``, ``named``, ``progress``, ``paused`` and
    ``report`` are written as an explicit ``null`` when there is no such thing,
    and ``pending`` as an empty array when the backlog is empty — an empty
    backlog is a fact about the channel, and a missing key would decode as a
    body somebody truncated.

    ``pending`` keeps the channel's order, oldest first by admission sequence.
    A codec that sorted it would silently repair a peer that had it wrong, and
    the repair would be indistinguishable from the peer being right.

    ``report`` is the loot or explore mission's ledger document, carried as the
    object it is. This module does not walk it: it is assembled by the mission
    that owns it, its shape is that mission's to declare, and a codec that
    restated it would be a second declaration to keep in step. What bounds it is
    the transport's own response cap, which is checked on the bytes before
    anything is parsed.
    """
    return {
        _ACTIVE_FIELD: None if status.active is None else encode_goal_record(status.active),
        _PENDING_FIELD: [encode_goal_record(record) for record in status.pending],
        _NAMED_FIELD: None if status.named is None else encode_goal_record(status.named),
        _PROGRESS_FIELD: None if status.progress is None else encode_goal_progress(status.progress),
        _PAUSED_FIELD: None if status.paused is None else encode_paused_goal(status.paused),
        _REPORT_FIELD: None if status.report is None else dict(status.report),
    }


def decode_goal_channel_status(payload: Mapping[str, Any]) -> GoalChannelStatus:
    """Read a channel status back, bounded backlog and all.

    ``named`` stays ``None`` when the peer sent none, and this function does not
    try to work out whether that means "no id was asked about" or "the id names
    nothing". It cannot: only the caller knows whether it sent an id. The
    distinction is kept by :class:`~pz_agent_mcp.router.ToolRouter`, which does.

    The three tails decode to the dataclass's own ``None`` when the peer did not
    carry them, which is the honest nothing
    :class:`~pz_agent_mcp.ports.GoalChannelStatus` documents for a port that
    cannot answer them — never to a phase, a marker or a ledger this side made
    up. A caller that must not report "not paused" from that ``None`` says
    "unreported" instead; that reading belongs to the caller, and this function
    only refuses to decide it here.

    Raises:
        CodecError: a field is missing or ill-typed, the backlog is longer than
            :data:`MAX_GOALS_ON_THE_WIRE`, one of the records is not one, or a
            tail is present and is not the object it claims to be.
    """
    active = _optional_record(payload, _ACTIVE_FIELD, where=_CHANNEL)
    named = _optional_record(payload, _NAMED_FIELD, where=_CHANNEL)

    progress: GoalProgress | None = None
    if payload.get(_PROGRESS_FIELD) is not None:
        progress = decode_goal_progress(require_mapping(payload, _PROGRESS_FIELD, where=_CHANNEL))
    paused: PausedGoalRecord | None = None
    if payload.get(_PAUSED_FIELD) is not None:
        paused = decode_paused_goal(require_mapping(payload, _PAUSED_FIELD, where=_CHANNEL))
    report: JsonDict | None = None
    if payload.get(_REPORT_FIELD) is not None:
        report = dict(require_mapping(payload, _REPORT_FIELD, where=_CHANNEL))

    raw_pending = require_sequence(payload, _PENDING_FIELD, where=_CHANNEL)
    if len(raw_pending) > MAX_GOALS_ON_THE_WIRE:
        raise CodecError(
            f"{_CHANNEL}: pending carries more than the {MAX_GOALS_ON_THE_WIRE} goals "
            f"a channel remembers"
        )
    pending: list[GoalRecord] = []
    for position, raw_record in enumerate(raw_pending):
        # The position is structural — it comes from the array, not from
        # anything the payload said — so naming it leaks nothing.
        where = f"{_CHANNEL}: pending[{position}]"
        if not isinstance(raw_record, Mapping):
            raise CodecError(f"{where} must be an object")
        pending.append(decode_goal_record(raw_record))

    return GoalChannelStatus(
        active=active,
        pending=tuple(pending),
        named=named,
        progress=progress,
        paused=paused,
        report=report,
    )


def encode_goal_cancellation(cancellation: GoalCancellation) -> JsonDict:
    """The result body for ``goal.cancel``.

    ``requested`` is not "cancelled": the channel applies a cancellation on its
    next tick, so an accepted request is routinely reported against a goal that
    is still running. The field keeps the channel's own word for it, and the
    goal's actual state travels inside the record.
    """
    return {
        _REQUESTED_FIELD: cancellation.requested,
        _GOAL_FIELD: None if cancellation.goal is None else encode_goal_record(cancellation.goal),
    }


def decode_goal_cancellation(payload: Mapping[str, Any]) -> GoalCancellation:
    """Read a cancellation back, invariant and all.

    ``requested`` is required rather than defaulted. ``False`` is the
    safe-looking default and it is the wrong one: a peer that dropped the key
    would be reported as "there was nothing to cancel", and a user told that
    about a goal that is still running has been told the opposite of the truth.

    The pairing rule — an accepted request must name the goal it was accepted
    for — is :class:`~pz_agent_mcp.ports.GoalCancellation`'s own, and this
    function calls the constructor rather than restating it.

    Raises:
        CodecError: ``requested`` is missing or is not a boolean, ``goal`` is
            present and is not a record, or the pair is one a cancellation
            refuses to hold.
    """
    requested = require_bool(payload, _REQUESTED_FIELD, where=_CANCEL)
    goal = _optional_record(payload, _GOAL_FIELD, where=_CANCEL)
    try:
        return GoalCancellation(goal=goal, requested=requested)
    except ValueError:
        raise CodecError(
            f"{_CANCEL}: a cancellation that was accepted must name the goal it was accepted for"
        ) from None


def encode_goal_query(goal_id: str | None) -> JsonDict:
    """The params body for ``goal.status``.

    The id is optional — asking with none is asking about the channel rather
    than about a goal — and it is written as an explicit ``null`` so the body
    always carries the field. A peer that omits it is saying the same thing and
    the decoder accepts either.
    """
    return {_GOAL_ID_FIELD: goal_id}


def decode_goal_query(payload: Mapping[str, Any]) -> str | None:
    """Read a ``goal.status`` params body, or refuse it.

    Raises:
        CodecError: the id is present and is not a string. Absent and ``null``
            are the documented "tell me about the channel" request, not an
            error.
    """
    return optional_str(payload, _GOAL_ID_FIELD, where=_QUERY_PARAMS)


def encode_goal_id(goal_id: str) -> JsonDict:
    """The params body for ``goal.cancel``, whose id is not optional."""
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
