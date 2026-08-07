"""The seven ports, implemented against a core that is in the other process.

:class:`~pz_agent_mcp.ports.CoreServices` is what the router reads through, and
until this module existed nothing in the tree could produce one: an MCP client
launches ``pz-agent-mcp`` as a subprocess, so the session, the observation
store, the action engine, the capability report and the memory are all owned by
a process this one does not share. ``__main__`` said so in as many words and
refused to serve. :class:`RemoteCoreServices` is the answer — every port a call
over the Local Core RPC link described in ``docs/CORE_RPC.md``.

Built from a state directory and nothing else, because that is all the MCP
executable is given: :func:`~pz_agent_core.rpc.descriptor.load_descriptor` finds
the address, :func:`~pz_agent_core.rpc.token.read_token` finds the key, and
:class:`~pz_agent_core.rpc.transport.RpcClient` makes the call.

Nothing is spelled twice
------------------------

Method names come from :class:`~pz_agent_mcp.remote.methods.Method` and record
shapes come from :mod:`pz_agent_mcp.remote.codec`. Not one method name is
written as a literal here and not one record is assembled as a hand-rolled
dict: a name or a key spelled once in this module and once in the server is two
spellings that drift, and the drift arrives on a user's machine as
``UNKNOWN_METHOD`` or as a field that silently reads back as its default.

Two parameter objects have no codec because they are not records — the port
states them as a signature, and a signature is not something a wire can carry:
``session.arm`` (a mode and a backup confirmation) and ``action.status`` (an
id). Their keys are named once, as constants, and pinned in the tests against
hand-written payloads rather than against this module's own output.

Resolved fresh on every call
----------------------------

The descriptor and the token are read per call rather than once at
construction. Three reasons, in order of how much they cost when ignored:

* The sidecar can restart while the MCP server is alive — the MCP client owns
  this process's lifetime, not the sidecar. A cached token would then
  authenticate against a key that no longer exists and every call for the rest
  of the session would fail, which looks exactly like a broken install.
* :func:`load_descriptor` is where the *stale* case is caught, and staleness is
  a property of the moment, not of startup. A descriptor that named a live
  process a minute ago names a dead one now.
* Constructing :class:`RemoteCoreServices` therefore cannot fail. The boundary
  can be built before the sidecar is up, and "the sidecar is not running"
  arrives as a per-call answer rather than as a crash during startup, which is
  the difference between an MCP client showing a tool error and showing nothing
  at all.

The cost is two small file reads beside a connection this transport was already
making per call. Nothing is cached, so nothing goes stale.

Three ways to be told no, and they are never the same
-----------------------------------------------------

* :class:`CoreRefused` — the core was asked and said no. Arming without a
  backup, a failed doctor, a precondition the engine will not skip. The core's
  own message is relayed; this module does not reclassify it, because deciding
  a refusal here is how the boundary starts disagreeing with the engine.
* :class:`SidecarUnavailable` — there was nothing to ask. No descriptor, a
  stale one, no token, no listener, a connection that dropped, or no answer
  inside the deadline. The message says the sidecar is not running and names
  the command that starts it.
* :class:`CoreAnswerUnreadable` — something answered and the answer is not one
  this build can read: a malformed envelope, a protocol mismatch, a method the
  peer does not have, or a payload the codec refuses.

Collapsing them is the failure this taxonomy exists to prevent. (a) reported as
(b) tells a user to restart a sidecar whose session is perfectly healthy; (b)
reported as (a) tells them the core refused when nothing was ever asked; and
either reported as (c) sends them looking for a version mismatch that is not
there.

``observations.latest()`` deserves its own sentence, because it is the one port
whose success value is ``None``: before the first observation arrives there is
nothing to report. A dropped link must therefore *raise* rather than answer
``None``, or a session with a dead sidecar reads as a session waiting for its
first observation — forever, and quietly. The codec already separates the two
(absent key versus unreadable body); this module keeps them separate by never
turning a transport failure into a value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeVar

from pz_agent_core.actions import ActionRequest
from pz_agent_core.capabilities.model import CapabilityReport
from pz_agent_core.protocol import JsonDict, Observation, SessionMode
from pz_agent_core.rpc.descriptor import DescriptorError, load_descriptor, runtime_dir
from pz_agent_core.rpc.token import TokenError, read_token
from pz_agent_core.rpc.transport import (
    DEFAULT_DEADLINE_SECONDS,
    RpcClient,
    RpcUnavailable,
)
from pz_agent_core.rpc.wire import ErrorCode, RpcError, TooLarge
from pz_agent_mcp.ports import (
    ActionRecord,
    CoreServices,
    DoctorCheck,
    LogRecord,
    MemoryRecord,
    PlanRecord,
    PlanRequest,
    SessionSnapshot,
    StopReport,
)
from pz_agent_mcp.remote.codec import CodecError
from pz_agent_mcp.remote.codec.actions import (
    decode_action_record,
    decode_action_status,
    encode_action_request,
)
from pz_agent_mcp.remote.codec.capabilities import decode_capability_report
from pz_agent_mcp.remote.codec.diagnostics import (
    TailQuery,
    decode_doctor_checks,
    decode_log_records,
    encode_tail_query,
)
from pz_agent_mcp.remote.codec.memory import (
    MemoryQuery,
    decode_memory_records,
    encode_memory_query,
)
from pz_agent_mcp.remote.codec.observations import decode_latest_observation
from pz_agent_mcp.remote.codec.plans import (
    decode_plan_current,
    decode_plan_record,
    encode_plan_request,
)
from pz_agent_mcp.remote.codec.session import decode_session_snapshot, decode_stop_report
from pz_agent_mcp.remote.methods import Method

__all__ = [
    "SIDECAR_NOT_RUNNING",
    "SIDECAR_REMEDY",
    "CoreAnswerUnreadable",
    "CoreLink",
    "CoreRefused",
    "RemoteActionPort",
    "RemoteCapabilityPort",
    "RemoteCoreError",
    "RemoteCoreServices",
    "RemoteDiagnosticsPort",
    "RemoteMemoryPort",
    "RemoteObservationPort",
    "RemotePlanPort",
    "RemoteSessionPort",
    "SidecarUnavailable",
]

#: The sentence a user has to read to know this is not their session's fault.
#: One constant rather than one phrasing per call site, so that every way of
#: failing to reach the sidecar says the same thing and a caller can look for
#: it.
SIDECAR_NOT_RUNNING: Final = "the sidecar is not running or has stopped answering"

#: What to do about it. Named commands, because "start the sidecar" is not an
#: instruction to somebody who has only ever launched the game.
SIDECAR_REMEDY: Final = "start it with 'pz-agent start', then check it with 'pz-agent status'"

#: The params of ``session.arm``. No codec defines them: they are not a record,
#: they are the port's own signature, and the two halves have to agree on the
#: spelling anyway. Named here once so this module cannot disagree with itself,
#: and pinned in the tests against a hand-written payload so it cannot silently
#: disagree with the server either.
_ARM_MODE: Final = "mode"
_ARM_CONFIRM_BACKUP: Final = "confirm_backup"

#: The one param of ``action.status``, for the same reason.
_STATUS_ACTION_ID: Final = "action_id"

_T = TypeVar("_T")

#: What a codec's decoding half looks like from here: one payload in, one record
#: out. Stated so :meth:`CoreLink.read` can be one function rather than fourteen
#: copies of "call, then decode, then translate the codec's refusal".
Decoder = Callable[[Mapping[str, Any]], _T]


class RemoteCoreError(RuntimeError):
    """A call over the Core RPC link did not produce a record.

    A base for the three kinds below and never raised itself. Callers that only
    want to know "the core did not answer this" catch this; callers that have
    different things to say about a refusal and a dead sidecar — which is every
    caller facing a user — catch the subclasses.
    """


class SidecarUnavailable(RemoteCoreError):
    """There was no running core to ask.

    Missing or stale descriptor, missing token, nothing listening, a connection
    that dropped mid-exchange, or silence past the deadline. Deliberately the
    same class for all of them: the user-facing fact is identical and the
    remedy is identical, and the specific cause is in the message.
    """


class CoreRefused(RemoteCoreError):
    """The core was asked, and answered no.

    Carries the core's own message verbatim rather than a re-interpretation.
    The refusals that reach here are the ones the engine owns — a missing
    backup, an unsupported build, a precondition — and this side of the link
    has strictly less information about them than the side that raised them.
    """

    def __init__(self, method: str, code: str, detail: str) -> None:
        super().__init__(
            f"{method}: the core refused ({detail})" if detail else f"{method}: the core refused"
        )
        self.method = method
        self.code = code
        self.detail = detail


class CoreAnswerUnreadable(RemoteCoreError):
    """Something answered, and it was not an answer this build can read.

    A malformed envelope, a protocol major this build does not speak, a method
    the peer does not have, an id that belongs to another exchange, or a result
    the codec refuses. All of them mean the same thing to a caller — the state
    is unknown — and none of them mean the core said no.
    """


@dataclass(frozen=True, slots=True)
class CoreLink:
    """One state directory, and the calls that can be made through it.

    Holds no connection and no credentials: :meth:`result` resolves the
    descriptor and the token, makes one call, and lets both go. That is what
    keeps a restarted sidecar reachable and a stale descriptor detectable, and
    it is why constructing this object cannot fail.
    """

    state_dir: Path
    deadline: float = DEFAULT_DEADLINE_SECONDS

    def __post_init__(self) -> None:
        if self.deadline <= 0:
            # Checked here rather than left to `RpcClient`, which would raise on
            # the first call — a bad deadline is a wiring mistake and belongs at
            # the point of wiring.
            raise ValueError(f"deadline must be positive, got {self.deadline}")

    def _unavailable(self, method: str, reason: str) -> SidecarUnavailable:
        return SidecarUnavailable(f"{method}: {SIDECAR_NOT_RUNNING} ({reason}); {SIDECAR_REMEDY}")

    def _client(self, method: str) -> RpcClient:
        """Find the running sidecar, or say that there is not one.

        Both failures here are the same kind: a descriptor that is missing,
        malformed, stale or from another build, and a token that is missing or
        too short, all mean this process has no core to talk to. Their messages
        name the file and the reason and never the token's contents.
        """
        try:
            descriptor = load_descriptor(self.state_dir)
            token = read_token(runtime_dir(self.state_dir))
        except (DescriptorError, TokenError) as exc:
            raise self._unavailable(method, str(exc)) from exc
        return RpcClient(descriptor, authkey=token, deadline=self.deadline)

    def result(self, method: str, params: JsonDict | None = None) -> JsonDict:
        """Make one call and return its result body.

        Raises:
            SidecarUnavailable: there was no running core to ask, or it stopped
                answering during the exchange.
            CoreRefused: the core answered, and its answer was a refusal.
            CoreAnswerUnreadable: something answered and this build cannot read
                it.
            TooLarge: this process built a request over the transport's cap.
                Deliberately *not* one of the three kinds and deliberately not
                translated: nothing was asked and nothing was answered, so the
                honest report is the transport's own, which names the method and
                both sizes and quotes no payload. The tool boundary caps every
                free-text field long before this, so reaching it is a defect
                here rather than a condition of the link.
        """
        client = self._client(method)
        try:
            response = client.call(method, params)
        except RpcUnavailable as exc:
            raise self._unavailable(method, str(exc)) from exc
        except TooLarge:
            raise
        except RpcError as exc:
            raise CoreAnswerUnreadable(
                f"{method}: the answer is not one this build can read ({exc})"
            ) from exc
        if response.ok:
            return dict(response.result)
        # `ok` is stored on the envelope rather than inferred, so this branch is
        # a failure the peer stated. Which failure it is decides which of the
        # three kinds the caller sees, and only one code means the core itself
        # answered.
        code = response.error_code or ErrorCode.MALFORMED
        if code == ErrorCode.CORE_REFUSED:
            raise CoreRefused(method, code, response.error_message)
        raise CoreAnswerUnreadable(
            f"{method}: the sidecar answered {code} instead of a result ({response.error_message})"
        )

    def read(self, method: str, decode: Decoder[_T], params: JsonDict | None = None) -> _T:
        """Make one call and decode its result with *decode*.

        Raises:
            CoreAnswerUnreadable: the codec refused the body. Its message names
                the field and the reason and never the value, so it is relayed
                as-is; a second description written here would be a worse one.
            SidecarUnavailable: as :meth:`result`.
            CoreRefused: as :meth:`result`.
        """
        payload = self.result(method, params)
        try:
            return decode(payload)
        except CodecError as exc:
            raise CoreAnswerUnreadable(f"{method}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class RemoteSessionPort:
    """:class:`~pz_agent_mcp.ports.SessionPort` over the link."""

    link: CoreLink

    def status(self) -> SessionSnapshot:
        """The core's current session state."""
        return self.link.read(Method.SESSION_STATUS, decode_session_snapshot)

    def arm(self, mode: SessionMode, *, confirm_backup: bool) -> SessionSnapshot:
        """Ask the core to move to *mode*.

        The refusal path is the reason the three failure kinds exist. A core
        that declines because there is no backup answers ``CORE_REFUSED`` and
        raises :class:`CoreRefused` here, carrying its own words; nothing in
        this method decides which precondition failed.
        """
        return self.link.read(
            Method.SESSION_ARM,
            decode_session_snapshot,
            {_ARM_MODE: mode.value, _ARM_CONFIRM_BACKUP: confirm_backup},
        )

    def disarm(self) -> SessionSnapshot:
        """Return the core to ``OBSERVE``."""
        return self.link.read(Method.SESSION_DISARM, decode_session_snapshot)

    def stop(self) -> StopReport:
        """Panic stop, and what it cleared."""
        return self.link.read(Method.SESSION_STOP, decode_stop_report)


