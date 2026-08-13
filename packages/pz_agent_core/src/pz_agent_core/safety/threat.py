"""Deterministic threat assessment: how bad is it, right now, and why.

The rule this module exists to encode is the one a distance check gets wrong:
**a distant zombie that is chasing outranks a closer one that has not noticed
the player.** A zombie that is coming for you will arrive; one that is standing
still at half the distance will not, and treating the second as the emergency is
how an assistant learns to cry wolf until the user stops listening.

So the ladder is evaluated per zombie against three inputs — is it chasing, can
it see the player, how far away is it — and the resulting levels are combined.
Counts escalate (a pack is worse than the sum of its members), a different floor
caps the contribution (the stairs buy time), and bleeding raises the floor
because the character is already losing a fight they have not started.

Every number here is a starting point for tests, exactly as §17 says, which is
why they all live in :class:`ThreatConfig` rather than in the branches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..protocol import DangerLevel, NearbyView, NearbyZombie, PlayerState

_LADDER: Final = (
    DangerLevel.NONE,
    DangerLevel.LOW,
    DangerLevel.MEDIUM,
    DangerLevel.HIGH,
    DangerLevel.CRITICAL,
)


@dataclass(frozen=True, slots=True)
class ThreatConfig:
    """Tunable thresholds for :func:`assess_threat`.

    Distances are in tiles, measured the way the observation reports them.
    """

    #: Contact range: a chasing zombie this close is already a fight.
    critical_distance: float = 2.0
    #: Reaction range: a chasing zombie this close arrives before a long action
    #: can finish.
    close_distance: float = 6.0
    #: Awareness range: beyond this a zombie is a fact, not a situation.
    alert_distance: float = 15.0
    #: Chasing zombies needed before the pack itself escalates the level.
    chasing_horde: int = 3
    #: Chasing zombies needed before it escalates twice.
    chasing_swarm: int = 6
    #: Zombies within :attr:`alert_distance` that make a crowd dangerous even
    #: when none of them has noticed the player yet.
    crowd_count: int = 8
    #: A zombie on another floor has to path around; it cannot contribute more
    #: than this on its own.
    other_floor_cap: DangerLevel = DangerLevel.MEDIUM
    #: An actively bleeding character is never in a "nothing is happening"
    #: situation, even in an empty room.
    bleeding_floor: DangerLevel = DangerLevel.LOW

    def __post_init__(self) -> None:
        distances = (self.critical_distance, self.close_distance, self.alert_distance)
        if not 0 < distances[0] < distances[1] < distances[2]:
            raise ValueError(f"threat distances must be positive and increasing, got {distances}")
        if not 1 <= self.chasing_horde <= self.chasing_swarm:
            raise ValueError(
                f"chasing_horde must be within 1..chasing_swarm, got {self.chasing_horde}"
            )
        if self.crowd_count < 1:
            raise ValueError(f"crowd_count must be positive, got {self.crowd_count}")


DEFAULT_THREAT_CONFIG: Final = ThreatConfig()


@dataclass(frozen=True, slots=True)
class ThreatAssessment:
    """The level plus the evidence that produced it.

    The evidence is not decoration: a safety event has to be able to say *why*
    it interrupted the player, and recomputing the reason at the point of
    speaking would risk saying something the decision was not based on.
    """

    level: DangerLevel
    zombie_count: int
    chasing_count: int
    #: Zombies whose intent the build could not report. Counted separately so
    #: the spoken reason never says "three chasing" about one observed chaser
    #: and two the reader could not answer for.
    chasing_unknown_count: int
    nearest_distance: float | None
    nearest_chasing_distance: float | None
    reason: str


def assess_danger(
    nearby: NearbyView,
    player: PlayerState,
    config: ThreatConfig = DEFAULT_THREAT_CONFIG,
) -> DangerLevel:
    """Coarse danger level for the current tick."""
    return assess_threat(nearby, player, config).level


def assess_threat(
    nearby: NearbyView,
    player: PlayerState,
    config: ThreatConfig = DEFAULT_THREAT_CONFIG,
) -> ThreatAssessment:
    """Full assessment: level, counts, distances and a speakable reason."""
    floor = player.position.z
    zombies = nearby.zombies
    chasing = [z for z in zombies if z.chasing is True]
    chasing_unknown = [z for z in zombies if z.chasing is None]
    in_alert_radius = [z for z in zombies if _distance(z) <= config.alert_distance]

    level = DangerLevel.NONE
    for zombie in zombies:
        level = max(level, _zombie_level(zombie, floor, config))

    if len(chasing) >= config.chasing_swarm:
        level = _escalate(level, 2)
    elif len(chasing) >= config.chasing_horde:
        level = _escalate(level, 1)
    if len(in_alert_radius) >= config.crowd_count:
        level = _escalate(level, 1)

    # A body the mod could not read is not a body with nothing wrong with it.
    # The wound list is empty in both cases, so the floor keys on the mod's own
    # declaration too -- the cost is a floor held on a build whose body reader
    # is absent, the other direction is the clock already running unnoticed.
    if player.is_bleeding or player.wounds_unread:
        level = max(level, config.bleeding_floor)
        if chasing:
            # Bleeding while something is actively closing in is the case the
            # blueprint ranks above a plain chase: the clock is already running.
            level = _escalate(level, 1)

    nearest = min((_distance(z) for z in zombies), default=None)
    nearest_chasing = min((_distance(z) for z in chasing), default=None)
    return ThreatAssessment(
        level=level,
        zombie_count=len(zombies),
        chasing_count=len(chasing),
        chasing_unknown_count=len(chasing_unknown),
        nearest_distance=nearest,
        nearest_chasing_distance=nearest_chasing,
        reason=_reason(
            level,
            zombie_count=len(zombies),
            chasing_count=len(chasing),
            nearest=nearest,
            nearest_chasing=nearest_chasing,
            bleeding=player.is_bleeding,
            wounds_unread=player.wounds_unread,
        ),
    )


def _distance(zombie: NearbyZombie) -> float:
    # A negative distance is nonsense the mod should never send; clamping is
    # safer than letting it slide under every threshold at once.
    return max(0.0, zombie.distance)


def _zombie_level(zombie: NearbyZombie, player_floor: int, config: ThreatConfig) -> DangerLevel:
    """One zombie's contribution.

    The three ladders differ by a full rung at every band, which is what encodes
    "chasing beats close": a chaser anywhere inside the alert radius is at least
    MEDIUM, while an unaware zombie only reaches MEDIUM at contact range.
    """
    distance = _distance(zombie)
    # ``None`` -- the build could not say -- takes this branch with ``True``. The
    # guard exists for the case nobody can rule out, and the cost is a more
    # cautious agent on a build with no ``getTarget``; the other direction is a
    # chaser at contact range assessed as an unaware one.
    if zombie.chasing is not False:
        if distance <= config.critical_distance:
            level = DangerLevel.CRITICAL
        elif distance <= config.close_distance:
            level = DangerLevel.HIGH
        elif distance <= config.alert_distance:
            level = DangerLevel.MEDIUM
        else:
            level = DangerLevel.LOW
    elif zombie.visible:
        if distance <= config.critical_distance:
            level = DangerLevel.MEDIUM
        elif distance <= config.close_distance:
            level = DangerLevel.LOW
        else:
            level = DangerLevel.NONE
    elif distance <= config.critical_distance:
        # Out of sight but at arm's length: it will notice, but it has not yet.
        level = DangerLevel.LOW
    else:
        level = DangerLevel.NONE

    if zombie.position is not None and zombie.position.z != player_floor:
        level = min(level, config.other_floor_cap)
    return level


def _escalate(level: DangerLevel, steps: int) -> DangerLevel:
    """Move *steps* rungs up the ladder, saturating at CRITICAL."""
    return _LADDER[min(level.rank + steps, len(_LADDER) - 1)]


def _reason(
    level: DangerLevel,
    *,
    zombie_count: int,
    chasing_count: int,
    nearest: float | None,
    nearest_chasing: float | None,
    bleeding: bool,
    wounds_unread: bool,
) -> str:
    """A short, speakable explanation. Contains no refs and no game text.

    ``bleeding`` and ``wounds_unread`` stay separate all the way to the words:
    the reason is spoken to the player, and "you are bleeding" about a body
    nobody read would be the same invented reading the floor exists to avoid.
    """
    body = "bleeding" if bleeding else ("the body could not be read" if wounds_unread else None)
    if zombie_count == 0:
        return f"{body}, nothing nearby" if body else "nothing nearby"
    parts = [f"{zombie_count} zombie(s) nearby"]
    if chasing_count and nearest_chasing is not None:
        parts.append(f"{chasing_count} chasing, nearest at {nearest_chasing:.1f} tiles")
    elif nearest is not None:
        parts.append(f"none chasing, nearest at {nearest:.1f} tiles")
    if bleeding:
        parts.append("player is bleeding")
    elif wounds_unread:
        parts.append("the player's body could not be read this tick")
    return f"{level.value}: " + "; ".join(parts)
