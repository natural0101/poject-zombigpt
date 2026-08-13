"""The craft mission: one run per command, and the product counted afterwards.

Driven at the mission seam like the care, avoid and combat missions, because
these are the promises the crafting wave's safety argument turns into
behaviour:

* the deterministic crafting policy decides before every run, so which recipe
  spends which materials is an answer about the bag as it is now;
* one ``crafting.craft`` per attempt, ``count`` pinned at the adapter's own
  one-run ceiling, bounded by an attempt budget and a recipe budget — never an
  "until it works" loop;
* a failed craft retires its recipe, and a policy that names it again ends the
  mission rather than retrying an irreversible command;
* a shortfall is a report: the mission names what is short and stops, and it
  never emits a step that goes looking for materials;
* success is the product observed in the inventory, measured against the
  observation the mission started from — never the craft being queued;
* every refusal's ``detail`` fits the one bounded printable line a goal record
  will accept, however the game spelled the material it is naming.
"""

from __future__ import annotations

from typing import Any

import pytest

from pz_agent_cli.craft_mission import (
    CRAFT_PHASES,
    ENDED_EXHAUSTED,
    ENDED_RECIPES_SPENT,
    ENDED_REFUSED,
    MAX_CRAFT_ATTEMPTS,
    MAX_DETAIL_TYPE_CHARS,
    MAX_RECIPES_TRIED,
    REFUSAL_REASONS,
    CraftItemMission,
    CraftMissionLimits,
)
from pz_agent_cli.loot_mission import (
    ENDED_COMPLETE,
    ENDED_NO_PROGRESS,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
)
from pz_agent_core.actions.adapters import crafting as crafting_adapter
from pz_agent_core.actions.adapters.crafting import MAX_CRAFT_COUNT
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.goals.model import MAX_DETAIL_CHARS, GoalKind, GoalState, GoalTransition
from pz_agent_core.policy.config import PolicyConfig
from pz_agent_core.policy.crafting import CRAFTING_KEY, CraftingRefusal
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ItemView,
    Observation,
    ReasonCode,
)
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.adapter_worlds import MAIN_REF, a_world, an_item, main_container

GOAL_ID = "7c2a1d40-0000-4000-8000-0000000000c1"

SPEAR = "Base.SpearCrude"
BRANCH = "Base.TreeBranch"
TWINE = "Base.Twine"

MAKE_SPEAR = "MakeSpear"
CARVE_SPEAR = "CarveSpear"


# --------------------------------------------------------------------------
# worlds
# --------------------------------------------------------------------------


