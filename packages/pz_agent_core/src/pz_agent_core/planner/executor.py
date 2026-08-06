"""One step at a time, against a world that is re-observed between every one.

The master prompt's Этап 6 splits planning from execution and lists what this
half owns: check the preconditions, run one step, wait for the result, update
the plan, apply a *bounded* recovery on failure, and ask or stop when uncertain.
Keeping the two apart is the whole point — a component that both proposes and
performs is a component where a proposal is already halfway to being performed.

Four rules shape the loop:

* **The world is re-read before every step.** Not the cached snapshot the plan
  was built from: a plan is a statement about the world at the moment it was
  written, and the character has been walking around since. A step whose
  precondition no longer holds is never sent — the engine would refuse it, but
  refusing it here means the mod never sees a command that was already wrong.
* **One engine call is one attempt.** Every request goes out with
  ``max_retries = 0``, so the retry budget in this module is the *only* retry
  budget. Two layers retrying independently multiply into a number neither of
  them chose.
* **Recovery is bounded three ways** — attempts per step, replans per run, and
  a hard ceiling on engine calls for the whole run. The third exists because the
  first two are per-step and a plan that keeps replanning into skipped steps
  could otherwise circle.
* **Ambiguity stops.** ``POSTCONDITION_FAILED`` and ``INTERNAL_ERROR`` mean the
  world may or may not have changed. Retrying might repeat something that
  already happened; replanning would plan against a state nobody has confirmed.
  Both are guesses, so neither is made: the run stops and reports.

The plan's :class:`~pz_agent_core.planner.plan.SuccessCriterion` is used here as
a *precondition*, never as proof. A step whose goal already holds is skipped
instead of spending an action; a step that runs is only ever marked succeeded
because :class:`~pz_agent_core.actions.ActionEngine` observed the adapter's
postcondition, which is the one place in this codebase where success is decided.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

from ..actions import ActionEngine, ActionRequest, ObservationSource
from ..actions.engine import ANY_SEQ, DEFAULT_LEASE_MS
from ..ipc.clocks import Clock, system_clock_ms
from ..policy.autonomy import DEFAULT_AUTONOMY_CONFIG, AutonomyConfig, derive_needs
from ..policy.config import DEFAULT_POLICY_CONFIG, PolicyConfig
from ..protocol import (
    PLAN_FATAL_CODES,
    RETRYABLE_CODES,
    ActionName,
    ActionResult,
    ActionStatus,
    CommandPolicy,
    JsonDict,
    Observation,
    Priority,
    ReasonCode,
    RefKind,
)
from ..safety.priority import (
    DEFAULT_ANTI_LOOP_CONFIG,
    AntiLoopConfig,
    Arbitration,
    Need,
    NeedArbiter,
    RunningPlan,
)
from .critic import PlanCritic
from .plan import FailureMode, Plan, PlanStep, StepFailure
from .provider import PlanProvider, PlanRequest

__all__ = [
    "AMBIGUOUS_CODES",
    "DEFAULT_EXECUTOR_CONFIG",
    "MAX_ENGINE_CALLS",
    "ExecutorConfig",
    "PlanExecutor",
    "PlanOutcome",
    "PlanReport",
    "StepReport",
    "StepState",
]

#: Failures after which nobody can say whether the action happened. The mod
#: claimed success and no postcondition appeared; or something in this process
#: threw while the command was already with the game. Either way the next move
#: is a question, not a retry.
AMBIGUOUS_CODES: Final[frozenset[ReasonCode]] = frozenset(
    {ReasonCode.POSTCONDITION_FAILED, ReasonCode.INTERNAL_ERROR}
)

#: Hard ceiling on engine calls for one run, whatever the per-step budgets add
#: up to. Bounded everything, including the arithmetic.
MAX_ENGINE_CALLS: Final = 32


class StepState(StrEnum):
    """Where one step ended up.

    A subset of §7.4's list: ``ready`` and ``verifying`` are states *inside*
    :meth:`~pz_agent_core.actions.ActionEngine.execute`, which owns the
    enqueue-observe-verify cycle and hands back one terminal answer. Reporting
    them here would be this module claiming to know something it never sees.
    """

    PENDING = "pending"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class PlanOutcome(StrEnum):
    """How a run ended."""

    #: Every step reached a terminal success or was skipped as already satisfied.
    COMPLETED = "completed"
    #: The plan or its replacement was refused before anything ran.
    REJECTED = "rejected"
    #: Something needs a human answer (§7.7).
    ASK_USER = "ask_user"
    #: A higher-priority need took over. §7.2: suspended, never destroyed.
    SUSPENDED = "suspended"
    #: Recovery was exhausted, or the state was too uncertain to continue.
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    """Every bound one run obeys."""

    #: Engine calls one step may consume, retries included.
    max_attempts_per_step: int = 2
    #: §7.13 allows one repair attempt; the same number applies to a replan.
    max_replans: int = 1
    max_engine_calls: int = 12
    lease_ms: int = DEFAULT_LEASE_MS
    observation_timeout_ms: int = 2_000
    #: Where the plan sits on the §7.2 ladder. A need above it may suspend it.
    plan_priority: Priority = Priority.USER_COMMAND

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts_per_step <= 4:
            raise ValueError(
                f"max_attempts_per_step must be within 1..4, got {self.max_attempts_per_step}"
            )
        if not 0 <= self.max_replans <= 2:
            raise ValueError(f"max_replans must be within 0..2, got {self.max_replans}")
        if not 1 <= self.max_engine_calls <= MAX_ENGINE_CALLS:
            raise ValueError(
                f"max_engine_calls must be within 1..{MAX_ENGINE_CALLS}, "
                f"got {self.max_engine_calls}"
            )
        if self.observation_timeout_ms <= 0:
            raise ValueError(
                f"observation_timeout_ms must be positive, got {self.observation_timeout_ms}"
            )


DEFAULT_EXECUTOR_CONFIG: Final = ExecutorConfig()


@dataclass(frozen=True, slots=True)
class StepReport:
    """What happened to one step, in the order it was attempted."""

    step_id: str
    action: ActionName
    state: StepState
    attempts: int
    detail: str
    reason_code: ReasonCode | None = None
    evidence: JsonDict | None = None

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("a step report must explain itself")
        if self.state is StepState.SUCCEEDED and not self.evidence:
            raise ValueError(
                "a succeeded step reports the observed evidence it succeeded on; there is no "
                "other way for this state to be reached"
            )


@dataclass(frozen=True, slots=True)
class PlanReport:
    """The whole run: what ran, what did not, and why it ended."""

    goal_id: str
    outcome: PlanOutcome
    detail: str
    steps: tuple[StepReport, ...] = ()
    reason_code: ReasonCode | None = None
    replans: int = 0
    engine_calls: int = 0
    #: The need that took over, on :attr:`PlanOutcome.SUSPENDED`.
    suspended_by: Need | None = None
    #: Steps of the current plan that never ran, so a caller can resume (§7.2).
    pending_step_ids: tuple[str, ...] = ()
    #: What a replan would have to avoid proposing again.
    failures: tuple[StepFailure, ...] = ()

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("a plan report must explain itself")
        settled = self.outcome in (PlanOutcome.COMPLETED, PlanOutcome.SUSPENDED)
        if settled and self.reason_code is not None:
            raise ValueError(f"a {self.outcome.value} run is not a failure and carries no code")
        if not settled and self.reason_code is None:
            raise ValueError(f"a {self.outcome.value} run must say why")
        if (self.outcome is PlanOutcome.SUSPENDED) != (self.suspended_by is not None):
            raise ValueError("a suspended run names the need that suspended it, and only it")

    @property
    def succeeded(self) -> bool:
        return self.outcome is PlanOutcome.COMPLETED


@dataclass(frozen=True, slots=True)
class _Gate:
    """The verdict of the pre-step checks: go, skip, or do not send."""

    skip: bool = False
    reason_code: ReasonCode | None = None
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return self.reason_code is not None


_GO: Final = _Gate()


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One try at one step that did not succeed, however it came to that.

    A precondition the executor refused and a failure the engine reported are
    the same thing to the recovery table — a step that did not happen and a
    reason code — so they are carried in one shape and there is only one table.
    """

    step: PlanStep
    index: int
    number: int
    reason_code: ReasonCode
    detail: str
    evidence: JsonDict | None = None


