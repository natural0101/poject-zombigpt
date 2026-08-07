"""The two shapes every tool call ends in, and the one retry table behind them.

``docs/MCP_TOOLS.md`` fixes the error payload: a stable ``reason_code`` from the
protocol's closed set plus a ``retryable`` flag derived from
:data:`~pz_agent_core.protocol.RETRYABLE_CODES`. Deriving the flag rather than
letting each call site state it is the whole point — a client that trusted a
hand-written flag would be maintaining a second copy of the retry table, and the
two would disagree the first time a code moved.

The success envelope holds the mirror of the engine's honesty rule: a
:class:`ToolSuccess` whose ``status`` is ``succeeded`` refuses to exist without
the observed postcondition in ``data``. Queued is ``accepted`` here for exactly
the same reason it is there.

A call can also fail before the core ever hears it, because the core is in
another process (``docs/CORE_RPC.md``). The three ways that can happen are three
distinct exceptions and they get three distinct codes here, because the reason
code is what a client branches on and the three remedies have nothing in
common: a refusal means read what the core said, a dead sidecar means start it,
and an unreadable answer means the two halves are from different installs.
Reporting any of them as another sends a user to fix something that is not
broken — which is what a single ``INTERNAL_ERROR`` for all three did.

Every free-text field is bounded, because these payloads are rendered to a user
and written to a log.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final, TypeAlias

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.capabilities.model import CapabilityError
from pz_agent_core.protocol import (
    RETRYABLE_CODES,
    ActionStatus,
    JsonDict,
    ProtocolError,
    ReasonCode,
    RefError,
)

# Imported at module scope, having checked that it is not a cycle: nothing under
# ``pz_agent_mcp.remote`` imports this module — the client half reads
# ``pz_agent_mcp.ports`` and its own codecs and nothing else from the boundary —
# so the table below can name the classes directly rather than resolving them on
# every refusal. `tests/unit/test_envelope_failures.py` imports this module
# first, before the package, so the check is not resting on import order that
# happens to hold today. Nothing here reaches the optional ``mcp`` SDK either;
# only :mod:`pz_agent_mcp.server` does.
from pz_agent_mcp.remote.client import (
    CoreAnswerUnreadable,
    CoreRefused,
    RemoteCoreError,
    SidecarUnavailable,
)

__all__ = [
    "MAPPED_EXCEPTIONS",
    "MAX_DIAGNOSTICS",
    "MAX_MESSAGE_CHARS",
    "MAX_WARNINGS",
    "IdFactory",
    "ToolFailure",
    "ToolOutcome",
    "ToolSuccess",
    "failure_from",
    "is_retryable",
    "new_request_id",
]

#: A message is a sentence for a human, not a transcript.
MAX_MESSAGE_CHARS: Final = 300

MAX_DIAGNOSTICS: Final = 10
MAX_WARNINGS: Final = 8

#: Injected so a test can assert on a fixed request id instead of a fresh UUID.
IdFactory: TypeAlias = Callable[[], str]


def new_request_id() -> str:
    """Default :data:`IdFactory`."""
    return str(uuid.uuid4())


def is_retryable(code: ReasonCode) -> bool:
    """Whether another attempt with the same intent is worth making."""
    return code in RETRYABLE_CODES


def _clip(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 1] + "…"


def _clip_all(values: Iterable[str], limit: int) -> tuple[str, ...]:
    return tuple(_clip(value) for value in list(values)[:limit])


class ToolFailure(Exception):
    """A tool call that ends in a domain refusal rather than a crash.

    Raised throughout the boundary and serialised once, so that "which code
    explains this" is decided where the refusal happens and the wire shape is
    decided in exactly one place.
    """

    def __init__(
        self,
        reason_code: ReasonCode,
        message: str,
        *,
        diagnostics: Iterable[str] = (),
    ) -> None:
        if reason_code is ReasonCode.POSTCONDITION_MET:
            # The one code reserved for observed success cannot explain a
            # refusal; allowing it would put a success code on a failed call.
            raise ValueError("POSTCONDITION_MET cannot explain a tool failure")
        super().__init__(message)
        self.reason_code = reason_code
        self.message = _clip(message)
        self.diagnostics = _clip_all(diagnostics, MAX_DIAGNOSTICS)

    @property
    def retryable(self) -> bool:
        return is_retryable(self.reason_code)

    def to_payload(self, *, tool: str, request_id: str) -> JsonDict:
        """The error document of ``docs/MCP_TOOLS.md``."""
        return {
            "ok": False,
            "tool": tool,
            "request_id": request_id,
            "reason_code": self.reason_code.value,
            "message": self.message,
            "retryable": self.retryable,
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What one handler produced, before the call's identity is attached.

    Handlers do not mint request ids and do not know whether they are answering
    a first call or a replay, so they return this and the router turns it into a
    :class:`ToolSuccess`. Keeping the two apart is what lets the replay path
    re-render a fresh reading of an action under the original call's identity.
    """

    data: JsonDict = field(default_factory=dict)
    message: str = ""
    status: str = "ok"
    warnings: tuple[str, ...] = ()
    action_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSuccess:
    """A tool call that was accepted, with whatever the core reported back.

    ``status`` is an :class:`~pz_agent_core.protocol.ActionStatus` value for the
    long-running tools and ``"ok"`` for the ones that answer immediately.
    ``action_id`` is present exactly when the call put work in flight, so a
    caller can wait on it without parsing the message.
    """

    tool: str
    request_id: str
    status: str = "ok"
    message: str = ""
    data: JsonDict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    action_id: str | None = None
    replayed: bool = False

    def __post_init__(self) -> None:
        if self.status == ActionStatus.SUCCEEDED.value and not self.data.get("evidence"):
            raise ValueError(
                "a succeeded tool result must carry the observed postcondition under 'evidence'"
            )
        object.__setattr__(self, "message", _clip(self.message))
        object.__setattr__(self, "warnings", _clip_all(self.warnings, MAX_WARNINGS))

    @classmethod
    def of(
        cls,
        outcome: ToolOutcome,
        *,
        tool: str,
        request_id: str,
        warnings: tuple[str, ...] | None = None,
        replayed: bool = False,
    ) -> ToolSuccess:
        """Attach a call's identity to a handler's *outcome*."""
        return cls(
            tool=tool,
            request_id=request_id,
            status=outcome.status,
            message=outcome.message,
            data=outcome.data,
            warnings=outcome.warnings if warnings is None else warnings,
            action_id=outcome.action_id,
            replayed=replayed,
        )

    def to_payload(self) -> JsonDict:
        out: JsonDict = {
            "ok": True,
            "tool": self.tool,
            "request_id": self.request_id,
            "status": self.status,
            "message": self.message,
            "data": self.data,
            "warnings": list(self.warnings),
            "replayed": self.replayed,
        }
        if self.action_id is not None:
            out["action_id"] = self.action_id
        return out