def recipe_payload(
    name: str = MAKE_SPEAR,
    *,
    product: str = SPEAR,
    materials: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A recipe the character knows, needing one branch, craftable in hand."""
    payload: dict[str, Any] = {
        "name": name,
        "product": product,
        "display_name": "Make Crude Spear",
        "known": True,
        "needs_surface": False,
        "materials": materials if materials is not None else [{"full_type": BRANCH, "count": 1}],
    }
    payload.update(overrides)
    return payload


def material(
    runtime_id: str = "m1",
    *,
    full_type: str = BRANCH,
    recipes: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> ItemView:
    """One material item, carrying the crafting readout it participates in."""
    return an_item(
        runtime_id=runtime_id,
        container_ref=MAIN_REF,
        full_type=full_type,
        display_name=full_type,
        category="Item",
        extra={
            CRAFTING_KEY: {
                "recipe_count": 1,
                "known_recipe_count": 1,
                "recipes": recipes if recipes is not None else [recipe_payload()],
            }
        },
        **overrides,
    )


def made(runtime_id: str = "p1", full_type: str = SPEAR) -> ItemView:
    """A finished product: no readout of its own, it is what a recipe makes."""
    return an_item(
        runtime_id=runtime_id,
        container_ref=MAIN_REF,
        full_type=full_type,
        display_name=full_type,
        category="Item",
    )


def world(seq: int = 1, *items: ItemView, no_inventory: bool = False) -> Observation:
    return a_world(
        seq=seq,
        items=list(items),
        containers=[main_container()],
        no_inventory=no_inventory,
    )


def stocked(seq: int = 1, *extra: ItemView) -> Observation:
    """The workable world: one branch, the spear recipe on it, nothing made."""
    return world(seq, material(), *extra)


def succeeded(request: ActionRequest, *, seq: int, evidence: dict[str, object]) -> ActionResult:
    return ActionResult.succeeded(
        session_id=request.session_id,
        seq=seq,
        command_id=f"c-{seq}",
        action=request.action.value,
        timestamp_ms=1_700_000_000_000 + seq,
        evidence=dict(evidence),
    )


def failed(request: ActionRequest, *, seq: int, reason: ReasonCode) -> ActionResult:
    return ActionResult.failure(
        session_id=request.session_id,
        seq=seq,
        command_id=f"c-{seq}",
        action=request.action.value,
        timestamp_ms=1_700_000_000_000 + seq,
        reason_code=reason,
    )


def crafted(request: ActionRequest, *, seq: int) -> ActionResult:
    return succeeded(
        request,
        seq=seq,
        evidence={"recipe": MAKE_SPEAR, "product": SPEAR, "count_before": 0, "count_after": 1},
    )


def step_of(move: object) -> ActionRequest:
    assert isinstance(move, MissionStep)
    return move.request


def mission(**kwargs: Any) -> CraftItemMission:
    return CraftItemMission(GOAL_ID, product=SPEAR, **kwargs)


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


class TestTheCraft:
    def test_one_command_then_the_observed_product_completes(self) -> None:
        """One run, one bounded command, and the ending comes from the bag —
        not from the craft's own result."""
        craft = mission()

        first = step_of(craft.next_step(stocked(1)))
        assert first.action is ActionName.CRAFTING_CRAFT
        assert first.args == {"recipe": MAKE_SPEAR, "count": MAX_CRAFT_COUNT}
        assert first.idempotency_key == f"craft:{GOAL_ID}:s1"
        craft.note_result(crafted(first, seq=2))

        done = craft.next_step(world(3, made()))
        assert isinstance(done, MissionComplete)
        assert craft.ended == ENDED_COMPLETE
        assert craft.report["made"] == 1
        assert craft.report["attempts"] == 1
        assert craft.report["crafts_succeeded"] == 1
        assert craft.report["recipes_tried"] == [MAKE_SPEAR]

    def test_a_succeeded_craft_whose_product_is_not_there_yet_crafts_again(self) -> None:
        """The swing is never trusted: a craft the mod called succeeded, with
        nothing new in the bag, is another run rather than a completion."""
        craft = mission()
        first = step_of(craft.next_step(stocked(1)))
        craft.note_result(crafted(first, seq=2))

        second = step_of(craft.next_step(stocked(3)))
        assert second.action is ActionName.CRAFTING_CRAFT
        assert second.idempotency_key == f"craft:{GOAL_ID}:s2"
        assert craft.report["made"] == 0

    def test_two_wanted_takes_two_commands_and_completes_on_the_second(self) -> None:
        craft = mission(count=2)

        first = step_of(craft.next_step(stocked(1, material("m2"))))
        craft.note_result(crafted(first, seq=2))
        halfway = craft.next_step(world(3, material("m2"), made("p1")))
        second = step_of(halfway)
        assert second.action is ActionName.CRAFTING_CRAFT
        assert craft.report["made"] == 1
        craft.note_result(crafted(second, seq=4))

        done = craft.next_step(world(5, made("p1"), made("p2")))
        assert isinstance(done, MissionComplete)
        assert craft.report["made"] == 2
        assert craft.report["attempts"] == 2

    def test_the_count_is_measured_against_the_starting_observation(self) -> None:
        """«сделай копьё» when one is already carried means one *more*."""
        craft = mission()
        first = step_of(craft.next_step(stocked(1, made("old"))))
        craft.note_result(crafted(first, seq=2))

        done = craft.next_step(world(3, made("old"), made("new")))
        assert isinstance(done, MissionComplete)
        assert craft.report["made"] == 1

    def test_one_step_in_flight_at_a_time(self) -> None:
        craft = mission()
        step_of(craft.next_step(stocked(1)))
        assert craft.next_step(stocked(2)) is None

    def test_the_phases_are_the_closed_tokens(self) -> None:
        assert CRAFT_PHASES == ("start", "craft")


# --------------------------------------------------------------------------
# the policy decides, and the mission reports what it said
# --------------------------------------------------------------------------


class TestThePolicyDecides:
    def test_missing_materials_refuse_typed_and_name_what_is_short(self) -> None:
        """The wave's central promise: short of materials is a report, with
        the shortfall named, and nothing spent."""
        readout = recipe_payload(
            materials=[{"full_type": BRANCH, "count": 1}, {"full_type": TWINE, "count": 2}]
        )
        ended = mission().next_step(world(1, material(recipes=[readout])))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.RECIPE_MATERIALS_MISSING
        assert TWINE in ended.detail
        assert "0 of 2" in ended.detail

    def test_a_shortfall_never_becomes_an_errand(self) -> None:
        """The mission does not go looting for what is missing: chaining a
        craft onto a loot is the user's next sentence, not this goal's private
        decision. The first move is the refusal, and there is no step at all."""
        short = world(1, material(recipes=[recipe_payload(materials=[{"full_type": TWINE}])]))
        craft = mission()

        first = craft.next_step(short)

        assert isinstance(first, MissionRefused)
        assert craft.report["attempts"] == 0
        assert craft.report["recipes_tried"] == []
        # And it stays refused: a second look at the same world does not
        # promote the mission into going and fetching.
        assert craft.next_step(world(2, material())) is first

    def test_an_unknown_recipe_refuses_with_the_recipe_reason(self) -> None:
        unlearned = world(1, material(recipes=[recipe_payload(known=None)]))

        ended = mission().next_step(unlearned)

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.RECIPE_UNKNOWN
        assert "not observed to have learned" in ended.detail

    def test_nothing_that_makes_the_product_refuses_by_name(self) -> None:
        ended = mission().next_step(world(1, material(recipes=[recipe_payload(product=TWINE)])))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.RECIPE_UNKNOWN
        assert "no observed item participates" in ended.detail

    def test_a_reserve_is_a_refusal_the_user_can_lift_not_a_missing_material(self) -> None:
        """The loot policy's unbreakable rule, honoured here: "make me a
        spear" is not permission to break what the player marked as kept."""
        kept = material(favorite=True)

        ended = mission(policy=PolicyConfig()).next_step(world(1, kept))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.RESOURCE_RESERVED
        assert mission().report["refusal"] is None

    def test_the_refusal_table_is_total_and_matches_the_adapter(self) -> None:
        """Two copies of one table, knowingly — and pinned equal here rather
        than left to a comment. The adapter's is private to it because it
        refuses at validation time; this one refuses before a command exists."""
        assert set(REFUSAL_REASONS) == set(CraftingRefusal)
        assert REFUSAL_REASONS == crafting_adapter._REFUSAL_REASONS

    def test_the_report_carries_the_policy_token_and_the_shortfalls(self) -> None:
        readout = recipe_payload(materials=[{"full_type": TWINE, "count": 3}])
        craft = mission()

        craft.next_step(world(1, material(recipes=[readout])))

        report = craft.report
        assert report["refusal"] == CraftingRefusal.MATERIALS_MISSING.value
        assert report["shortfalls"] == [{"full_type": TWINE, "needed": 3, "free": 0, "reserved": 0}]
        assert report["ended"] == ENDED_REFUSED
        # A requirement line the character's own bags do not cover is travel
        # by definition, which is the fact the craft adapter escalates its
        # risk tier on — worth carrying in the report even on a refusal.
        assert report["needs_travel"] is True


# --------------------------------------------------------------------------
# nothing runs twice
# --------------------------------------------------------------------------


class TestNoRetryOfAnIrreversibleCommand:
    def test_a_failed_craft_retires_its_recipe(self) -> None:
        """The policy naming the same recipe again is the policy saying
        nothing has changed — which ends the mission rather than spending the
        materials on a second identical guess."""
        craft = mission(limits=CraftMissionLimits(max_consecutive_failures=2))
        first = step_of(craft.next_step(stocked(1)))
        craft.note_result(failed(first, seq=2, reason=ReasonCode.ACTION_TIMEOUT))

        ended = craft.next_step(stocked(3))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.NO_PROGRESS
        assert "already failed" in ended.detail
        assert craft.ended == ENDED_NO_PROGRESS
        assert craft.report["attempts"] == 1

    def test_a_second_recipe_is_a_decision_and_a_third_is_a_search(self) -> None:
        """A different recipe is the policy genuinely changing its mind, which
        is worth serving; past the recipe budget it is a search paid for in
        materials, which is not."""
        craft = mission(count=2, limits=CraftMissionLimits(max_recipes=1))
        first = step_of(craft.next_step(stocked(1)))
        assert first.args["recipe"] == MAKE_SPEAR
        craft.note_result(crafted(first, seq=2))

        # One made, and the only way left to make another is a second recipe.
        other = material(
            "m2",
            full_type=TWINE,
            recipes=[recipe_payload(CARVE_SPEAR, materials=[{"full_type": TWINE, "count": 1}])],
        )
        ended = craft.next_step(world(3, other, made("p1")))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.NO_PROGRESS
        assert craft.ended == ENDED_RECIPES_SPENT
        assert craft.report["recipes_tried"] == [MAKE_SPEAR]

    def test_the_attempt_budget_ends_the_mission_short(self) -> None:
        craft = mission(count=2, limits=CraftMissionLimits(max_attempts=1))
        first = step_of(craft.next_step(stocked(1, material("m2"))))
        craft.note_result(crafted(first, seq=2))

        ended = craft.next_step(world(3, material("m2"), made("p1")))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.NO_PROGRESS
        assert craft.ended == ENDED_EXHAUSTED
        assert craft.report["made"] == 1

    def test_consecutive_failures_end_the_mission(self) -> None:
        craft = mission(limits=CraftMissionLimits(max_consecutive_failures=1))
        first = step_of(craft.next_step(stocked(1)))
        craft.note_result(failed(first, seq=2, reason=ReasonCode.CAPABILITY_UNAVAILABLE))

        ended = craft.next_step(stocked(3))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.NO_PROGRESS
        assert "consecutive craft commands failed" in ended.detail

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("max_attempts", MAX_CRAFT_ATTEMPTS + 1),
            ("max_recipes", MAX_RECIPES_TRIED + 1),
            ("max_attempts", 0),
            ("max_recipes", 0),
        ],
    )
    def test_the_limits_ceilings_cannot_be_raised(self, field: str, value: int) -> None:
        with pytest.raises(ValueError, match=field):
            CraftMissionLimits(**{field: value})

    def test_one_run_per_command_is_the_adapters_own_ceiling(self) -> None:
        # Not a restatement: a widening of the adapter's ceiling should be a
        # review here too, which only holds while the mission imports it.
        assert MAX_CRAFT_COUNT == 1
        first = step_of(mission().next_step(stocked(1)))
        assert first.args["count"] == MAX_CRAFT_COUNT