class PlanExecutor:
    """Drives an approved plan through the action engine, one step at a time."""

    def __init__(
        self,
        *,
        engine: ActionEngine,
        observations: ObservationSource,
        provider: PlanProvider,
        critic: PlanCritic,
        session_id: str,
        clock: Clock = system_clock_ms,
        config: ExecutorConfig = DEFAULT_EXECUTOR_CONFIG,
        policy: PolicyConfig = DEFAULT_POLICY_CONFIG,
        autonomy: AutonomyConfig = DEFAULT_AUTONOMY_CONFIG,
        anti_loop: AntiLoopConfig = DEFAULT_ANTI_LOOP_CONFIG,
        arbiter: NeedArbiter | None = None,
    ) -> None:
        self._engine = engine
        self._observations = observations
        self._provider = provider
        self._critic = critic
        self._session_id = session_id
        self._clock = clock
        self._config = config
        self._policy = policy
        self._autonomy = autonomy
        self._arbiter = arbiter if arbiter is not None else NeedArbiter(anti_loop)

    @property
    def arbiter(self) -> NeedArbiter:
        """The shared arbiter, so a caller can clear a need it saw resolved."""
        return self._arbiter

    def run(self, request: PlanRequest) -> PlanReport:
        """Plan for *request*, review the plan, and execute it step by step.

        Always returns a report; the engine never lets an exception escape and
        neither does this loop's recovery table.
        """
        state = _RunState(goal_id=request.goal.goal_id, failures=list(request.recent_failures))
        observation = self._fresh()
        if observation is None:
            return state.finish(
                PlanOutcome.STOPPED,
                ReasonCode.GAME_DISCONNECTED,
                f"no observation arrived within {self._config.observation_timeout_ms} ms.",
            )
        opened = self._open(request, observation, state)
        if isinstance(opened, PlanReport):
            return opened
        return self._drive(request, opened, state)

    # -- planning ----------------------------------------------------------

    def _open(
        self, request: PlanRequest, observation: Observation, state: _RunState
    ) -> Plan | PlanReport:
        """Propose and review, reporting a refusal from either as REJECTED."""
        proposal = self._provider.propose(
            replace(request, observation=observation, recent_failures=tuple(state.failures))
        )
        plan = proposal.plan
        if plan is None:
            return state.finish(
                PlanOutcome.REJECTED,
                proposal.reason_code or ReasonCode.PRECONDITION_FAILED,
                proposal.detail,
            )
        verdict = self._critic.review(plan, observation, recent_failures=tuple(state.failures))
        if verdict.refused:
            return state.finish(
                PlanOutcome.REJECTED,
                verdict.reason_code or ReasonCode.POLICY_DENIED,
                verdict.detail,
            )
        return plan

    # -- the step loop -----------------------------------------------------

    def _drive(self, request: PlanRequest, plan: Plan, state: _RunState) -> PlanReport:
        index = 0
        attempts = 0
        while index < len(plan.steps):
            step = plan.steps[index]
            if state.engine_calls >= self._config.max_engine_calls:
                return state.finish(
                    PlanOutcome.STOPPED,
                    ReasonCode.POLICY_DENIED,
                    f"this run has already spent {state.engine_calls} action(s); stopping "
                    "rather than continuing unattended.",
                    pending=plan.steps[index:],
                )

            observation = self._fresh()
            if observation is None:
                return state.finish(
                    PlanOutcome.STOPPED,
                    ReasonCode.GAME_DISCONNECTED,
                    f"the world stopped reporting after {len(state.steps)} step(s).",
                    pending=plan.steps[index:],
                )

            interrupted = self._arbitrate(plan, observation, state, pending=plan.steps[index:])
            if interrupted is not None:
                return interrupted

            gate = self._gate(step, observation)
            if gate.skip:
                state.record(
                    StepReport(
                        step_id=step.step_id,
                        action=step.action,
                        state=StepState.SKIPPED,
                        attempts=attempts,
                        detail=gate.detail,
                    )
                )
                index, attempts = index + 1, 0
                continue

            attempts += 1
            if gate.blocked:
                attempt = _Attempt(
                    step=step,
                    index=index,
                    number=attempts,
                    reason_code=gate.reason_code or ReasonCode.PRECONDITION_FAILED,
                    detail=gate.detail,
                )
            else:
                result = self._execute(plan, step, attempts)
                state.engine_calls += 1
                if result.status is ActionStatus.SUCCEEDED:
                    state.record(
                        StepReport(
                            step_id=step.step_id,
                            action=step.action,
                            state=StepState.SUCCEEDED,
                            attempts=attempts,
                            detail=result.message or "postcondition observed",
                            evidence=dict(result.evidence),
                        )
                    )
                    index, attempts = index + 1, 0
                    continue
                attempt = _Attempt(
                    step=step,
                    index=index,
                    number=attempts,
                    reason_code=result.reason_code,
                    detail=result.message or f"the engine reported {result.status.value}",
                    evidence=dict(result.evidence) or None,
                )

            recovery = self._recover(plan, attempt, state)
            if isinstance(recovery, PlanReport):
                return recovery
            if recovery is _Recovery.RETRY:
                continue
            if recovery is _Recovery.ADVANCE:
                index, attempts = index + 1, 0
                continue
            replanned = self._replan(request, state)
            if isinstance(replanned, PlanReport):
                return replanned
            plan, index, attempts = replanned, 0, 0
        return state.finish(
            PlanOutcome.COMPLETED,
            None,
            f"all {len(plan.steps)} step(s) of the plan are done.",
        )

    def _execute(self, plan: Plan, step: PlanStep, attempt: int) -> ActionResult:
        return self._engine.execute(
            ActionRequest(
                action=step.action,
                session_id=self._session_id,
                idempotency_key=f"{plan.goal_id}:{step.step_id}:{attempt}",
                args=step.args.to_payload(),
                # The engine's own retry budget is switched off on purpose: this
                # module owns recovery, and two budgets would compound.
                policy=CommandPolicy(allow_interrupt=True, max_retries=0),
                lease_ms=self._config.lease_ms,
            )
        )

    # -- recovery ----------------------------------------------------------

    def _recover(self, plan: Plan, attempt: _Attempt, state: _RunState) -> _Recovery | PlanReport:
        """Decide what a failed step means for the run.

        The order is the priority order: what ends the plan, what nobody can
        interpret, what may be tried again, and only then the step's own
        ``on_failure``.
        """
        step = attempt.step
        pending = plan.steps[attempt.index :]
        if attempt.reason_code in PLAN_FATAL_CODES:
            state.record(_failed(attempt, state=StepState.CANCELLED))
            return state.finish(
                PlanOutcome.STOPPED,
                attempt.reason_code,
                f"{attempt.detail} Nothing further runs under this plan.",
                pending=pending,
            )
        if attempt.reason_code in AMBIGUOUS_CODES:
            state.record(_failed(attempt))
            state.note(attempt)
            return state.finish(
                PlanOutcome.STOPPED,
                attempt.reason_code,
                f"{attempt.detail} I cannot tell whether that step changed anything, so I am "
                "not guessing at what to do next.",
                pending=pending,
            )
        if (
            attempt.reason_code in RETRYABLE_CODES
            and attempt.number < self._config.max_attempts_per_step
        ):
            return _Recovery.RETRY

        state.record(_failed(attempt))
        state.note(attempt)
        if step.on_failure is FailureMode.SKIP:
            return _Recovery.ADVANCE
        if step.on_failure is FailureMode.ASK_USER:
            return state.finish(
                PlanOutcome.ASK_USER,
                attempt.reason_code,
                f"{attempt.detail} I have nothing else to try for this step without you.",
                pending=pending,
            )
        if step.on_failure is FailureMode.REPLAN_ONCE and state.replans < self._config.max_replans:
            return _Recovery.REPLAN
        return state.finish(
            PlanOutcome.STOPPED,
            attempt.reason_code,
            f"{attempt.detail} Recovery for this step is spent.",
            pending=pending,
        )

    def _replan(self, request: PlanRequest, state: _RunState) -> Plan | PlanReport:
        """One fresh plan, reviewed like the first, or the run ends."""
        state.replans += 1
        observation = self._fresh()
        if observation is None:
            return state.finish(
                PlanOutcome.STOPPED,
                ReasonCode.GAME_DISCONNECTED,
                "the world stopped reporting while I was replanning.",
            )
        opened = self._open(request, observation, state)
        if isinstance(opened, PlanReport):
            # A refusal after work has already been done is not the same event as
            # a refusal before anything ran, so it is reported as a stop.
            return replace(opened, outcome=PlanOutcome.STOPPED)
        return opened

    # -- gates -------------------------------------------------------------

    def _arbitrate(
        self,
        plan: Plan,
        observation: Observation,
        state: _RunState,
        *,
        pending: tuple[PlanStep, ...],
    ) -> PlanReport | None:
        """§7.2: does something outrank the plan that is running?"""
        needs = derive_needs(observation, policy=self._policy, config=self._autonomy)
        decision = self._arbiter.arbitrate(
            RunningPlan(key=plan.goal_id, priority=self._config.plan_priority),
            needs,
            self._clock(),
        )
        if decision.outcome is Arbitration.CONTINUE or decision.serve is None:
            # The arbiter only returns SUSPEND or ABORT with a need to serve;
            # restating that here keeps the report's "a suspension names the
            # need" invariant reachable without an assertion.
            return None
        if decision.outcome is Arbitration.SUSPEND:
            return state.finish(
                PlanOutcome.SUSPENDED,
                None,
                decision.detail,
                pending=pending,
                suspended_by=decision.serve,
            )
        if decision.outcome is Arbitration.ASK_USER:
            return state.finish(
                PlanOutcome.ASK_USER, ReasonCode.NO_PROGRESS, decision.detail, pending=pending
            )
        return state.finish(
            PlanOutcome.STOPPED,
            ReasonCode.CANCELLED_BY_REQUEST,
            decision.detail,
            pending=pending,
        )

    def _gate(self, step: PlanStep, observation: Observation) -> _Gate:
        """Everything checked before a command is composed, against *observation*."""
        halt = _session_gate(observation)
        if halt is not None:
            return halt
        if step.success.holds(step.args, observation) is True:
            return _Gate(
                skip=True,
                detail=(
                    f"{step.action.value} is not needed: {step.success.kind.value} already "
                    f"holds at observation {observation.seq}."
                ),
            )
        return _ref_gate(step, observation)

    def _fresh(self) -> Observation | None:
        """Wait for an observation strictly newer than anything already seen."""
        latest = self._observations.latest()
        after = ANY_SEQ if latest is None else latest.seq
        return self._observations.wait_for_next(after, self._config.observation_timeout_ms)


