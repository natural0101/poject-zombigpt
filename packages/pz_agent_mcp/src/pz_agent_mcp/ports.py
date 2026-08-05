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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pz_agent_core.actions import ActionRequest
from pz_agent_core.capabilities.model import CapabilityReport
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
    "ActionPort",
    "ActionRecord",
    "CapabilityPort",
    "CoreServices",
    "DiagnosticsPort",
    "DoctorCheck",
    "LogRecord",
    "MemoryPort",
    "MemoryRecord",
    "ObservationPort",
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
    """Every port the router needs, in one injectable bundle."""

    session: SessionPort
    observations: ObservationPort
    capabilities: CapabilityPort
    actions: ActionPort
    plans: PlanPort
    memory: MemoryPort
    diagnostics: DiagnosticsPort


def evidence_payload(result: ActionResult) -> JsonDict:
    """The observed postcondition of *result*, or an empty object.

    A helper rather than an attribute access because the emptiness is load
    bearing: a caller reading ``evidence`` on a non-successful result must get
    nothing, not the failure's world snapshot, which proves no postcondition.
    """
    if result.reason_code is not ReasonCode.POSTCONDITION_MET:
        return {}
    return dict(result.evidence)