# --------------------------------------------------------------------------
# the edges
# --------------------------------------------------------------------------


class TestTheEdges:
    def test_a_missing_inventory_tier_is_a_typed_refusal(self) -> None:
        """No inventory is no proof: a craft whose product could not be
        counted afterwards must not be started."""
        ended = mission().next_step(world(1, no_inventory=True))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE

    def test_a_product_that_arrives_without_a_succeeded_command_takes_the_probe(self) -> None:
        """Real, not formal: the craft failed as far as this side could tell,
        and the spear is in the bag anyway. There is no result of ours to
        succeed the goal with, so the bounded probe earns one."""
        craft = mission(limits=CraftMissionLimits(max_consecutive_failures=2))
        first = step_of(craft.next_step(stocked(1)))
        craft.note_result(failed(first, seq=2, reason=ReasonCode.ACTION_TIMEOUT))

        move = craft.next_step(world(3, made()))

        assert isinstance(move, MissionProbe)
        assert move.request.action is ActionName.MOVEMENT_MOVE_TO
        assert move.request.idempotency_key == f"craft:{GOAL_ID}:done1"
        assert craft.ended == ENDED_COMPLETE
        assert craft.any_success is False

    def test_a_sealed_ending_replays(self) -> None:
        craft = mission()
        first = craft.next_step(world(1, no_inventory=True))
        second = craft.next_step(stocked(2))
        assert first is second

    def test_mark_abandoned_seals_only_a_mission_in_flight(self) -> None:
        running = mission()
        step_of(running.next_step(stocked(1)))
        running.mark_abandoned()
        assert running.ended == "cancelled"

        finished = mission()
        finished.next_step(world(1, no_inventory=True))
        finished.mark_abandoned()
        assert finished.ended == ENDED_NO_PROGRESS

    def test_results_for_foreign_actions_are_ignored(self) -> None:
        craft = mission()
        step = step_of(craft.next_step(stocked(1)))
        foreign = succeeded(
            ActionRequest(
                action=ActionName.MOVEMENT_MOVE_TO,
                session_id=step.session_id,
                idempotency_key="someone-else",
                args={},
            ),
            seq=2,
            evidence={"position": {"x": 1}},
        )
        craft.note_result(foreign)
        # Still waiting on its own step: the foreign result changed nothing.
        assert craft.next_step(stocked(3)) is None

    def test_a_mission_must_name_a_product_and_a_positive_count(self) -> None:
        with pytest.raises(ValueError, match="product"):
            CraftItemMission(GOAL_ID, product="")
        with pytest.raises(ValueError, match="at least one"):
            CraftItemMission(GOAL_ID, product=SPEAR, count=0)
        with pytest.raises(ValueError, match="goal_id"):
            CraftItemMission("", product=SPEAR)


