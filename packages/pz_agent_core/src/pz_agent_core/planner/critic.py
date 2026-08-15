"""The last gate before a plan touches the game (§7.1, "Critic").

The planner says what it wants; this module says whether it may. It is
deterministic, it reads no clock and it starts nothing — a review is a pure
function of the plan, the freshest observation, the capability probes and the
session's configuration, so the same plan against the same world is always
answered the same way.

What it refuses, and why each rule exists rather than being left to the layer
below:

* **Over the step cap.** The executor would run them one at a time and the
  engine would refuse none of them; nothing else in the stack knows how long a
  plan is allowed to be.
* **An action with no adapter, or one whose capability is not usable.** §7.13.
  The engine refuses these too, but only after the first step has been spent —
  a plan whose third step cannot run is not worth starting the first.
* **A permission the ladder does not grant.** Answered by
  :func:`~pz_agent_core.policy.permissions.check_permission`, never by a second
  copy of it here. The risk it is asked about comes from the *adapter*, which
  can read the arguments: a transfer into a world container and a transfer
  inside the backpack share an action name and not a risk tier.
* **A reference from another session.** §3.7: after a save/load the runtime ids
  in an old reference may denote entirely different objects, so this is
  ``INVALID_REF`` and not something a retry can fix.
* **Leaving the autonomous radius.** §7.10 bounds only what the agent starts on
  its own; a user-directed plan *is* the explicit goal that clause exempts, so
  the check applies to self-directed initiative alone.
* **Repeating a step that just failed non-retryably.** §7.8's brake, applied to
  the plan rather than to the need: re-proposing the step that produced an
  ``INVALID_REF`` is how a replan turns into a loop.

The critic never approves on a technicality it could not check. Where the world
does not tell it enough to answer — a ``move_near`` whose object is not in the
observation, a transfer whose item has gone — it refuses, because "I could not
tell" and "it is fine" are not the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from ..actions.adapter import (
    ActionAdapter,
    AdapterRegistry,
    PreconditionFailed,
    UnknownActionError,
)
from ..policy.autonomy import DEFAULT_AUTONOMY_CONFIG, MAX_HOME_RADIUS, HomeSquare
from ..policy.permissions import (
    DEFAULT_PERMISSION_CONFIG,
    Initiative,
    PermissionConfig,
    PermissionRequest,
    check_permission,
)
from ..policy.selection import NO_CAPABILITIES, CapabilityLookup
from ..protocol import (
    RETRYABLE_CODES,
    Command,
    Observation,
    Position,
    ReasonCode,
    RiskClass,
    belongs_to_session,
)
from ..protocol.messages import MIN_LEASE_MS
from ..protocol.refs import RefKind
from .plan import (
    MAX_PLAN_STEPS,
    PLAN_COORDINATE_LIMIT,
    MoveNearArgs,
    MoveToArgs,
    Plan,
    PlanStep,
    StepFailure,
    SuccessKind,
)

#: Success criteria whose verdict is read off the item the step names. These are
#: the ones an *absent* item satisfies, which is why an unobserved reference
#: under one of them is refused rather than left to the executor. See
#: :meth:`PlanCritic._unobserved_ref`.
_ITEM_READING_CRITERIA: Final = frozenset({SuccessKind.ITEM_CONSUMED})

__all__ = [
    "DEFAULT_HOME_RADIUS",
    "CriticRule",
    "CriticVerdict",
    "PlanCritic",
    "RiskAssessor",
]

DEFAULT_HOME_RADIUS: Final = DEFAULT_AUTONOMY_CONFIG.home_radius

#: The draft command handed to :meth:`RiskAssessor.risk_for` is never shipped:
#: the adapters' risk functions are pure over ``args`` and the observation, and
#: this is the shape they read them from. A fixed id makes it obvious in a
#: debugger that nothing here was ever meant to reach the mod.
_DRAFT_COMMAND_ID: Final = "00000000-0000-4000-8000-000000000000"


class CriticRule(StrEnum):
    """Which gate refused.

    Separate from the wire's :class:`~pz_agent_core.protocol.ReasonCode` because
    two rules can share a code — a plan that names an unregistered action and one
    whose capability was never probed are both ``CAPABILITY_UNAVAILABLE`` — and a
    caller fixing the plan needs to know which of them it hit.
    """

    STEP_CAP_EXCEEDED = "step_cap_exceeded"
    ACTION_UNAVAILABLE = "action_unavailable"
    FOREIGN_REF = "foreign_ref"
    UNRESOLVABLE_STEP = "unresolvable_step"
    CAPABILITY_UNUSABLE = "capability_unusable"
    PERMISSION_DENIED = "permission_denied"
    OUTSIDE_HOME_RADIUS = "outside_home_radius"
    REPEATED_FAILURE = "repeated_failure"
    UNOBSERVED_REF = "unobserved_ref"


@dataclass(frozen=True, slots=True)
class CriticVerdict:
    """Approved, or refused with the rule, the code and the offending step."""

    approved: bool
    detail: str
    rule: CriticRule | None = None
    reason_code: ReasonCode | None = None
    step_id: str | None = None
    #: Highest tier any step was assessed at, once every step was assessed.
    risk: RiskClass | None = None
    #: True when a decision *from the user* would change the answer (§7.7).
    requires_grant: bool = False

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("a critic verdict must explain itself")
        if self.approved != (self.rule is None):
            raise ValueError("a refusal names the rule it broke and an approval names none")
        if self.approved != (self.reason_code is None):
            raise ValueError("a refusal carries a reason code and an approval carries none")
        if self.approved and self.requires_grant:
            raise ValueError("an approved plan is not waiting on a grant")

    @property
    def refused(self) -> bool:
        return not self.approved


@runtime_checkable
class RiskAssessor(Protocol):
    """An adapter that can tell the risk of *these* arguments, not just this action.

    Three adapters implement it — transfer, ``move_to`` and ``move_near`` — and
    the rest carry a fixed tier. Structural rather than part of
    :class:`~pz_agent_core.actions.ActionAdapter` so an adapter with nothing
    argument-dependent to say needs no method that returns its own constant.
    """

    def risk_for(self, command: Command, observation: Observation) -> RiskClass:
        """The tier this command needs, never below the adapter's fixed one."""
        ...


