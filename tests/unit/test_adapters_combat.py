"""The four combat adapters: sidecar-side gates, re-observed postconditions.

Two properties carry the epic's safety weight and both live here. The policy
refusal happens in ``validate`` — before any command exists to send — so a
refused engagement never costs wire traffic, and the engage verify's absence
rule is exercised from both sides: the vanish that counts (fought at close
range, nothing left within reach) and the vanishes that must not (a distant
sighting wandering off, a replacement stepping into the gap).
"""

from __future__ import annotations

from typing import Any

import pytest

from pz_agent_core.actions.adapter import PreconditionFailed
from pz_agent_core.actions.adapters.combat import (
    _REFUSAL_REASONS,
    CombatEngageAdapter,
    CombatEquipBestAdapter,
    CombatRetreatAdapter,
    CombatShoveAdapter,
)
from pz_agent_core.combat import (
    ENGAGE_RANGE,
    GROUP_RING,
    SHOVE_RANGE,
    CombatRefusal,
)
from pz_agent_core.protocol import (
    ActionName,
    Command,
    Hands,
    ItemView,
    NearbyView,
    NearbyZombie,
    Observation,
    Position,
    ReasonCode,
)
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.adapter_worlds import HOME_X, HOME_Y, a_command, a_world, an_item, prepare

WEAPON_REF = f"item:{DEFAULT_SESSION}:player-main:w1:0"
TARGET_REF = f"zombie:{DEFAULT_SESSION}:z1:0"


def a_zombie(
    rid: str = "z1",
    distance: float = 1.5,
    *,
    chasing: bool = False,
    state: str | None = "standing",
    z: int = 0,
) -> NearbyZombie:
    return NearbyZombie(
        ref=f"zombie:{DEFAULT_SESSION}:{rid}:0",
        distance=distance,
        visible=True,
        chasing=chasing,
        position=Position(x=float(HOME_X) + distance, y=float(HOME_Y), z=z),
        state=state,
    )


def a_weapon(
    condition: float | None = 8, condition_max: float | None = 10, rid: str = "w1"
) -> ItemView:
    block: dict[str, Any] = {}
    if condition is not None:
        block["condition"] = condition
    if condition_max is not None:
        block["condition_max"] = condition_max
    return an_item(
        runtime_id=rid,
        full_type="Base.BaseballBat",
        display_name="Baseball Bat",
        category="Weapon",
        extra={"weapon": block} if block else {},
    )


def combat_world(
    *,
    zombies: list[NearbyZombie] | None = None,
    items: list[Any] | None = None,
    primary: str | None = WEAPON_REF,
    seq: int = 1,
    no_nearby: bool = False,
    no_inventory: bool = False,
) -> Observation:
    return a_world(
        seq=seq,
        items=items if items is not None else [a_weapon()],
        hands=Hands(primary=primary),
        nearby=NearbyView(zombies=list(zombies if zombies is not None else [a_zombie()])),
        no_nearby=no_nearby,
        no_inventory=no_inventory,
    )


# ---------------------------------------------------------------------------
# the refusal-reason table is total
# ---------------------------------------------------------------------------


def test_every_policy_refusal_has_a_wire_reason() -> None:
    assert set(_REFUSAL_REASONS) == set(CombatRefusal)


# ---------------------------------------------------------------------------
# combat.engage — validate
# ---------------------------------------------------------------------------


