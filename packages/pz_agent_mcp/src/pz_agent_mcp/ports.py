"""What the boundary needs from the core, stated as narrow ports.

The MCP layer owns no domain state. Everything it reports it reads through one
of the protocols below, which the sidecar wiring implements over the session
manager, the observation store, the action engine and the capability report.
Stating them here rather than importing those subsystems directly is what keeps
the translation logic testable with no game, no filesystem and no MCP SDK —
which, given the SDK is an optional dependency, is the only way it is testable
at all.

The record types are the boundary's own vocabulary, and two of them carry an
invariant rather than just fields:

* :class:`ActionRecord` refuses to describe an action as ``succeeded`` without
  the terminal :class:`~pz_agent_core.protocol.ActionResult` and its observed
  postcondition. That is the engine's honesty rule restated where a client
  reads it, so a port implementation cannot report success early even by
  accident.
* :class:`MemoryRecord` carries numbers and references in ``data`` and puts
  everything a human would read into ``label``, which is quarantined on the way
  out. A memory store cannot smuggle game text through a numeric field.

:class:`GoalPort` is the exception to "the record types are the boundary's own
vocabulary": it speaks :mod:`pz_agent_core.goals` directly. Restating
:class:`~pz_agent_core.goals.GoalRequest` here would mean restating the closed
kind set, the per-kind parameter table and the three budget bounds — and a
second copy of a closed set is a set that eventually admits something the first
one refuses, which is the whole property this channel exists to have. What is
added here is only the two shapes a *port* answers with, because a queue method
returning ``bool`` is not enough to tell a client what happened.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from pz_agent_core.actions import ActionRequest
from pz_agent_core.capabilities.model import CapabilityReport
from pz_agent_core.goals import GoalAdmission, GoalRecord, GoalRequest
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    DangerLevel,
    JsonDict,
    Observation,
    ReasonCode,
    SessionMode,
)

__all__ = [
    "MAX_PROGRESS_COUNTERS",
    "ActionPort",
    "ActionRecord",
    "CapabilityPort",
    "CoreServices",
    "DiagnosticsPort",
    "DoctorCheck",
    "GoalCancellation",
    "GoalChannelStatus",
    "GoalPort",
    "GoalProgress",
    "LogRecord",
    "MemoryPort",
    "MemoryRecord",
    "ObservationPort",
    "PausedGoalRecord",
    "PlanPort",
    "PlanRecord",
    "PlanRequest",
    "PlanStepRecord",
    "SessionPort",
    "SessionSnapshot",
    "StopReport",
    "evidence_payload",
]


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Session identity and liveness, as the core currently holds it.

    ``save_id`` is the raw save directory fragment and never leaves this
    process: the boundary publishes
    :func:`~pz_agent_core.observation.compact.save_scope` of it instead, because
    the raw value can embed the player's profile name.
    """

    session_id: str
    mode: SessionMode
    armed: bool
    connected: bool
    protocol_version: str
    build: str | None = None
    save_id: str | None = None
    danger_level: DangerLevel = DangerLevel.NONE
    observation_seq: int | None = None
    capability_revision: int = 0
    game_heartbeat_ok: bool = False
    sidecar_heartbeat_ok: bool = False
    active_action_id: str | None = None


