"""Local navigation: a bounded map of observed squares and a deterministic
route executor.

Nothing here calls a model, touches I/O or reads a clock. The LLM (or any
caller) picks a target; :class:`LocalMap` remembers what past observations
proved about the surrounding squares — walkability, doors with their tri-state
lock/barricade knowledge, stairs links, visited cells — and :class:`Journey`
turns the target into one :class:`~pz_agent_core.actions.ActionRequest` at a
time for the existing action engine, handling doors, stairs, blocked passages
and stuck detection itself. Arrival is only ever declared from an observation;
every budget is bounded and every exhausted bound is a typed refusal.
"""

from __future__ import annotations

from .executor import (
    DOOR_STEP_COST,
    MAX_CONSECUTIVE_FAILURES,
    MAX_EXPANDED_NODES,
    MAX_JOURNEY_ID_LEN,
    MAX_LEGS,
    MAX_REPLANS,
    Arrived,
    Journey,
    JourneyLimits,
    JourneyState,
    NavigationError,
    NavigationFailure,
    NavigationTarget,
    NextStep,
    Refused,
    Step,
)
from .local_map import (
    MAX_CELLS,
    SEMANTIC_OBSTACLE,
    CellSnapshot,
    DoorKnowledge,
    GridSquare,
    LocalMap,
    square_of,
)

__all__ = [
    "DOOR_STEP_COST",
    "MAX_CELLS",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_EXPANDED_NODES",
    "MAX_JOURNEY_ID_LEN",
    "MAX_LEGS",
    "MAX_REPLANS",
    "SEMANTIC_OBSTACLE",
    "Arrived",
    "CellSnapshot",
    "DoorKnowledge",
    "GridSquare",
    "Journey",
    "JourneyLimits",
    "JourneyState",
    "LocalMap",
    "NavigationError",
    "NavigationFailure",
    "NavigationTarget",
    "NextStep",
    "Refused",
    "Step",
    "square_of",
]
