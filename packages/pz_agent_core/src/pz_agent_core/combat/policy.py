"""Whether one observed zombie may be engaged, decided the boring way.

The threat directive splits into «если одиночного зомби безопасно убить —
убей, иначе отступи», and this module is the whole of the *безопасно* — a pure
function over one observation that answers "engage", "shove first" or a typed
refusal, identically every time. The model may express the goal; it never
decides the fight is safe (AGENTS.md, "Determinism where it counts"), and the
assisted rung this epic ships puts every one of these gates in front of every
single attack window, because the policy is re-run per window and each window
is its own command.

Honesty rules, in the tri-state discipline the door adapters established:

* **Every input is observed.** The zombie count, the distances, the state
  token, the vitals and the weapon's condition all come off the observation
  the caller handed in — nothing here queries, caches or predicts.
* **An absent reader is never the good reading — for what a fight spends.**
  Unreadable endurance, unreadable health and an unreadable (or absent)
  weapon condition each refuse: a swing spends exactly those three, and
  assuming any of them fine would be the "assume it works" this project
  forbids. Panic is the one deliberate exception, on the needs arbiter's own
  precedent: a build without a panic reader reports *no reading*, which is
  not a panicking character, and refusing all combat on a missing optional
  reader would gate the feature on the least essential instrument. The
  mandatory gates above still stand either way.
* **Distances are the threat ladder's.** The group ring is the ladder's
  reaction range (``close_distance``) and the shove range its contact range
  (``critical_distance``) — imported, not restated, so the band the ladder
  calls dangerous and the band this policy fights in cannot drift apart. The
  vital thresholds have no ladder numbers to borrow; they are this policy's
  own starting points, tunable per §17 and pinned by tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..protocol import ItemView, NearbyZombie, Observation
from ..safety.threat import DEFAULT_THREAT_CONFIG

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

#: The bounds on how many zombies a user may configure the policy to tolerate
#: inside the group ring. One is the directive's own word («одиночного»), and
#: three is the ladder's ``chasing_horde`` — the count at which the pack
#: itself escalates the danger level, which is exactly where "a configurable
#: limit" must stop being configurable.
MIN_GROUP_LIMIT: Final = 1
MAX_GROUP_LIMIT: Final = 3

#: The ring the group is counted in: the ladder's reaction range, inside
#: which a zombie arrives before a long action can finish. A second zombie
#: out past this ring is a fact; inside it, it is a second opponent.
GROUP_RING: Final = DEFAULT_THREAT_CONFIG.close_distance

#: How far away a target may stand and still be the subject of one attack
#: window. The same reaction range: past it, closing the gap is navigation's
#: job (a journey with its own budgets), not a swing's.
ENGAGE_RANGE: Final = DEFAULT_THREAT_CONFIG.close_distance

#: Contact range — the ladder's ``critical_distance``. A standing zombie
#: inside it gets shoved before it gets swung at, and "no zombie within
#: reach" in the engage adapter's absence rule means within this.
SHOVE_RANGE: Final = DEFAULT_THREAT_CONFIG.critical_distance

#: State tokens that mean the zombie is down or destroyed, per the observer
#: contract on :class:`~pz_agent_core.protocol.NearbyZombie.state`.
#: ``crawling`` is deliberately absent: a crawler is a body plan, not a
#: knockdown — it still lunges, and counting it as down would declare a
#: fight over while teeth are at ankle height. ``None`` (no reader) is never
#: a member of anything: an unreadable state proves nothing either way.
DOWN_STATES: Final[frozenset[str]] = frozenset({"prone", "dead"})


class EngagementVerdict(StrEnum):
    """The three answers, closed. There is no "probably"."""

    ENGAGE = "engage"
    SHOVE_FIRST = "shove_first"
    REFUSE = "refuse"


class CombatRefusal(StrEnum):
    """Why an engagement is refused. Closed tokens that reach reports and
    idempotent detail lines, so they are constants here and nowhere else."""

    GROUP_OVER_LIMIT = "group_over_limit"
    ENDURANCE_CRITICAL = "endurance_critical"
    PANIC_CRITICAL = "panic_critical"
    WOUNDED = "wounded"
    WEAPON_UNUSABLE = "weapon_unusable"
    TARGET_UNREACHABLE = "target_unreachable"
    TARGET_NOT_OBSERVED = "target_not_observed"


@dataclass(frozen=True, slots=True)
class CombatPolicy:
    """Tunable thresholds for :func:`assess_engagement`.

    Shaped like :class:`~pz_agent_core.safety.threat.ThreatConfig` for the
    same §17 reason: every number is a starting point for tests, held in one
    frozen record rather than scattered through branches. Distances are not
    fields on purpose — they are the ladder's, imported above, and a policy
    that could disagree with the ladder about what "close" means would be two
    definitions of the band the character is fighting in.
    """

    #: Zombies tolerated inside :data:`GROUP_RING`, the engaged target
    #: included. The default is the directive's «одиночного»; the ceiling is
    #: :data:`MAX_GROUP_LIMIT`.
    max_group: int = 1
    #: Endurance below this refuses: a swing spends endurance, and a
    #: character too spent to swing is one who cannot disengage either.
    min_endurance: float = 0.3
    #: Fatigue above this refuses, under the same ``endurance_critical``
    #: token — the two are one fact ("too exhausted to fight") measured on
    #: two clocks, and two tokens would ask every caller to care which clock
    #: ran out. Like panic, an absent fatigue reader is "no reading": the
    #: endurance and health gates above it are the mandatory ones.
    max_fatigue: float = 0.8
    #: Panic above this refuses. Panic is reported on the observation's own
    #: 0..1 stat convention; an absent reader is "no reading" (see module
    #: docstring), never a zero and never a refusal by itself.
    max_panic: float = 0.7
    #: A weapon below this fraction of its maximum condition is one swing
    #: from breaking mid-window, which is the worst possible time.
    min_weapon_condition_fraction: float = 0.25
    #: Health below this refuses as wounded even without observed bleeding.
    health_floor: float = 0.5

    def __post_init__(self) -> None:
        if not MIN_GROUP_LIMIT <= self.max_group <= MAX_GROUP_LIMIT:
            raise ValueError(
                f"max_group must be within {MIN_GROUP_LIMIT}..{MAX_GROUP_LIMIT}, "
                f"got {self.max_group}"
            )
        for name in (
            "min_endurance",
            "max_fatigue",
            "max_panic",
            "min_weapon_condition_fraction",
            "health_floor",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within 0.0..1.0, got {value}")


DEFAULT_COMBAT_POLICY: Final = CombatPolicy()


@dataclass(frozen=True, slots=True)
class EngagementDecision:
    """The verdict plus the observed facts that produced it.

    The facts are not decoration: a refusal has to be able to say *why*
    without recomputing anything, and the mission's report reads the counts
    straight off the decision it acted on. ``detail`` is assembled from
    constants and numbers only — no refs, no game-authored text — so it may
    travel into a goal record's detail line unquoted.
    """

    verdict: EngagementVerdict
    refusal: CombatRefusal | None
    detail: str
    #: Zombies observed inside :data:`GROUP_RING`, target included.
    group_count: int
    #: The target's observed distance, or None when it was not observed.
    target_distance: float | None
    #: Whether the target's observed state token is in :data:`DOWN_STATES`;
    #: None when the state reader is absent — never conflated with standing.
    target_down: bool | None

    def __post_init__(self) -> None:
        if (self.verdict is EngagementVerdict.REFUSE) != (self.refusal is not None):
            raise ValueError("a refusal carries its token and only a refusal does")
        if not self.detail.strip():
            raise ValueError("a decision must say what it saw")

    @property
    def refused(self) -> bool:
        return self.verdict is EngagementVerdict.REFUSE


def weapon_condition_fraction(item: ItemView) -> float | None:
    """The observed condition fraction of a weapon item, or None.

    Read from the item's ``weapon`` block — ``{"condition": n,
    "condition_max": m}`` in the observer's own units — which rides
    :attr:`~pz_agent_core.protocol.ItemView.extra` until the typed views
    grow a weapon domain. ``None`` covers every way the fraction cannot be
    read: no block, a non-numeric reading, a zero or negative maximum.
    ``None`` is *unreadable*, and the policy refuses on it — this function
    never guesses a number.
    """
    block = item.extra.get("weapon")
    if not isinstance(block, Mapping):
        return None
    condition = block.get("condition")
    maximum = block.get("condition_max")
    if isinstance(condition, bool) or not isinstance(condition, (int, float)):
        return None
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or maximum <= 0:
        return None
    return max(0.0, min(1.0, float(condition) / float(maximum)))


def assess_engagement(
    observation: Observation,
    target_ref: str,
    policy: CombatPolicy = DEFAULT_COMBAT_POLICY,
    *,
    weapon_required: bool = True,
) -> EngagementDecision:
    """One target, one observation, one deterministic answer.

    Total: every input shape yields a decision, never an exception. The gate
    order is fixed and load-bearing — the same picture always produces the
    same refusal, however many gates it fails — and runs from the world
    inward: the target, the crowd, the body, the weapon.

    ``weapon_required`` is the one difference between the engage gates and
    the shove gates: a shove is empty-handed by design, so its adapter passes
    ``False`` and keeps every other gate exactly as it stands. Only a keyword
    so no call site can transpose it with anything.
    """
    nearby = observation.nearby
    zombies: tuple[NearbyZombie, ...] = () if nearby is None else tuple(nearby.zombies)
    target = next((zombie for zombie in zombies if zombie.ref == target_ref), None)
    group_count = sum(1 for zombie in zombies if _distance(zombie) <= GROUP_RING)

    if target is None:
        return _refuse(
            CombatRefusal.TARGET_NOT_OBSERVED,
            "the target is not in the observed surroundings",
            group_count=group_count,
            target_distance=None,
            target_down=None,
        )
    distance = _distance(target)
    down = None if target.state is None else target.state in DOWN_STATES

    player = observation.player
    if target.position is not None and target.position.z != player.position.z:
        return _refuse(
            CombatRefusal.TARGET_UNREACHABLE,
            "the target is observed on another floor",
            group_count=group_count,
            target_distance=distance,
            target_down=down,
        )
    if distance > ENGAGE_RANGE:
        return _refuse(
            CombatRefusal.TARGET_UNREACHABLE,
            f"the target is observed {distance:.1f} tiles away, past the "
            f"{ENGAGE_RANGE:.1f}-tile engage range",
            group_count=group_count,
            target_distance=distance,
            target_down=down,
        )
    if group_count > policy.max_group:
        return _refuse(
            CombatRefusal.GROUP_OVER_LIMIT,
            f"{group_count} zombies observed within {GROUP_RING:.1f} tiles, "
            f"above the limit of {policy.max_group}",
            group_count=group_count,
            target_distance=distance,
            target_down=down,
        )

    if player.wounds_unread:
        # The same rule the unreadable-health refusal below applies, on the
        # reader beside it: the wound list is empty both when nothing is wrong
        # and when the mod could not read the body, and a fight is not the place
        # to spend the difference.
        return _refuse(
            CombatRefusal.WOUNDED,
            "the mod could not read the character's body this tick",
            group_count=group_count,
            target_distance=distance,
            target_down=down,
        )

    if player.is_bleeding:
        return _refuse(
            CombatRefusal.WOUNDED,
            "the character is observed bleeding",
            group_count=group_count,
            target_distance=distance,
            target_down=down,
        )
    health = _stat(observation, "health")
    if health is None or health < policy.health_floor:
        cause = (
            "health is unreadable this tick"
            if health is None
            else f"health {health:.2f} is under the {policy.health_floor:.2f} floor"
        )
        return _refuse(
            CombatRefusal.WOUNDED,
            cause,
            group_count=group_count,
            target_distance=distance,
            target_down=down,
        )
    endurance = _stat(observation, "endurance")
    if endurance is None or endurance < policy.min_endurance:
        cause = (
            "endurance is unreadable this tick"
            if endurance is None
            else f"endurance {endurance:.2f} is under the {policy.min_endurance:.2f} floor"
        )
        return _refuse(
            CombatRefusal.ENDURANCE_CRITICAL,
            cause,
            group_count=group_count,
            target_distance=distance,
            target_down=down,
        )
    fatigue = _stat(observation, "fatigue")
    if fatigue is not None and fatigue > policy.max_fatigue:
        return _refuse(
            CombatRefusal.ENDURANCE_CRITICAL,
            f"fatigue {fatigue:.2f} is over the {policy.max_fatigue:.2f} ceiling",
            group_count=group_count,
            target_distance=distance,
            target_down=down,
        )
    panic = _stat(observation, "panic")
    if panic is not None and panic > policy.max_panic:
        return _refuse(
            CombatRefusal.PANIC_CRITICAL,
            f"panic {panic:.2f} is over the {policy.max_panic:.2f} ceiling",
            group_count=group_count,
            target_distance=distance,
            target_down=down,
        )

    if weapon_required:
        weapon_gate = _weapon_gate(observation, policy)
        if weapon_gate is not None:
            return _refuse(
                CombatRefusal.WEAPON_UNUSABLE,
                weapon_gate,
                group_count=group_count,
                target_distance=distance,
                target_down=down,
            )

    if distance <= SHOVE_RANGE and down is not True:
        # Standing — or unreadable, which must never be read as safely down —
        # at contact range: knock it over before swinging at it. A target
        # observed down skips the shove; the window finishes it on the floor.
        return EngagementDecision(
            verdict=EngagementVerdict.SHOVE_FIRST,
            refusal=None,
            detail=f"target at {distance:.1f} tiles, standing at contact range: shove first",
            group_count=group_count,
            target_distance=distance,
            target_down=down,
        )
    return EngagementDecision(
        verdict=EngagementVerdict.ENGAGE,
        refusal=None,
        detail=f"target at {distance:.1f} tiles, group of {group_count}: one bounded window",
        group_count=group_count,
        target_distance=distance,
        target_down=down,
    )


def _weapon_gate(observation: Observation, policy: CombatPolicy) -> str | None:
    """Why the held weapon cannot back a window, or None when it can.

    Absent is never usable: an empty primary hand refuses, because a
    bare-handed "attack window" is a sequence of punches nobody asked for.
    Unreadable refuses too — the item not being resolvable in the observed
    inventory, or resolving without a readable condition, are both "the one
    fact this gate exists to check cannot be checked".
    """
    held = observation.player.hands.primary
    if held is None:
        return "no weapon is held in the primary hand; absent is never usable"
    inventory = observation.inventory
    item = None if inventory is None else inventory.item(held)
    if item is None:
        return "the held item is not in the observed inventory, so its condition is unreadable"
    fraction = weapon_condition_fraction(item)
    if fraction is None:
        return "the held item's weapon condition is unreadable; unreadable is never usable"
    if fraction < policy.min_weapon_condition_fraction:
        return (
            f"the held weapon is at {fraction:.2f} of its condition, under the "
            f"{policy.min_weapon_condition_fraction:.2f} floor"
        )
    return None


def _refuse(
    refusal: CombatRefusal,
    detail: str,
    *,
    group_count: int,
    target_distance: float | None,
    target_down: bool | None,
) -> EngagementDecision:
    return EngagementDecision(
        verdict=EngagementVerdict.REFUSE,
        refusal=refusal,
        detail=detail,
        group_count=group_count,
        target_distance=target_distance,
        target_down=target_down,
    )


def _distance(zombie: NearbyZombie) -> float:
    # The threat ladder's own clamp, for the ladder's own reason: a negative
    # distance is nonsense that must not slide under every threshold at once.
    return max(0.0, zombie.distance)


def _stat(observation: Observation, name: str) -> float | None:
    """One vital as a number, or None when the mod did not report it.

    The care missions' reader, restated here because this package must not
    import the CLI: "not reported" and "zero" are different facts, and the
    convenience properties on :class:`~pz_agent_core.protocol.PlayerState`
    deliberately read the first as a default this policy must not trust.
    """
    if name not in observation.player.stats:
        return None
    value = observation.player.stats[name]
    if value is None or isinstance(value, bool):
        return None
    return float(value)
