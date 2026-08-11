"""The deterministic engagement policy: every gate, both directions.

The combat policy is the whole of «безопасно» in the threat directive, so
every refusal token has a test that produces it and a neighbouring world one
field away that does not — a gate that cannot be shown firing on exactly its
own fact is a gate that could be firing on anything.
"""

from __future__ import annotations

from typing import Any

import pytest

from pz_agent_core.combat import (
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
from pz_agent_core.protocol import (
    Hands,
    ItemView,
    NearbyView,
    NearbyZombie,
    Observation,
    Position,
    Wound,
)
from pz_agent_core.safety.threat import DEFAULT_THREAT_CONFIG
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.adapter_worlds import HOME_X, HOME_Y, a_world, an_item

WEAPON_REF = f"item:{DEFAULT_SESSION}:player-main:w1:0"
TARGET_REF = f"zombie:{DEFAULT_SESSION}:z1:0"


def a_zombie(
    rid: str = "z1",
    distance: float = 1.5,
    *,
    chasing: bool = False,
    visible: bool = True,
    state: str | None = "standing",
    z: int = 0,
    with_position: bool = True,
) -> NearbyZombie:
    return NearbyZombie(
        ref=f"zombie:{DEFAULT_SESSION}:{rid}:0",
        distance=distance,
        visible=visible,
        chasing=chasing,
        position=(
            Position(x=float(HOME_X) + distance, y=float(HOME_Y), z=z) if with_position else None
        ),
        state=state,
    )


def a_weapon(
    condition: float | None = 8, condition_max: float | None = 10, **overrides: Any
) -> ItemView:
    block: dict[str, Any] = {}
    if condition is not None:
        block["condition"] = condition
    if condition_max is not None:
        block["condition_max"] = condition_max
    return an_item(
        runtime_id="w1",
        full_type="Base.BaseballBat",
        display_name="Baseball Bat",
        category="Weapon",
        extra={"weapon": block} if block else {},
        **overrides,
    )


def observed(
    *,
    zombies: list[NearbyZombie] | None = None,
    weapon: Any | None = None,
    stats: dict[str, Any] | None = None,
    wounds: list[Wound] | None = None,
    primary: str | None = WEAPON_REF,
    no_nearby: bool = False,
    no_inventory: bool = False,
) -> Observation:
    """A world in which the default engagement is approved.

    One standing zombie at contact range, a sound bat in the primary hand,
    healthy vitals from the fixture defaults. Each refusal test changes the
    one field its gate reads.
    """
    item = weapon if weapon is not None else a_weapon()
    merged_stats = dict(stats or {})
    world = a_world(
        items=[item],
        hands=Hands(primary=primary),
        stats=merged_stats,
        nearby=NearbyView(zombies=list(zombies if zombies is not None else [a_zombie()])),
        no_nearby=no_nearby,
        no_inventory=no_inventory,
    )
    if wounds:
        world.player.wounds.extend(wounds)
    return world


def decide(observation: Observation, *, weapon_required: bool = True) -> EngagementDecision:
    return assess_engagement(
        observation, TARGET_REF, DEFAULT_COMBAT_POLICY, weapon_required=weapon_required
    )


# ---------------------------------------------------------------------------
# the distances are the ladder's, not restatements
# ---------------------------------------------------------------------------


def test_the_rings_are_imported_from_the_threat_ladder() -> None:
    assert DEFAULT_THREAT_CONFIG.close_distance == GROUP_RING
    assert DEFAULT_THREAT_CONFIG.close_distance == ENGAGE_RANGE
    assert DEFAULT_THREAT_CONFIG.critical_distance == SHOVE_RANGE


def test_crawling_is_not_a_down_state() -> None:
    # A crawler still lunges; counting it down would end fights early.
    assert "crawling" not in DOWN_STATES
    assert {"prone", "dead"} == DOWN_STATES


# ---------------------------------------------------------------------------
# the policy record itself
# ---------------------------------------------------------------------------


def test_max_group_is_bounded_one_to_three() -> None:
    assert MIN_GROUP_LIMIT == 1 and MAX_GROUP_LIMIT == 3
    assert CombatPolicy(max_group=3).max_group == 3
    with pytest.raises(ValueError, match="max_group"):
        CombatPolicy(max_group=0)
    with pytest.raises(ValueError, match="max_group"):
        CombatPolicy(max_group=4)


@pytest.mark.parametrize(
    "field_name",
    ["min_endurance", "max_fatigue", "max_panic", "min_weapon_condition_fraction", "health_floor"],
)
def test_every_fraction_field_is_bounded(field_name: str) -> None:
    over: dict[str, Any] = {field_name: 1.5}
    under: dict[str, Any] = {field_name: -0.1}
    with pytest.raises(ValueError, match=field_name):
        CombatPolicy(**over)
    with pytest.raises(ValueError, match=field_name):
        CombatPolicy(**under)


def test_the_default_limit_is_the_directives_single_zombie() -> None:
    assert DEFAULT_COMBAT_POLICY.max_group == 1


def test_a_decision_is_a_refusal_exactly_when_it_carries_a_token() -> None:
    with pytest.raises(ValueError, match="refusal"):
        EngagementDecision(
            verdict=EngagementVerdict.REFUSE,
            refusal=None,
            detail="broken",
            group_count=0,
            target_distance=None,
            target_down=None,
        )
    with pytest.raises(ValueError, match="refusal"):
        EngagementDecision(
            verdict=EngagementVerdict.ENGAGE,
            refusal=CombatRefusal.WOUNDED,
            detail="broken",
            group_count=0,
            target_distance=None,
            target_down=None,
        )


# ---------------------------------------------------------------------------
# the happy paths
# ---------------------------------------------------------------------------


def test_a_standing_zombie_at_contact_range_is_shove_first() -> None:
    decision = decide(observed())
    assert decision.verdict is EngagementVerdict.SHOVE_FIRST
    assert decision.refusal is None
    assert decision.group_count == 1
    assert decision.target_distance == 1.5


def test_an_unreadable_state_at_contact_range_still_shoves_first() -> None:
    # None must never be read as "safely down": the shove is the answer for
    # anything not observed down.
    decision = decide(observed(zombies=[a_zombie(state=None)]))
    assert decision.verdict is EngagementVerdict.SHOVE_FIRST
    assert decision.target_down is None


def test_a_prone_target_at_contact_range_engages_without_a_shove() -> None:
    decision = decide(observed(zombies=[a_zombie(state="prone")]))
    assert decision.verdict is EngagementVerdict.ENGAGE
    assert decision.target_down is True


def test_a_standing_target_past_contact_range_engages_directly() -> None:
    decision = decide(observed(zombies=[a_zombie(distance=4.0)]))
    assert decision.verdict is EngagementVerdict.ENGAGE


def test_the_same_picture_always_yields_the_same_decision() -> None:
    first = decide(observed())
    second = decide(observed())
    assert first == second


# ---------------------------------------------------------------------------
# the refusals, one field each
# ---------------------------------------------------------------------------


def test_an_unobserved_target_refuses_target_not_observed() -> None:
    decision = decide(observed(zombies=[a_zombie(rid="other")]))
    assert decision.refusal is CombatRefusal.TARGET_NOT_OBSERVED


def test_a_missing_nearby_tier_refuses_target_not_observed() -> None:
    decision = decide(observed(no_nearby=True))
    assert decision.refusal is CombatRefusal.TARGET_NOT_OBSERVED


def test_a_target_past_the_engage_range_refuses_unreachable() -> None:
    decision = decide(observed(zombies=[a_zombie(distance=ENGAGE_RANGE + 0.5)]))
    assert decision.refusal is CombatRefusal.TARGET_UNREACHABLE


def test_a_target_on_another_floor_refuses_unreachable() -> None:
    decision = decide(observed(zombies=[a_zombie(z=1)]))
    assert decision.refusal is CombatRefusal.TARGET_UNREACHABLE


def test_a_second_zombie_in_the_ring_refuses_group_over_limit() -> None:
    decision = decide(observed(zombies=[a_zombie(), a_zombie(rid="z2", distance=GROUP_RING - 0.5)]))
    assert decision.refusal is CombatRefusal.GROUP_OVER_LIMIT
    assert decision.group_count == 2


def test_a_second_zombie_outside_the_ring_is_a_fact_not_an_opponent() -> None:
    decision = decide(observed(zombies=[a_zombie(), a_zombie(rid="z2", distance=GROUP_RING + 1.0)]))
    assert decision.refusal is None


def test_a_wider_limit_admits_the_pair_and_is_capped_at_three() -> None:
    observation = observed(zombies=[a_zombie(), a_zombie(rid="z2", distance=GROUP_RING - 0.5)])
    wide = CombatPolicy(max_group=2)
    assert assess_engagement(observation, TARGET_REF, wide).refusal is None


def test_observed_bleeding_refuses_wounded() -> None:
    decision = decide(
        observed(
            wounds=[
                Wound(
                    ref=f"wound:{DEFAULT_SESSION}:Hand_L:0",
                    kind="scratch",
                    severity=0.4,
                    bleeding=True,
                )
            ]
        )
    )
    assert decision.refusal is CombatRefusal.WOUNDED


def test_health_under_the_floor_refuses_wounded() -> None:
    decision = decide(observed(stats={"health": 0.3}))
    assert decision.refusal is CombatRefusal.WOUNDED


def test_unreadable_health_refuses_wounded() -> None:
    # An absent reader is never the good reading, for what a fight spends.
    decision = decide(observed(stats={"health": None}))
    assert decision.refusal is CombatRefusal.WOUNDED


def test_endurance_under_the_floor_refuses_endurance_critical() -> None:
    decision = decide(observed(stats={"endurance": 0.1}))
    assert decision.refusal is CombatRefusal.ENDURANCE_CRITICAL


def test_unreadable_endurance_refuses_endurance_critical() -> None:
    decision = decide(observed(stats={"endurance": None}))
    assert decision.refusal is CombatRefusal.ENDURANCE_CRITICAL


def test_fatigue_over_the_ceiling_refuses_under_the_endurance_token() -> None:
    decision = decide(observed(stats={"fatigue": 0.95}))
    assert decision.refusal is CombatRefusal.ENDURANCE_CRITICAL


def test_panic_over_the_ceiling_refuses_panic_critical() -> None:
    decision = decide(observed(stats={"panic": 0.9}))
    assert decision.refusal is CombatRefusal.PANIC_CRITICAL


def test_an_absent_panic_reader_is_no_reading_not_a_refusal() -> None:
    # The arbiter's own precedent: a missing optional reader can neither
    # cross nor be crossed. The mandatory gates (health, endurance, weapon)
    # still stand in this world and pass.
    decision = decide(observed())
    assert "panic" not in observed().player.stats
    assert decision.refusal is None


# ---------------------------------------------------------------------------
# the weapon gate, engage only
# ---------------------------------------------------------------------------


def test_an_empty_primary_hand_refuses_weapon_unusable() -> None:
    decision = decide(observed(primary=None))
    assert decision.refusal is CombatRefusal.WEAPON_UNUSABLE


def test_an_unreadable_condition_refuses_rather_than_assumes() -> None:
    decision = decide(observed(weapon=a_weapon(condition=None, condition_max=None)))
    assert decision.refusal is CombatRefusal.WEAPON_UNUSABLE


def test_a_hand_item_absent_from_the_inventory_refuses() -> None:
    decision = decide(observed(no_inventory=True))
    assert decision.refusal is CombatRefusal.WEAPON_UNUSABLE


def test_a_broken_weapon_refuses_weapon_unusable() -> None:
    decision = decide(observed(weapon=a_weapon(condition=1, condition_max=10)))
    assert decision.refusal is CombatRefusal.WEAPON_UNUSABLE


def test_the_shove_variant_keeps_every_gate_but_the_weapon() -> None:
    # Empty-handed: engage refuses, shove proceeds.
    assert decide(observed(primary=None)).refusal is CombatRefusal.WEAPON_UNUSABLE
    assert decide(observed(primary=None), weapon_required=False).refusal is None
    # And the rest of the gates still fire for the shove.
    hurt = observed(primary=None, stats={"health": 0.3})
    assert decide(hurt, weapon_required=False).refusal is CombatRefusal.WOUNDED
    crowd = observed(
        primary=None,
        zombies=[a_zombie(), a_zombie(rid="z2", distance=GROUP_RING - 0.5)],
    )
    assert decide(crowd, weapon_required=False).refusal is CombatRefusal.GROUP_OVER_LIMIT


# ---------------------------------------------------------------------------
# the condition reader
# ---------------------------------------------------------------------------


def test_weapon_condition_fraction_reads_the_block_and_nothing_else() -> None:
    assert weapon_condition_fraction(a_weapon(condition=5, condition_max=10)) == 0.5
    assert weapon_condition_fraction(a_weapon(condition=None, condition_max=None)) is None
    assert weapon_condition_fraction(a_weapon(condition=5, condition_max=0)) is None
    assert weapon_condition_fraction(a_weapon(condition=True, condition_max=10)) is None
    plain = an_item(runtime_id="w2", category="Weapon")
    assert weapon_condition_fraction(plain) is None


def test_the_fraction_is_clamped_to_the_unit_interval() -> None:
    assert weapon_condition_fraction(a_weapon(condition=15, condition_max=10)) == 1.0
    assert weapon_condition_fraction(a_weapon(condition=-2, condition_max=10)) == 0.0