@dataclass(frozen=True, slots=True)
class RemoteObservationPort:
    """:class:`~pz_agent_mcp.ports.ObservationPort` over the link."""

    link: CoreLink

    def latest(self) -> Observation | None:
        """The newest accepted observation, or ``None`` before the first.

        ``None`` is reachable exactly one way: the core answered, and its answer
        carried no observation. Every failure to reach the core raises, because
        a dropped link answered as ``None`` would be indistinguishable from a
        session that has not seen its first observation yet — and unlike that
        session, this one is never going to.
        """
        return self.link.read(Method.OBSERVATION_LATEST, decode_latest_observation)


@dataclass(frozen=True, slots=True)
class RemoteCapabilityPort:
    """:class:`~pz_agent_mcp.ports.CapabilityPort` over the link."""

    link: CoreLink

    def report(self) -> CapabilityReport:
        """The probe results that decide which tools are published.

        There is no empty-report fallback. A report this build cannot read means
        the boundary does not know which capabilities exist, and answering with
        an empty one would withdraw every tool while looking like a game that
        supports nothing.
        """
        return self.link.read(Method.CAPABILITIES_REPORT, decode_capability_report)


@dataclass(frozen=True, slots=True)
class RemoteActionPort:
    """:class:`~pz_agent_mcp.ports.ActionPort` over the link."""

    link: CoreLink

    def submit(self, request: ActionRequest) -> ActionRecord:
        """Hand *request* to the core and return the record it answers with.

        One call, and it is over when the answer arrives. This method does not
        poll :meth:`status` until the action is terminal, and that is a safety
        property rather than a performance one: the transport is one exchange
        per connection, so a submit that waited for a postcondition would hold
        the link for as long as the action took, and ``pz_session_stop`` — the
        one tool a user reaches for while a long action is running — could not
        get through.

        Success is not claimed here either. Whatever status the core reported is
        what comes back, and :class:`~pz_agent_mcp.ports.ActionRecord` refuses to
        exist as ``SUCCEEDED`` without the terminal result and its observed
        postcondition, so a core that claimed one it could not prove fails to
        decode rather than being believed.
        """
        return self.link.read(
            Method.ACTION_SUBMIT, decode_action_record, encode_action_request(request)
        )

    def status(self, action_id: str) -> ActionRecord | None:
        """The record for *action_id*, or ``None`` if the core has no such action.

        ``None`` means the core answered and had nothing under that id — the
        codec spells that as an absent key, distinct from an empty object, which
        would be a decode failure. A link that is down raises.
        """
        return self.link.read(
            Method.ACTION_STATUS, decode_action_status, {_STATUS_ACTION_ID: action_id}
        )


