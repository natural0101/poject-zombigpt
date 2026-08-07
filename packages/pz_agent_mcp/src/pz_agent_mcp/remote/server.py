"""The server half of the Local Core RPC link: one name, one port call.

The sidecar owns the session, the observation store, the action engine, the
capability report and the memory. The MCP executable owns none of it — an MCP
client launches it as a subprocess, so the two are separate processes by the
protocol's design. :class:`CoreRouter` is what stands in front of the real
:class:`~pz_agent_mcp.ports.CoreServices` on the owning side: it takes a decoded
:class:`~pz_agent_core.rpc.RpcRequest`, calls exactly one port method, and hands
back an :class:`~pz_agent_core.rpc.RpcResponse`. It is a
:data:`~pz_agent_core.rpc.transport.Handler`, so it is passed to
:class:`~pz_agent_core.rpc.transport.RpcServer` as-is.

The router decides nothing
--------------------------

Not "decides little" — nothing. There is no capability check here, no arming
check, no idempotency cache, no clamp on a limit, no policy of any kind. Every
one of those already exists in the core or in :mod:`pz_agent_mcp.router`, and a
copy on this side would be a second implementation of a safety rule that drifts
out of step with the first. The drift would not look like a crash: it would look
like a tool that is published when the core would have withheld it, or a submit
that the core refused and this layer allowed through a stale cached answer.

So the module is written to make such a rule hard to add by accident. It
contains no comparison, no boolean operator, no ``if``, and no loop: dispatch is
a dictionary lookup, each handler is a straight line from decoded parameters to
an encoded answer, and ``tests/unit/test_remote_router.py`` reads this file's
syntax tree and fails if any of those appear. A policy check cannot be written
without one of them.

Nor does it scrub. Everything the game wrote — an item name, a log line, a
memory label — travels this link verbatim and is quarantined by
:mod:`pz_agent_mcp.scrub` and :mod:`pz_agent_core.observation.compact` at the
boundary that faces the tool caller, which is the process on the *other* end of
this pipe. Both ends of this link are ours, on one machine, over a named pipe or
a Unix socket with no address to reach from elsewhere, authenticated per run by
a token. Scrubbing here would mean scrubbing twice — once on text a redactor has
already seen — and neither half could then say what the game actually said. It
is the same reasoning that keeps the raw ``save_id`` on the wire and the digest
in :mod:`pz_agent_mcp.router`; see :mod:`pz_agent_mcp.remote.codec.session`.

Three failures, told apart on purpose
-------------------------------------

===================  ==========================================================
``UNKNOWN_METHOD``   No such name in :data:`~pz_agent_mcp.remote.methods.ALL_METHODS`.
                     The peer is a different build; nothing was called.
``MALFORMED``        The name was known and its parameters could not be read.
                     The caller's bug; nothing was called.
``CORE_REFUSED``     A port raised. The core's own decision, carried with its
                     message.
===================  ==========================================================

A client that could not tell them apart could not act: ``UNKNOWN_METHOD`` means
reinstall, ``MALFORMED`` means fix the call, and ``CORE_REFUSED`` means read
what the core said — most often "make a backup first" or "the game is not
running". So the phases are separated in :meth:`CoreRouter.__call__` rather than
wrapped in one ``try``: a decode never reaches a port, and a port call is never
reported as a caller's mistake.

Two things the router never does with an exception: raise it, and quote it
blindly. Raising would leave the transport to answer — it does, with
``CORE_REFUSED``, which would mislabel a decode failure. And only a
:class:`~pz_agent_mcp.remote.codec.CodecError` has a message this module will
repeat, because that class is written to name the field and never the value;
any other exception from a reader may quote what it read, so it is reported by
method and phase alone.

Two parameter bodies live here
------------------------------

``session.arm`` and ``action.status`` take parameters that are not any record in
:mod:`pz_agent_mcp.ports`: the first is the two arguments of
:meth:`~pz_agent_mcp.ports.SessionPort.arm`, the second is one action id. They
have no codec module, so their JSON shape is defined here — once, as an
``encode``/``decode`` pair the client half imports rather than spelling a second
time. That is the same rule the codec package states for records, applied to the
two bodies that are not records: a key written in both halves is a key that can
drift, and the drift arrives on a user's machine as a refusal nobody can read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Generic, TypeVar

from pz_agent_core.actions import ActionRequest
from pz_agent_core.protocol import JsonDict, SessionMode
from pz_agent_core.rpc import ErrorCode, RpcRequest, RpcResponse
from pz_agent_mcp.ports import CoreServices, PlanRequest
from pz_agent_mcp.remote.codec import CodecError, require_bool, require_enum, require_str
from pz_agent_mcp.remote.codec.actions import (
    decode_action_request,
    encode_action_record,
    encode_action_status,
)
from pz_agent_mcp.remote.codec.capabilities import encode_capability_report
from pz_agent_mcp.remote.codec.diagnostics import (
    TailQuery,
    decode_tail_query,
    encode_doctor_checks,
    encode_log_records,
)
from pz_agent_mcp.remote.codec.memory import (
    MemoryQuery,
    decode_memory_query,
    encode_memory_records,
)
from pz_agent_mcp.remote.codec.observations import encode_latest_observation
from pz_agent_mcp.remote.codec.plans import (
    decode_plan_request,
    encode_plan_current,
    encode_plan_record,
)
from pz_agent_mcp.remote.codec.session import encode_session_snapshot, encode_stop_report
from pz_agent_mcp.remote.methods import Method

__all__ = [
    "ROUTED_METHODS",
    "ArmParams",
    "CoreRouter",
    "decode_action_status_params",
    "decode_arm_params",
    "encode_action_status_params",
    "encode_arm_params",
]

_ARM: Final = "session.arm params"
_ACTION_STATUS: Final = "action.status params"

#: The key an action id travels under. Named once so both halves read it from
#: here; see this module's docstring.
_ACTION_ID_FIELD: Final = "action_id"


@dataclass(frozen=True, slots=True)
class ArmParams:
    """The two arguments of :meth:`~pz_agent_mcp.ports.SessionPort.arm`.

    Neither field has a default. ``mode`` is the authority the session is being
    moved to and ``confirm_backup`` is the user's statement that their save is
    backed up; a default for either would be this layer answering a question it
    was not asked — and the safe-looking default for ``confirm_backup`` is the
    one that arms a session on a promise nobody made.
    """

    mode: SessionMode
    confirm_backup: bool


def encode_arm_params(params: ArmParams) -> JsonDict:
    """The params body for ``session.arm``.

    ``mode`` travels as its ``.value``, matching every other enum on this link:
    an ordinal changes when a member is inserted and a name changes when it is
    renamed, while the value is what the protocol already pins.
    """
    return {"mode": params.mode.value, "confirm_backup": params.confirm_backup}


def decode_arm_params(payload: Mapping[str, Any]) -> ArmParams:
    """Read a ``session.arm`` params body, or refuse it.

    Raises:
        CodecError: ``mode`` is missing or names a mode this build does not
            know, or ``confirm_backup`` is missing or is not a boolean. Neither
            is defaulted: a peer that sent no ``confirm_backup`` has not
            confirmed a backup, and reading its silence as ``False`` would be
            just as wrong as reading it as ``True`` — the first invents a
            refusal, the second arms on a promise nobody made.
    """
    return ArmParams(
        mode=require_enum(payload, "mode", SessionMode, where=_ARM),
        confirm_backup=require_bool(payload, "confirm_backup", where=_ARM),
    )


def encode_action_status_params(action_id: str) -> JsonDict:
    """The params body for ``action.status``."""
    return {_ACTION_ID_FIELD: action_id}


def decode_action_status_params(payload: Mapping[str, Any]) -> str:
    """Read an ``action.status`` params body, or refuse it.

    Raises:
        CodecError: the id is missing or is not a string. An empty string is
            carried rather than refused — "the core has no action with this id"
            is the port's own answer and an empty id gets it, whereas refusing
            here would report an unreadable link instead.
    """
    return require_str(payload, _ACTION_ID_FIELD, where=_ACTION_STATUS)


def _no_params(_payload: Mapping[str, Any]) -> None:
    """The decoder for a method that takes none.

    Extra keys are ignored rather than refused, matching every codec in this
    package: a peer from a later minor version may send a field this build has
    no use for, and refusing it would turn an additive protocol change into an
    unreadable link.
    """
    return None


def _session_status(services: CoreServices, _params: None) -> JsonDict:
    return encode_session_snapshot(services.session.status())


def _session_arm(services: CoreServices, params: ArmParams) -> JsonDict:
    return encode_session_snapshot(
        services.session.arm(params.mode, confirm_backup=params.confirm_backup)
    )


def _session_disarm(services: CoreServices, _params: None) -> JsonDict:
    return encode_session_snapshot(services.session.disarm())


def _session_stop(services: CoreServices, _params: None) -> JsonDict:
    return encode_stop_report(services.session.stop())


def _observation_latest(services: CoreServices, _params: None) -> JsonDict:
    return encode_latest_observation(services.observations.latest())


def _capabilities_report(services: CoreServices, _params: None) -> JsonDict:
    return encode_capability_report(services.capabilities.report())


def _action_submit(services: CoreServices, params: ActionRequest) -> JsonDict:
    """Submit and answer with the record the port returned.

    ``ActionPort.submit`` returns as soon as the action has an id and does not
    wait for the postcondition, which is what keeps a long action from holding
    this connection — and the stop lever needs a connection exactly then.
    """
    return encode_action_record(services.actions.submit(params))


def _action_status(services: CoreServices, params: str) -> JsonDict:
    return encode_action_status(services.actions.status(params))


def _plan_execute(services: CoreServices, params: PlanRequest) -> JsonDict:
    return encode_plan_record(services.plans.execute(params))


def _plan_current(services: CoreServices, _params: None) -> JsonDict:
    return encode_plan_current(services.plans.current())


def _memory_query(services: CoreServices, params: MemoryQuery) -> JsonDict:
    """Answer a memory query with the limit the caller sent.

    The limit is passed through, not clamped. ``MAX_MEMORY_RESULTS`` is the
    ceiling and :mod:`pz_agent_mcp.router` applies it before the call is ever
    made; a second ceiling here would be a second policy, and the two would
    disagree the moment one of them moved.
    """
    return encode_memory_records(services.memory.query(kinds=params.kinds, limit=params.limit))


def _diagnostics_doctor(services: CoreServices, _params: None) -> JsonDict:
    return encode_doctor_checks(services.diagnostics.doctor())


def _diagnostics_tail(services: CoreServices, params: TailQuery) -> JsonDict:
    """Answer a tail with the filters the caller sent, and no others.

    Like the memory limit, ``limit`` is carried rather than clamped, and a
    ``None`` filter is passed as ``None`` rather than dropped: the port reads
    that as "do not filter on this", and turning it into an empty string would
    ask for the records whose level is ``""``.
    """
    return encode_log_records(
        services.diagnostics.tail(
            limit=params.limit,
            level=params.level,
            component=params.component,
            action_id=params.action_id,
        )
    )


_ParamsT = TypeVar("_ParamsT")


@dataclass(frozen=True, slots=True)
class _Route(Generic[_ParamsT]):
    """One method, in its two phases.

    Split rather than written as a single function so the router can tell a
    decode failure from a port's refusal without inspecting an exception's
    type: whatever ``decode`` raises, the core was not called, and whatever
    ``invoke`` raises, it was.
    """

    decode: Callable[[Mapping[str, Any]], _ParamsT]
    invoke: Callable[[CoreServices, _ParamsT], JsonDict]


#: The whole dispatch table. Keys are the constants from
#: :mod:`pz_agent_mcp.remote.methods` and never string literals — a name spelled
#: twice is a name that can drift, and the drift reaches a user as
#: ``UNKNOWN_METHOD`` rather than reaching CI as a failure.
_ROUTES: Final[Mapping[str, _Route[Any]]] = {
    Method.SESSION_STATUS: _Route(_no_params, _session_status),
    Method.SESSION_ARM: _Route(decode_arm_params, _session_arm),
    Method.SESSION_DISARM: _Route(_no_params, _session_disarm),
    Method.SESSION_STOP: _Route(_no_params, _session_stop),
    Method.OBSERVATION_LATEST: _Route(_no_params, _observation_latest),
    Method.CAPABILITIES_REPORT: _Route(_no_params, _capabilities_report),
    Method.ACTION_SUBMIT: _Route(decode_action_request, _action_submit),
    Method.ACTION_STATUS: _Route(decode_action_status_params, _action_status),
    Method.PLAN_EXECUTE: _Route(decode_plan_request, _plan_execute),
    Method.PLAN_CURRENT: _Route(_no_params, _plan_current),
    Method.MEMORY_QUERY: _Route(decode_memory_query, _memory_query),
    Method.DIAGNOSTICS_DOCTOR: _Route(_no_params, _diagnostics_doctor),
    Method.DIAGNOSTICS_TAIL: _Route(decode_tail_query, _diagnostics_tail),
}

#: Exactly the names this router answers, derived from the table rather than
#: restated. ``tests/unit/test_remote_router.py`` asserts it *equals*
#: :data:`~pz_agent_mcp.remote.methods.ALL_METHODS`: a declared name that is not
#: routed is a call a client makes and nothing answers, and a routed name that
#: is not declared is a capability nobody reviewed.
ROUTED_METHODS: Final[frozenset[str]] = frozenset(_ROUTES)


def _refused(request: RpcRequest, code: str, message: str) -> RpcResponse:
    """A failed answer for *request*.

    ``ok`` is written explicitly rather than left to be inferred from the
    presence of an error, which is the envelope's own rule: a reader that took
    an absent error for success would turn a dropped field into a success.
    """
    return RpcResponse(id=request.id, ok=False, error_code=code, error_message=message)


class CoreRouter:
    """Answers one :class:`~pz_agent_core.rpc.RpcRequest` against real ports.

    Constructed with the :class:`~pz_agent_mcp.ports.CoreServices` the sidecar
    owns and called with a request, so an instance is exactly the
    :data:`~pz_agent_core.rpc.transport.Handler` that
    :class:`~pz_agent_core.rpc.transport.RpcServer` wants::

        server = RpcServer(address, authkey=token, handler=CoreRouter(services))

    It holds no state of its own — no cache, no counter, no last-seen id. Two
    calls with the same parameters reach the ports twice, because deciding that
    they should not is the idempotency rule and the core owns it.
    """

    __slots__ = ("_services",)

    def __init__(self, services: CoreServices) -> None:
        self._services = services

    def __call__(self, request: RpcRequest) -> RpcResponse:
        """Answer *request*. Never raises, whatever the ports do.

        A raised exception would leave the transport to answer, and it answers
        ``CORE_REFUSED`` — right for a port that refused and wrong for a
        parameter that could not be read, which is the one distinction this
        method exists to keep.
        """
        try:
            route = _ROUTES[request.method]
        except KeyError:
            return _refused(
                request,
                ErrorCode.UNKNOWN_METHOD,
                f"{request.method} is not a method this build answers; "
                "the two halves are from different installs, so reinstall both",
            )

        try:
            params = route.decode(request.params)
        except CodecError as exc:
            # `CodecError` is the one exception whose message this module
            # repeats: the codec package writes it to name the field and the
            # reason and never the value.
            return _refused(request, ErrorCode.MALFORMED, f"{request.method}: {exc}")
        except Exception:
            # Anything else from a reader may quote what it read — a game
            # label, a path, a token — and this string reaches a log and a bug
            # report before any redactor sees it. So the method and the phase,
            # and nothing from the payload.
            return _refused(
                request,
                ErrorCode.MALFORMED,
                f"{request.method}: the parameters could not be read; "
                "check them against docs/CORE_RPC.md",
            )

        try:
            result = route.invoke(self._services, params)
        except Exception as exc:
            # The core's own refusal, carried with its message: a missing
            # backup, a failed doctor, an absent player. Also where an answer
            # the codec will not carry lands — a memory record smuggling text
            # in a numeric field is the core's record, not the caller's
            # mistake, and calling it MALFORMED would blame the wrong side.
            return _refused(
                request,
                ErrorCode.CORE_REFUSED,
                f"{request.method}: {type(exc).__name__}: {exc}",
            )

        return RpcResponse(id=request.id, ok=True, result=result)
