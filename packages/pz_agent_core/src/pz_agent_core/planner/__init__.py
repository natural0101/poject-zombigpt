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
same critic and executor as anything else would. The model-backed providers in
:mod:`pz_agent_core.planner.providers` join at the same seam and get no more
trust for it: their answers reach the executor only as a ``Plan`` the parser
built and the critic approved.
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
from .providers import (
    DEFAULT_TRANSPORT_CONFIG,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_TEAMON,
    CredentialUnavailable,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    InvalidEndpoint,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    StdlibHttpTransport,
    TeamONConfig,
    TeamONHealth,
    TeamONProvider,
    TransportConfig,
    TransportError,
    key_from_env,
)

__all__ = [
    "ACTION_RISK",
    "AMBIGUOUS_CODES",
    "DEFAULT_EXECUTOR_CONFIG",
    "DEFAULT_TRANSPORT_CONFIG",
    "MAX_ENGINE_CALLS",
    "MAX_PLAN_STEPS",
    "PLANNABLE_ACTIONS",
    "PLAN_SCHEMA_VERSION",
    "PROVIDER_NONE",
    "PROVIDER_OPENAI_COMPATIBLE",
    "PROVIDER_TEAMON",
    "ConsumeArgs",
    "CredentialUnavailable",
    "CriticRule",
    "CriticVerdict",
    "ExecutorConfig",
    "FailureMode",
    "Goal",
    "GoalKind",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "InvalidEndpoint",
    "ItemArgs",
    "MoveNearArgs",
    "MoveToArgs",
    "NullProvider",
    "OpenAICompatibleConfig",
    "OpenAICompatibleProvider",
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
    "StdlibHttpTransport",
    "StepArgs",
    "StepFailure",
    "StepReport",
    "StepState",
    "SuccessCriterion",
    "SuccessKind",
    "TeamONConfig",
    "TeamONHealth",
    "TeamONProvider",
    "TransferArgs",
    "TransportConfig",
    "TransportError",
    "WaitArgs",
    "key_from_env",
    "step_signature",
]