class _Recovery(StrEnum):
    """What the loop does next after a step that did not succeed."""

    RETRY = "retry"
    ADVANCE = "advance"
    REPLAN = "replan"


@dataclass(slots=True)
class _RunState:
    """The mutable half of one run. Everything on it is bounded."""

    goal_id: str
    failures: list[StepFailure]
    steps: list[StepReport] = field(default_factory=list)
    replans: int = 0
    engine_calls: int = 0

    def record(self, report: StepReport) -> None:
        self.steps.append(report)

    def note(self, attempt: _Attempt) -> None:
        """Remember a step so a replan cannot propose it again unchanged."""
        self.failures.append(
            StepFailure(
                signature=attempt.step.signature,
                reason_code=attempt.reason_code,
                detail=attempt.detail,
            )
        )

    def finish(
        self,
        outcome: PlanOutcome,
        reason_code: ReasonCode | None,
        detail: str,
        *,
        pending: tuple[PlanStep, ...] = (),
        suspended_by: Need | None = None,
    ) -> PlanReport:
        return PlanReport(
            goal_id=self.goal_id,
            outcome=outcome,
            detail=detail,
            steps=tuple(self.steps),
            reason_code=reason_code,
            replans=self.replans,
            engine_calls=self.engine_calls,
            suspended_by=suspended_by,
            pending_step_ids=tuple(step.step_id for step in pending),
            failures=tuple(self.failures),
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _failed(attempt: _Attempt, *, state: StepState = StepState.FAILED) -> StepReport:
    return StepReport(
        step_id=attempt.step.step_id,
        action=attempt.step.action,
        state=state,
        attempts=attempt.number,
        detail=attempt.detail,
        reason_code=attempt.reason_code,
        evidence=attempt.evidence,
    )


def _session_gate(observation: Observation) -> _Gate | None:
    """Reasons not to send anything at all, in interrupt-hierarchy order.

    The engine checks these too. Checking them here as well is not duplication
    for its own sake: it is the difference between the mod receiving a command
    that was already wrong and the mod never hearing about it.
    """
    if not observation.player.present or not observation.player.alive:
        return _Gate(
            reason_code=ReasonCode.SESSION_TERMINATED,
            detail="there is no live character to act for.",
        )
    if observation.safety.manual_takeover:
        return _Gate(reason_code=ReasonCode.USER_TAKEOVER, detail="you have taken manual control.")
    if observation.safety.sidecar_stale:
        return _Gate(
            reason_code=ReasonCode.STALE_SESSION, detail="the mod considers this sidecar stale."
        )
    if not observation.safety.armed:
        return _Gate(reason_code=ReasonCode.NOT_ARMED, detail="automation is disarmed.")
    if observation.action.blocks_automation:
        return _Gate(
            reason_code=ReasonCode.PLAYER_BUSY_MANUAL_ACTION,
            detail=(
                f"the action queue is running a {observation.action.ownership.value} action; "
                "I will not queue behind it."
            ),
        )
    return None


def _ref_gate(step: PlanStep, observation: Observation) -> _Gate:
    """Refuse a step whose target the freshest observation no longer carries.

    Skipped entirely when the observation has no inventory tier. "The mod did
    not send an inventory this tick" is not evidence that the tin is gone, and
    the adapter re-resolves the reference against its own fresh observation
    before anything is queued.
    """
    inventory = observation.inventory
    if inventory is None:
        return _GO
    for ref in step.args.refs():
        if ref.kind is RefKind.ITEM and inventory.item(ref.value) is None:
            return _Gate(
                reason_code=ReasonCode.INVALID_REF,
                detail=(
                    f"the item this step acts on is no longer in the inventory at "
                    f"observation {observation.seq}."
                ),
            )
        if ref.kind is RefKind.CONTAINER and inventory.container(ref.value) is None:
            return _Gate(
                reason_code=ReasonCode.INVALID_REF,
                detail=(
                    f"the container this step names is not in the inventory at "
                    f"observation {observation.seq}."
                ),
            )
    return _GO