@dataclass(frozen=True, slots=True)
class StopReport:
    """What a panic stop actually did.

    ``cleared`` counts mod-owned queue entries only. A stop that touched an
    action the player queued would be a safety failure, so the number the tool
    reports is the number the core is allowed to have cleared.
    """

    cleared: int
    disarmed: bool
    mode: SessionMode

    def __post_init__(self) -> None:
        if self.cleared < 0:
            raise ValueError(f"cleared must be non-negative, got {self.cleared}")


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """One in-flight or finished action, as the boundary reports it."""

    action_id: str
    action: ActionName
    status: ActionStatus
    idempotency_key: str
    progress: float | None = None
    result: ActionResult | None = None

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("an action record must carry the id the caller waits on")
        if self.progress is not None and not 0.0 <= self.progress <= 1.0:
            raise ValueError(f"progress must be within 0..1, got {self.progress}")
        if self.status is not ActionStatus.SUCCEEDED:
            return
        if self.result is None or not self.result.evidence:
            raise ValueError(
                "a succeeded action record requires the terminal result and its "
                "observed postcondition; queued work is 'accepted'"
            )
        if self.result.reason_code is not ReasonCode.POSTCONDITION_MET:
            raise ValueError(
                f"a succeeded action record must carry POSTCONDITION_MET, "
                f"got {self.result.reason_code.value}"
            )

    @property
    def terminal(self) -> bool:
        return self.status.is_terminal


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """A goal to plan for, with the limits the caller accepts."""

    goal: str
    mode: SessionMode
    max_steps: int
    max_real_seconds: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PlanStepRecord:
    """One step of a plan and how it ended."""

    index: int
    action: ActionName
    status: ActionStatus
    reason_code: ReasonCode | None = None
    action_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """A plan, where it got to, and why it stopped."""

    plan_id: str
    status: ActionStatus
    step_index: int
    steps: tuple[PlanStepRecord, ...] = ()
    stopped_reason: ReasonCode | None = None

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("a plan record must carry the id the caller polls")
        if self.step_index < 0:
            raise ValueError(f"step_index must be non-negative, got {self.step_index}")


#: Counters one progress answer may carry. The producers today use two; the
#: bound exists so a drive growing a ledger could only ever widen a status
#: answer this much, never without limit.
MAX_PROGRESS_COUNTERS: Final = 8

#: What a phase or a counter name may look like: a short lowercase token. The
#: values are minted by our own deterministic drives — a journey state, a
#: mission pipeline phase — and a value that is not one is a producer bug, so
#: the constructor refuses it rather than quarantining a sentence into a field
#: a client is told is closed.
_PROGRESS_TOKEN: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True, slots=True)
class GoalProgress:
    """Where the deterministic drive serving one goal stands, right now.

    ``phase`` is a closed token the drive itself minted — a journey's
    ``planning``/``moving``/``arrived``/``refused``, a loot mission's
    ``start``/``approach``/``open``/``inspect``/``transfer``, an explore
    mission's ``start``/``approach`` — never caller or game text. It is the
    "progress only on phase change" primitive: the value moves exactly when
    the work does, so a client reports transitions instead of poll-spamming.

    ``counters`` carries the drive's own detail-free numbers (legs walked,
    containers inspected). Numbers and closed tokens only, both enforced here:
    a port answer is another process's word, and a sentence smuggled into a
    counter name would leave this boundary looking like data.

    A goal served by a plan provider has no instance of this at all — an
    LLM-served goal honestly has no deterministic phase — which is why the
    field carrying it is optional everywhere it appears.
    """

    phase: str
    counters: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _PROGRESS_TOKEN.fullmatch(self.phase):
            raise ValueError(f"a progress phase must be a closed token, got {self.phase!r}")
        if len(self.counters) > MAX_PROGRESS_COUNTERS:
            raise ValueError(
                f"a progress answer carries at most {MAX_PROGRESS_COUNTERS} counters, "
                f"got {len(self.counters)}"
            )
        for name, value in self.counters.items():
            if not isinstance(name, str) or not _PROGRESS_TOKEN.fullmatch(name):
                raise ValueError(f"a counter name must be a closed token, got {name!r}")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"counter {name} must be a non-negative integer, got {value!r}")


@dataclass(frozen=True, slots=True)
class PausedGoalRecord:
    """The goal a user's takeover parked, as the status surface reports it.

    The queue's own record honestly ended ``CANCELLED`` — the goals package
    has no ``PAUSED`` state — and this marker is the other half of the truth:
    paused by the user's own hand, not abandoned by the agent. It stays in the
    answer until an explicit fresh activation replaces it; nothing resumes the
    goal implicitly.

    ``reason`` is the loop's own sentence ("manual takeover"). It is carried
    as the string it is and quarantined on the way out like every other free
    text, rather than trusted on the strength of who wrote it.
    """

    goal_id: str
    kind: str
    reason: str
    paused_at_ms: int

    def __post_init__(self) -> None:
        if not self.goal_id:
            raise ValueError("a paused marker must name the goal it parked")
        if not self.kind:
            raise ValueError("a paused marker must carry the goal's kind")
        if self.paused_at_ms < 0:
            raise ValueError(f"paused_at_ms must be non-negative, got {self.paused_at_ms}")


