"""Planner, critic and executor: the three halves §7.1 insists are separate.

The planner turns a goal into a :class:`~pz_agent_core.planner.plan.Plan` and
does nothing else. The critic reviews that plan against the session it would run
in and is the last gate before anything reaches the game. The executor drives it
one step at a time, re-observing between each, and owns the whole recovery
budget.

Merging any two of them is how an agent ends up executing model output, so the
split is enforced by the types: a provider can only return a ``Plan``, a ``Plan``
can only hold actions from a closed set with typed arguments, and the only route
from a step to a command is
:meth:`~pz_agent_core.planner.executor.PlanExecutor.run`.

``provider = "none"`` — :class:`~pz_agent_core.planner.provider.NullProvider` —
is a full participant here, not a fallback: it plans from the deterministic
selection policies, needs no network and no key, and goes through exactly the
same critic and executor as anything else would.
"""

from __future__ import annotations

from .critic import CriticRule, CriticVerdict, PlanCritic, RiskAssessor
from .executor import (
    AMBIGUOUS_CODES,
    DEFAULT_EXECUTOR_CONFIG,
    MAX_ENGINE_CALLS,
    ExecutorConfig,
    PlanExecutor,
    PlanOutcome,
    PlanReport,
    StepReport,
    StepState,
)
from .plan import (
    ACTION_RISK,
    MAX_PLAN_STEPS,
    PLAN_SCHEMA_VERSION,
    PLANNABLE_ACTIONS,
    ConsumeArgs,
    FailureMode,
    ItemArgs,
    MoveNearArgs,
    MoveToArgs,
    Plan,
    PlanFault,
    PlanRef,
    PlanRejected,
    PlanStep,
    ReadArgs,
    StepArgs,
    StepFailure,
    SuccessCriterion,
    SuccessKind,
    TransferArgs,
    WaitArgs,
    step_signature,
)
from .provider import (
    PROVIDER_NONE,
    Goal,
    GoalKind,
    NullProvider,
    PlanProposal,
    PlanProvider,
    PlanRequest,
)

__all__ = [
    "ACTION_RISK",
    "AMBIGUOUS_CODES",
    "DEFAULT_EXECUTOR_CONFIG",
    "MAX_ENGINE_CALLS",
    "MAX_PLAN_STEPS",
    "PLANNABLE_ACTIONS",
    "PLAN_SCHEMA_VERSION",
    "PROVIDER_NONE",
    "ConsumeArgs",
    "CriticRule",
    "CriticVerdict",
    "ExecutorConfig",
    "FailureMode",
    "Goal",
    "GoalKind",
    "ItemArgs",
    "MoveNearArgs",
    "MoveToArgs",
    "NullProvider",
    "Plan",
    "PlanCritic",
    "PlanExecutor",
    "PlanFault",
    "PlanOutcome",
    "PlanProposal",
    "PlanProvider",
    "PlanRef",
    "PlanRejected",
    "PlanReport",
    "PlanRequest",
    "PlanStep",
    "ReadArgs",
    "RiskAssessor",
    "StepArgs",
    "StepFailure",
    "StepReport",
    "StepState",
    "SuccessCriterion",
    "SuccessKind",
    "TransferArgs",
    "WaitArgs",
    "step_signature",
]