# --------------------------------------------------------------------------
# what a goal record will actually accept
# --------------------------------------------------------------------------


class TestTheDetailFitsTheGoalChannel:
    """A refusal the queue cannot record is a goal that never ends.

    ``GoalQueue.fail`` builds a record whose ``detail`` must be one printable
    line of at most :data:`MAX_DETAIL_CHARS` characters, and the material names
    in a craft refusal come from the game. These are the tests that keep a
    modded item type from turning a typed failure into an exception.
    """

    def hostile_world(self) -> Observation:
        """A readout spelled as unpleasantly as a mod can spell one."""
        nasty = "Mod.\n\tA" + "Ω" * 40 + "_" * 200
        readout = recipe_payload(
            name=nasty,
            materials=[
                {"full_type": nasty, "count": 10**9},
                {"full_type": "Ω" * 30, "count": 7},
                {"full_type": "Mod.AlsoMissing", "count": 2},
            ],
        )
        return world(1, material(recipes=[readout]))

    def test_the_refusal_detail_is_one_bounded_printable_line(self) -> None:
        craft = mission(count=10**9)

        ended = craft.next_step(self.hostile_world())

        assert isinstance(ended, MissionRefused)
        assert len(ended.detail) <= MAX_DETAIL_CHARS
        assert "\n" not in ended.detail and "\t" not in ended.detail
        # The goal channel's own validator, run on the real string: this is
        # the constructor GoalQueue.fail's transition goes through.
        GoalTransition(
            goal_id=GOAL_ID,
            kind=GoalKind.CRAFT_ITEM,
            previous=GoalState.ACTIVE,
            state=GoalState.FAILED,
            reason_code=ended.reason_code,
            at_ms=1,
            detail=ended.detail,
        )

    def test_an_unnameable_type_is_named_rather_than_left_blank(self) -> None:
        readout = recipe_payload(materials=[{"full_type": "Ω Ω Ω", "count": 1}])

        ended = mission().next_step(world(1, material(recipes=[readout])))

        assert isinstance(ended, MissionRefused)
        assert "unnamed" in ended.detail

    def test_only_the_first_two_shortfalls_reach_the_line(self) -> None:
        readout = recipe_payload(
            materials=[
                {"full_type": "Mod.One", "count": 1},
                {"full_type": "Mod.Two", "count": 1},
                {"full_type": "Mod.Three", "count": 1},
            ]
        )
        craft = mission()

        ended = craft.next_step(world(1, material(recipes=[readout])))

        assert isinstance(ended, MissionRefused)
        assert "Mod.Three" not in ended.detail
        # The whole list is still in the report, which is where a reader who
        # wants all of it looks.
        assert len(craft.report["shortfalls"]) == 3

    def test_the_summary_line_is_constants_and_counts_only(self) -> None:
        craft = mission()
        craft.next_step(world(1, no_inventory=True))
        line = craft.summary_line()
        assert line == "craft no_progress: made=0/1 attempts=0 recipes=0 refusal=none"
        assert "\n" not in line and DEFAULT_SESSION not in line

    def test_a_type_longer_than_the_detail_bound_is_truncated_not_dropped(self) -> None:
        long_type = "Mod." + "A" * 120
        readout = recipe_payload(materials=[{"full_type": long_type, "count": 1}])

        ended = mission().next_step(world(1, material(recipes=[readout])))

        assert isinstance(ended, MissionRefused)
        assert long_type[:MAX_DETAIL_TYPE_CHARS] in ended.detail
        assert long_type not in ended.detail