@dataclass(frozen=True, slots=True)
class RemotePlanPort:
    """:class:`~pz_agent_mcp.ports.PlanPort` over the link."""

    link: CoreLink

    def execute(self, request: PlanRequest) -> PlanRecord:
        """Submit a typed plan and return the record the core answers with.

        Like :meth:`RemoteActionPort.submit`, this returns when the plan has an
        id, not when it has finished. ``max_steps`` and ``max_real_seconds``
        travel with the request, so the bound the caller chose is the bound the
        core runs to.
        """
        return self.link.read(Method.PLAN_EXECUTE, decode_plan_record, encode_plan_request(request))

    def current(self) -> PlanRecord | None:
        """The plan the core is running, or ``None`` if there is none."""
        return self.link.read(Method.PLAN_CURRENT, decode_plan_current)


@dataclass(frozen=True, slots=True)
class RemoteMemoryPort:
    """:class:`~pz_agent_mcp.ports.MemoryPort` over the link."""

    link: CoreLink

    def query(self, *, kinds: Sequence[str], limit: int) -> Sequence[MemoryRecord]:
        """Read remembered facts.

        The keyword signature becomes a
        :class:`~pz_agent_mcp.remote.codec.memory.MemoryQuery`, which is what a
        wire can carry. ``limit`` is passed through rather than clamped: the
        ceiling belongs to the router, and a second one here would be a second
        policy that drifts out of step with the first.

        Raises:
            CodecError: *kinds* is a single string. The codec refuses that on
                its own — a ``str`` is a sequence of one-character strings, so
                ``"home"`` would encode as four kinds — but only if it is still
                a string when the codec sees it, and ``MemoryQuery`` declares a
                ``tuple``. Converting first would launder ``"home"`` into
                ``("h", "o", "m", "e")``, which is a well-formed query for four
                kinds that do not exist and an empty answer that looks like a
                fact. So the one case the conversion would hide is refused
                before the conversion happens.
        """
        if isinstance(kinds, str):
            raise CodecError(
                f"{Method.MEMORY_QUERY}: kinds must be a sequence of kind names, not one string"
            )
        return self.link.read(
            Method.MEMORY_QUERY,
            decode_memory_records,
            encode_memory_query(MemoryQuery(kinds=tuple(kinds), limit=limit)),
        )