@dataclass(frozen=True, slots=True)
class GoalChannelStatus:
    """The goal channel as it stands, plus the one goal the caller asked about.

    ``named`` is ``None`` for two different situations that a client must not
    have collapsed for it: no id was asked about, and an id was asked about that
    this channel has never minted or has already forgotten. The router knows
    which it is — it knows whether it passed an id — and turns the second into a
    refusal rather than into an answer that reads as "that goal is not running".

    ``pending`` is ordered oldest first, the way
    :attr:`~pz_agent_core.goals.GoalQueue.pending` orders it: by admission
    sequence and never by timestamp, because Windows' wall clock advances in
    ~15.6 ms granules and two goals submitted in one granule carry the same one.

    The three optional tails are additive and each defaults to the honest
    nothing, so a port that cannot answer them — a bundle without the
    deterministic wrapper, a core link whose codec does not carry them yet —
    answers ``None`` rather than an invented value:

    * ``progress`` describes the goal this answer is *about* — the named goal
      when an id was asked, the active one otherwise — and only while a live
      deterministic drive serves it. An LLM-served goal has none.
    * ``paused`` mirrors the loop's paused-by-takeover marker.
    * ``report`` is the loot or explore mission's ledger document for the
      named goal: the live snapshot while the mission runs, the sealed report
      after it ends, for as long as the bounded ledger keeps it.
    """

    active: GoalRecord | None = None
    pending: tuple[GoalRecord, ...] = ()
    named: GoalRecord | None = None
    progress: GoalProgress | None = None
    paused: PausedGoalRecord | None = None
    report: JsonDict | None = None


@dataclass(frozen=True, slots=True)
class GoalCancellation:
    """What a cancel request did to the goal it named.

    ``requested`` is not "cancelled": the channel observes a cancellation on its
    next tick, so an accepted request can be reported against a goal that is
    still running, and a client that read ``requested`` as "it has stopped"
    would be making the early claim :class:`ActionRecord` refuses to make about
    a postcondition. ``False`` means there was nothing to cancel — the goal had
    already reached a terminal state — which is a different thing again from the
    request being rejected.

    ``goal`` is ``None`` only for an id the channel does not know, and then
    nothing can have been requested.
    """

    goal: GoalRecord | None = None
    requested: bool = False

    def __post_init__(self) -> None:
        if self.requested and self.goal is None:
            raise ValueError(
                "a cancellation that was accepted must name the goal it was accepted for"
            )


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One remembered fact.

    ``data`` holds numbers and booleans, ``refs`` holds references, and
    ``label`` holds the one string a human would read. Splitting them this way
    means a memory store has nowhere to put free text that is not quarantined.
    """

    kind: str
    key: str
    label: str | None = None
    refs: tuple[str, ...] = ()
    data: Mapping[str, float | int | bool | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One environment check with a stable code."""

    code: str
    ok: bool
    detail: str = ""
    remediation: str = ""


@dataclass(frozen=True, slots=True)
class LogRecord:
    """One structured log line, already selected by the core's filters."""

    timestamp_ms: int
    level: str
    component: str
    message: str
    code: str | None = None
    action_id: str | None = None


class SessionPort(Protocol):
    """Session identity, arming and the stop lever."""

    def status(self) -> SessionSnapshot:
        """Current session state. Answers even when the game is gone."""
        ...

    def arm(self, mode: SessionMode, *, confirm_backup: bool) -> SessionSnapshot:
        """Move to *mode*.

        Raises:
            Exception: the core's own refusal — a missing backup, a failed
                doctor, an unsupported build, an absent player or a stale
                heartbeat. The boundary translates it; it never second-guesses
                which of those conditions applies.
        """
        ...

    def disarm(self) -> SessionSnapshot:
        """Return to ``OBSERVE``. Always permitted."""
        ...

    def stop(self) -> StopReport:
        """Panic stop: clear mod-owned entries, disarm, report what was cleared."""
        ...


