"""Deterministic selection policy: what to eat, what to drink, what to read.

The working agreement puts these three decisions in code rather than in a
prompt (AGENTS.md, "Determinism where it counts"). The model may express a
goal; this package picks the item, and every pick is explainable, bounded and
reproducible — the same inventory in a different order always yields the same
answer, down to the tiebreak.

Nothing here executes anything. A selection is a value: an item reference, a
portion, a rationale and a score breakdown, or a refusal carrying the reason
each candidate failed. Turning that value into a command is the action layer's
job.
"""

from __future__ import annotations

from .config import (
    CAPABILITY_DRINK_CARRIED,
    CAPABILITY_DRINK_PERCENTAGE,
    CAPABILITY_DRINK_WORLD_SOURCE,
    CAPABILITY_EAT_PERCENTAGE,
    CAPABILITY_READ_LITERATURE,
    DEFAULT_POLICY_CONFIG,
    PolicyConfig,
)
from .drink import DrinkChoice, DrinkSelection, DrinkView, select_drink
from .food import FoodChoice, FoodSelection, FoodView, select_food
from .literature import (
    LiteratureChoice,
    LiteratureGoal,
    LiteratureGoalKind,
    LiteratureKind,
    LiteratureSelection,
    LiteratureView,
    select_literature,
)
from .selection import (
    NO_CAPABILITIES,
    CapabilityLookup,
    CapabilitySnapshot,
    Rejection,
    RejectionReason,
    ScoreBreakdown,
    ScoredCandidate,
    ScoreFactor,
)

__all__ = [
    "CAPABILITY_DRINK_CARRIED",
    "CAPABILITY_DRINK_PERCENTAGE",
    "CAPABILITY_DRINK_WORLD_SOURCE",
    "CAPABILITY_EAT_PERCENTAGE",
    "CAPABILITY_READ_LITERATURE",
    "DEFAULT_POLICY_CONFIG",
    "NO_CAPABILITIES",
    "CapabilityLookup",
    "CapabilitySnapshot",
    "DrinkChoice",
    "DrinkSelection",
    "DrinkView",
    "FoodChoice",
    "FoodSelection",
    "FoodView",
    "LiteratureChoice",
    "LiteratureGoal",
    "LiteratureGoalKind",
    "LiteratureKind",
    "LiteratureSelection",
    "LiteratureView",
    "PolicyConfig",
    "Rejection",
    "RejectionReason",
    "ScoreBreakdown",
    "ScoreFactor",
    "ScoredCandidate",
    "select_drink",
    "select_food",
    "select_literature",
]
