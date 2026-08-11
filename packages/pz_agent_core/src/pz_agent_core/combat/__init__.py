"""Deterministic combat policy for the ASSISTED rung, and nothing beyond it.

This package exists so that every gate in front of a swing is ordinary,
separately tested policy code — the same arrangement food selection and threat
assessment already have. It decides *whether* one observed zombie may be
engaged; it never decides to fight on its own, and nothing in it can reach the
wire. The autonomous rung stays where §12.4 left it: refused by design, with
its probe's ceiling pinned in :mod:`pz_agent_core.capabilities.probes`.
"""

from __future__ import annotations

from .policy import (
    DEFAULT_COMBAT_POLICY,
    DOWN_STATES,
    ENGAGE_RANGE,
    GROUP_RING,
    MAX_GROUP_LIMIT,
    MIN_GROUP_LIMIT,
    SHOVE_RANGE,
    CombatPolicy,
    CombatRefusal,
    EngagementDecision,
    EngagementVerdict,
    assess_engagement,
    weapon_condition_fraction,
)

__all__ = [
    "DEFAULT_COMBAT_POLICY",
    "DOWN_STATES",
    "ENGAGE_RANGE",
    "GROUP_RING",
    "MAX_GROUP_LIMIT",
    "MIN_GROUP_LIMIT",
    "SHOVE_RANGE",
    "CombatPolicy",
    "CombatRefusal",
    "EngagementDecision",
    "EngagementVerdict",
    "assess_engagement",
    "weapon_condition_fraction",
]
