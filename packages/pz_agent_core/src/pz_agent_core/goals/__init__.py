"""The typed goal channel: what a user, a model or a microphone may ask for.

Everything above the core — the MCP boundary, the CLI, the voice loop, a plan
provider — expresses intent through this package and through nothing else. That
is a narrow opening on purpose: :mod:`.model` holds a closed set of kinds whose
parameters are typed enums and range-checked numbers, and :mod:`.queue` holds a
bounded channel in which one goal runs at a time, every goal reaches a terminal
state, and every refusal says what failed and what to do without quoting a
single byte the caller supplied.

The channel does not decide *how* a goal is served. Selection stays in
:mod:`pz_agent_core.policy`, arbitration stays in :mod:`pz_agent_core.safety`,
and the lifecycle of a single command stays in :mod:`pz_agent_core.actions`;
:func:`~.model.to_planner_goal` is the one-line bridge to the deterministic
planner, and it is total, so a goal that cannot be served cannot be admitted.
"""

from __future__ import annotations

from .model import (
    DEFAULT_BUDGETS,
    GOAL_SPECS,
    MAX_DETAIL_CHARS,
    MAX_EVIDENCE_KEYS,
    MAX_GOAL_STEPS,
    MAX_GOAL_WALL_MS,
    MAX_IDEMPOTENCY_KEY_LEN,
    MAX_PARSED_TOKEN_CHARS,
    MAX_PENDING_TTL_MS,
    MAX_SKILL_LEVEL,
    MIN_GOAL_WALL_MS,
    NUMERIC_RANGES,
    PARAM_NAMES,
    TERMINAL_GOAL_STATES,
    GoalAdmission,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalRecord,
    GoalRefusal,
    GoalRequest,
    GoalSpec,
    GoalState,
    GoalTransition,
    NumericRange,
    TrainableSkill,
    key_digest,
    mint_goal_id,
    normalise_evidence_keys,
    parse_kind,
    parse_skill,
    to_planner_goal,
)
from .queue import DEFAULT_MAX_OPEN, DEFAULT_MAX_REMEMBERED, GoalQueue, UnknownGoalError

__all__ = [
    "DEFAULT_BUDGETS",
    "DEFAULT_MAX_OPEN",
    "DEFAULT_MAX_REMEMBERED",
    "GOAL_SPECS",
    "MAX_DETAIL_CHARS",
    "MAX_EVIDENCE_KEYS",
    "MAX_GOAL_STEPS",
    "MAX_GOAL_WALL_MS",
    "MAX_IDEMPOTENCY_KEY_LEN",
    "MAX_PARSED_TOKEN_CHARS",
    "MAX_PENDING_TTL_MS",
    "MAX_SKILL_LEVEL",
    "MIN_GOAL_WALL_MS",
    "NUMERIC_RANGES",
    "PARAM_NAMES",
    "TERMINAL_GOAL_STATES",
    "GoalAdmission",
    "GoalBudget",
    "GoalKind",
    "GoalParams",
    "GoalQueue",
    "GoalRecord",
    "GoalRefusal",
    "GoalRequest",
    "GoalSpec",
    "GoalState",
    "GoalTransition",
    "NumericRange",
    "TrainableSkill",
    "UnknownGoalError",
    "key_digest",
    "mint_goal_id",
    "normalise_evidence_keys",
    "parse_kind",
    "parse_skill",
    "to_planner_goal",
]
