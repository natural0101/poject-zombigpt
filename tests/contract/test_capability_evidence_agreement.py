"""What a probe demands as proof must be what its adapter actually observes.

A capability leaves ``available_unverified`` in exactly one place:
:func:`~pz_agent_core.capabilities.probes.confirm`, and only when a succeeded ack
carries every key in the probe's ``evidence_keys``. The gate is
``key not in ack.evidence`` — an exact string match, with no normalisation and no
near-miss. The keys on the other side of it are written in the adapters, by hand,
in a different package, several of them assembled from an f-string rather than
spelled out.

So an adapter that reports ``hunger_delta`` where its probe declares
``hunger_after`` costs the capability forever: the action works, the ack says
``succeeded``, the evidence is there and correct, and the report still says
unverified — on every install, for every user, until someone reads both files
side by side. Neither existing suite can see it. The probe tests assert the
probe's own declarations against acks the test wrote; the adapter tests assert
the adapter's own evidence against observations the test wrote. Both are green
while the two disagree, because nothing compares them.

This file compares them, by running them: the adapter's real ``verify`` against a
world its happy path holds in, the resulting evidence into the ack a probe run
would receive, and that ack the whole way through ``resolve_static`` and
``confirm`` to ``verified``. Stopping at the key comparison would prove less than
it looks: a probe can also be blocked by its ceiling, by its static state, or by
symbols the scan never found, and none of those show up in a set difference.

One note on the shape of the ack, because two documents here are both an
``ActionResult`` and only one of them is the probe's input. The mod's ack carries
the adapter's observed values flat — ``ActionRuntime.observedPairs`` says so
where it names this module as its reader — and that is what ``missing_keys``
matches against. The sidecar's *own* result wraps the same bag under
``observed``, for the MCP boundary. The last test in this file pins the
difference so the two are never mistaken for one another.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.actions.adapter import AdapterRegistry, Evidence
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.capabilities.model import REASON_NO_VERIFIED_API
from pz_agent_core.capabilities.probes import (
    AUTONOMOUS_ATTACK,
    BUILDING,
    COMBAT_ASSIST,
    CRAFTING,
    DOOR_TOGGLE,
    DRINK_CARRIED,
    DRINK_WORLD_SOURCE,
    EAT_PERCENTAGE,
    EQUIPMENT_EQUIP,
    EQUIPMENT_UNEQUIP,
    INVENTORY_TRANSFER,
    MEDICAL_BANDAGE,
    MOVE_TO_SQUARE,
    PROBES,
    READ_LITERATURE,
    SURVIVAL_REST,
    SURVIVAL_SLEEP,
    confirm,
    probe_for,
    resolve_static,
)
from pz_agent_core.capabilities.scanner import SymbolIndex, scan_lua_tree
from pz_agent_core.policy.building import BUILDING_KEY, SEMANTIC_BLOCKED, SEMANTIC_LOADED
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    CapabilityState,
    Command,
    Hands,
    InventoryView,
    NearbyObject,
    NearbyView,
    NearbyZombie,
    Observation,
    Position,
    ReasonCode,
    Wound,
)
from tests.fixtures import DEFAULT_SESSION, make_game, make_observation, make_player
from tests.fixtures.adapter_worlds import (
    BAG_REF,
    HOME_X,
    HOME_Y,
    MAIN_REF,
    a_command,
    a_square,
    a_world,
    a_world_object,
    an_item,
    bag_container,
    main_container,
    moved,
    prepare,
    square_ref,
)
from tests.fixtures.capability_trees import full_lua_tree, probe_ack
from tests.fixtures.policy_items import drink_item, food_item, literature_item

BUILD: Final = "42.20"
WHEN: Final = "2026-01-02T03:04:05+00:00"

TARGET_X: Final = HOME_X + 4
TARGET_Y: Final = HOME_Y
ARM: Final = "ForeArm_L"

BEANS: Final = food_item("42")
BOTTLE: Final = drink_item("43")
BOOK: Final = literature_item("44")
BANDAGE: Final = an_item("45", display_name="Bandage", full_type="Base.Bandage", category="Medical")
AXE: Final = an_item("46", display_name="Axe", full_type="Base.Axe", category="Weapon")
STASHED: Final = an_item("47", container_ref=BAG_REF, display_name="Biscuit")


@dataclass(frozen=True, slots=True)
class Exercise:
    """One probe, and a run of its action that its adapter says worked.

    The two observations are the whole point: ``verify`` is a comparison, so a
    single world proves nothing, and evidence assembled by hand would only prove
    that this file agrees with itself.
    """

    capability: str
    command: Command
    before: Observation
    after: Observation


def _registry() -> AdapterRegistry:
    return register_game_adapters(AdapterRegistry())


def _move_to_square() -> Exercise:
    square = [a_square(TARGET_X, TARGET_Y)]
    return Exercise(
        capability=MOVE_TO_SQUARE,
        command=a_command(
            ActionName.MOVEMENT_MOVE_TO,
            {"target": {"x": TARGET_X, "y": TARGET_Y, "z": 0}},
        ),
        before=a_world(objects=square),
        after=a_world(
            seq=2,
            position=Position(x=float(TARGET_X), y=float(TARGET_Y), z=0, direction="S"),
            objects=square,
        ),
    )


def _inventory_transfer() -> Exercise:
    containers = [main_container(), bag_container()]
    return Exercise(
        capability=INVENTORY_TRANSFER,
        command=a_command(
            ActionName.INVENTORY_TRANSFER,
            {"item_ref": STASHED.ref, "destination_container_ref": MAIN_REF},
        ),
        before=a_world(items=[STASHED], containers=containers),
        after=a_world(seq=2, items=[moved(STASHED, MAIN_REF)], containers=containers),
    )


def _eat_percentage() -> Exercise:
    containers = [main_container(), bag_container()]
    return Exercise(
        capability=EAT_PERCENTAGE,
        command=a_command(ActionName.CONSUME_EAT, {"item_ref": BEANS.ref, "fraction": 1.0}),
        before=a_world(items=[BEANS], containers=containers, stats={"hunger": 0.6}),
        after=a_world(seq=2, items=[BEANS], containers=containers, stats={"hunger": 0.1}),
    )


def _drink_carried() -> Exercise:
    containers = [main_container(), bag_container()]
    return Exercise(
        capability=DRINK_CARRIED,
        command=a_command(ActionName.CONSUME_DRINK, {"item_ref": BOTTLE.ref, "fraction": 1.0}),
        before=a_world(items=[BOTTLE], containers=containers, stats={"thirst": 0.6}),
        after=a_world(seq=2, items=[BOTTLE], containers=containers, stats={"thirst": 0.1}),
    )


def _drink_world_source() -> Exercise:
    """The sink case, which was an excuse in this file until the adapter arrived.

    ``source_ref`` is the key that makes this probe's confirmation mean
    something: without it an ordinary sip from a bottle would verify a
    capability §12.4 calls unproven. So the adapter has to carry it into the
    evidence, and this exercise is what proves it does.
    """
    containers = [main_container(), bag_container()]
    sink = a_world_object(square_ref(1204, 3400), kind="sink", semantics=["water_source"])
    return Exercise(
        capability=DRINK_WORLD_SOURCE,
        command=a_command(
            ActionName.CONSUME_DRINK_SOURCE,
            {"item_ref": BOTTLE.ref, "fraction": 1.0, "source_ref": sink.ref},
        ),
        before=a_world(
            items=[BOTTLE], containers=containers, objects=[sink], stats={"thirst": 0.6}
        ),
        after=a_world(
            seq=2, items=[BOTTLE], containers=containers, objects=[sink], stats={"thirst": 0.1}
        ),
    )


def _read_literature() -> Exercise:
    containers = [main_container(), bag_container()]
    return Exercise(
        capability=READ_LITERATURE,
        command=a_command(ActionName.LITERATURE_READ, {"item_ref": BOOK.ref, "pages": 10}),
        before=a_world(items=[BOOK], containers=containers),
        after=a_world(seq=2, items=[literature_item("44", pages_read=10)], containers=containers),
    )


def _equipment_equip() -> Exercise:
    containers = [main_container(), bag_container()]
    return Exercise(
        capability=EQUIPMENT_EQUIP,
        command=a_command(ActionName.EQUIPMENT_EQUIP, {"item_ref": AXE.ref}),
        before=a_world(items=[AXE], containers=containers),
        after=a_world(seq=2, items=[AXE], containers=containers, hands=Hands(primary=AXE.ref)),
    )


def _equipment_unequip() -> Exercise:
    containers = [main_container(), bag_container()]
    return Exercise(
        capability=EQUIPMENT_UNEQUIP,
        command=a_command(ActionName.EQUIPMENT_UNEQUIP, {"hand": "primary"}),
        before=a_world(items=[AXE], containers=containers, hands=Hands(primary=AXE.ref)),
        after=a_world(seq=2, items=[AXE], containers=containers),
    )


def _medical_bandage() -> Exercise:
    bleeding = Wound(ref=f"wound:{DEFAULT_SESSION}:{ARM}", kind="cut", severity=0.4, bleeding=True)
    dressed = Wound(ref=bleeding.ref, kind="cut", severity=0.2, bleeding=False)
    inventory = InventoryView(containers=[main_container(), bag_container()], items=[BANDAGE])
    return Exercise(
        capability=MEDICAL_BANDAGE,
        command=a_command(ActionName.MEDICAL_BANDAGE, {"body_part": ARM, "item_ref": BANDAGE.ref}),
        before=make_observation(seq=1, player=make_player(wounds=[bleeding]), inventory=inventory),
        after=make_observation(seq=2, player=make_player(wounds=[dressed]), inventory=inventory),
    )


def _survival_rest() -> Exercise:
    return Exercise(
        capability=SURVIVAL_REST,
        command=a_command(ActionName.SURVIVAL_REST, {"target_endurance": 0.8}),
        before=a_world(stats={"endurance": 0.3}),
        after=a_world(seq=2, stats={"endurance": 0.85}),
    )


def _survival_sleep() -> Exercise:
    # Eight hours of world time, because fatigue falling on its own is a nap and
    # the adapter refuses to call that a night.
    return Exercise(
        capability=SURVIVAL_SLEEP,
        command=a_command(
            ActionName.SURVIVAL_SLEEP,
            {"bed_ref": square_ref(HOME_X + 2, HOME_Y), "hours": 8},
        ),
        before=a_world(stats={"fatigue": 0.8}, game=make_game(world_time="1993-07-09T22:00:00")),
        after=a_world(
            seq=2, stats={"fatigue": 0.1}, game=make_game(world_time="1993-07-10T06:00:00")
        ),
    )


def _combat_assist() -> Exercise:
    """The shove case: the least-assumptive combat action confirms the probe.

    ``target_ref`` is the key the confirmation demands, and the re-observed
    zombie — standing before, prone after — is the only proof the adapter
    accepts; a shove needs no weapon, which is exactly why the probe rides
    it and not the swing.
    """
    target_ref = f"zombie:{DEFAULT_SESSION}:z1:0"

    def zombie(state: str) -> NearbyZombie:
        return NearbyZombie(
            ref=target_ref,
            distance=1.5,
            visible=True,
            chasing=False,
            position=Position(x=float(HOME_X) + 1.5, y=float(HOME_Y), z=0),
            state=state,
        )

    return Exercise(
        capability=COMBAT_ASSIST,
        command=a_command(ActionName.COMBAT_SHOVE, {"target_ref": target_ref}),
        before=a_world(nearby=NearbyView(zombies=[zombie("standing")])),
        after=a_world(seq=2, nearby=NearbyView(zombies=[zombie("prone")])),
    )


def _door_toggle() -> Exercise:
    door_ref = f"object:{DEFAULT_SESSION}:{HOME_X + 2}:{HOME_Y}:0:1"
    closed = a_world_object(
        door_ref,
        x=HOME_X + 2,
        y=HOME_Y,
        kind="door",
        semantics=["door", "obstacle"],
        open=False,
        locked=False,
        barricaded=False,
        orientation="north",
    )
    opened = a_world_object(
        door_ref,
        x=HOME_X + 2,
        y=HOME_Y,
        kind="door",
        semantics=["door", "obstacle"],
        open=True,
        locked=False,
        barricaded=False,
        orientation="north",
    )
    return Exercise(
        capability=DOOR_TOGGLE,
        command=a_command(ActionName.DOOR_OPEN, {"door_ref": door_ref}),
        before=a_world(objects=[closed]),
        after=a_world(seq=2, objects=[opened]),
    )


def _crafting() -> Exercise:
    """One crude spear made from one branch, proved by the two inventories.

    The probe's confirmation demands ``recipe`` and ``product``, and the
    adapter will only mint them from a re-observation where the product's count
    rose: a queued craft is not a spear in the bag. The branch carries the
    recipe readout because that is where the mod publishes it — on the first
    carried item of each type a recipe consumes — so the *before* world is also
    the world the policy reads the recipe out of.
    """
    branch_type = "Base.TreeBranch"
    spear_type = "Base.SpearCrude"
    branch = an_item(
        runtime_id="m1",
        container_ref=MAIN_REF,
        full_type=branch_type,
        display_name="Tree Branch",
        category="Item",
        extra={
            "crafting": {
                "recipes": [
                    {
                        "name": "MakeCrudeSpear",
                        "product": spear_type,
                        "display_name": "Make Crude Spear",
                        "known": True,
                        "needs_surface": False,
                        "materials": [{"full_type": branch_type, "count": 1}],
                    }
                ]
            }
        },
    )
    spear = an_item(
        runtime_id="p1",
        container_ref=MAIN_REF,
        full_type=spear_type,
        display_name="Crude Spear",
        category="Weapon",
    )
    containers = [main_container()]
    return Exercise(
        capability=CRAFTING,
        command=a_command(ActionName.CRAFTING_CRAFT, {"recipe": "MakeCrudeSpear", "count": 1}),
        before=a_world(items=[branch], containers=containers),
        after=a_world(seq=2, items=[spear], containers=containers),
    )


def _building() -> Exercise:
    """One wall raised on the square next door, proved by the square changing.

    The probe demands ``blueprint`` and ``square``, and the adapter mints them
    only from a *difference*: the wall stands on the square afterwards and did
    not stand there before. Both worlds therefore carry the same bounded window
    of described squares — without a before-picture there is no change to
    observe, only a square with something on it, which is as true of a square
    that always had it.
    """
    target_x, target_y = HOME_X + 1, HOME_Y
    wall = "WoodenWall"
    plank_type = "Base.Plank"
    plank = an_item(
        runtime_id="m1",
        container_ref=MAIN_REF,
        full_type=plank_type,
        display_name="Plank",
        category="Item",
        extra={
            BUILDING_KEY: {
                "structure_count": 1,
                "known_structure_count": 1,
                "structures": [
                    {
                        "name": wall,
                        "display_name": "Wooden Wall",
                        "known": True,
                        "blocks_movement": True,
                        "materials": [{"full_type": plank_type, "count": 1}],
                    }
                ],
            }
        },
    )

    def window(*, blocked: bool) -> list[NearbyObject]:
        described = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                marks = [SEMANTIC_LOADED]
                if blocked and (HOME_X + dx, HOME_Y + dy) == (target_x, target_y):
                    marks.append(SEMANTIC_BLOCKED)
                described.append(a_square(HOME_X + dx, HOME_Y + dy, 0, semantics=marks))
        return described

    containers = [main_container()]
    return Exercise(
        capability=BUILDING,
        command=a_command(
            ActionName.BUILDING_BUILD,
            {"blueprint": wall, "square": square_ref(target_x, target_y, 0)},
        ),
        before=a_world(items=[plank], containers=containers, objects=window(blocked=False)),
        after=a_world(seq=2, items=[plank], containers=containers, objects=window(blocked=True)),
    )


#: One run per probe this file can drive. Hand-built for the same reason the
#: argument-agreement table is: a generated world would prove the generator
#: agrees with itself, and the adapters are exactly what must not be trusted.
EXERCISES: Final[tuple[Exercise, ...]] = (
    _move_to_square(),
    _door_toggle(),
    _inventory_transfer(),
    _eat_percentage(),
    _drink_carried(),
    _drink_world_source(),
    _read_literature(),
    _equipment_equip(),
    _equipment_unequip(),
    _medical_bandage(),
    _survival_rest(),
    _survival_sleep(),
    _combat_assist(),
    _crafting(),
    _building(),
)

#: The probes no run above can reach, and why. Named rather than skipped: an
#: unexercised probe that nobody can see is the same defect in a new place.
NOT_EXERCISED: Final[dict[str, str]] = {
    AUTONOMOUS_ATTACK: (
        "its ceiling is 'unsupported' by design (§12.4), so no ack can confirm it; "
        "the refusal itself is asserted below"
    ),
}


def _index(tmp_path: Path) -> SymbolIndex:
    """A scan of an install carrying every symbol the declared probes require."""
    return scan_lua_tree(full_lua_tree(tmp_path)).index()


def _observe(exercise: Exercise) -> Evidence:
    """Run one exercise through the real adapter and return what it observed.

    The command goes through ``prepare`` first because the engine ships the
    arguments ``build_args`` produced: verifying against the raw draft would
    exercise a command shape that never reaches the mod.
    """
    probe = probe_for(exercise.capability)
    adapter = _registry().get(probe.confirmation.action)
    command = prepare(adapter, exercise.command, exercise.before)
    evidence = adapter.verify(command, exercise.before, exercise.after)
    assert evidence is not None, (
        f"{exercise.capability}: the adapter for {probe.confirmation.action.value} found no "
        "postcondition in this world, so the case below would prove nothing about its keys"
    )
    return evidence


def _ack_from(exercise: Exercise, evidence: Evidence) -> ActionResult:
    """The ack a probe run would receive for *exercise*.

    ``ActionRuntime`` hands the adapter's observed bag to the ack unchanged and
    flat, which is the level ``missing_keys`` reads. Building it from the real
    ``Evidence.observed`` is what makes this a comparison of the two sides
    rather than of one side with itself.
    """
    return probe_ack(probe_for(exercise.capability).confirmation.action, dict(evidence.observed))


def _ids() -> list[str]:
    return [exercise.capability for exercise in EXERCISES]


# ---------------------------------------------------------------------------
# the seam
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exercise", EXERCISES, ids=_ids())
def test_every_key_a_probe_demands_is_one_its_adapter_emits(exercise: Exercise) -> None:
    evidence = _observe(exercise)
    probe = probe_for(exercise.capability)
    missing = probe.confirmation.missing_keys(_ack_from(exercise, evidence))

    assert missing == (), (
        f"{exercise.capability} declares evidence {sorted(missing)}, which the adapter for "
        f"{probe.confirmation.action.value} never observes — it emits "
        f"{sorted(evidence.observed)}. The action would work and the capability would "
        "stay unverified for the life of the install"
    )


@pytest.mark.parametrize("exercise", EXERCISES, ids=_ids())
def test_a_live_ack_carries_each_capability_all_the_way_to_verified(
    exercise: Exercise, tmp_path: Path
) -> None:
    """The keys agreeing is necessary and not sufficient.

    A probe can also be held below ``verified`` by its ceiling, by a required
    symbol the scan did not find, or by an ack for the wrong action. A test that
    stopped at the key comparison would call all of those green.
    """
    probe = probe_for(exercise.capability)
    evidence = _observe(exercise)
    static = resolve_static(probe, _index(tmp_path), build=BUILD, observed_at=WHEN)
    assert static.state is not CapabilityState.UNSUPPORTED, (
        f"{exercise.capability}: the static scan already refused it, so this test would "
        f"have proved nothing about the ack — {static.reason}"
    )

    confirmed = confirm(probe, static, _ack_from(exercise, evidence), build=BUILD, observed_at=WHEN)

    assert confirmed.state is CapabilityState.VERIFIED, (
        f"{exercise.capability} did not reach verified from its own adapter's evidence: "
        f"{confirmed.reason}"
    )
    assert confirmed.has_runtime_evidence
    assert confirmed.usable


# ---------------------------------------------------------------------------
# the probes no run can reach
# ---------------------------------------------------------------------------


def test_every_probe_is_either_exercised_here_or_named_as_out_of_reach() -> None:
    exercised = {exercise.capability for exercise in EXERCISES}
    declared = {probe.capability for probe in PROBES}

    assert exercised.isdisjoint(NOT_EXERCISED), (
        "a capability cannot be both exercised and excused: "
        f"{sorted(exercised & set(NOT_EXERCISED))}"
    )
    assert exercised | set(NOT_EXERCISED) == declared, (
        "these probes are neither exercised nor named as out of reach: "
        f"{sorted(declared - exercised - set(NOT_EXERCISED))}"
    )


def test_the_list_of_probes_this_file_cannot_reach_does_not_grow_quietly() -> None:
    """One, and the reason for it is asserted by a test of its own.

    Pinned as a literal set rather than derived, so that moving a probe out of
    the exercised table and into the excuses has to be done here, in the open,
    by someone who then has to write the test that keeps the excuse honest.

    It was two. ``drink_world_source`` sat here because §4.9's adapter had not
    been written; it is exercised above now, and the test that guarded the
    excuse has become the test below that the two drink actions stay separate.
    """
    assert set(NOT_EXERCISED) == {AUTONOMOUS_ATTACK}
    assert all(reason.strip() for reason in NOT_EXERCISED.values())


def test_autonomous_attack_is_refused_by_its_ceiling_rather_than_left_untested(
    tmp_path: Path,
) -> None:
    """§12.4: not a missing probe run, a decision. It has to read as one."""
    probe = probe_for(AUTONOMOUS_ATTACK)
    assert not probe.can_be_verified

    static = resolve_static(probe, _index(tmp_path), build=BUILD, observed_at=WHEN)
    # Evidence generous enough to satisfy the confirmation on its face, so that
    # what refuses it is demonstrably the ceiling and not a missing key.
    ack = probe_ack(probe.confirmation.action, {"never": True, "killed": 3})
    confirmed = confirm(probe, static, ack, build=BUILD, observed_at=WHEN)

    assert probe.confirmation.missing_keys(ack) == ()
    assert confirmed.state is CapabilityState.UNSUPPORTED
    assert confirmed.reason == REASON_NO_VERIFIED_API
    assert not confirmed.has_runtime_evidence


def test_the_two_drink_actions_do_not_share_a_capability() -> None:
    """The gate for ``drink_world_source``, asserted rather than assumed.

    §12.4 caps this probe at ``experimental``, which means upgradeable but not
    usable — and the engine reads ``required_capability`` from the adapter that
    owns the action. While the world source was an optional ``refill_from``
    argument on ``consume.drink``, the whole path ran under ``drink_carried``,
    which a static scan verifies: an unproven capability reachable through a
    proven one. Splitting the action is what closes that, so a merge back into
    one action has to fail here.
    """
    registry = _registry()

    carried = registry.get(ActionName.CONSUME_DRINK)
    from_source = registry.get(ActionName.CONSUME_DRINK_SOURCE)
    assert carried.required_capability == DRINK_CARRIED
    assert from_source.required_capability == DRINK_WORLD_SOURCE

    claimed = [
        action
        for action in registry.names()
        if registry.get(action).required_capability == DRINK_WORLD_SOURCE
    ]
    assert claimed == [ActionName.CONSUME_DRINK_SOURCE], (
        f"{sorted(a.value for a in claimed)} claim drink_world_source; exactly one action "
        "may, or the capability stops meaning what the probe checks"
    )

    # By execution, not by reading: the carried-container adapter refuses the
    # argument that would put it on the world-source path.
    world = a_world(items=[BOTTLE], containers=[main_container(), bag_container()])
    command = a_command(
        ActionName.CONSUME_DRINK,
        {"item_ref": BOTTLE.ref, "source_ref": square_ref(1204, 3400)},
    )
    with pytest.raises(PreconditionFailed) as caught:
        carried.validate(command, world)
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_the_world_source_adapter_will_not_verify_without_the_source_it_names() -> None:
    """Thirst falling is necessary; naming where the water came from is too.

    A postcondition that dropped ``source_ref`` would let the probe confirm on
    an ordinary sip, which is the one substitution this capability exists to
    prevent.
    """
    exercise = _drink_world_source()
    evidence = _observe(exercise)

    assert "source_ref" in evidence.observed
    assert evidence.observed["source_ref"] == exercise.command.args["source_ref"]
