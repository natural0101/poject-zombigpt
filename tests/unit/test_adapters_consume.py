"""``consume.eat`` and ``consume.drink``.

Three of these tests exist because the naive postcondition is wrong in three
different ways: a bite too small to move the hunger stat, an item eaten to
nothing, and an item that "disappeared" only because the observation carried no
inventory at all. The first two are successes, the third is not, and only the
pair of observations can tell them apart.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import (
    ActionEngine,
    ActionRequest,
    AdapterRegistry,
    PreconditionFailed,
)
from pz_agent_core.actions.adapters import DrinkAdapter, DrinkSourceAdapter, EatAdapter
from pz_agent_core.actions.adapters.consume import MIN_CONSUME_FRACTION
from pz_agent_core.capabilities import DRINK_CARRIED, DRINK_WORLD_SOURCE, EAT_PERCENTAGE
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    ContainerView,
    ItemView,
    NearbyObject,
    Observation,
    Position,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.action_doubles import AckPlan, FakeClock, FakeCommandSink, FakeObservationSource
from tests.fixtures.adapter_worlds import (
    BAG_REF,
    MAIN_REF,
    a_command,
    a_world,
    bag_container,
    main_container,
    prepare,
)
from tests.fixtures.policy_items import drink_item, food_item

BEANS = food_item("42")
BOTTLE = drink_item("43")


def eating(
    *items: ItemView,
    hunger: float = 0.5,
    thirst: float = 0.5,
    seq: int = 1,
    containers: list[ContainerView] | None = None,
) -> Observation:
    return a_world(
        seq=seq,
        items=list(items),
        containers=containers if containers is not None else [main_container(), bag_container()],
        stats={"hunger": hunger, "thirst": thirst},
    )


def eat_command(item_ref: str = BEANS.ref, **extra: object) -> Command:
    args: dict[str, object] = {"item_ref": item_ref}
    args.update(extra)
    return a_command(ActionName.CONSUME_EAT, args)


def drink_command(item_ref: str = BOTTLE.ref, **extra: object) -> Command:
    args: dict[str, object] = {"item_ref": item_ref}
    args.update(extra)
    return a_command(ActionName.CONSUME_DRINK, args)


# --------------------------------------------------------------------------
# eat: the postcondition
# --------------------------------------------------------------------------


def test_hunger_falling_is_the_evidence() -> None:
    adapter = EatAdapter()
    before = eating(BEANS, hunger=0.5)
    command = prepare(adapter, eat_command(), before)

    evidence = adapter.verify(command, before, eating(BEANS, hunger=0.2, seq=2))

    assert evidence is not None
    assert evidence.kind == "hunger_decreased"
    assert evidence.observed["hunger_before"] == pytest.approx(0.5)
    assert evidence.observed["hunger_after"] == pytest.approx(0.2)


def test_a_bite_too_small_to_move_the_stat_is_proven_by_the_portion_counter() -> None:
    adapter = EatAdapter()
    before = eating(BEANS, hunger=0.5)
    command = prepare(adapter, eat_command(), before)
    nibbled = food_item("42", remaining_portions=3)

    evidence = adapter.verify(command, before, eating(nibbled, hunger=0.5, seq=2))

    assert evidence is not None
    assert evidence.kind == "portions_decreased"
    assert evidence.observed["portions_before"] == 4
    assert evidence.observed["portions_after"] == 3


def test_an_item_eaten_to_nothing_is_a_success_not_a_lost_reference() -> None:
    """§4.8: the item being gone after the action is the expected outcome."""
    adapter = EatAdapter()
    before = eating(BEANS, hunger=0.5)
    command = prepare(adapter, eat_command(), before)

    evidence = adapter.verify(command, before, eating(hunger=0.5, seq=2))

    assert evidence is not None
    assert evidence.kind == "item_consumed_entirely"
    assert evidence.observed["item_present_after"] is False


def test_nothing_changing_is_not_evidence() -> None:
    adapter = EatAdapter()
    before = eating(BEANS, hunger=0.5)
    command = prepare(adapter, eat_command(), before)

    assert adapter.verify(command, before, eating(BEANS, hunger=0.5, seq=2)) is None


def test_hunger_rising_is_not_evidence() -> None:
    adapter = EatAdapter()
    before = eating(BEANS, hunger=0.5)
    command = prepare(adapter, eat_command(), before)

    assert adapter.verify(command, before, eating(BEANS, hunger=0.6, seq=2)) is None


def test_an_observation_with_no_inventory_is_not_an_eaten_item() -> None:
    """The whole difference between "consumed" and "unobserved" is this test."""
    adapter = EatAdapter()
    before = eating(BEANS, hunger=0.5)
    command = prepare(adapter, eat_command(), before)
    blind = a_world(seq=2, no_inventory=True, stats={"hunger": 0.5})

    assert adapter.verify(command, before, blind) is None


def test_an_item_that_was_never_observed_cannot_have_been_eaten() -> None:
    """Absent before and absent after is not a disappearance."""
    adapter = EatAdapter()
    before = eating(BEANS, hunger=0.5)
    command = prepare(adapter, eat_command(), before)

    never_there = eating(hunger=0.5, seq=2)

    assert adapter.verify(command, eating(hunger=0.5), never_there) is None


def test_a_before_observation_without_an_inventory_cannot_prove_a_disappearance() -> None:
    """Both ends of the pair have to see the tier, not just the later one."""
    adapter = EatAdapter()
    command = prepare(adapter, eat_command(), eating(BEANS, hunger=0.5))
    blind_before = a_world(seq=1, no_inventory=True, stats={"hunger": 0.5})

    assert adapter.verify(command, blind_before, eating(hunger=0.5, seq=2)) is None


def test_a_duplicated_item_cannot_be_followed_across_the_meal() -> None:
    """One runtime id in two containers has no "the item" whose counter fell."""
    adapter = EatAdapter()
    before = eating(BEANS, hunger=0.5)
    command = prepare(adapter, eat_command(), before)

    nibbled = food_item("42", container_ref=BAG_REF, remaining_portions=1)
    duplicated = eating(nibbled, BEANS, hunger=0.5, seq=2)

    assert adapter.verify(command, before, duplicated) is None


# --------------------------------------------------------------------------
# eat: refusals
# --------------------------------------------------------------------------


def test_an_item_that_is_not_food_is_refused() -> None:
    not_food = ItemView(
        ref=BEANS.ref.replace(":42:", ":50:"),
        container_ref=MAIN_REF,
        full_type="Base.Hammer",
        display_name="Hammer",
        category="Tool",
        weight=1.0,
    )

    with pytest.raises(PreconditionFailed) as caught:
        EatAdapter().validate(eat_command(not_food.ref), eating(not_food))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_food_the_game_calls_inedible_is_refused() -> None:
    inedible = food_item("42", edible=False)

    with pytest.raises(PreconditionFailed) as caught:
        EatAdapter().validate(eat_command(), eating(inedible))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_food_the_game_marks_destroyed_is_refused() -> None:
    """Edible and destroyed at once is the state a rotted-away item ends in."""
    gone = food_item("42", destroyed=True)

    with pytest.raises(PreconditionFailed) as caught:
        EatAdapter().validate(eat_command(), eating(gone))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_food_with_no_portions_left_is_refused() -> None:
    empty = food_item("42", remaining_portions=0)

    with pytest.raises(PreconditionFailed) as caught:
        EatAdapter().validate(eat_command(), eating(empty))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_food_in_a_bag_names_the_transfer_it_depends_on() -> None:
    """§4.7: the dependency is declared, never performed behind the command."""
    in_bag = food_item("42", container_ref=BAG_REF)

    with pytest.raises(PreconditionFailed) as caught:
        EatAdapter().validate(eat_command(in_bag.ref), eating(in_bag))

    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED
    prerequisite = caught.value.evidence["prerequisites"][0]
    assert prerequisite["action"] == ActionName.INVENTORY_ENSURE_MAIN.value
    assert prerequisite["args"]["item_ref"] == in_bag.ref


def test_an_unresolvable_reference_is_refused_as_invalid() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        EatAdapter().validate(eat_command(), eating())
    assert caught.value.reason_code is ReasonCode.INVALID_REF


@pytest.mark.parametrize("fraction", [0.0, -0.5, 1.5, "half", True])
def test_an_impossible_fraction_is_refused(fraction: object) -> None:
    with pytest.raises(PreconditionFailed) as caught:
        EatAdapter().validate(eat_command(fraction=fraction), eating(BEANS))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_the_smallest_worthwhile_bite_is_accepted() -> None:
    EatAdapter().validate(eat_command(fraction=MIN_CONSUME_FRACTION), eating(BEANS))


def test_the_chosen_fraction_is_what_the_mod_receives() -> None:
    args = EatAdapter().build_args(eat_command(fraction=0.5), eating(BEANS))

    assert args == {"item_ref": BEANS.ref, "fraction": 0.5}


def test_eating_declares_the_capability_it_needs() -> None:
    assert EatAdapter().required_capability == EAT_PERCENTAGE
    assert EatAdapter().risk is RiskClass.P2


# --------------------------------------------------------------------------
# drink
# --------------------------------------------------------------------------


def test_thirst_falling_is_the_evidence() -> None:
    adapter = DrinkAdapter()
    before = eating(BOTTLE, thirst=0.6)
    command = prepare(adapter, drink_command(), before)

    evidence = adapter.verify(command, before, eating(BOTTLE, thirst=0.3, seq=2))

    assert evidence is not None
    assert evidence.kind == "thirst_decreased"
    assert evidence.observed["thirst_before"] == pytest.approx(0.6)


def test_a_sip_too_small_to_move_the_stat_is_proven_by_the_volume() -> None:
    adapter = DrinkAdapter()
    before = eating(BOTTLE, thirst=0.6)
    command = prepare(adapter, drink_command(), before)
    sipped = drink_item("43", remaining_units=0.6)

    evidence = adapter.verify(command, before, eating(sipped, thirst=0.6, seq=2))

    assert evidence is not None
    assert evidence.kind == "volume_decreased"
    assert evidence.observed["volume_after"] == pytest.approx(0.6)


def test_an_untouched_bottle_is_not_evidence() -> None:
    adapter = DrinkAdapter()
    before = eating(BOTTLE, thirst=0.6)
    command = prepare(adapter, drink_command(), before)

    assert adapter.verify(command, before, eating(BOTTLE, thirst=0.6, seq=2)) is None


def test_an_empty_container_is_refused() -> None:
    empty = drink_item("43", remaining_units=0.0)

    with pytest.raises(PreconditionFailed) as caught:
        DrinkAdapter().validate(drink_command(), eating(empty))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_destroyed_container_is_refused() -> None:
    smashed = drink_item("43", destroyed=True)

    with pytest.raises(PreconditionFailed) as caught:
        DrinkAdapter().validate(drink_command(), eating(smashed))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_an_item_with_no_fluid_and_no_food_sub_object_is_refused_as_the_wrong_kind() -> None:
    hammer = ItemView(
        ref=BOTTLE.ref.replace(":43:", ":51:"),
        container_ref=MAIN_REF,
        full_type="Base.Hammer",
        display_name="Hammer",
        category="Tool",
        weight=1.0,
    )

    with pytest.raises(PreconditionFailed) as caught:
        DrinkAdapter().validate(drink_command(hammer.ref), eating(hammer))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_a_tin_of_beans_is_read_as_a_drink_and_refused_as_undrinkable() -> None:
    """A canned drink is a food item, so the kind check has to fall through to the flag."""
    with pytest.raises(PreconditionFailed) as caught:
        DrinkAdapter().validate(drink_command(BEANS.ref), eating(BEANS))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_bottle_in_a_bag_names_the_transfer_it_depends_on() -> None:
    in_bag = drink_item("43", container_ref=BAG_REF)

    with pytest.raises(PreconditionFailed) as caught:
        DrinkAdapter().validate(drink_command(in_bag.ref), eating(in_bag))

    assert caught.value.evidence["prerequisites"][0]["action"] == (
        ActionName.INVENTORY_ENSURE_MAIN.value
    )


def test_drinking_from_a_world_source_is_not_this_adapters_business() -> None:
    """§4.9 puts the sink and the well behind their own experimental probe."""
    with pytest.raises(PreconditionFailed) as caught:
        DrinkAdapter().validate(
            drink_command(source_ref=f"object:{DEFAULT_SESSION}:sink-1:0"), eating(BOTTLE)
        )
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_drinking_declares_the_capability_it_needs() -> None:
    assert DrinkAdapter().required_capability == DRINK_CARRIED
    assert DrinkAdapter().risk is RiskClass.P2


# --------------------------------------------------------------------------
# a square is asked about, not the first thing listed on it
# --------------------------------------------------------------------------

#: A water source is addressed by its *square*, and both sides agree on that:
#: ``source_ref`` is parsed as ``RefKind.SQUARE`` and the mod's Consumption
#: adapter reads it back the same way and looks for water on that square.
#: ``ObserveModel.buildObject`` accordingly mints the square's reference for
#: every non-container, non-door object, so several objects on one square
#: legitimately answer to one reference — and the mod scans several objects per
#: square by design.
WATER_SQUARE = f"square:{DEFAULT_SESSION}:1201:3400:0"


def _square_holding(*objects: NearbyObject) -> Observation:
    return a_world(
        items=[BOTTLE],
        containers=[main_container()],
        stats={"hunger": 0.5, "thirst": 0.5},
        objects=list(objects),
    )


def _a_thing(kind: str, *semantics: str) -> NearbyObject:
    return NearbyObject(
        ref=WATER_SQUARE,
        kind=kind,
        distance=1.0,
        position=Position(x=1201.0, y=3400.0, z=0),
        semantics=list(semantics),
    )


def _drink_from_source(observation: Observation) -> None:
    DrinkSourceAdapter().validate(
        a_command(
            ActionName.CONSUME_DRINK_SOURCE,
            {"item_ref": BOTTLE.ref, "source_ref": WATER_SQUARE},
        ),
        observation,
    )


def test_a_sink_alone_on_its_square_is_a_water_source() -> None:
    """The control: without it the two tests below could pass vacuously."""
    _drink_from_source(_square_holding(_a_thing("sink", "water_source")))


def test_a_sink_is_still_a_water_source_when_something_is_listed_before_it() -> None:
    """The question is about the square, not about its first occupant.

    Both objects carry the square's reference, because that is the reference
    scheme for anything that is not a container or a door. Resolving it with
    "the first entry that matches" answers about the tree and reports that a
    square with a sink on it has no water — a refusal that names a real sink as
    absent.
    """
    _drink_from_source(
        _square_holding(_a_thing("tree", "tree", "obstacle"), _a_thing("sink", "water_source"))
    )


def test_a_square_entry_beside_the_sink_does_not_hide_it() -> None:
    """The case that killed the first square-tier attempt.

    A square tier would put an entry describing the *ground* into
    ``nearby.objects`` carrying the square's own reference — the very reference
    the sink already answers to, since anything that is not a container or a door
    is referenced by its square. Sorted by distance the ground ties with
    everything standing on it, so the square entry can land first.

    Resolved with "the first entry that matches", that turned a sink into bare
    ground and refused the drink with ``NO_SAFE_DRINK``. Asking every object at
    the reference is what makes the tier survivable here; this test is the guard
    on that, and it is deliberately written against the shape a tier would
    produce rather than against any tier, because none is shipped.
    """
    ground = _a_thing("square", "loaded")
    _drink_from_source(_square_holding(ground, _a_thing("sink", "water_source")))


def test_drinking_from_a_source_is_proven_by_thirst_falling() -> None:
    """The postcondition of a registered adapter that nothing had ever run."""
    command = a_command(
        ActionName.CONSUME_DRINK_SOURCE,
        {"item_ref": BOTTLE.ref, "source_ref": WATER_SQUARE},
    )
    before = _square_holding(_a_thing("sink", "water_source"))
    after = a_world(
        seq=2,
        items=[BOTTLE],
        containers=[main_container()],
        stats={"hunger": 0.5, "thirst": 0.2},
        objects=[_a_thing("sink", "water_source")],
    )

    evidence = DrinkSourceAdapter().verify(command, before, after)

    assert evidence is not None
    assert evidence.kind == "thirst_decreased"
    assert evidence.observed["thirst_before"] == 0.5
    assert evidence.observed["thirst_after"] == 0.2
    # Carried on purpose: without it an ordinary sip from a bottle would stand
    # as confirmation of a capability nobody has seen work.
    assert evidence.observed["source_ref"] == WATER_SQUARE


def test_a_source_drink_that_did_not_move_thirst_proves_nothing() -> None:
    """Unchanged and *worse* both have to answer None, not just unchanged."""
    command = a_command(
        ActionName.CONSUME_DRINK_SOURCE,
        {"item_ref": BOTTLE.ref, "source_ref": WATER_SQUARE},
    )
    before = _square_holding(_a_thing("sink", "water_source"))

    for thirst in (0.5, 0.6):
        after = a_world(
            seq=2,
            items=[BOTTLE],
            containers=[main_container()],
            stats={"hunger": 0.5, "thirst": thirst},
            objects=[_a_thing("sink", "water_source")],
        )
        assert DrinkSourceAdapter().verify(command, before, after) is None


def test_drinking_from_a_source_declares_the_capability_it_needs() -> None:
    assert DrinkSourceAdapter().required_capability == DRINK_WORLD_SOURCE
    assert DrinkSourceAdapter().risk is RiskClass.P2


def test_a_square_with_nothing_watery_on_it_is_still_refused() -> None:
    """The fix must not turn the check into a formality."""
    with pytest.raises(PreconditionFailed) as caught:
        _drink_from_source(
            _square_holding(_a_thing("tree", "tree", "obstacle"), _a_thing("wall", "obstacle"))
        )

    assert caught.value.reason_code is ReasonCode.NO_SAFE_DRINK
    assert caught.value.evidence["kinds"] == ["tree", "wall"]


# --------------------------------------------------------------------------
# through the engine
# --------------------------------------------------------------------------


def run_eat(
    observations: list[Observation],
    *,
    repeated: Observation | None = None,
    acks: list[AckPlan] | None = None,
    capability: bool = True,
) -> tuple[FakeCommandSink, ActionResult]:
    clock = FakeClock()
    sink = FakeCommandSink(clock, acks=acks or [])
    source = FakeObservationSource(clock)
    source.push(*observations)
    if repeated is not None:
        source.repeat(repeated)
    registry = AdapterRegistry()
    registry.register(EatAdapter(timeout_ms=2_000, poll_interval_ms=250))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        capability_check=lambda _name: capability,
    )
    result = engine.execute(
        ActionRequest(
            action=ActionName.CONSUME_EAT,
            session_id=DEFAULT_SESSION,
            idempotency_key="eat",
            args={"item_ref": BEANS.ref},
            policy=CommandPolicy(max_retries=0),
        )
    )
    return sink, result


def test_a_mod_that_acks_a_meal_nobody_ate_fails() -> None:
    untouched = eating(BEANS, hunger=0.5)
    _sink, result = run_eat(
        [untouched],
        repeated=untouched,
        acks=[AckPlan(status=ActionStatus.SUCCEEDED, after_polls=1)],
    )

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.POSTCONDITION_FAILED


def test_the_meal_succeeds_once_hunger_is_observed_falling() -> None:
    fed = eating(BEANS, hunger=0.1, seq=2)
    _sink, result = run_eat([eating(BEANS, hunger=0.5), fed], repeated=fed)

    assert result.status is ActionStatus.SUCCEEDED
    assert result.evidence["kind"] == "hunger_decreased"


def test_an_unverified_eat_capability_sends_nothing() -> None:
    sink, result = run_eat([eating(BEANS, hunger=0.5)], capability=False)

    assert result.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert sink.sent == []


def test_a_precondition_failure_sends_nothing() -> None:
    sink, result = run_eat([eating(hunger=0.5)])

    assert result.reason_code is ReasonCode.INVALID_REF
    assert sink.sent == []
