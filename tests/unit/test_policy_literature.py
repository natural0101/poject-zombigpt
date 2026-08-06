"""Literature selection: traits, level windows, pages read, and the goal.

Every refusal in this file is checked for the *reason* it reports, because the
explanation is the deliverable: "you are already level 4" is what the agent
says out loud when it declines to read the beginner book.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from pz_agent_core.policy import (
    LiteratureGoal,
    LiteratureGoalKind,
    LiteratureKind,
    LiteratureSelection,
    PolicyConfig,
    RejectionReason,
    select_literature,
)
from pz_agent_core.policy.literature import (
    SKILL_STAT_PREFIX,
    TRAIT_ILLITERATE_STAT,
    LiteratureView,
    skill_level,
)
from pz_agent_core.protocol import InventoryView, ReasonCode
from tests.fixtures import make_item
from tests.fixtures.policy_items import (
    BACKPACK_REF,
    MAIN_REF,
    inventory,
    literature_item,
    main_container,
    policy_item_ref,
    reader,
)

BOREDOM = LiteratureGoal.boredom()


def reason_for(selection: LiteratureSelection, item_ref: str) -> RejectionReason:
    for rejection in selection.rejections:
        if rejection.item_ref == item_ref:
            return rejection.reason
    raise AssertionError(
        f"{item_ref} was not rejected; got {[r.as_dict() for r in selection.rejections]}"
    )


def carpentry_book(runtime_id: str = "book", **overrides: Any) -> Any:
    base: dict[str, Any] = {
        "display_name": "Carpentry 1",
        "full_type": "Base.BookCarpentry1",
        "kind": "skill_book",
        "skill": "Carpentry",
        "min_level": 0,
        "max_level": 2,
    }
    base.update(overrides)
    return literature_item(runtime_id, **base)


def test_empty_inventory_refuses_with_no_suitable_literature() -> None:
    selection = select_literature(InventoryView(), reader(), BOREDOM)

    assert selection.is_refusal
    assert selection.reason_code is ReasonCode.NO_SUITABLE_LITERATURE
    assert selection.rejections == ()
    assert "nothing to read" in selection.explain()


def test_a_paperback_answers_a_boredom_goal() -> None:
    book = literature_item("paperback")

    selection = select_literature(inventory(book), reader(), BOREDOM)

    choice = selection.choice
    assert choice is not None
    assert choice.item_ref == book.ref
    assert choice.kind is LiteratureKind.GENERIC
    assert choice.pages_remaining == 200
    assert not choice.needs_transfer_to_main


def test_an_illiterate_character_rejects_every_candidate_by_name() -> None:
    novel = literature_item("novel", display_name="Novel")
    manual = carpentry_book("manual")

    selection = select_literature(
        inventory(novel, manual), reader(**{TRAIT_ILLITERATE_STAT: True}), BOREDOM
    )

    assert selection.is_refusal
    assert reason_for(selection, novel.ref) is RejectionReason.ILLITERATE
    assert reason_for(selection, manual.ref) is RejectionReason.ILLITERATE
    assert "Illiterate" in selection.explain()


def test_a_finished_book_is_refused_and_says_so() -> None:
    finished = literature_item("finished", pages_read=200)

    selection = select_literature(inventory(finished), reader(), BOREDOM)

    assert selection.is_refusal
    assert reason_for(selection, finished.ref) is RejectionReason.ALREADY_READ
    assert "200 pages" in selection.rejections[0].detail


def test_the_already_read_flag_is_honoured_without_a_page_count() -> None:
    finished = literature_item("finished", pages_total=0, pages_read=0, already_read=True)

    selection = select_literature(inventory(finished), reader(), BOREDOM)

    assert reason_for(selection, finished.ref) is RejectionReason.ALREADY_READ


def test_rereading_can_be_permitted_by_configuration() -> None:
    finished = literature_item("finished", pages_read=200)

    selection = select_literature(
        inventory(finished), reader(), BOREDOM, PolicyConfig(allow_reread=True)
    )

    assert selection.choice is not None


def test_a_book_the_user_named_may_be_read_again() -> None:
    # Re-reading is worthless in game and refused by default, but "read this
    # one" is an instruction, not the policy wasting the character's time.
    finished = literature_item("finished", display_name="The Requested Book", pages_read=200)

    refused = select_literature(inventory(finished), reader(), BOREDOM)
    assert reason_for(refused, finished.ref) is RejectionReason.ALREADY_READ

    requested = select_literature(
        inventory(finished), reader(), LiteratureGoal.specific(finished.ref)
    )
    assert requested.choice is not None
    assert requested.choice.item_ref == finished.ref


def test_a_specific_request_does_not_unlock_rereading_every_other_book() -> None:
    wanted = literature_item("wanted", display_name="Wanted")
    other_finished = literature_item(
        "other", display_name="Other", full_type="Base.Book2", pages_read=200
    )

    selection = select_literature(
        inventory(other_finished, wanted), reader(), LiteratureGoal.specific(wanted.ref)
    )

    assert selection.choice is not None
    assert selection.choice.item_ref == wanted.ref
    assert reason_for(selection, other_finished.ref) is RejectionReason.ALREADY_READ


def test_a_book_below_the_characters_level_is_refused_with_the_window() -> None:
    beginner = carpentry_book("beginner")

    selection = select_literature(
        inventory(beginner),
        reader(**{f"{SKILL_STAT_PREFIX}Carpentry": 4}),
        LiteratureGoal.train("Carpentry"),
    )

    assert selection.is_refusal
    assert reason_for(selection, beginner.ref) is RejectionReason.SKILL_TOO_HIGH
    assert "level 4" in selection.rejections[0].detail


def test_a_book_above_the_characters_level_is_refused() -> None:
    advanced = carpentry_book("advanced", display_name="Carpentry 4", min_level=6, max_level=8)

    selection = select_literature(
        inventory(advanced),
        reader(**{f"{SKILL_STAT_PREFIX}Carpentry": 1}),
        LiteratureGoal.train("Carpentry"),
    )

    assert reason_for(selection, advanced.ref) is RejectionReason.SKILL_TOO_LOW


def test_a_book_for_another_skill_is_refused_by_name() -> None:
    cooking = carpentry_book(
        "cooking", display_name="Cooking 1", skill="Cooking", full_type="Base.BookCooking1"
    )

    selection = select_literature(inventory(cooking), reader(), LiteratureGoal.train("Carpentry"))

    assert reason_for(selection, cooking.ref) is RejectionReason.WRONG_SKILL
    assert "Carpentry" in selection.rejections[0].detail


def test_the_wrong_skill_is_reported_even_when_the_book_is_also_outgrown() -> None:
    # Both filters match; the goal mismatch is the one the user asked about.
    cooking = carpentry_book(
        "cooking",
        display_name="Cooking 1",
        skill="Cooking",
        full_type="Base.BookCooking1",
        min_level=0,
        max_level=2,
    )

    selection = select_literature(
        inventory(cooking),
        reader(**{f"{SKILL_STAT_PREFIX}Cooking": 9}),
        LiteratureGoal.train("Carpentry"),
    )

    assert reason_for(selection, cooking.ref) is RejectionReason.WRONG_SKILL


def test_a_newspaper_cannot_train_a_skill() -> None:
    paper = literature_item(
        "paper", display_name="Newspaper", full_type="Base.Newspaper", kind="newspaper"
    )

    selection = select_literature(inventory(paper), reader(), LiteratureGoal.train("Carpentry"))

    assert reason_for(selection, paper.ref) is RejectionReason.WRONG_KIND_FOR_GOAL


def test_the_lowest_still_useful_skill_book_wins() -> None:
    tier_one = carpentry_book("tier1", display_name="Carpentry 1", min_level=0, max_level=2)
    tier_two = carpentry_book(
        "tier2",
        display_name="Carpentry 3",
        full_type="Base.BookCarpentry2",
        min_level=0,
        max_level=4,
    )

    selection = select_literature(
        inventory(tier_two, tier_one),
        reader(**{f"{SKILL_STAT_PREFIX}Carpentry": 1}),
        LiteratureGoal.train("Carpentry"),
    )

    assert selection.choice is not None
    assert selection.choice.item_ref == tier_one.ref


def test_a_recipe_goal_skips_magazines_with_nothing_new_in_them() -> None:
    known = literature_item(
        "known",
        display_name="Read Cookbook",
        full_type="Base.CookingMag1",
        kind="magazine",
        unread_recipes=0,
    )
    fresh = literature_item(
        "fresh",
        display_name="New Cookbook",
        full_type="Base.CookingMag2",
        kind="magazine",
        unread_recipes=2,
    )

    selection = select_literature(inventory(known, fresh), reader(), LiteratureGoal.recipe())

    assert selection.choice is not None
    assert selection.choice.item_ref == fresh.ref
    assert reason_for(selection, known.ref) is RejectionReason.NO_UNREAD_RECIPES


def test_a_specific_request_rejects_everything_else() -> None:
    wanted = literature_item("wanted", display_name="The Requested Book")
    other = literature_item("other", display_name="Something Else", full_type="Base.Book2")

    selection = select_literature(
        inventory(other, wanted), reader(), LiteratureGoal.specific(wanted.ref)
    )

    assert selection.choice is not None
    assert selection.choice.item_ref == wanted.ref
    assert reason_for(selection, other.ref) is RejectionReason.NOT_REQUESTED_ITEM


def test_a_boredom_goal_prefers_a_magazine_over_a_skill_book() -> None:
    magazine = literature_item(
        "mag", display_name="Magazine", full_type="Base.Magazine", kind="magazine"
    )
    manual = carpentry_book("manual")

    selection = select_literature(inventory(manual, magazine), reader(), BOREDOM)

    assert selection.choice is not None
    assert selection.choice.item_ref == magazine.ref


def test_a_partly_read_book_is_finished_before_a_fresh_one_is_opened() -> None:
    started = literature_item("started", display_name="Started", pages_read=150)
    untouched = literature_item("untouched", display_name="Untouched", full_type="Base.Book2")

    selection = select_literature(inventory(untouched, started), reader(), BOREDOM)

    assert selection.choice is not None
    assert selection.choice.item_ref == started.ref


@pytest.mark.parametrize(
    "marker",
    [{"favorite": True}, {"tags": ["reserved"]}, {"extra": {"reserved": True}}],
)
def test_user_reserved_books_are_never_taken(marker: dict[str, Any]) -> None:
    kept = literature_item("kept", **marker)

    selection = select_literature(inventory(kept), reader(), BOREDOM)

    assert reason_for(selection, kept.ref) is RejectionReason.USER_RESERVED


def test_a_destroyed_book_is_refused() -> None:
    ruined = literature_item("ruined", destroyed=True)

    selection = select_literature(inventory(ruined), reader(), BOREDOM)

    assert reason_for(selection, ruined.ref) is RejectionReason.DESTROYED


def test_an_unreachable_book_is_refused() -> None:
    stray = literature_item("stray", container_ref=BACKPACK_REF)

    selection = select_literature(
        inventory(stray, containers=[main_container()]), reader(), BOREDOM
    )

    assert reason_for(selection, stray.ref) is RejectionReason.CONTAINER_UNREACHABLE


def test_non_literature_items_are_not_reported_as_rejections() -> None:
    hammer = make_item(
        policy_item_ref("hammer"), MAIN_REF, full_type="Base.Hammer", category="Item", food=None
    )

    selection = select_literature(inventory(hammer), reader(), BOREDOM)

    assert selection.is_refusal
    assert selection.rejections == ()


def test_selection_is_identical_under_shuffled_input() -> None:
    items = [
        literature_item("a", display_name="Novel", full_type="Base.Book"),
        literature_item("b", display_name="Started Novel", full_type="Base.Book2", pages_read=40),
        literature_item(
            "c",
            display_name="Magazine",
            full_type="Base.Magazine",
            kind="magazine",
            unread_recipes=1,
        ),
        literature_item(
            "d", display_name="Newspaper", full_type="Base.Newspaper", kind="newspaper"
        ),
        carpentry_book("e"),
        literature_item("f", display_name="Read Novel", full_type="Base.Book3", pages_read=200),
    ]
    player = reader(**{f"{SKILL_STAT_PREFIX}Carpentry": 1})
    baseline = select_literature(inventory(*items), player, BOREDOM)

    for seed in range(8):
        shuffled = list(items)
        random.Random(seed).shuffle(shuffled)
        result = select_literature(inventory(*shuffled), player, BOREDOM)

        assert result.choice is not None and baseline.choice is not None
        assert result.choice.item_ref == baseline.choice.item_ref
        assert [c.item_ref for c in result.ranked] == [c.item_ref for c in baseline.ranked]
        assert result.choice.rationale == baseline.choice.rationale


def test_ties_break_on_the_item_reference() -> None:
    first = literature_item("aaa", display_name="Twin", full_type="Base.Twin")
    second = literature_item("zzz", display_name="Twin", full_type="Base.Twin")

    forward = select_literature(inventory(first, second), reader(), BOREDOM)
    backward = select_literature(inventory(second, first), reader(), BOREDOM)

    assert forward.choice is not None and backward.choice is not None
    assert forward.choice.item_ref == backward.choice.item_ref == min(first.ref, second.ref)


def test_the_rejection_list_is_bounded_and_says_what_it_left_out() -> None:
    config = PolicyConfig(max_reported_rejections=2)
    shelf = [
        literature_item(f"b{index}", full_type=f"Base.Book{index}", pages_read=200)
        for index in range(8)
    ]

    selection = select_literature(inventory(*shelf), reader(), BOREDOM, config)

    assert selection.is_refusal
    assert len(selection.rejections) == 2
    assert selection.rejected_count == 8
    assert selection.rejections_truncated
    assert "6 further rejected candidates" in selection.explain()
    assert selection.as_dict()["rejected_count"] == 8


def test_a_selection_cannot_undercount_the_candidates_it_refused() -> None:
    rejection = select_literature(
        inventory(literature_item("done", pages_read=200)), reader(), BOREDOM
    ).rejections[0]

    with pytest.raises(ValueError, match="is below the"):
        LiteratureSelection(
            choice=None,
            reason_code=ReasonCode.NO_SUITABLE_LITERATURE,
            goal=BOREDOM,
            rejections=(rejection,),
            ranked=(),
            rejected_count=0,
        )


def test_a_goal_must_carry_what_it_needs() -> None:
    with pytest.raises(ValueError, match="name the skill"):
        LiteratureGoal(kind=LiteratureGoalKind.TRAIN_SKILL)
    with pytest.raises(ValueError, match="name the item"):
        LiteratureGoal(kind=LiteratureGoalKind.READ_SPECIFIC)


def test_skill_level_reading_is_case_insensitive_and_defaults_to_zero() -> None:
    player = reader(**{f"{SKILL_STAT_PREFIX}carpentry": 3})

    assert skill_level(player, "carpentry") == 3
    assert skill_level(player, "Carpentry") == 3
    assert skill_level(player, "Cooking") == 0


def test_a_book_that_names_a_skill_is_classified_as_a_skill_book() -> None:
    item = literature_item("book", kind="", skill="Farming")

    view = LiteratureView.from_item(item)

    assert view is not None
    assert view.kind is LiteratureKind.SKILL_BOOK


def test_the_refusal_payload_carries_the_goal_and_the_failed_requirements() -> None:
    beginner = carpentry_book("beginner")

    payload = select_literature(
        inventory(beginner),
        reader(**{f"{SKILL_STAT_PREFIX}Carpentry": 5}),
        LiteratureGoal.train("Carpentry"),
    ).as_dict()

    assert payload["choice"] is None
    assert payload["reason_code"] == ReasonCode.NO_SUITABLE_LITERATURE.value
    assert payload["goal"] == {
        "kind": LiteratureGoalKind.TRAIN_SKILL.value,
        "skill": "Carpentry",
        "item_ref": "",
    }
    assert payload["rejections"][0]["reason"] == RejectionReason.SKILL_TOO_HIGH.value
