"""Safety: threat assessment, the deterministic reflex guard, and arbitration.

This package is the part of the agent that must keep working when everything
else is unavailable. It has no IO, no third-party imports and no dependency on a
planner: §7.1 puts reflexes below the model precisely so that a missing, slow or
misbehaving provider cannot disable the rules that protect the character.
"""

from __future__ import annotations

from .priority import (
    DEFAULT_ANTI_LOOP_CONFIG,
    AntiLoopConfig,
    Arbitration,
    ArbitrationDecision,
    Need,
    NeedArbiter,
    RunningPlan,
)
from .reflex import (
    DEFAULT_REFLEX_CONFIG,
    DEFAULT_VULNERABLE_ACTIONS,
    MAX_EVENTS,
    InFlightCommand,
    ReflexConfig,
    ReflexGuard,
    ReflexSignals,
    SafetyEvent,
    may_cancel_running_action,
)
from .threat import (
    DEFAULT_THREAT_CONFIG,
    ThreatAssessment,
    ThreatConfig,
    assess_danger,
    assess_threat,
)

__all__ = [
    "DEFAULT_ANTI_LOOP_CONFIG",
    "DEFAULT_REFLEX_CONFIG",
    "DEFAULT_THREAT_CONFIG",
    "DEFAULT_VULNERABLE_ACTIONS",
    "MAX_EVENTS",
    "AntiLoopConfig",
    "Arbitration",
    "ArbitrationDecision",
    "InFlightCommand",
    "Need",
    "NeedArbiter",
    "ReflexConfig",
    "ReflexGuard",
    "ReflexSignals",
    "RunningPlan",
    "SafetyEvent",
    "ThreatAssessment",
    "ThreatConfig",
    "assess_danger",
    "assess_threat",
    "may_cancel_running_action",
]