class ObservationPort(Protocol):
    """The boundary's only window onto the world."""

    def latest(self) -> Observation | None:
        """The newest accepted observation, or None before the first arrives."""
        ...


class CapabilityPort(Protocol):
    """The probe results that decide which tools are published."""

    def report(self) -> CapabilityReport: ...


class ActionPort(Protocol):
    """Submitting work and asking how it is going.

    :meth:`submit` returns as soon as the action has an id. It must not block
    until the postcondition is observed: a long-running action that held the
    transport would make the stop tool unreachable exactly when it is needed.
    """

    def submit(self, request: ActionRequest) -> ActionRecord: ...

    def status(self, action_id: str) -> ActionRecord | None: ...


class PlanPort(Protocol):
    """Typed plans: submission and progress."""

    def execute(self, request: PlanRequest) -> PlanRecord: ...

    def current(self) -> PlanRecord | None: ...


class GoalPort(Protocol):
    """The typed goal channel: submission, progress and the cancel lever.

    :meth:`submit` admits a goal to the backlog and returns as soon as it has an
    id, exactly as :meth:`ActionPort.submit` does — the goal is *not* served
    inside the call. Activation is the sidecar's own loop, so what a caller gets
    back is ``pending``, and that is the honest word rather than a placeholder.

    :meth:`cancel` asks; it does not promise. See :class:`GoalCancellation`.
    """

    def submit(self, request: GoalRequest) -> GoalAdmission: ...

    def status(self, goal_id: str | None = None) -> GoalChannelStatus: ...

    def cancel(self, goal_id: str) -> GoalCancellation: ...


class MemoryPort(Protocol):
    """Read-only semantic memory."""

    def query(self, *, kinds: Sequence[str], limit: int) -> Sequence[MemoryRecord]: ...


class DiagnosticsPort(Protocol):
    """Environment checks and the recent log."""

    def doctor(self) -> Sequence[DoctorCheck]: ...

    def tail(
        self,
        *,
        limit: int,
        level: str | None = None,
        component: str | None = None,
        action_id: str | None = None,
    ) -> Sequence[LogRecord]: ...


@dataclass(frozen=True, slots=True)
class CoreServices:
    """Every port the router needs, in one injectable bundle.

    ``goals`` is the one optional member, and the default is not a convenience.

    This paragraph used to say the Local Core RPC link carried no ``goal.*``
    method, which was true when it was written and is not now: ``goal.submit``,
    ``goal.status`` and ``goal.cancel`` are in ``ALL_METHODS``, routed by
    :class:`~pz_agent_mcp.remote.server.CoreRouter`, and
    :class:`~pz_agent_mcp.remote.client.RemoteCoreServices` fills this field in.
    So the ``None`` case is no longer "this build" — it is a bundle assembled
    without the channel, which stays assemblable on purpose: an embedder
    supplying its own ports need not have one.

    Such a bundle says so by leaving this ``None``, and
    :class:`~pz_agent_mcp.router.ToolRouter` answers the three goal tools with
    ``CAPABILITY_UNAVAILABLE`` naming exactly that. The alternative — a default
    port that accepted goals and dropped them — is the fabricated success this
    project does not ship.
    """

    session: SessionPort
    observations: ObservationPort
    capabilities: CapabilityPort
    actions: ActionPort
    plans: PlanPort
    memory: MemoryPort
    diagnostics: DiagnosticsPort
    goals: GoalPort | None = None


def evidence_payload(result: ActionResult) -> JsonDict:
    """The observed postcondition of *result*, or an empty object.

    A helper rather than an attribute access because the emptiness is load
    bearing: a caller reading ``evidence`` on a non-successful result must get
    nothing, not the failure's world snapshot, which proves no postcondition.
    """
    if result.reason_code is not ReasonCode.POSTCONDITION_MET:
        return {}
    return dict(result.evidence)
