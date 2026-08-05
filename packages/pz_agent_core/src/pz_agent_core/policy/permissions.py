"""Whether a command may run at all: the mode-by-risk-by-capability ladder (§7.5).

The vocabulary lives in :mod:`pz_agent_core.protocol.enums`; this module is what
reads it, and what turns "no" into a sentence a user can be told together with a
reason code a caller can branch on.

Three properties are structural here rather than conventional:

* **P4 has no autonomous path.** :data:`MODE_LIMITS` names no ``P4`` ceiling for
  any mode, and the only branch that can allow a ``P4`` command demands an
  explicit per-call grant on a *user-directed* call. Two independent barriers,
  because §7.5's "P4 всегда требует отдельного permission" is the line here whose
  violation is irreversible inside the game.
* **Stopping is never gated.** :data:`~pz_agent_core.protocol.ALWAYS_ALLOWED_ACTIONS`
  is answered before the mode, the risk and the capability are even looked at.
  A stop that could be refused by a probe result or a mode is not a stop.
* **An unproven capability refuses instead of trying.** The default state of a
  capability nobody probed is ``unsupported``, and this module treats that as a
  refusal — never as "probably fine". On the agent's own initiative even
  ``available_unverified`` is not enough: §7.7 puts an unverified capability on
  the ask-the-user side of the line.

Nothing here reads a clock, a file or an observation. A permission decision is a
pure function of the request, the capability probes and the configuration, so the
whole ladder is reviewable — and testable — one cell at a time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ..protocol import (
    ALWAYS_ALLOWED_ACTIONS,
    MUTATING_MODES,
    READ_ONLY_ACTIONS,
    SELF_DIRECTED_MODES,
    ActionName,
    CapabilityState,
    ReasonCode,
    RiskClass,
    SessionMode,
)
from .selection import NO_CAPABILITIES, CapabilityLookup

__all__ = [
    "DEFAULT_AUTONOMOUS_P3_ACTIONS",
    "DEFAULT_PERMISSION_CONFIG",
    "MODE_LIMITS",
    "NOTHING_PERMITTED",
    "Initiative",
    "ModeLimits",
    "PermissionConfig",
    "PermissionDecision",
    "PermissionRequest",
    "check_permission",
    "is_mutating",
]


class Initiative(StrEnum):
    """Who wanted this command.

    The distinction is the whole reason two ceilings exist: §7.5 grants
    ``ASSISTED`` up to P3 "по явной цели" — on the user's explicit goal — while
    an agent acting on its own gets a strictly smaller set in one mode only.
    """

    #: The user asked for it, directly or as a step of a goal they stated.
    USER = "user"
    #: The autonomy loop proposed it. No user is answering right now.
    SELF_DIRECTED = "self_directed"


@dataclass(frozen=True, slots=True)
class ModeLimits:
    """The highest risk one session mode permits, per initiative.

    ``None`` means "no command of any risk class", which is not the same as
    ``P0``: it is what ``OFF`` and every non-self-directed mode's autonomous
    column say, and it cannot be lifted by a grant.
    """

    user_max: RiskClass | None
    self_directed_max: RiskClass | None

    def __post_init__(self) -> None:
        if RiskClass.P4 in (self.user_max, self.self_directed_max):
            raise ValueError(
                "P4 may never be a mode ceiling; it is reachable only through an "
                "explicit per-call grant"
            )
        if self.self_directed_max is not None and self.user_max is None:
            raise ValueError("a mode that forbids user-directed commands cannot permit autonomous")

    def ceiling(self, initiative: Initiative) -> RiskClass | None:
        return self.user_max if initiative is Initiative.USER else self.self_directed_max


#: What a mode this table does not know permits: nothing. Failing closed matters
#: because the table is looked up by enum value — a mode added to the protocol
#: and forgotten here must refuse commands, not raise or wave them through.
NOTHING_PERMITTED: Final = ModeLimits(user_max=None, self_directed_max=None)

#: The §7.5 defaults. ``AUTONOMOUS`` stops at P2 on its own initiative;
#: "ограниченный P3" is expressed by :attr:`PermissionConfig.autonomous_p3_actions`
#: rather than by a ceiling, because §7.5 limits it per action and not per rank.
MODE_LIMITS: Final[Mapping[SessionMode, ModeLimits]] = MappingProxyType(
    {
        SessionMode.OFF: NOTHING_PERMITTED,
        SessionMode.OBSERVE: ModeLimits(user_max=RiskClass.P0, self_directed_max=None),
        # The reflex guard cancels and stops; both are always-allowed actions and
        # never reach the ladder. What this mode may additionally do is look.
        SessionMode.REFLEX_ONLY: ModeLimits(user_max=RiskClass.P0, self_directed_max=None),
        SessionMode.ASSISTED: ModeLimits(user_max=RiskClass.P3, self_directed_max=None),
        SessionMode.AUTONOMOUS: ModeLimits(user_max=RiskClass.P3, self_directed_max=RiskClass.P2),
        SessionMode.EXPERIMENTAL_INPUT: ModeLimits(user_max=RiskClass.P3, self_directed_max=None),
    }
)

#: Empty by design. §7.5 permits "ограниченный P3" autonomously without naming
#: which actions qualify, and guessing which world-touching action is safe
#: unattended is not a guess this project makes. A deployment that wants one
#: opts in explicitly through :class:`PermissionConfig`.
DEFAULT_AUTONOMOUS_P3_ACTIONS: Final[frozenset[ActionName]] = frozenset()


def is_mutating(action: ActionName) -> bool:
    """True when *action* can change the world.

    Derived from the protocol's own sets rather than restated, so an action
    added to :data:`~pz_agent_core.protocol.READ_ONLY_ACTIONS` cannot end up
    classified two different ways in two modules.
    """
    return action not in READ_ONLY_ACTIONS and action not in ALWAYS_ALLOWED_ACTIONS


@dataclass(frozen=True, slots=True)
class PermissionConfig:
    """The tunable half of the ladder."""

    #: P3 actions the agent may take on its own initiative in ``AUTONOMOUS``.
    autonomous_p3_actions: frozenset[ActionName] = DEFAULT_AUTONOMOUS_P3_ACTIONS
    #: When true, an ``available_unverified`` capability is enough for a
    #: user-directed command but not for a self-directed one (§7.7: an
    #: unverified capability is on the ask-the-user side).
    require_verified_capability_for_autonomy: bool = True

    def __post_init__(self) -> None:
        if not self.autonomous_p3_actions <= set(ActionName):
            raise ValueError("autonomous_p3_actions may only name known actions")
        forbidden = self.autonomous_p3_actions & READ_ONLY_ACTIONS
        if forbidden:
            raise ValueError(
                f"read-only actions are permitted without a P3 grant already: {sorted(forbidden)}"
            )


DEFAULT_PERMISSION_CONFIG: Final = PermissionConfig()


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """One command's permission question.

    *risk* is supplied rather than derived: several adapters compute a risk that
    depends on the command's arguments (reaching into a world container is P3,
    the same transfer within the backpack is P1), and re-deriving it here would
    be a second, weaker copy of that judgement.
    """

    action: ActionName
    risk: RiskClass
    mode: SessionMode
    initiative: Initiative = Initiative.USER
    #: Probe name the action needs, or None when it drives no game API.
    capability: str | None = None
    #: Per-call consent the user gave for this one command (the wire's
    #: ``policy.risk_permission``). Ignored on a self-directed call: a grant is
    #: an answer to a question, and nobody was asked.
    grant: RiskClass | None = None

    def __post_init__(self) -> None:
        if self.capability is not None and not self.capability.strip():
            raise ValueError("capability must be a probe name, not an empty string")


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """Allowed, or refused with a reason code and a sentence.

    ``requires_grant`` is the ask-vs-act signal of §7.7: it is set only when a
    decision *from the user* would change the answer. A refusal that says "this
    mode never issues P2 commands" is not one of those — the fix is a different
    mode, not a per-call yes.
    """

    allowed: bool
    detail: str
    reason_code: ReasonCode | None = None
    requires_grant: bool = False

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("a permission decision must explain itself")
        if self.allowed != (self.reason_code is None):
            raise ValueError("a refusal carries a reason code and an allowance carries none")
        if self.allowed and self.requires_grant:
            raise ValueError("an allowed command is not waiting on a grant")

    @property
    def denied(self) -> bool:
        return not self.allowed

    @classmethod
    def permit(cls, detail: str) -> PermissionDecision:
        return cls(allowed=True, detail=detail)

    @classmethod
    def refuse(
        cls,
        reason_code: ReasonCode,
        detail: str,
        *,
        requires_grant: bool = False,
    ) -> PermissionDecision:
        if reason_code is ReasonCode.POSTCONDITION_MET:
            raise ValueError("POSTCONDITION_MET cannot explain a refusal")
        return cls(
            allowed=False,
            detail=detail,
            reason_code=reason_code,
            requires_grant=requires_grant,
        )


def check_permission(
    request: PermissionRequest,
    *,
    capabilities: CapabilityLookup = NO_CAPABILITIES,
    config: PermissionConfig = DEFAULT_PERMISSION_CONFIG,
) -> PermissionDecision:
    """Decide whether *request* may run, and say why when it may not.

    The gates run in a fixed order — always-allowed, mode, risk, capability —
    so the reported refusal is the most fundamental one rather than whichever
    check happened to run first. That order is also why a panic stop is answered
    before anything else: it must not be refusable by a mode or a probe.
    """
    if request.action in ALWAYS_ALLOWED_ACTIONS:
        return PermissionDecision.permit(
            f"{request.action.value} is accepted in every mode, including OFF."
        )
    for gate in (
        _mode_gate(request),
        _risk_gate(request, config),
        _capability_gate(request, capabilities, config),
    ):
        if gate is not None:
            return gate
    return PermissionDecision.permit(
        f"{request.action.value} ({request.risk.value}) is within what {request.mode.value} "
        f"permits on {request.initiative.value} initiative."
    )


def _mode_gate(request: PermissionRequest) -> PermissionDecision | None:
    """Refusals that are about the session mode alone."""
    if request.mode is SessionMode.OFF:
        return PermissionDecision.refuse(
            ReasonCode.NOT_ARMED,
            "Automation is off. Only stopping, disarming and cancelling are accepted.",
        )
    if request.initiative is Initiative.SELF_DIRECTED and request.mode not in SELF_DIRECTED_MODES:
        return PermissionDecision.refuse(
            ReasonCode.POLICY_DENIED,
            f"{request.mode.value} never acts on my own initiative; ask me and I will.",
        )
    if is_mutating(request.action) and request.mode not in MUTATING_MODES:
        return PermissionDecision.refuse(
            ReasonCode.POLICY_DENIED,
            f"{request.mode.value} is an observation mode; {request.action.value} would "
            "change the world.",
        )
    return None


def _risk_gate(request: PermissionRequest, config: PermissionConfig) -> PermissionDecision | None:
    """Refusals that are about the risk class."""
    if request.risk is RiskClass.P4:
        return _p4_gate(request)
    ceiling = _ceiling(request)
    if ceiling is None:
        return PermissionDecision.refuse(
            ReasonCode.POLICY_DENIED,
            f"{request.mode.value} issues no {request.initiative.value} commands at all.",
        )
    if request.risk.rank <= ceiling.rank:
        return None
    if (
        request.initiative is Initiative.SELF_DIRECTED
        and request.risk is RiskClass.P3
        and request.action in config.autonomous_p3_actions
    ):
        return None
    return PermissionDecision.refuse(
        ReasonCode.POLICY_DENIED,
        f"{request.action.value} is {request.risk.value} and {request.mode.value} allows at "
        f"most {ceiling.value} on {request.initiative.value} initiative.",
        requires_grant=request.initiative is Initiative.SELF_DIRECTED,
    )


def _p4_gate(request: PermissionRequest) -> PermissionDecision | None:
    """The only route to a P4 command, and it always ends at the user.

    Kept apart from the ceiling comparison on purpose. If P4 were just another
    rank, one wrong entry in :data:`MODE_LIMITS` would open it; here a mistake in
    that table cannot, because no value of it is consulted.
    """
    if request.initiative is not Initiative.USER:
        return PermissionDecision.refuse(
            ReasonCode.POLICY_DENIED,
            f"{request.action.value} is P4 and I never take a P4 action on my own "
            "initiative. Tell me to and I will ask you to confirm it.",
            requires_grant=True,
        )
    if request.mode not in MUTATING_MODES:
        return PermissionDecision.refuse(
            ReasonCode.POLICY_DENIED,
            f"{request.mode.value} accepts no world-changing command, let alone a P4 one.",
        )
    if request.grant is not RiskClass.P4:
        return PermissionDecision.refuse(
            ReasonCode.POLICY_DENIED,
            f"{request.action.value} is P4 and needs your explicit permission for this one call.",
            requires_grant=True,
        )
    return None


def _ceiling(request: PermissionRequest) -> RiskClass | None:
    """The highest risk this request may reach, grant included.

    A grant raises the ceiling only inside a mutating mode. Letting one lift
    ``OBSERVE`` to P3 would make the mode a suggestion: the user would have said
    "observe only" and then been taken at the word of a per-call flag instead.
    """
    limits = MODE_LIMITS.get(request.mode, NOTHING_PERMITTED)
    ceiling = limits.ceiling(request.initiative)
    if request.initiative is not Initiative.USER or request.grant is None:
        return ceiling
    if ceiling is None or request.mode not in MUTATING_MODES:
        return ceiling
    if request.grant is RiskClass.P4 or request.grant.rank <= ceiling.rank:
        return ceiling
    return request.grant


def _capability_gate(
    request: PermissionRequest,
    capabilities: CapabilityLookup,
    config: PermissionConfig,
) -> PermissionDecision | None:
    """Refusals that are about what the game has actually been proven to do."""
    if request.capability is None:
        return None
    state = capabilities.state(request.capability)
    if state is CapabilityState.VERIFIED:
        return None
    if state is CapabilityState.AVAILABLE_UNVERIFIED:
        if (
            request.initiative is Initiative.SELF_DIRECTED
            and config.require_verified_capability_for_autonomy
        ):
            return PermissionDecision.refuse(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                f"{request.capability} looks present but has never been run against this "
                "build. I will not try it unattended — ask me and I will.",
                requires_grant=True,
            )
        return None
    if state is CapabilityState.EXPERIMENTAL:
        return PermissionDecision.refuse(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            f"{request.capability} is experimental on this build; it needs your say-so "
            "for each call.",
            requires_grant=True,
        )
    if state is CapabilityState.DISABLED_BY_POLICY:
        return PermissionDecision.refuse(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            f"{request.capability} is switched off by configuration.",
        )
    return PermissionDecision.refuse(
        ReasonCode.CAPABILITY_UNAVAILABLE,
        f"{request.capability} is not available on this build — no probe has shown it "
        f"working, so {request.action.value} cannot be attempted.",
    )