class PlanCritic:
    """Reviews a plan against the session it would run in."""

    def __init__(
        self,
        *,
        session_id: str,
        registry: AdapterRegistry,
        capabilities: CapabilityLookup = NO_CAPABILITIES,
        permissions: PermissionConfig = DEFAULT_PERMISSION_CONFIG,
        initiative: Initiative = Initiative.USER,
        grant: RiskClass | None = None,
        home: HomeSquare | None = None,
        home_radius: int = DEFAULT_HOME_RADIUS,
        max_steps: int = MAX_PLAN_STEPS,
    ) -> None:
        if not 1 <= home_radius <= MAX_HOME_RADIUS:
            raise ValueError(f"home_radius must be within 1..{MAX_HOME_RADIUS}, got {home_radius}")
        if not 1 <= max_steps <= MAX_PLAN_STEPS:
            raise ValueError(f"max_steps must be within 1..{MAX_PLAN_STEPS}, got {max_steps}")
        self._session_id = session_id
        self._registry = registry
        self._capabilities = capabilities
        self._permissions = permissions
        self._initiative = initiative
        self._grant = grant
        self._home = home
        self._home_radius = home_radius
        self._max_steps = max_steps

    def review(
        self,
        plan: Plan,
        observation: Observation,
        *,
        recent_failures: tuple[StepFailure, ...] = (),
    ) -> CriticVerdict:
        """Approve *plan*, or refuse it naming the first rule it breaks.

        Gates run plan-wide first and then step by step in order, so the reported
        refusal is the most fundamental one rather than whichever check happened
        to be written first.
        """
        if len(plan.steps) > self._max_steps:
            return _refuse(
                CriticRule.STEP_CAP_EXCEEDED,
                ReasonCode.POLICY_DENIED,
                f"this plan has {len(plan.steps)} steps and I run at most {self._max_steps} "
                "before checking back.",
            )
        blocked = frozenset(
            failure.signature
            for failure in recent_failures
            if failure.reason_code not in RETRYABLE_CODES
        )
        highest = RiskClass.P0
        for step in plan.steps:
            assessed = self._review_step(step, observation, blocked)
            if isinstance(assessed, CriticVerdict):
                return assessed
            if assessed.rank > highest.rank:
                highest = assessed
        return CriticVerdict(
            approved=True,
            detail=(
                f"{len(plan.steps)} step(s), at most {highest.value}, all within what "
                f"{observation.safety.mode.value} permits on {self._initiative.value} initiative."
            ),
            risk=highest,
        )

    # -- one step ----------------------------------------------------------

    def _review_step(
        self, step: PlanStep, observation: Observation, blocked: frozenset[str]
    ) -> CriticVerdict | RiskClass:
        """The assessed risk of *step*, or the verdict that refuses it."""
        try:
            adapter = self._registry.get(step.action)
        except UnknownActionError as exc:
            return _refuse(
                CriticRule.ACTION_UNAVAILABLE,
                exc.reason_code,
                f"{step.action.value} has no adapter in this session, so nothing could run "
                "or verify it.",
                step_id=step.step_id,
            )

        foreign = self._foreign_ref(step)
        if foreign is not None:
            return foreign

        unobserved = self._unobserved_ref(step, observation)
        if unobserved is not None:
            return unobserved

        risk = self._assess_risk(adapter, step, observation)
        if isinstance(risk, CriticVerdict):
            return risk

        permission = self._permission(adapter, step, risk, observation)
        if permission is not None:
            return permission

        radius = self._radius(step, observation)
        if radius is not None:
            return radius

        if step.signature in blocked:
            return _refuse(
                CriticRule.REPEATED_FAILURE,
                ReasonCode.PRECONDITION_FAILED,
                f"{step.action.value} with these arguments has already failed in a way that "
                "retrying cannot fix; proposing it again would be a loop.",
                step_id=step.step_id,
            )
        return risk

    def _unobserved_ref(self, step: PlanStep, observation: Observation) -> CriticVerdict | None:
        """Refuse a step whose *own success criterion* reads an item nobody reported.

        AGENTS.md: the model "may emit a typed plan and nothing else … no raw
        refs it invented". The plan type enforces most of that structurally, and
        ``_foreign_ref`` catches a reference minted by another session. A
        well-formed reference from *this* session naming an item that was never
        observed was approved — measured — and what happened next is the reason
        this gate is here rather than left to the executor.

        ``SuccessKind.ITEM_CONSUMED`` asks whether the item is *gone*. An item
        that never existed is absent, so the criterion holds, and
        ``PlanExecutor._gate`` checks ``success.holds`` **before** it reaches
        ``_ref_gate`` — the check that would have refused the reference with
        ``INVALID_REF``. So the step was skipped as "already satisfied", the run
        completed with every step accounted for, no command was ever sent, and
        the character never ate. A hallucinated reference produced a finished
        plan and a silent nothing.

        Narrow on purpose. Only criteria that *read the item* are gated, and only
        the item is looked up. A step may legitimately name a reference the
        current observation does not carry — a later step acting on what an
        earlier one will reveal — and refusing those would break plans that
        work; the executor re-checks against a fresh observation at the step
        itself, which is where that case belongs. What cannot be allowed is a
        criterion whose *satisfaction is the referent's absence* riding on a
        reference nothing ever reported.
        """
        if step.success.kind not in _ITEM_READING_CRITERIA:
            return None
        inventory = observation.inventory
        if inventory is None:
            # "The mod sent no inventory this tick" is not evidence that the
            # item is missing, and the executor answers None here for the same
            # reason. Refusing on an absent tier would refuse every plan made
            # during a gap in observation.
            return None
        for ref in step.args.refs():
            if ref.kind is RefKind.ITEM and inventory.item(ref.value) is None:
                return _refuse(
                    CriticRule.UNOBSERVED_REF,
                    ReasonCode.INVALID_REF,
                    f"{step.action.value} is judged by {step.success.kind.value}, which this "
                    f"item's absence would satisfy — and no observation has reported it. A "
                    f"reference nobody observed cannot be the thing a step proves it acted on.",
                    step_id=step.step_id,
                )
        return None

    def _foreign_ref(self, step: PlanStep) -> CriticVerdict | None:
        for ref in step.args.refs():
            if not belongs_to_session(ref.value, self._session_id):
                return _refuse(
                    CriticRule.FOREIGN_REF,
                    ReasonCode.INVALID_REF,
                    f"the {ref.kind.value} reference in this step was minted by a different "
                    "session and no longer names anything I can be sure of.",
                    step_id=step.step_id,
                )
        return None

    def _assess_risk(
        self, adapter: ActionAdapter, step: PlanStep, observation: Observation
    ) -> CriticVerdict | RiskClass:
        """Ask the adapter what *these* arguments are worth, not just the action.

        An adapter that cannot answer — because the item or container the step
        names is not in the observation — has told us the step is unresolvable,
        which is a refusal rather than a reason to fall back on the static tier.
        """
        if not isinstance(adapter, RiskAssessor):
            return step.risk
        draft = Command(
            session_id=self._session_id,
            seq=0,
            command_id=_DRAFT_COMMAND_ID,
            idempotency_key=f"critic-{step.step_id}",
            issued_at_ms=0,
            lease_ms=MIN_LEASE_MS,
            action=step.action,
            args=step.args.to_payload(),
        )
        try:
            assessed = adapter.risk_for(draft, observation)
        except PreconditionFailed as exc:
            return _refuse(
                CriticRule.UNRESOLVABLE_STEP,
                exc.reason_code,
                f"{step.action.value} cannot be assessed against what the mod reported: {exc}",
                step_id=step.step_id,
            )
        return assessed if assessed.rank > step.risk.rank else step.risk

    def _permission(
        self,
        adapter: ActionAdapter,
        step: PlanStep,
        risk: RiskClass,
        observation: Observation,
    ) -> CriticVerdict | None:
        decision = check_permission(
            PermissionRequest(
                action=step.action,
                risk=risk,
                mode=observation.safety.mode,
                initiative=self._initiative,
                capability=adapter.required_capability,
                grant=self._grant,
            ),
            capabilities=self._capabilities,
            config=self._permissions,
        )
        if decision.allowed:
            return None
        rule = (
            CriticRule.CAPABILITY_UNUSABLE
            if decision.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
            else CriticRule.PERMISSION_DENIED
        )
        # `check_permission` refuses with a reason code by construction; naming
        # the fallback keeps the type honest without asserting.
        return _refuse(
            rule,
            decision.reason_code or ReasonCode.POLICY_DENIED,
            decision.detail,
            step_id=step.step_id,
            requires_grant=decision.requires_grant,
        )

    def _radius(self, step: PlanStep, observation: Observation) -> CriticVerdict | None:
        """§7.10, applied only to what the agent starts unasked."""
        if self._initiative is not Initiative.SELF_DIRECTED:
            return None
        target = _destination(step, observation)
        if target is None:
            return None
        if self._home is None:
            return _refuse(
                CriticRule.OUTSIDE_HOME_RADIUS,
                ReasonCode.PRECONDITION_FAILED,
                "this step travels and no home point is set, so there is no radius for me "
                "to stay inside.",
                step_id=step.step_id,
            )
        distance = _distance_from(self._home, target)
        if distance is None or distance > self._home_radius:
            return _refuse(
                CriticRule.OUTSIDE_HOME_RADIUS,
                ReasonCode.TARGET_OUT_OF_RANGE,
                f"this step would put me outside the {self._home_radius}-square home radius; "
                "tell me to go and I will.",
                step_id=step.step_id,
            )
        return None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _destination(step: PlanStep, observation: Observation) -> Position | None:
    """Where a travelling step would end up, or None when it does not travel.

    A ``move_near`` whose object the observation does not carry is deliberately
    *not* treated as "does not travel": the caller cannot tell where it goes, so
    it is answered with an out-of-map position, which fails the radius check.
    """
    if isinstance(step.args, MoveToArgs):
        return Position(x=float(step.args.x), y=float(step.args.y), z=step.args.z)
    if not isinstance(step.args, MoveNearArgs):
        return None
    nearby = observation.nearby
    found = (
        None
        if nearby is None
        else next((o for o in nearby.objects if o.ref == step.args.object_ref), None)
    )
    if found is None or found.position is None:
        return _UNPLACEABLE
    return found.position


#: Stands in for "the mod did not say where this is". Every axis is beyond
#: :data:`~pz_agent_core.planner.plan.PLAN_COORDINATE_LIMIT`, so a radius check
#: against it always fails — an unlocatable target is refused, never waved past.
_UNPLACEABLE: Final = Position(x=float(PLAN_COORDINATE_LIMIT + 1), y=0.0, z=0)


def _distance_from(home: HomeSquare, target: Position) -> int | None:
    """Chebyshev distance in squares, or None across a floor change.

    A home point one storey down is not "zero squares away" in any sense a
    travel budget can use, and the caller treats None as outside the radius.
    """
    if home.z != target.z:
        return None
    return max(abs(int(target.x) - home.x), abs(int(target.y) - home.y))


def _refuse(
    rule: CriticRule,
    reason_code: ReasonCode,
    detail: str,
    *,
    step_id: str | None = None,
    requires_grant: bool = False,
) -> CriticVerdict:
    return CriticVerdict(
        approved=False,
        detail=detail,
        rule=rule,
        reason_code=reason_code,
        step_id=step_id,
        requires_grant=requires_grant,
    )