class TestEngageValidate:
    def adapter(self) -> CombatEngageAdapter:
        return CombatEngageAdapter()

    def command(self, target_ref: str = TARGET_REF) -> Command:
        return a_command(ActionName.COMBAT_ENGAGE, {"target_ref": target_ref})

    def test_the_approved_engagement_passes(self) -> None:
        self.adapter().validate(self.command(), combat_world())

    def test_a_policy_refusal_happens_sidecar_side_with_the_token(self) -> None:
        crowd = combat_world(zombies=[a_zombie(), a_zombie(rid="z2", distance=GROUP_RING - 0.5)])
        with pytest.raises(PreconditionFailed) as caught:
            self.adapter().validate(self.command(), crowd)
        assert caught.value.reason_code is ReasonCode.POLICY_DENIED
        assert caught.value.evidence["refusal"] == "group_over_limit"

    def test_an_absent_weapon_is_refused_for_engage(self) -> None:
        with pytest.raises(PreconditionFailed) as caught:
            self.adapter().validate(self.command(), combat_world(primary=None))
        assert caught.value.evidence["refusal"] == "weapon_unusable"

    def test_an_unobserved_target_is_target_not_loaded(self) -> None:
        with pytest.raises(PreconditionFailed) as caught:
            self.adapter().validate(self.command(), combat_world(zombies=[a_zombie(rid="other")]))
        assert caught.value.reason_code is ReasonCode.TARGET_NOT_LOADED

    def test_a_distant_target_is_out_of_range(self) -> None:
        far = combat_world(zombies=[a_zombie(distance=ENGAGE_RANGE + 1)])
        with pytest.raises(PreconditionFailed) as caught:
            self.adapter().validate(self.command(), far)
        assert caught.value.reason_code is ReasonCode.TARGET_OUT_OF_RANGE

    def test_a_foreign_sessions_reference_is_refused(self) -> None:
        with pytest.raises(PreconditionFailed) as caught:
            self.adapter().validate(
                self.command("zombie:00000000-0000-0000-0000-00000000dead:z1:0"),
                combat_world(),
            )
        assert caught.value.reason_code is ReasonCode.INVALID_REF

    def test_unknown_arguments_are_refused_never_dropped(self) -> None:
        command = a_command(ActionName.COMBAT_ENGAGE, {"target_ref": TARGET_REF, "swings": 50})
        with pytest.raises(PreconditionFailed) as caught:
            self.adapter().validate(command, combat_world())
        assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# combat.engage — verify (the honest absence rule)
# ---------------------------------------------------------------------------


class TestEngageVerify:
    def prepared(self, before: Observation) -> Command:
        return prepare(
            CombatEngageAdapter(),
            a_command(ActionName.COMBAT_ENGAGE, {"target_ref": TARGET_REF}),
            before,
        )

    def test_a_target_observed_prone_is_the_postcondition(self) -> None:
        before = combat_world()
        after = combat_world(zombies=[a_zombie(state="prone")], seq=2)
        evidence = CombatEngageAdapter().verify(self.prepared(before), before, after)
        assert evidence is not None
        assert evidence.kind == "zombie_down"
        assert evidence.observed["outcome"] == "down"
        assert evidence.observed["target_ref"] == TARGET_REF

    def test_a_target_still_standing_is_no_evidence(self) -> None:
        before = combat_world()
        after = combat_world(seq=2)
        assert CombatEngageAdapter().verify(self.prepared(before), before, after) is None

    def test_an_unreadable_state_is_never_read_as_down(self) -> None:
        before = combat_world()
        after = combat_world(zombies=[a_zombie(state=None)], seq=2)
        assert CombatEngageAdapter().verify(self.prepared(before), before, after) is None

    def test_absence_counts_only_after_a_close_fight_with_nothing_in_reach(self) -> None:
        before = combat_world()  # fought at 1.5 tiles
        after = combat_world(zombies=[], seq=2)
        evidence = CombatEngageAdapter().verify(self.prepared(before), before, after)
        assert evidence is not None
        assert evidence.observed["outcome"] == "gone"
        assert evidence.observed["distance_before"] == 1.5

    def test_a_distant_sighting_wandering_off_is_not_a_kill(self) -> None:
        before = combat_world(zombies=[a_zombie(distance=GROUP_RING + 2)])
        after = combat_world(zombies=[], seq=2)
        assert CombatEngageAdapter().verify(self.prepared(before), before, after) is None

    def test_a_replacement_inside_reach_blocks_the_absence_claim(self) -> None:
        before = combat_world()
        after = combat_world(zombies=[a_zombie(rid="z2", distance=SHOVE_RANGE - 0.5)], seq=2)
        assert CombatEngageAdapter().verify(self.prepared(before), before, after) is None

    def test_a_missing_nearby_tier_proves_nothing(self) -> None:
        before = combat_world()
        after = combat_world(no_nearby=True, seq=2)
        assert CombatEngageAdapter().verify(self.prepared(before), before, after) is None

    def test_a_target_never_seen_before_cannot_vanish_into_a_kill(self) -> None:
        before = combat_world(zombies=[])
        after = combat_world(zombies=[], seq=2)
        assert CombatEngageAdapter().verify(self.prepared(before), before, after) is None