@dataclass(frozen=True, slots=True)
class RemoteDiagnosticsPort:
    """:class:`~pz_agent_mcp.ports.DiagnosticsPort` over the link."""

    link: CoreLink

    def doctor(self) -> Sequence[DoctorCheck]:
        """Every environment check the core ran."""
        return self.link.read(Method.DIAGNOSTICS_DOCTOR, decode_doctor_checks)

    def tail(
        self,
        *,
        limit: int,
        level: str | None = None,
        component: str | None = None,
        action_id: str | None = None,
    ) -> Sequence[LogRecord]:
        """Recent log records, filtered by the core.

        The three filters travel even when they are ``None``: an absent filter
        is a statement, and the codec writes it as one rather than as a key
        somebody forgot.
        """
        return self.link.read(
            Method.DIAGNOSTICS_TAIL,
            decode_log_records,
            encode_tail_query(
                TailQuery(limit=limit, level=level, component=component, action_id=action_id)
            ),
        )


class RemoteCoreServices(CoreServices):
    """Every port of :class:`~pz_agent_mcp.ports.CoreServices`, over one link.

    A subclass rather than a factory function so that it *is* a
    :class:`CoreServices` to the type checker as well as at runtime: the router
    takes the bundle, and a remote implementation that only duck-typed would be
    accepted by mypy in some call sites and not others.
    """

    def __init__(self, link: CoreLink) -> None:
        super().__init__(
            session=RemoteSessionPort(link),
            observations=RemoteObservationPort(link),
            capabilities=RemoteCapabilityPort(link),
            actions=RemoteActionPort(link),
            plans=RemotePlanPort(link),
            memory=RemoteMemoryPort(link),
            diagnostics=RemoteDiagnosticsPort(link),
        )

    @classmethod
    def from_state_dir(
        cls, state_dir: Path, *, deadline: float = DEFAULT_DEADLINE_SECONDS
    ) -> RemoteCoreServices:
        """The boundary's services for the sidecar whose state is *state_dir*.

        The whole constructor the MCP executable needs: it is launched with a
        state directory and nothing else. Reads nothing here — every call
        resolves the descriptor and the token itself — so this succeeds whether
        or not a sidecar is running, and a sidecar that starts afterwards is
        reachable through the same object.
        """
        return cls(CoreLink(state_dir=Path(state_dir), deadline=deadline))
