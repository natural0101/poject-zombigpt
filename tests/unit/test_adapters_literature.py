"""``literature.read``.

The one thing this adapter must never do is report the queue. A book that is in
the character's hands with ``ISReadABook`` running and a page counter that has
not moved is a read that has not happened yet, and the tests below are mostly
that same sentence in different clothes.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import (
    ActionEngine,
    ActionRequest,
    AdapterRegistry,
    PreconditionFailed,
)
from pz_agent_core.actions.adapters import ReadAdapter
from pz_agent_core.actions.adapters.literature import (
    DEFAULT_READ_PAGES,
    MAX_READ_PAGES,
    READ_NO_PROGRESS_MS,
)
from pz_agent_core.capabilities import READ_LITERATURE
from pz_agent_core.policy.literature import TRAIT_ILLITERATE_STAT
from pz_agent_core.protocol import (
    ActionName,
    ActionOwnership,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    ItemView,
    Observation,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import DEFAULT_SESSION, make_action_state
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
from tests.fixtures.policy_items import literature_item

BOOK = literature_item("42")


def reading(
    *items: ItemView,
    seq: int = 1,
    illiterate: bool = False,
    action_type: str | None = None,
) -> Observation:
    stats: dict[str, object] = {}
    if illiterate:
        stats[TRAIT_ILLITERATE_STAT] = True
    action = (
        make_action_state(ownership=ActionOwnership.MOD, busy=True, type=action_type)
        if action_type is not None
        else make_action_state()
    )
    return a_world(
        seq=seq,
        items=list(items),
        containers=[main_container(), bag_container()],
        stats=stats,
        action=action,
    )


def read_command(item_ref: str = BOOK.ref, **extra: object) -> Command:
    args: dict[str, object] = {"item_ref": item_ref}
    args.update(extra)
    return a_command(ActionName.LITERATURE_READ, args)


def read_to(pages_read: int, *, runtime_id: str = "42") -> ItemView:
    return literature_item(runtime_id, pages_read=pages_read)


# --------------------------------------------------------------------------
# the postcondition
# --------------------------------------------------------------------------


def test_pages_advancing_to_the_target_is_the_evidence() -> None:
    adapter = ReadAdapter()
    before = reading(BOOK)
    command = prepare(adapter, read_command(pages=10), before)

    evidence = adapter.verify(command, before, reading(read_to(10), seq=2))

    assert evidence is not None
    assert evidence.kind == "reading_progress_advanced"
    assert evidence.observed["pages_before"] == 0
    assert evidence.observed["pages_after"] == 10
    assert evidence.observed["reading_started"] is True


def test_a_book_that_has_not_advanced_is_not_a_completed_read() -> None:
    adapter = ReadAdapter()
    before = reading(BOOK)
    command = prepare(adapter, read_command(pages=10), before)

    assert adapter.verify(command, before, reading(BOOK, seq=2)) is None


def test_a_reading_action_that_is_running_is_not_by_itself_evidence() -> None:
    """Started is a different result from read (§4.10)."""
    adapter = ReadAdapter()
    before = reading(BOOK)
    command = prepare(adapter, read_command(pages=10), before)
    busy = reading(BOOK, seq=2, action_type="ISReadABook")

    assert adapter.verify(command, before, busy) is None


def test_partial_progress_is_not_the_requested_read() -> None:
    adapter = ReadAdapter()
    before = reading(BOOK)
    command = prepare(adapter, read_command(pages=10), before)

    assert adapter.verify(command, before, reading(read_to(4), seq=2)) is None


def test_finishing_the_book_counts_even_when_the_target_was_higher() -> None:
    adapter = ReadAdapter()
    partly = literature_item("42", pages_read=195)
    before = reading(partly)
    command = a_command(ActionName.LITERATURE_READ, {"item_ref": partly.ref, "pages": 20})
    prepared = prepare(adapter, command, before)

    finished = literature_item("42", pages_read=200, already_read=True)
    evidence = adapter.verify(prepared, before, reading(finished, seq=2))

    assert evidence is not None
    assert evidence.observed["finished"] is True


def test_the_page_target_is_clamped_to_what_is_left_in_the_book() -> None:
    adapter = ReadAdapter()
    nearly_done = literature_item("42", pages_read=195)

    args = adapter.build_args(
        a_command(ActionName.LITERATURE_READ, {"item_ref": nearly_done.ref, "pages": 50}),
        reading(nearly_done),
    )

    assert args["pages"] == 5


def test_a_book_that_left_the_inventory_proves_nothing() -> None:
    adapter = ReadAdapter()
    before = reading(BOOK)
    command = prepare(adapter, read_command(pages=10), before)

    assert adapter.verify(command, before, a_world(seq=2, no_inventory=True)) is None
    assert adapter.verify(command, before, reading(seq=2)) is None


def test_progress_is_the_fraction_of_the_target_read() -> None:
    adapter = ReadAdapter()
    before = reading(BOOK)
    command = prepare(adapter, read_command(pages=10), before)

    assert adapter.progress(command, before, reading(read_to(5), seq=2)) == pytest.approx(0.5)
    assert adapter.progress(command, before, reading(BOOK, seq=2)) == 0.0


def test_the_no_progress_window_a_reader_needs_is_wider_than_one_page() -> None:
    """A page turn is slow; the engine default would cancel a healthy read."""
    assert ReadAdapter().poll_interval_ms < READ_NO_PROGRESS_MS


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_an_illiterate_character_is_refused_outright() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        ReadAdapter().validate(read_command(), reading(BOOK, illiterate=True))
    assert caught.value.reason_code is ReasonCode.POLICY_DENIED
    assert "Illiterate" in str(caught.value)


def test_an_item_that_is_not_literature_is_refused() -> None:
    hammer = ItemView(
        ref=BOOK.ref.replace(":42:", ":50:"),
        container_ref=MAIN_REF,
        full_type="Base.Hammer",
        display_name="Hammer",
        category="Tool",
        weight=1.0,
    )

    with pytest.raises(PreconditionFailed) as caught:
        ReadAdapter().validate(read_command(hammer.ref), reading(hammer))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_a_finished_book_is_refused() -> None:
    done = literature_item("42", already_read=True)

    with pytest.raises(PreconditionFailed) as caught:
        ReadAdapter().validate(read_command(), reading(done))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_destroyed_book_is_refused() -> None:
    ruined = literature_item("42", destroyed=True)

    with pytest.raises(PreconditionFailed) as caught:
        ReadAdapter().validate(read_command(), reading(ruined))
    assert caught.value.reason_code is ReasonCode.PRECONDITION_FAILED


def test_a_book_with_no_page_count_cannot_be_verified_so_it_is_not_started() -> None:
    countless = literature_item("42", pages_total=0)

    with pytest.raises(PreconditionFailed) as caught:
        ReadAdapter().validate(read_command(), reading(countless))
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_a_book_in_a_bag_names_the_transfer_it_depends_on() -> None:
    in_bag = literature_item("42", container_ref=BAG_REF)

    with pytest.raises(PreconditionFailed) as caught:
        ReadAdapter().validate(read_command(in_bag.ref), reading(in_bag))

    prerequisite = caught.value.evidence["prerequisites"][0]
    assert prerequisite["action"] == ActionName.INVENTORY_ENSURE_MAIN.value


def test_an_unresolvable_reference_is_an_invalid_ref() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        ReadAdapter().validate(read_command(), reading())
    assert caught.value.reason_code is ReasonCode.INVALID_REF


@pytest.mark.parametrize("pages", [0, -3, MAX_READ_PAGES + 1, 1.5, True])
def test_a_page_target_outside_the_bound_is_refused(pages: object) -> None:
    with pytest.raises(PreconditionFailed) as caught:
        ReadAdapter().validate(read_command(pages=pages), reading(BOOK))
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_the_default_target_is_a_chapter_rather_than_a_whole_book() -> None:
    args = ReadAdapter().build_args(read_command(), reading(BOOK))

    assert args["pages"] == DEFAULT_READ_PAGES
    assert DEFAULT_READ_PAGES < MAX_READ_PAGES


def test_reading_declares_the_capability_and_tier_it_needs() -> None:
    assert ReadAdapter().required_capability == READ_LITERATURE
    assert ReadAdapter().risk is RiskClass.P2


# --------------------------------------------------------------------------
# through the engine
# --------------------------------------------------------------------------


def run_read(
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
    registry.register(ReadAdapter(timeout_ms=2_000, poll_interval_ms=250))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        capability_check=lambda _name: capability,
        no_progress_ms=READ_NO_PROGRESS_MS,
    )
    result = engine.execute(
        ActionRequest(
            action=ActionName.LITERATURE_READ,
            session_id=DEFAULT_SESSION,
            idempotency_key="read",
            args={"item_ref": BOOK.ref, "pages": 10},
            policy=CommandPolicy(max_retries=0),
        )
    )
    return sink, result


def test_a_mod_that_acks_a_read_with_no_pages_turned_fails() -> None:
    unread = reading(BOOK, action_type="ISReadABook")
    _sink, result = run_read(
        [unread], repeated=unread, acks=[AckPlan(status=ActionStatus.SUCCEEDED, after_polls=1)]
    )

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.POSTCONDITION_FAILED


def test_the_read_succeeds_once_the_page_counter_reaches_the_target() -> None:
    done = reading(read_to(10), seq=2)
    _sink, result = run_read([reading(BOOK), done], repeated=done)

    assert result.status is ActionStatus.SUCCEEDED
    assert result.evidence["kind"] == "reading_progress_advanced"


def test_an_unverified_read_capability_sends_nothing() -> None:
    sink, result = run_read([reading(BOOK)], capability=False)

    assert result.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert sink.sent == []
