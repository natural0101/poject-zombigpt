"""Actions across the process boundary: what was asked for, and how it went.

Two records travel here and they are not symmetric in what a mistake costs.

:class:`~pz_agent_core.actions.ActionRequest` goes *out*, from the MCP server to
the sidecar, and a field lost on the way is a command the core executes with a
different meaning than the one the caller asked for — a shorter lease, an empty
argument object, a policy that permits a retry nobody sanctioned. So every one
of its fields is written on every encode and required on every decode. None of
them is declared ``| None``, and a default on the reading side would silently
turn "the peer did not send it" into "the peer sent the default".

:class:`~pz_agent_mcp.ports.ActionRecord` comes *back*, and it carries the
project's honesty rule: ``succeeded`` means a postcondition was observed, which
the record enforces in its own ``__post_init__`` by demanding the terminal
:class:`~pz_agent_core.protocol.ActionResult` with ``POSTCONDITION_MET`` and
non-empty evidence. That check is deliberately *not* re-implemented here.
:func:`decode_action_record` builds the real record and lets the record refuse,
so a remote core cannot report a success it cannot prove merely by being on the
other side of a pipe. A payload that says ``succeeded`` with no result, with a
different reason code, or with empty evidence is a decode failure, not a
success.

Both nested protocol types — :class:`~pz_agent_core.protocol.CommandPolicy` and
``ActionResult`` — already have ``to_dict``/``from_dict``, pinned by
``schemas/command.schema.json`` and ``schemas/action_result.schema.json`` and
exercised by the contract suite. They are reused rather than re-encoded: a
second encoding of the same document is a second definition that drifts in the
direction nobody tests.

Their parse errors are ``ProtocolError``, whose text can quote the value it
rejected. Those are replaced with a :class:`CodecError` naming the field and the
reason and chained through ``from`` — the original stays reachable for a
debugger, and nothing from the payload reaches the message that a bug report
will quote.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pz_agent_core.actions import ActionRequest
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    CommandPolicy,
    JsonDict,
)
from pz_agent_mcp.ports import ActionRecord
from pz_agent_mcp.remote.codec import (
    CodecError,
    require_enum,
    require_float,
    require_int,
    require_mapping,
    require_str,
)

__all__ = [
    "decode_action_record",
    "decode_action_request",
    "decode_action_status",
    "encode_action_record",
    "encode_action_request",
    "encode_action_status",
]

_REQUEST = "action.request"
_RECORD = "action.record"
_STATUS = "action.status"

#: The key the record sits under in an ``action.status`` body. Present only
#: when there is a record; see :func:`encode_action_status`.
_RECORD_FIELD = "record"

#: Policy keys that must be on the wire, taken from what the policy's own
#: ``to_dict`` always emits so that a field added there is required here
#: without this module being edited.
#:
#: ``CommandPolicy.from_dict`` defaults these when absent, which is right on the
#: mod link — the mod may omit the whole object — and wrong on this one. A
#: renamed key would decode as ``allow_interrupt=True`` and ``max_retries=1``,
#: which is to say a peer's *silence* would grant an interrupt and a retry.
#: Presence is checked here; the parsing itself is still the protocol type's.
_POLICY_REQUIRED: Final[frozenset[str]] = frozenset(CommandPolicy().to_dict())


def _optional_float(payload: Mapping[str, Any], field: str, *, where: str) -> float | None:
    """Read an optional number, reusing the strict reader for the type check.

    Absent and explicitly null are the same answer — "no value" — because the
    encoder omits the key and a peer that writes ``null`` means the same thing.
    Anything else present is checked by :func:`require_float`, so a string in a
    numeric field is refused here exactly as it would be in a required one.
    """
    if payload.get(field) is None:
        return None
    return require_float(payload, field, where=where)


def encode_action_request(request: ActionRequest) -> JsonDict:
    """The params body for ``action.submit``.

    Every field, always. ``args``, ``policy`` and ``lease_ms`` have defaults in
    the dataclass, but a default is what the *caller* gets when it says nothing
    — it is not licence for the wire to drop a value the caller did choose.
    """
    return {
        "action": request.action.value,
        "session_id": request.session_id,
        "idempotency_key": request.idempotency_key,
        "args": dict(request.args),
        "policy": request.policy.to_dict(),
        "lease_ms": request.lease_ms,
    }


def decode_action_request(payload: Mapping[str, Any]) -> ActionRequest:
    """Read an ``action.submit`` params body.

    Raises:
        CodecError: a field is missing, has the wrong type, names an action
            this build does not know, or carries a policy or bounds the request
            type itself refuses.
    """
    action = require_enum(payload, "action", ActionName, where=_REQUEST)
    session_id = require_str(payload, "session_id", where=_REQUEST)
    idempotency_key = require_str(payload, "idempotency_key", where=_REQUEST)
    args = dict(require_mapping(payload, "args", where=_REQUEST))
    raw_policy = require_mapping(payload, "policy", where=_REQUEST)
    lease_ms = require_int(payload, "lease_ms", where=_REQUEST)

    missing = sorted(_POLICY_REQUIRED - set(raw_policy))
    if missing:
        raise CodecError(f"{_REQUEST}: policy is missing {', '.join(missing)}")

    try:
        policy = CommandPolicy.from_dict(raw_policy)
    except ValueError as exc:
        raise CodecError(f"{_REQUEST}: policy is not a command policy this build can read") from exc

    try:
        return ActionRequest(
            action=action,
            session_id=session_id,
            idempotency_key=idempotency_key,
            args=args,
            policy=policy,
            lease_ms=lease_ms,
        )
    except ValueError as exc:
        # The request's own bounds: idempotency_key length and lease_ms range.
        # Reported without the offending value — an idempotency key is derived
        # from caller-supplied text and a bug report should not carry it.
        raise CodecError(
            f"{_REQUEST}: idempotency_key length or lease_ms range is outside "
            f"the bounds the request enforces"
        ) from exc


def encode_action_record(record: ActionRecord) -> JsonDict:
    """The result body for ``action.submit``, and the inner body of a status.

    ``progress`` and ``result`` are the record's two optional fields and are
    omitted when absent rather than written as ``null``: absent is what the
    decoder reads as "not reported", and the two spellings should not both need
    testing on every peer.
    """
    out: JsonDict = {
        "action_id": record.action_id,
        "action": record.action.value,
        "status": record.status.value,
        "idempotency_key": record.idempotency_key,
    }
    if record.progress is not None:
        out["progress"] = record.progress
    if record.result is not None:
        out["result"] = record.result.to_dict()
    return out


def decode_action_record(payload: Mapping[str, Any]) -> ActionRecord:
    """Read a record back, invariant and all.

    The honesty rule is enforced by constructing the real
    :class:`~pz_agent_mcp.ports.ActionRecord`: a payload claiming ``succeeded``
    without the terminal result carrying ``POSTCONDITION_MET`` and observed
    evidence raises rather than decoding. Checking it here instead would be a
    second copy of the rule, and the copy that drifts is always the one on the
    side that reports success.

    Raises:
        CodecError: a field is missing or ill-typed, the result cannot be read,
            or the record's own invariant refuses the combination.
    """
    action_id = require_str(payload, "action_id", where=_RECORD)
    action = require_enum(payload, "action", ActionName, where=_RECORD)
    status = require_enum(payload, "status", ActionStatus, where=_RECORD)
    idempotency_key = require_str(payload, "idempotency_key", where=_RECORD)
    progress = _optional_float(payload, "progress", where=_RECORD)

    result: ActionResult | None = None
    if payload.get("result") is not None:
        raw_result = require_mapping(payload, "result", where=_RECORD)
        try:
            result = ActionResult.from_dict(raw_result)
        except (ValueError, TypeError, KeyError) as exc:
            # `from_dict` raises the protocol's own error, whose text can quote
            # a field value; evidence carries game-derived data. Replaced, not
            # chained into the message.
            raise CodecError(f"{_RECORD}: result is not an action result") from exc

    try:
        return ActionRecord(
            action_id=action_id,
            action=action,
            status=status,
            idempotency_key=idempotency_key,
            progress=progress,
            result=result,
        )
    except ValueError as exc:
        raise CodecError(
            f"{_RECORD}: action_id, status, progress and result are not a "
            f"combination the record permits — a succeeded action requires the "
            f"terminal result carrying POSTCONDITION_MET and observed evidence, "
            f"action_id must be present, and progress must lie within 0..1"
        ) from exc


def encode_action_status(record: ActionRecord | None) -> JsonDict:
    """The result body for ``action.status``.

    ``ActionPort.status`` answers ``None`` for an id the core has never seen,
    and ``None`` is not something :func:`encode_action_record` can produce. It
    is encoded as a missing key rather than as ``{}``, because an empty object
    decodes as "a record with every field missing" — an error — and the caller
    would learn "the answer was malformed" when the truth is "there is no such
    action".
    """
    if record is None:
        return {}
    return {_RECORD_FIELD: encode_action_record(record)}


def decode_action_status(payload: Mapping[str, Any]) -> ActionRecord | None:
    """Read that body back, distinguishing "no such action" from "unreadable".

    Raises:
        CodecError: the key is present but is not a record. Deliberately not
            raised when the key is absent: that is the documented "the core has
            no action with this id" answer.
    """
    if _RECORD_FIELD not in payload:
        return None
    return decode_action_record(require_mapping(payload, _RECORD_FIELD, where=_STATUS))