#: Domain exceptions the ports may raise, and the code each one means here.
#: Anything absent from this table is a defect rather than a refusal, and is
#: reported as ``INTERNAL_ERROR`` instead of being guessed at.
#:
#: **The order is the resolution order.** :func:`failure_from` answers with the
#: first entry that matches by ``isinstance``, so an entry placed above one of
#: its own subclasses would answer for both and the subclass's code would be
#: unreachable. :class:`~pz_agent_mcp.remote.client.RemoteCoreError` is the live
#: example and is deliberately absent: it is the base of all three link
#: failures, so one entry for it — anywhere — would either collapse them back
#: into a single code or quietly answer for a fourth kind added later. The
#: three are listed individually instead, and
#: ``tests/unit/test_envelope_failures.py`` fails if any entry precedes a
#: subclass of itself, or if any subclass of ``RemoteCoreError`` is missing.
MAPPED_EXCEPTIONS: Final[tuple[tuple[type[Exception], ReasonCode], ...]] = (
    # The core was asked and said no. ``PRECONDITION_FAILED`` is the code the
    # engine's own `PreconditionFailed` defaults to, and a refusal that crossed
    # the link is the same event seen from one process further away. The
    # engine's *specific* code does not survive the wire — `CORE_REFUSED`
    # carries a message and no reason code — so this is the honest floor rather
    # than a guess at which precondition it was.
    (CoreRefused, ReasonCode.PRECONDITION_FAILED),
    # There was nothing to ask. The sidecar owns the session, so no sidecar is
    # no session: the same code the core itself answers with when the peer that
    # should be alive is not (`session/handshake.py`), fatal to a plan, and not
    # retryable, because no number of retries starts a process.
    (SidecarUnavailable, ReasonCode.STALE_SESSION),
    # Something answered and it was not an answer this build can read. Not the
    # caller's argument and not the core's decision: the two halves are from
    # different installs, or a body failed its codec. That is a defect in what
    # is installed, which is what ``INTERNAL_ERROR`` means here.
    (CoreAnswerUnreadable, ReasonCode.INTERNAL_ERROR),
    (RefError, ReasonCode.INVALID_REF),
    (CapabilityError, ReasonCode.CAPABILITY_UNAVAILABLE),
    (ProtocolError, ReasonCode.INVALID_ARGUMENT),
)

#: Exceptions whose own text is already the sentence a user has to read, so it
#: is relayed rather than prefixed with a class name. Everything else gets the
#: type name, which is the only clue a caller has about an exception nobody
#: wrote a message for.
#:
#: The link failures qualify as a class: each is phrased for the person who has
#: to act on it — the dead one names the command that starts the sidecar, the
#: refusal carries the core's own words. Prefixing them would spend characters
#: of a bounded field on an internal class name that says less than the code
#: now does.
_SPEAKS_FOR_ITSELF: Final[tuple[type[Exception], ...]] = (
    PreconditionFailed,
    RemoteCoreError,
)


def _describe(exc: Exception) -> str:
    """The message of *exc*, with a type name only where it adds something."""
    if isinstance(exc, _SPEAKS_FOR_ITSELF):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def failure_from(exc: Exception) -> ToolFailure:
    """Translate a core exception into the boundary's refusal.

    :class:`~pz_agent_core.actions.PreconditionFailed` already carries the code
    the recovery table is keyed by, so it is relayed rather than reclassified —
    re-deciding it here is how the boundary starts disagreeing with the engine.
    :class:`~pz_agent_mcp.remote.client.CoreRefused` is the same decision made
    one process away and is relayed on the same grounds: this side of the link
    knows strictly less about why the core said no than the side that said it.
    """
    if isinstance(exc, ToolFailure):
        return exc
    if isinstance(exc, PreconditionFailed):
        return ToolFailure(exc.reason_code, _describe(exc))
    for kind, code in MAPPED_EXCEPTIONS:
        if isinstance(exc, kind):
            return ToolFailure(code, _describe(exc))
    return ToolFailure(ReasonCode.INTERNAL_ERROR, _describe(exc))
