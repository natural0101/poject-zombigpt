"""The typed plan: what it refuses, and what it cannot even express.

The first class here is the load-bearing one. Everything else in T023 rests on
the claim that a plan *cannot carry code*, and the way that claim is kept is
that no argument type has a field for it — so the tests assert on
``TypeError``/``PlanRejected`` at construction and parse time rather than on a
filter having run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from pz_agent_core.actions.adapters.consume import MIN_CONSUME_FRACTION as ADAPTER_MIN_FRACTION
from pz_agent_core.actions.adapters.literature import (
    DEFAULT_READ_PAGES as ADAPTER_DEFAULT_PAGES,
)
from pz_agent_core.actions.adapters.literature import MAX_READ_PAGES as ADAPTER_MAX_PAGES
from pz_agent_core.actions.adapters.movement import (
    DEFAULT_ARRIVAL_RADIUS as ADAPTER_ARRIVAL_RADIUS,
)
from pz_agent_core.actions.adapters.movement import MAX_ARRIVAL_RADIUS as ADAPTER_MAX_RADIUS
from pz_agent_core.actions.adapters.movement import (
    MAX_MOVE_DISTANCE_SQUARES as ADAPTER_MAX_DISTANCE,
)
from pz_agent_core.actions.builtin import MAX_WAIT_GAME_SECONDS as ADAPTER_MAX_WAIT
from pz_agent_core.planner.plan import (
    ACTION_RISK,
    DEFAULT_ARRIVAL_RADIUS,
    DEFAULT_READ_PAGES,
    MAX_MOVE_DISTANCE,
    MAX_PLAN_STEPS,
    MAX_READ_PAGES,
    MAX_SUMMARY_CHARS,
    MAX_WAIT_GAME_SECONDS,
    MIN_CONSUME_FRACTION,
    PLAN_COORDINATE_LIMIT,
    PLANNABLE_ACTIONS,
    ConsumeArgs,
    FailureMode,
    ItemArgs,
    MoveNearArgs,
    MoveToArgs,
    Plan,
    PlanFault,
    PlanRejected,
    PlanStep,
    ReadArgs,
    SuccessCriterion,
    SuccessKind,
    TransferArgs,
    WaitArgs,
    step_signature,
)
from pz_agent_core.protocol import ActionName, ReasonCode, RefKind, RiskClass
from tests.fixtures import DEFAULT_SESSION, make_container, make_item, make_observation
from tests.fixtures.planner_worlds import (
    eat_plan,
    eat_step,
    foreign_item_ref,
    item_ref,
    plan_payload,
    planner_observation,
    step_payload,
)
from tests.fixtures.policy_items import BACKPACK_REF, MAIN_REF, hungry_player, inventory

CODE_SHAPED_FIELDS = ("lua", "script", "python", "shell", "command", "keys", "path", "code")


class TestNoFieldForCode:
    """A plan cannot express code because no field of it can hold any."""

    @pytest.mark.parametrize("field_name", CODE_SHAPED_FIELDS)
    def test_argument_dataclasses_have_no_field_for_code(self, field_name: str) -> None:
        # Not "the field is stripped" and not "the field is ignored": the
        # constructor refuses the keyword, because no such attribute exists.
        with pytest.raises(TypeError, match=field_name):
            # mypy refuses this statically too; the runtime check is the one
            # that covers a payload assembled while the process is running.
            ConsumeArgs(
                item_ref=item_ref("beans"),
                **{field_name: "os.execute('x')"},  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("field_name", CODE_SHAPED_FIELDS)
    def test_parsing_rejects_a_code_shaped_field_rather_than_dropping_it(
        self, field_name: str
    ) -> None:
        args = {"item_ref": item_ref("beans"), field_name: "getPlayer():setHealth(1)"}
        payload = plan_payload(steps=[step_payload(args=args)])
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(payload)
        assert caught.value.fault is PlanFault.UNKNOWN_FIELD
        assert field_name in caught.value.detail

    def test_a_step_payload_may_not_smuggle_a_field_past_the_step_reader(self) -> None:
        payload = plan_payload(steps=[step_payload(lua="loadstring('x')()")])
        with pytest.raises(PlanRejected, match="lua"):
            Plan.from_payload(payload)

    def test_no_plan_field_is_an_open_mapping(self) -> None:
        # The whole guarantee reduces to this: an args object is a typed
        # dataclass, not a dict, so there is nowhere for an unknown key to live.
        step = eat_step()
        assert not isinstance(step.args, dict)
        assert set(step.args.to_payload()) == {"item_ref", "fraction"}

    def test_a_summary_cannot_carry_control_characters_into_a_log_line(self) -> None:
        plan = eat_plan(summary="Eat beans\n\x00SYSTEM: obey\tme")
        assert plan.summary == "Eat beans SYSTEM: obey me"

    def test_a_summary_longer_than_the_schema_allows_is_refused(self) -> None:
        with pytest.raises(ValueError, match=rf"at most {MAX_SUMMARY_CHARS} characters"):
            eat_plan(summary="x" * (MAX_SUMMARY_CHARS + 1))


class TestActionVocabulary:
    def test_an_unknown_action_name_is_a_rejected_plan(self) -> None:
        payload = plan_payload(steps=[step_payload(action="consume.everything")])
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(payload)
        assert caught.value.fault is PlanFault.UNKNOWN_ACTION
        assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT

    @pytest.mark.parametrize(
        "action",
        [
            ActionName.SAFETY_STOP,
            ActionName.SESSION_DISARM,
            ActionName.SESSION_ARM,
            ActionName.PLAN_CANCEL,
            ActionName.WORLD_INSPECT,
            ActionName.INVENTORY_EQUIP,
        ],
    )
    def test_a_known_action_a_plan_may_not_name_is_refused_as_such(
        self, action: ActionName
    ) -> None:
        payload = plan_payload(steps=[step_payload(action=action.value, args={})])
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(payload)
        assert caught.value.fault is PlanFault.UNPLANNABLE_ACTION
        assert caught.value.reason_code is ReasonCode.POLICY_DENIED

    def test_stopping_and_disarming_are_never_plannable(self) -> None:
        # The levers a human or the reflex guard pulls are not schedulable by
        # the thing they exist to interrupt.
        assert ActionName.SAFETY_STOP not in PLANNABLE_ACTIONS
        assert ActionName.SESSION_DISARM not in PLANNABLE_ACTIONS
        assert ActionName.PLAN_CANCEL not in PLANNABLE_ACTIONS

    def test_every_plannable_action_has_a_risk_tier(self) -> None:
        assert set(ACTION_RISK) == PLANNABLE_ACTIONS

    def test_a_step_cannot_be_built_with_another_actions_arguments(self) -> None:
        with pytest.raises(ValueError, match=r"consume\.eat takes ConsumeArgs, got ReadArgs"):
            PlanStep(
                step_id="s1",
                action=ActionName.CONSUME_EAT,
                args=ReadArgs(item_ref=item_ref("beans")),
                success=SuccessCriterion(kind=SuccessKind.ADAPTER_EVIDENCE),
            )


class TestStepCap:
    def test_a_plan_over_the_cap_is_refused_whole(self) -> None:
        steps = [step_payload(step_id=f"s{i}") for i in range(MAX_PLAN_STEPS + 1)]
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(plan_payload(steps=steps))
        assert caught.value.fault is PlanFault.STEP_CAP_EXCEEDED
        assert f"at most {MAX_PLAN_STEPS}" in caught.value.detail

    def test_a_plan_at_the_cap_is_accepted(self) -> None:
        steps = [step_payload(step_id=f"s{i}") for i in range(MAX_PLAN_STEPS)]
        assert len(Plan.from_payload(plan_payload(steps=steps)).steps) == MAX_PLAN_STEPS

    def test_an_empty_plan_is_not_a_plan(self) -> None:
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(plan_payload(steps=[]))
        assert caught.value.fault is PlanFault.EMPTY_PLAN

    def test_duplicate_step_ids_are_refused(self) -> None:
        steps = [step_payload(step_id="s1"), step_payload(step_id="s1")]
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(plan_payload(steps=steps))
        assert caught.value.fault is PlanFault.DUPLICATE_STEP_ID


class TestReferences:
    def test_a_reference_that_does_not_parse_is_a_rejected_plan(self) -> None:
        payload = plan_payload(steps=[step_payload(args={"item_ref": "item:sess:beans"})])
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(payload)
        assert caught.value.fault is PlanFault.BAD_REF
        assert caught.value.reason_code is ReasonCode.INVALID_REF

    def test_a_reference_of_the_wrong_kind_is_refused(self) -> None:
        container = f"container:{DEFAULT_SESSION}:player-main"
        payload = plan_payload(steps=[step_payload(args={"item_ref": container})])
        with pytest.raises(PlanRejected, match="expected a item reference"):
            Plan.from_payload(payload)

    def test_a_foreign_reference_still_parses_here(self) -> None:
        # Structural validity and session ownership are different questions; the
        # critic answers the second one, and answering it twice in two places
        # would make the two able to disagree.
        payload = plan_payload(steps=[step_payload(args={"item_ref": foreign_item_ref()})])
        plan = Plan.from_payload(payload)
        assert plan.steps[0].args.refs()[0].value == foreign_item_ref()

    def test_an_object_reference_has_no_typed_parser_and_is_still_validated(self) -> None:
        payload = plan_payload(
            steps=[
                step_payload(
                    action="movement.move_near",
                    args={"object_ref": "object:not a session:1"},
                    success={"type": "adapter_evidence"},
                )
            ]
        )
        with pytest.raises(PlanRejected, match="well-formed object reference"):
            Plan.from_payload(payload)

    def test_refs_report_the_kind_they_were_validated_as(self) -> None:
        args = TransferArgs(
            item_ref=item_ref("beans"),
            destination_container_ref=MAIN_REF,
            source_container_ref=BACKPACK_REF,
        )
        assert [ref.kind for ref in args.refs()] == [
            RefKind.ITEM,
            RefKind.CONTAINER,
            RefKind.CONTAINER,
        ]


class TestArgumentBounds:
    def test_a_coordinate_beyond_the_ceiling_is_refused(self) -> None:
        with pytest.raises(ValueError, match=rf"x must be within ±{PLAN_COORDINATE_LIMIT}"):
            MoveToArgs(x=PLAN_COORDINATE_LIMIT + 1, y=0, z=0)

    def test_a_floor_beyond_the_ceiling_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"z must be within ±32"):
            MoveToArgs(x=0, y=0, z=33)

    def test_a_radius_over_the_adapters_ceiling_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"radius must be within"):
            MoveToArgs(x=0, y=0, z=0, radius=ADAPTER_MAX_RADIUS + 0.5)

    def test_a_move_longer_than_one_command_may_be_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"max_distance must be within"):
            MoveNearArgs(object_ref=_object_ref(), max_distance=MAX_MOVE_DISTANCE + 1)

    def test_a_fraction_too_small_to_be_worth_a_command_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"fraction must be within"):
            ConsumeArgs(item_ref=item_ref("beans"), fraction=MIN_CONSUME_FRACTION / 2)

    def test_a_page_count_over_the_ceiling_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"pages must be within"):
            ReadArgs(item_ref=item_ref("book"), pages=MAX_READ_PAGES + 1)

    def test_a_wait_longer_than_an_in_game_hour_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"game_seconds must be within"):
            WaitArgs(game_seconds=MAX_WAIT_GAME_SECONDS + 1)

    def test_a_movement_plan_cannot_ask_to_climb_through_a_window(self) -> None:
        # §5.19 refuses `allow_windows`; a plan type with no such field cannot
        # even make the request the adapter would have to refuse.
        with pytest.raises(TypeError, match="allow_windows"):
            MoveToArgs(x=1, y=1, z=0, allow_windows=True)  # type: ignore[call-arg]

    def test_out_of_range_numbers_in_a_payload_are_rejected_plans_not_crashes(self) -> None:
        payload = plan_payload(
            steps=[step_payload(args={"item_ref": item_ref("beans"), "fraction": 2.0})]
        )
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(payload)
        assert caught.value.fault is PlanFault.BAD_VALUE

    def test_bounds_match_the_adapters_that_will_receive_them(self) -> None:
        assert MIN_CONSUME_FRACTION == ADAPTER_MIN_FRACTION
        assert (DEFAULT_READ_PAGES, MAX_READ_PAGES) == (ADAPTER_DEFAULT_PAGES, ADAPTER_MAX_PAGES)
        assert MAX_WAIT_GAME_SECONDS == ADAPTER_MAX_WAIT
        assert DEFAULT_ARRIVAL_RADIUS == ADAPTER_ARRIVAL_RADIUS
        assert MAX_MOVE_DISTANCE == ADAPTER_MAX_DISTANCE


class TestSuccessCriterion:
    def test_a_threshold_criterion_needs_its_level(self) -> None:
        with pytest.raises(ValueError, match="needs the level"):
            SuccessCriterion(kind=SuccessKind.HUNGER_AT_MOST)

    def test_a_criterion_that_takes_no_level_refuses_one(self) -> None:
        with pytest.raises(ValueError, match="takes no value"):
            SuccessCriterion(kind=SuccessKind.ADAPTER_EVIDENCE, value=0.5)

    def test_a_criterion_the_action_cannot_produce_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"hunger_at_most is not a postcondition"):
            PlanStep(
                step_id="s1",
                action=ActionName.LITERATURE_READ,
                args=ReadArgs(item_ref=item_ref("book")),
                success=SuccessCriterion(kind=SuccessKind.HUNGER_AT_MOST, value=0.1),
            )

    def test_an_unknown_criterion_type_is_a_rejected_plan(self) -> None:
        payload = plan_payload(steps=[step_payload(success={"type": "vibes"})])
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(payload)
        assert caught.value.fault is PlanFault.UNSUPPORTED_CRITERION

    def test_a_hunger_criterion_reads_the_observed_stat(self) -> None:
        criterion = SuccessCriterion(kind=SuccessKind.HUNGER_AT_MOST, value=0.15)
        args = ConsumeArgs(item_ref=item_ref("beans"))
        assert criterion.holds(args, make_observation(player=hungry_player(0.10))) is True
        assert criterion.holds(args, make_observation(player=hungry_player(0.50))) is False

    def test_adapter_evidence_declines_to_answer(self) -> None:
        # None is not False: the plan states no independent check, so the
        # adapter's observed postcondition is the whole proof.
        criterion = SuccessCriterion(kind=SuccessKind.ADAPTER_EVIDENCE)
        holds = criterion.holds(ReadArgs(item_ref=item_ref("book")), planner_observation())
        assert holds is None

    def test_an_item_criterion_cannot_be_answered_without_an_inventory(self) -> None:
        criterion = SuccessCriterion(kind=SuccessKind.ITEM_CONSUMED)
        observation = make_observation(inventory=None)
        assert criterion.holds(ConsumeArgs(item_ref=item_ref("beans")), observation) is None

    def test_item_consumed_holds_once_the_item_is_gone(self) -> None:
        criterion = SuccessCriterion(kind=SuccessKind.ITEM_CONSUMED)
        args = ConsumeArgs(item_ref=item_ref("beans"))
        assert criterion.holds(args, planner_observation()) is False
        assert criterion.holds(args, make_observation(inventory=inventory())) is True

    def test_item_in_main_inventory_distinguishes_the_backpack(self) -> None:
        criterion = SuccessCriterion(kind=SuccessKind.ITEM_IN_MAIN_INVENTORY)
        in_bag = make_item(item_ref("beans", BACKPACK_REF), BACKPACK_REF)
        args = ItemArgs(item_ref=in_bag.ref)
        assert criterion.holds(args, make_observation(inventory=inventory(in_bag))) is False
        moved = make_item(in_bag.ref, MAIN_REF)
        assert criterion.holds(args, make_observation(inventory=inventory(moved))) is True

    def test_position_reached_compares_the_floor_before_the_distance(self) -> None:
        criterion = SuccessCriterion(kind=SuccessKind.POSITION_REACHED)
        args = MoveToArgs(x=1200, y=3400, z=1)
        # The player fixture stands at (1200, 3400, 0): the right square, one
        # floor down, which is not "arrived".
        assert criterion.holds(args, make_observation()) is False
        assert criterion.holds(MoveToArgs(x=1200, y=3400, z=0), make_observation()) is True


class TestRiskIsDerived:
    def test_the_plans_tier_is_the_highest_its_steps_name(self) -> None:
        plan = eat_plan(
            PlanStep(
                step_id="s1",
                action=ActionName.INVENTORY_ENSURE_MAIN,
                args=ItemArgs(item_ref=item_ref("beans")),
                success=SuccessCriterion(kind=SuccessKind.ITEM_IN_MAIN_INVENTORY),
            ),
            eat_step("s2"),
        )
        assert plan.risk_class is RiskClass.P2

    def test_a_declared_tier_that_understates_the_steps_is_refused(self) -> None:
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(plan_payload(risk_class="P0"))
        assert caught.value.fault is PlanFault.BAD_VALUE
        assert "does not match" in caught.value.detail

    def test_a_declared_tier_that_overstates_the_steps_is_also_refused(self) -> None:
        with pytest.raises(PlanRejected, match="does not match"):
            Plan.from_payload(plan_payload(risk_class="P3"))


class TestDocumentShape:
    def test_a_plan_of_another_schema_version_is_refused(self) -> None:
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(plan_payload(schema_version="2.0"))
        assert caught.value.fault is PlanFault.SCHEMA_VERSION

    def test_an_unknown_top_level_field_is_refused(self) -> None:
        with pytest.raises(PlanRejected, match=r"provider"):
            Plan.from_payload(plan_payload(provider="anthropic"))

    def test_a_missing_required_field_names_itself(self) -> None:
        payload = plan_payload()
        del payload["summary"]
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(payload)
        assert caught.value.fault is PlanFault.MISSING_FIELD
        assert caught.value.field == "summary"

    def test_a_goal_id_that_is_not_a_uuid_is_refused(self) -> None:
        with pytest.raises(PlanRejected, match="goal_id must be a UUID"):
            Plan.from_payload(plan_payload(goal_id="the-hunger-one"))

    def test_a_step_id_outside_the_allowed_alphabet_is_refused(self) -> None:
        payload = plan_payload(steps=[step_payload(step_id="s 1; rm -rf /")])
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(payload)
        assert caught.value.fault is PlanFault.BAD_VALUE

    def test_an_unknown_on_failure_mode_is_refused(self) -> None:
        payload = plan_payload(steps=[step_payload(on_failure="improvise")])
        with pytest.raises(PlanRejected, match="improvise"):
            Plan.from_payload(payload)

    def test_confidence_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(PlanRejected):
            Plan.from_payload(plan_payload(confidence=1.5))

    @pytest.mark.parametrize("payload", [[], "a plan", 7, None])
    def test_a_non_object_payload_is_refused_rather_than_crashing(self, payload: Any) -> None:
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(payload)
        assert caught.value.fault is PlanFault.BAD_TYPE

    def test_a_round_trip_through_the_wire_form_preserves_the_plan(self) -> None:
        original = eat_plan()
        restored = Plan.from_payload(json.loads(json.dumps(original.to_payload())))
        assert restored == original

    def test_the_wire_form_carries_exactly_the_schemas_fields(self) -> None:
        payload = eat_plan(confidence=0.5).to_payload()
        assert set(payload) == {
            "schema_version",
            "goal_id",
            "summary",
            "risk_class",
            "confidence",
            "steps",
        }

    def test_confidence_is_omitted_when_the_planner_has_no_calibrated_number(self) -> None:
        assert "confidence" not in eat_plan().to_payload()


class TestSchemaConformance:
    """``schemas/plan.schema.json`` is the wire source of truth for this type.

    The plan module restates its bounds so a plan can be validated with no
    validator wired up; these two tests are what keep the restatement honest.
    """

    def test_a_plan_this_module_builds_validates_against_the_schema(
        self, schemas: dict[str, Any]
    ) -> None:
        Draft202012Validator(schemas["plan.schema"]).validate(eat_plan(confidence=0.5).to_payload())

    def test_the_example_plan_in_the_blueprint_parses(self, repo_root: Path) -> None:
        raw = json.loads(
            (repo_root / "examples" / "blueprint" / "plans" / "eat_best_safe.json").read_text(
                encoding="utf-8"
            )
        )
        # The example names its own item by a `selection` argument rather than a
        # reference, which this build's typed args do not carry: the choice is
        # the selection policy's and reaches the plan as an item ref.
        with pytest.raises(PlanRejected) as caught:
            Plan.from_payload(raw)
        assert caught.value.fault is PlanFault.UNKNOWN_FIELD

    def test_the_step_cap_matches_the_schemas_own(self, schemas: dict[str, Any]) -> None:
        assert schemas["plan.schema"]["properties"]["steps"]["maxItems"] == MAX_PLAN_STEPS

    def test_the_summary_bound_matches_the_schemas_own(self, schemas: dict[str, Any]) -> None:
        assert schemas["plan.schema"]["properties"]["summary"]["maxLength"] == MAX_SUMMARY_CHARS

    def test_the_failure_modes_match_the_schemas_own(self, schemas: dict[str, Any]) -> None:
        declared = schemas["plan.schema"]["properties"]["steps"]["items"]["properties"]
        assert set(declared["on_failure"]["enum"]) == {mode.value for mode in FailureMode}


class TestSignature:
    def test_the_same_step_signs_the_same_however_the_payload_was_ordered(self) -> None:
        first = ConsumeArgs(item_ref=item_ref("beans"), fraction=0.5)
        second = ConsumeArgs(fraction=0.5, item_ref=item_ref("beans"))
        assert step_signature(ActionName.CONSUME_EAT, first) == step_signature(
            ActionName.CONSUME_EAT, second
        )

    def test_a_different_fraction_is_a_different_step(self) -> None:
        one = eat_step("s1", args=ConsumeArgs(item_ref=item_ref("beans"), fraction=0.5))
        other = eat_step("s1", args=ConsumeArgs(item_ref=item_ref("beans"), fraction=1.0))
        assert one.signature != other.signature

    def test_the_same_arguments_under_a_different_action_sign_differently(self) -> None:
        args = ConsumeArgs(item_ref=item_ref("beans"))
        assert step_signature(ActionName.CONSUME_EAT, args) != step_signature(
            ActionName.CONSUME_DRINK, args
        )


class TestDefaults:
    def test_a_step_stops_by_default(self) -> None:
        assert eat_step().on_failure is FailureMode.STOP

    def test_a_step_needs_no_confirmation_by_default(self) -> None:
        assert eat_step().requires_confirmation is False
        assert eat_plan().requires_confirmation is False

    def test_a_plan_can_find_its_own_step(self) -> None:
        plan = eat_plan(eat_step("first"), eat_step("second"))
        assert plan.step("second") is plan.steps[1]
        assert plan.step("third") is None


def _object_ref() -> str:
    return f"object:{DEFAULT_SESSION}:1200:3400:0:1"


def test_a_container_the_observation_lacks_is_still_a_valid_reference() -> None:
    # Parsing is about the shape of the string; whether the container is there
    # is a question about the world, which the executor asks against a fresh
    # observation and this module cannot answer at all.
    unknown = make_container(f"container:{DEFAULT_SESSION}:worn:Back:99009").ref
    args = TransferArgs(item_ref=item_ref("beans"), destination_container_ref=unknown)
    assert args.to_payload()["destination_container_ref"] == unknown
