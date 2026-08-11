"""The four assisted-combat actions: equip, shove, engage, retreat.

All four ride the NEW ``combat_assist`` capability and all four are rated P4 —
the protocol's top tier, never taken on the agent's own initiative, one
explicit consent per call. The ``autonomous_attack`` capability is untouched by
this file and stays ``unsupported`` by design (§12.4): what is shipped here is
the *assisted* rung, where a human ordered the fight and the deterministic
policy in :mod:`pz_agent_core.combat.policy` gets a veto in front of every
single command.

No "attack until dead" black box exists anywhere in this file. ``combat.engage``
is ONE bounded attack window — a handful of swings inside the mod's own
declared ceilings — terminal when the window closes, whatever it observed. A
fight that needs a second window needs a second command, which means the
safety stop, the reflex guard and the user all get their turn between windows,
because between windows there is nothing running to interrupt.

The postcondition never trusts the swing. Every ``verify`` here re-reads the
*following observation* and answers only from what it says about the zombie:

=================  =========================================================
``equip_best``     the primary hand changed and now holds an observed weapon
``shove``          the target reads down, or strictly further away
``engage``         the target reads down — or is honestly gone (rule below)
``retreat``        the nearest observed zombie's distance grew
=================  =========================================================

The one subtle rule is engage's *absence*. A zombie missing from the after
observation is ambiguous: it may be destroyed, or it may have shambled out of
the reporting radius while the character swung at air. The honest reading,
fixed here and pinned by test: absence counts as "down or gone" ONLY when the
before observation had the target inside the ladder's close range (a fight,
not a sighting) AND the after observation has no zombie at all within contact
range (nothing stepped into its place). Anything softer would let a wandering
zombie be reported as a kill.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities.probes import COMBAT_ASSIST
from ...combat.policy import (
    DEFAULT_COMBAT_POLICY,
    DOWN_STATES,
    GROUP_RING,
    SHOVE_RANGE,
    CombatPolicy,
    CombatRefusal,
    EngagementDecision,
    assess_engagement,
    weapon_condition_fraction,
)
from ...protocol import (
    ActionName,
    Command,
    JsonDict,
    NearbyZombie,
    Observation,
    ReasonCode,
    RefKind,
    RiskClass,
)
from ..adapter import Evidence, PreconditionFailed
from .common import check_args, read_ref, require_inventory, require_nearby

__all__ = [
    "DEFAULT_ENGAGE_TIMEOUT_MS",
    "DEFAULT_EQUIP_BEST_TIMEOUT_MS",
    "DEFAULT_RETREAT_TIMEOUT_MS",
    "DEFAULT_SHOVE_TIMEOUT_MS",
    "WEAPON_CATEGORY",
    "CombatEngageAdapter",
    "CombatEquipBestAdapter",
    "CombatRetreatAdapter",
    "CombatShoveAdapter",
]

#: How the observation categorises a weapon item. The equip verify accepts a
#: readable weapon block *or* this category, because a build may report the
#: category without the condition block; the engage policy stays stricter — a
#: category proves what the item is, never what state it is in.
WEAPON_CATEGORY: Final = "weapon"

#: One in-inventory swap plus at most a step: no travel, so the tightest
#: budget in the file.
DEFAULT_EQUIP_BEST_TIMEOUT_MS: Final = 10_000
#: One shove animation plus the observation that reports its outcome.
DEFAULT_SHOVE_TIMEOUT_MS: Final = 6_000
#: One bounded attack window — a handful of swings, and the ceiling on the
#: whole window in milliseconds. Deliberately far below every other mutating
#: adapter's budget: a window that needs longer than this is a fight the
#: mission must re-decide, not an attempt to keep polling.
DEFAULT_ENGAGE_TIMEOUT_MS: Final = 8_000
#: One short bounded step away from the threats.
DEFAULT_RETREAT_TIMEOUT_MS: Final = 15_000

DEFAULT_COMBAT_POLL_MS: Final = 200

#: CombatRefusal → the protocol reason a validation refusal reports. The
#: policy's vocabulary is the mission's; the wire speaks reason codes, and
#: this table is the one place the two are joined so they cannot drift
#: per-adapter. Total over the enum — pinned by a test.
_REFUSAL_REASONS: Final[dict[CombatRefusal, ReasonCode]] = {
    CombatRefusal.TARGET_NOT_OBSERVED: ReasonCode.TARGET_NOT_LOADED,
    CombatRefusal.TARGET_UNREACHABLE: ReasonCode.TARGET_OUT_OF_RANGE,
    CombatRefusal.GROUP_OVER_LIMIT: ReasonCode.POLICY_DENIED,
    CombatRefusal.ENDURANCE_CRITICAL: ReasonCode.POLICY_DENIED,
    CombatRefusal.PANIC_CRITICAL: ReasonCode.POLICY_DENIED,
    CombatRefusal.WOUNDED: ReasonCode.POLICY_DENIED,
    CombatRefusal.WEAPON_UNUSABLE: ReasonCode.PRECONDITION_FAILED,
}


@dataclass(frozen=True, slots=True)
class _TargetSpec:
    """``target_ref`` — the one argument shove and engage share."""

    target_ref: str

    @classmethod
    def parse(cls, command: Command) -> _TargetSpec:
        check_args(command, allowed=("target_ref",), required=("target_ref",))
        return cls(target_ref=read_ref(command, "target_ref", kind=RefKind.ZOMBIE))

    def as_args(self) -> JsonDict:
        return {"target_ref": self.target_ref}


def _refuse_from(decision: EngagementDecision) -> PreconditionFailed:
    """The policy's refusal as the sidecar-side precondition failure it is.

    Raised in ``validate`` so the command never leaves this process: the
    refusal happens before any wire traffic, with the policy's own token and
    observed numbers as evidence.
    """
    refusal = decision.refusal
    assert refusal is not None  # only ever called on a refused decision
    return PreconditionFailed(
        f"combat policy refused: {decision.detail}",
        reason_code=_REFUSAL_REASONS[refusal],
        evidence={
            "refusal": refusal.value,
            "group_count": decision.group_count,
            "target_distance": decision.target_distance,
        },
    )


def _zombie_in(observation: Observation, target_ref: str) -> NearbyZombie | None:
    if observation.nearby is None:
        return None
    return next((z for z in observation.nearby.zombies if z.ref == target_ref), None)


def _nearest_zombie_distance(observation: Observation) -> float | None:
    if observation.nearby is None:
        return None
    return min((max(0.0, z.distance) for z in observation.nearby.zombies), default=None)


def _is_observed_weapon(observation: Observation, item_ref: str) -> bool:
    """Whether the observation itself calls this item a weapon.

    A readable weapon block is the strong answer; the item category is the
    fallback for builds that report the kind without the condition. Neither
    found means the fact cannot be checked, and an unverifiable equip is one
    ``verify`` declines rather than asserts.
    """
    inventory = observation.inventory
    item = None if inventory is None else inventory.item(item_ref)
    if item is None:
        return False
    if weapon_condition_fraction(item) is not None:
        return True
    return item.category.casefold() == WEAPON_CATEGORY


@dataclass(frozen=True, slots=True)
class CombatEquipBestAdapter:
    """``combat.equip_best``: the mod's deterministic best-weapon draw.

    Which weapon is "best" is decided by the mod's own adapter over the real
    inventory objects (reach, condition, two-hand fit — facts the observation
    reports partially at best); this side's job is the honest frame around
    that: refuse when a weapon is already held (an unchanged hand could not be
    told apart from a failed draw), and verify by the one observable fact —
    the primary hand *changed* and now holds something the observation itself
    calls a weapon.
    """

    timeout_ms: int = DEFAULT_EQUIP_BEST_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_COMBAT_POLL_MS

    name: ClassVar[ActionName] = ActionName.COMBAT_EQUIP_BEST
    risk: ClassVar[RiskClass] = RiskClass.P4
    required_capability: ClassVar[str | None] = COMBAT_ASSIST

    def validate(self, command: Command, observation: Observation) -> None:
        check_args(command, allowed=())
        require_inventory(observation)
        held = observation.player.hands.primary
        if held is not None and _is_observed_weapon(observation, held):
            raise PreconditionFailed(
                "a weapon is already held in the primary hand; the postcondition "
                "(the hand changing to a weapon) could not distinguish success "
                "from nothing happening",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"primary": held},
            )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return {}

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        held = after.player.hands.primary
        if held is None or held == before.player.hands.primary:
            return None
        if not _is_observed_weapon(after, held):
            # The hand changed to something the observation cannot call a
            # weapon: nothing here can claim the postcondition, whatever the
            # mod believes it drew.
            return None
        item = after.inventory.item(held) if after.inventory is not None else None
        return Evidence(
            kind="weapon_equipped",
            observation_seq=after.seq,
            observed={
                "item_ref": held,
                "primary_before": before.player.hands.primary,
                "primary_after": held,
                "condition_fraction": (None if item is None else weapon_condition_fraction(item)),
            },
        )


@dataclass(frozen=True, slots=True)
class CombatShoveAdapter:
    """``combat.shove``: push the named zombie, prove it from the next look.

    The least-assumptive combat action — no weapon, no damage claim — which
    is exactly why it is the capability probe's confirmation command. Every
    policy gate except the weapon gate runs in ``validate``, sidecar-side,
    before any command exists to send.
    """

    timeout_ms: int = DEFAULT_SHOVE_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_COMBAT_POLL_MS
    policy: CombatPolicy = DEFAULT_COMBAT_POLICY

    name: ClassVar[ActionName] = ActionName.COMBAT_SHOVE
    risk: ClassVar[RiskClass] = RiskClass.P4
    required_capability: ClassVar[str | None] = COMBAT_ASSIST

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _TargetSpec.parse(command)
        require_nearby(observation)
        decision = assess_engagement(
            observation, spec.target_ref, self.policy, weapon_required=False
        )
        if decision.refused:
            raise _refuse_from(decision)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _TargetSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _TargetSpec.parse(command)
        target_before = _zombie_in(before, spec.target_ref)
        target_after = _zombie_in(after, spec.target_ref)
        if target_before is None or target_after is None:
            # A shove claims no kill, so a vanished zombie proves nothing for
            # it — and with no before-reading there is no "further away".
            return None
        state = target_after.state
        down = state is not None and state in DOWN_STATES
        pushed = target_after.distance > target_before.distance
        if not down and not pushed:
            return None
        return Evidence(
            kind="zombie_shoved",
            observation_seq=after.seq,
            observed={
                "target_ref": spec.target_ref,
                "state_after": state,
                "distance_before": max(0.0, target_before.distance),
                "distance_after": max(0.0, target_after.distance),
            },
        )


@dataclass(frozen=True, slots=True)
class CombatEngageAdapter:
    """``combat.engage``: ONE bounded attack window against one named zombie.

    Terminal after the window: success is the re-observed zombie down (or
    honestly gone, under the absence rule in the module docstring), and
    anything else is the engine's ordinary timeout or the mod's own refusal.
    There is no retry policy, no "until dead", and no second window behind
    this command's id — a mission that wants another window issues another
    command, through every gate again.
    """

    timeout_ms: int = DEFAULT_ENGAGE_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_COMBAT_POLL_MS
    policy: CombatPolicy = DEFAULT_COMBAT_POLICY

    name: ClassVar[ActionName] = ActionName.COMBAT_ENGAGE
    risk: ClassVar[RiskClass] = RiskClass.P4
    required_capability: ClassVar[str | None] = COMBAT_ASSIST

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _TargetSpec.parse(command)
        require_nearby(observation)
        require_inventory(observation)  # the weapon gate reads it
        decision = assess_engagement(observation, spec.target_ref, self.policy)
        if decision.refused:
            raise _refuse_from(decision)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _TargetSpec.parse(command).as_args()

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _TargetSpec.parse(command)
        if after.nearby is None:
            # Out of view is not down — the door adapters' rule, at higher
            # stakes: with no nearby tier nothing can be claimed at all.
            return None
        target_before = _zombie_in(before, spec.target_ref)
        target_after = _zombie_in(after, spec.target_ref)
        distance_before = None if target_before is None else max(0.0, target_before.distance)
        if target_after is not None:
            state = target_after.state
            if state is None or state not in DOWN_STATES:
                return None
            return Evidence(
                kind="zombie_down",
                observation_seq=after.seq,
                observed={
                    "target_ref": spec.target_ref,
                    "outcome": "down",
                    "state_after": state,
                    "distance_before": distance_before,
                    "distance_after": max(0.0, target_after.distance),
                },
            )
        # The absence rule, exactly as documented: the before observation must
        # place the target inside the ladder's close range (this was a fight),
        # and the after observation must show no zombie within contact range
        # (nothing stepped into its place). Anything else stays unproven.
        if distance_before is None or distance_before > GROUP_RING:
            return None
        nearest_after = _nearest_zombie_distance(after)
        if nearest_after is not None and nearest_after <= SHOVE_RANGE:
            return None
        return Evidence(
            kind="zombie_down",
            observation_seq=after.seq,
            observed={
                "target_ref": spec.target_ref,
                "outcome": "gone",
                "state_after": None,
                "distance_before": distance_before,
                "distance_after": None,
            },
        )


@dataclass(frozen=True, slots=True)
class CombatRetreatAdapter:
    """``combat.retreat``: one short bounded step away, proven by distance.

    The between-windows disengage — deliberately not a journey: the avoid
    mission owns real retreats with routing and safe zones, and this action
    exists so a combat mission that must stop fighting can open the gap *now*
    with a single bounded command. The postcondition is the only honest one
    a step away has: the nearest observed zombie reads strictly further in
    the following observation (or nothing is observed at all any more).
    """

    timeout_ms: int = DEFAULT_RETREAT_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_COMBAT_POLL_MS

    name: ClassVar[ActionName] = ActionName.COMBAT_RETREAT
    risk: ClassVar[RiskClass] = RiskClass.P4
    required_capability: ClassVar[str | None] = COMBAT_ASSIST

    def validate(self, command: Command, observation: Observation) -> None:
        check_args(command, allowed=())
        nearby = require_nearby(observation)
        if not nearby.zombies:
            raise PreconditionFailed(
                "no zombie is observed, so there is nothing to retreat from and "
                "no distance whose growth could prove the retreat",
                reason_code=ReasonCode.PRECONDITION_FAILED,
                evidence={"observation_seq": observation.seq},
            )

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return {}

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        nearest_before = _nearest_zombie_distance(before)
        if nearest_before is None or after.nearby is None:
            return None
        nearest_after = _nearest_zombie_distance(after)
        if nearest_after is not None and nearest_after <= nearest_before:
            return None
        # Either the gap strictly opened, or no zombie is observed at all any
        # more — which, with one observed before, is the gap opening past the
        # reporting radius.
        return Evidence(
            kind="distance_opened",
            observation_seq=after.seq,
            observed={
                "nearest_before": nearest_before,
                "nearest_after": nearest_after,
                "zombies_after": len(after.nearby.zombies),
            },
        )
