"""``medical.bandage``.

The postcondition here is narrower than the action sounds, and deliberately so:
the observation reports whether a part is *bleeding* and never reports whether it
is bandaged. So the only thing this adapter can prove is that the bleeding
stopped — and the tests below pin both halves of that: it is proven when the
flag clears, and it is refused up front for a wound where the flag was never set,
because such a command could only ever end in an ack taken on trust.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import (
    ActionEngine,
    ActionRequest,
    AdapterRegistry,
    PreconditionFailed,
)
from pz_agent_core.actions.adapters import BandageAdapter
from pz_agent_core.capabilities.probes import MEDICAL_BANDAGE
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    ContainerView,
    InventoryView,
    ItemView,
    Observation,
    ReasonCode,
    RiskClass,
    Wound,
)
from tests.fixtures import DEFAULT_SESSION, make_observation, make_player
from tests.fixtures.action_doubles import (
    AckPlan,
    FakeClock,
    FakeCommandSink,
    FakeObservationSource,
)
from tests.fixtures.adapter_worlds import (
    BAG_REF,
    MAIN_REF,
    a_command,
    an_item,
    bag_container,
    main_container,
    prepare,
)

ARM = "ForeArm_L"
LEG = "LowerLeg_R"

BANDAGE = an_item(
    "42",
    container_ref=MAIN_REF,
    display_name="Bandage",
    full_type="Base.Bandage",
    category="Medical",
)


def wound(part: str = ARM, *, bleeding: bool = True, severity: float = 0.4) -> Wound:
    return Wound(
        ref=f"wound:{DEFAULT_SESSION}:{part}",
        kind="cut",
        severity=severity,
        bleeding=bleeding,
    )


def hurt(
    *items: ItemView,
    wounds: list[Wound] | None = None,
    containers: list[ContainerView] | None = None,
    seq: int = 1,
    no_inventory: bool = False,
) -> Observation:
    """A character with a bleeding forearm and a bandage in the main inventory."""
    inventory = InventoryView(
        containers=containers if containers is not None else [main_container(), bag_container()],
        items=list(items),
    )
    return make_observation(
        seq=seq,
        player=make_player(wounds=[wound()] if wounds is None else wounds),
        inventory=None if no_inventory else inventory,
    )


def bandage_command(body_part: str = ARM, item_ref: str = BANDAGE.ref, **extra: object) -> Command:
    args: dict[str, object] = {"body_part": body_part, "item_ref": item_ref}
    args.update(extra)
    return a_command(ActionName.MEDICAL_BANDAGE, args)


# --------------------------------------------------------------------------
# the postcondition
# --------------------------------------------------------------------------


def test_the_bleeding_flag_clearing_is_the_evidence() -> None:
    adapter = BandageAdapter()
    before = hurt(BANDAGE)
    command = prepare(adapter, bandage_command(), before)

    after = hurt(BANDAGE, wounds=[wound(bleeding=False)], seq=2)
    evidence = adapter.verify(command, before, after)

    assert evidence is not None
    assert evidence.kind == "bleeding_stopped"
    assert evidence.observed["body_part"] == ARM
    assert evidence.observed["bleeding_before"] is True
    assert evidence.observed["bleeding_after"] is False


def test_a_wound_that_stopped_being_reported_at_all_is_a_dressed_wound() -> None:
    adapter = BandageAdapter()
    before = hurt(BANDAGE)
    command = prepare(adapter, bandage_command(), before)

    healed = hurt(BANDAGE, wounds=[], seq=2)
    evidence = adapter.verify(command, before, healed)

    assert evidence is not None
    assert evidence.observed["wound_still_reported"] is False


def test_a_part_still_bleeding_is_not_a_dressed_wound() -> None:
    adapter = BandageAdapter()
    before = hurt(BANDAGE)
    command = prepare(adapter, bandage_command(), before)

    assert adapter.verify(command, before, hurt(BANDAGE, seq=2)) is None


def test_another_part_clearing_does_not_answer_for_the_one_that_was_dressed() -> None:
    adapter = BandageAdapter()
    before = hurt(BANDAGE, wounds=[wound(ARM), wound(LEG)])
    command = prepare(adapter, bandage_command(), before)

    leg_only = hurt(BANDAGE, wounds=[wound(ARM), wound(LEG, bleeding=False)], seq=2)

    assert adapter.verify(command, before, leg_only) is None


def test_a_dressing_that_vanished_is_context_and_never_the_proof() -> None:
    """A bandage can leave the inventory by being dropped."""
    adapter = BandageAdapter()
    before = hurt(BANDAGE)
    command = prepare(adapter, bandage_command(), before)

    dropped = hurt(wounds=[wound()], seq=2)

    assert adapter.verify(command, before, dropped) is None


def test_a_used_up_dressing_is_recorded_alongside_the_stopped_bleeding() -> None:
    adapter = BandageAdapter()
    before = hurt(BANDAGE)
    command = prepare(adapter, bandage_command(), before)

    treated = hurt(wounds=[wound(bleeding=False)], seq=2)
    evidence = adapter.verify(command, before, treated)

    assert evidence is not None
    assert evidence.observed["dressing_consumed"] is True


def test_a_part_that_was_never_bleeding_cannot_be_shown_to_have_been_treated() -> None:
    adapter = BandageAdapter()
    before = hurt(BANDAGE, wounds=[wound(bleeding=False)])
    command = prepare(adapter, bandage_command(), hurt(BANDAGE))

    assert adapter.verify(command, before, hurt(BANDAGE, wounds=[], seq=2)) is None


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_a_part_that_is_not_bleeding_is_refused_because_nothing_could_prove_it() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        BandageAdapter().validate(bandage_command(), hurt(BANDAGE, wounds=[wound(bleeding=False)]))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_part_with_no_wound_at_all_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        BandageAdapter().validate(bandage_command(LEG), hurt(BANDAGE))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_body_part_this_character_does_not_have_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        BandageAdapter().validate(bandage_command("Tail"), hurt(BANDAGE))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_an_item_that_is_not_a_dressing_is_refused() -> None:
    hammer = an_item("50", container_ref=MAIN_REF, display_name="Hammer", full_type="Base.Hammer")

    with pytest.raises(PreconditionFailed) as caught:
        BandageAdapter().validate(bandage_command(item_ref=hammer.ref), hurt(hammer))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_a_dressing_in_a_bag_names_the_transfer_it_depends_on() -> None:
    in_bag = an_item(
        "42",
        container_ref=BAG_REF,
        display_name="Bandage",
        full_type="Base.Bandage",
        category="Medical",
    )

    with pytest.raises(PreconditionFailed) as caught:
        BandageAdapter().validate(bandage_command(item_ref=in_bag.ref), hurt(in_bag))

    prerequisite = caught.value.evidence["prerequisites"][0]
    assert prerequisite["action"] == ActionName.INVENTORY_ENSURE_MAIN.value


def test_naming_no_dressing_is_refused_because_the_choice_belongs_to_policy() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        BandageAdapter().validate(
            a_command(ActionName.MEDICAL_BANDAGE, {"body_part": ARM}), hurt(BANDAGE)
        )
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_an_observation_without_an_inventory_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        BandageAdapter().validate(bandage_command(), hurt(no_inventory=True))
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_the_part_and_the_dressing_are_what_the_mod_receives() -> None:
    args = BandageAdapter().build_args(bandage_command(), hurt(BANDAGE))

    assert args == {"body_part": ARM, "item_ref": BANDAGE.ref}


def test_bandaging_declares_the_capability_it_needs() -> None:
    assert BandageAdapter().required_capability == MEDICAL_BANDAGE
    assert BandageAdapter().risk is RiskClass.P2


# --------------------------------------------------------------------------
# through the engine
# --------------------------------------------------------------------------


def run_bandage(
    observations: list[Observation],
    *,
    repeated: Observation | None = None,
    acks: list[AckPlan] | None = None,
) -> ActionResult:
    clock = FakeClock()
    sink = FakeCommandSink(clock, acks=acks or [])
    source = FakeObservationSource(clock)
    source.push(*observations)
    if repeated is not None:
        source.repeat(repeated)
    registry = AdapterRegistry()
    registry.register(BandageAdapter(timeout_ms=2_000, poll_interval_ms=250))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        capability_check=lambda _name: True,
    )
    return engine.execute(
        ActionRequest(
            action=ActionName.MEDICAL_BANDAGE,
            session_id=DEFAULT_SESSION,
            idempotency_key="bandage",
            args={"body_part": ARM, "item_ref": BANDAGE.ref},
            policy=CommandPolicy(max_retries=0),
        )
    )


def test_a_mod_that_acks_a_dressing_nobody_applied_fails() -> None:
    """The one test the whole postcondition exists for."""
    still_bleeding = hurt(BANDAGE)
    result = run_bandage(
        [still_bleeding],
        repeated=still_bleeding,
        acks=[AckPlan(status=ActionStatus.SUCCEEDED, after_polls=1)],
    )

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.POSTCONDITION_FAILED


def test_the_dressing_succeeds_once_the_bleeding_is_observed_stopping() -> None:
    treated = hurt(BANDAGE, wounds=[wound(bleeding=False)], seq=2)
    result = run_bandage([hurt(BANDAGE), treated], repeated=treated)

    assert result.status is ActionStatus.SUCCEEDED
    assert result.evidence["kind"] == "bleeding_stopped"