# ---------------------------------------------------------------------------
# combat.shove
# ---------------------------------------------------------------------------


class TestShove:
    def prepared(self, before: Observation) -> Command:
        return prepare(
            CombatShoveAdapter(),
            a_command(ActionName.COMBAT_SHOVE, {"target_ref": TARGET_REF}),
            before,
        )

    def test_a_weaponless_shove_passes_validation(self) -> None:
        CombatShoveAdapter().validate(
            a_command(ActionName.COMBAT_SHOVE, {"target_ref": TARGET_REF}),
            combat_world(primary=None),
        )

    def test_every_other_gate_still_fires(self) -> None:
        crowd = combat_world(
            primary=None,
            zombies=[a_zombie(), a_zombie(rid="z2", distance=GROUP_RING - 0.5)],
        )
        with pytest.raises(PreconditionFailed) as caught:
            CombatShoveAdapter().validate(
                a_command(ActionName.COMBAT_SHOVE, {"target_ref": TARGET_REF}), crowd
            )
        assert caught.value.evidence["refusal"] == "group_over_limit"

    def test_a_knocked_down_target_is_the_postcondition(self) -> None:
        before = combat_world()
        after = combat_world(zombies=[a_zombie(state="prone")], seq=2)
        evidence = CombatShoveAdapter().verify(self.prepared(before), before, after)
        assert evidence is not None
        assert evidence.kind == "zombie_shoved"
        assert evidence.observed["state_after"] == "prone"

    def test_a_pushed_back_target_is_the_postcondition_too(self) -> None:
        before = combat_world()
        after = combat_world(zombies=[a_zombie(distance=3.0)], seq=2)
        evidence = CombatShoveAdapter().verify(self.prepared(before), before, after)
        assert evidence is not None
        assert evidence.observed["distance_before"] == 1.5
        assert evidence.observed["distance_after"] == 3.0

    def test_an_unmoved_standing_target_is_no_evidence(self) -> None:
        before = combat_world()
        after = combat_world(seq=2)
        assert CombatShoveAdapter().verify(self.prepared(before), before, after) is None

    def test_a_vanished_target_proves_no_shove(self) -> None:
        # The shove claims no kill, so absence — engage's hard case — is
        # simply not evidence here.
        before = combat_world()
        after = combat_world(zombies=[], seq=2)
        assert CombatShoveAdapter().verify(self.prepared(before), before, after) is None


# ---------------------------------------------------------------------------
# combat.equip_best
# ---------------------------------------------------------------------------


class TestEquipBest:
    def command(self) -> Command:
        return a_command(ActionName.COMBAT_EQUIP_BEST, {})

    def test_bare_hands_pass_validation_and_args_are_empty(self) -> None:
        adapter = CombatEquipBestAdapter()
        world = combat_world(primary=None)
        adapter.validate(self.command(), world)
        assert adapter.build_args(self.command(), world) == {}

    def test_an_armed_hand_is_refused_as_indistinguishable(self) -> None:
        with pytest.raises(PreconditionFailed, match="already held"):
            CombatEquipBestAdapter().validate(self.command(), combat_world())

    def test_the_hand_changing_to_an_observed_weapon_is_the_postcondition(self) -> None:
        adapter = CombatEquipBestAdapter()
        before = combat_world(primary=None)
        after = combat_world(seq=2)
        prepared = prepare(adapter, self.command(), before)
        evidence = adapter.verify(prepared, before, after)
        assert evidence is not None
        assert evidence.kind == "weapon_equipped"
        assert evidence.observed["item_ref"] == WEAPON_REF
        assert evidence.observed["primary_before"] is None
        assert evidence.observed["condition_fraction"] == 0.8

    def test_an_unchanged_hand_is_no_evidence(self) -> None:
        adapter = CombatEquipBestAdapter()
        before = combat_world(primary=None)
        after = combat_world(primary=None, seq=2)
        assert adapter.verify(prepare(adapter, self.command(), before), before, after) is None

    def test_a_hand_holding_a_non_weapon_is_no_evidence(self) -> None:
        adapter = CombatEquipBestAdapter()
        tin_ref = f"item:{DEFAULT_SESSION}:player-main:tin:0"
        tin = an_item(runtime_id="tin", category="Food")
        before = combat_world(primary=None, items=[tin])
        after = combat_world(primary=tin_ref, items=[tin], seq=2)
        assert adapter.verify(prepare(adapter, self.command(), before), before, after) is None


# ---------------------------------------------------------------------------
# combat.retreat
# ---------------------------------------------------------------------------


class TestRetreat:
    def command(self) -> Command:
        return a_command(ActionName.COMBAT_RETREAT, {})

    def test_a_threatened_world_passes_and_args_are_empty(self) -> None:
        adapter = CombatRetreatAdapter()
        world = combat_world()
        adapter.validate(self.command(), world)
        assert adapter.build_args(self.command(), world) == {}

    def test_a_calm_world_is_refused_as_unprovable(self) -> None:
        with pytest.raises(PreconditionFailed, match="nothing to retreat from"):
            CombatRetreatAdapter().validate(self.command(), combat_world(zombies=[]))

    def test_the_distance_growing_is_the_postcondition(self) -> None:
        adapter = CombatRetreatAdapter()
        before = combat_world()
        after = combat_world(zombies=[a_zombie(distance=4.0)], seq=2)
        evidence = adapter.verify(prepare(adapter, self.command(), before), before, after)
        assert evidence is not None
        assert evidence.kind == "distance_opened"
        assert evidence.observed["nearest_before"] == 1.5
        assert evidence.observed["nearest_after"] == 4.0

    def test_an_emptied_radius_counts_as_the_gap_opening(self) -> None:
        adapter = CombatRetreatAdapter()
        before = combat_world()
        after = combat_world(zombies=[], seq=2)
        evidence = adapter.verify(prepare(adapter, self.command(), before), before, after)
        assert evidence is not None
        assert evidence.observed["nearest_after"] is None

    def test_an_unmoved_or_closing_gap_is_no_evidence(self) -> None:
        adapter = CombatRetreatAdapter()
        before = combat_world()
        same = combat_world(seq=2)
        closer = combat_world(zombies=[a_zombie(distance=1.0)], seq=3)
        prepared = prepare(adapter, self.command(), before)
        assert adapter.verify(prepared, before, same) is None
        assert adapter.verify(prepared, before, closer) is None

    def test_the_nearest_zombie_counts_not_the_target(self) -> None:
        # Two zombies: the far one leaves, the near one stays. The nearest
        # distance did not grow, so no retreat is claimed.
        adapter = CombatRetreatAdapter()
        before = combat_world(zombies=[a_zombie(), a_zombie(rid="z2", distance=5.0)])
        after = combat_world(zombies=[a_zombie()], seq=2)
        assert adapter.verify(prepare(adapter, self.command(), before), before, after) is None
