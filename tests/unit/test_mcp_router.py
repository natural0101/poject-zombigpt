"""The translation layer: the gates, the honest statuses, and what never leaks."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.capabilities.probes import (
    DOOR_TOGGLE,
    DRINK_CARRIED,
    DRINK_WORLD_SOURCE,
    EAT_PERCENTAGE,
    EQUIPMENT_EQUIP,
    EQUIPMENT_UNEQUIP,
    INVENTORY_TRANSFER,
    MEDICAL_BANDAGE,
    MOVE_TO_SQUARE,
    READ_LITERATURE,
    SURVIVAL_REST,
    SURVIVAL_SLEEP,
)
from pz_agent_core.observation.compact import (
    CONTENT_MARKER,
    REDACTED_PATH,
    UNTRUSTED_TEXT_KEY,
    save_scope,
)
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    ContainerKind,
    InventoryView,
    ReasonCode,
    RiskClass,
    SessionMode,
)
from pz_agent_mcp import router as router_module
from pz_agent_mcp.catalog import TOOLS, ToolKind, ToolSpec
from pz_agent_mcp.envelope import MAX_DIAGNOSTICS
from pz_agent_mcp.idempotency import IdempotencyCache
from pz_agent_mcp.ports import DoctorCheck, LogRecord, MemoryRecord, PlanRecord, PlanStepRecord
from pz_agent_mcp.router import ToolRouter
from tests.fixtures import (
    DEFAULT_SAVE,
    DEFAULT_SESSION,
    backpack_container_ref,
    item_ref,
    main_container_ref,
    make_container,
    make_item,
    make_observation,
)
from tests.fixtures.mcp_doubles import (
    HOSTILE_NAME,
    CountingIds,
    Doubles,
    failed_result,
    make_report,
    succeeded_result,
)
from tests.fixtures.safety_builders import make_nearby, make_object, make_zombie

ALL_CAPABILITIES = (
    MOVE_TO_SQUARE,
    DOOR_TOGGLE,
    INVENTORY_TRANSFER,
    EAT_PERCENTAGE,
    DRINK_CARRIED,
    DRINK_WORLD_SOURCE,
    READ_LITERATURE,
    EQUIPMENT_EQUIP,
    EQUIPMENT_UNEQUIP,
    MEDICAL_BANDAGE,
    SURVIVAL_REST,
    SURVIVAL_SLEEP,
)

#: A username that cannot occur by accident inside another word.
USERNAME = "zzuser"

#: A second session id, for the rule that idempotency keys do not cross one.
OTHER_SESSION = str(uuid.UUID(int=0x5E5511))

BEAN_REF = item_ref("501", "player-main")
BOOK_REF = item_ref("502", "worn:Back:99001")
MAIN_REF = main_container_ref()
CRATE_REF = f"container:{DEFAULT_SESSION}:world:1200:3400:0:crate:0"
SQUARE_REF = f"square:{DEFAULT_SESSION}:1200:3400:0"
DOOR_REF = f"object:{DEFAULT_SESSION}:1200:3401:0:2"

#: Windows drive paths, UNC paths and POSIX system paths — §3.13 forbids all of
#: them from crossing this boundary in any field.
PATH_SHAPED = re.compile(r"[A-Za-z]:[\\/]|\\\\[^\s\\]+\\|/(?:home|Users|mnt|etc|opt)/")


def full_report() -> Any:
    return make_report(usable=list(ALL_CAPABILITIES))


def armed_doubles(**overrides: Any) -> Doubles:
    doubles = Doubles()
    doubles.capabilities.report_value = full_report()
    for name, value in overrides.items():
        setattr(doubles, name, value)
    return doubles


def make_router(doubles: Doubles, **kwargs: Any) -> ToolRouter:
    return ToolRouter(doubles.services, request_ids=CountingIds(), **kwargs)


def rich_observation(**overrides: Any) -> Any:
    """An observation with a container tree, a nearby scan, and hostile text in it."""
    containers = [
        make_container(main_container_ref(), ContainerKind.PLAYER_MAIN),
        make_container(backpack_container_ref(), ContainerKind.CARRIED, name=HOSTILE_NAME),
        make_container(
            f"container:{DEFAULT_SESSION}:world:1200:3400:0:crate:0",
            ContainerKind.WORLD,
            parent_ref=None,
        ),
    ]
    items = [
        make_item(BEAN_REF, main_container_ref(), display_name=HOSTILE_NAME, category="Food"),
        make_item(
            BOOK_REF,
            backpack_container_ref(),
            display_name=f"Diary from C:\\Users\\{USERNAME}\\docs",
            category="Literature",
            full_type="Base.Book",
            literature={"kind": "book", "pages": 40, "pages_read": 3},
        ),
    ]
    base: dict[str, Any] = {
        "inventory": InventoryView(containers=containers, items=items),
        "nearby": make_nearby(
            make_zombie("31", 4.0, chasing=True),
            make_zombie("32", 19.0),
            objects=(
                make_object("81", 3.0, kind="container", semantics=["storage"]),
                make_object("82", 25.0, kind="door", semantics=["door"]),
            ),
        ),
    }
    base.update(overrides)
    return make_observation(**base)


def strings(payload: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every string in a payload, with the key path that carries it."""
    if isinstance(payload, str):
        yield path, payload
    elif isinstance(payload, dict):
        for key, value in payload.items():
            yield from strings(key, f"{path}.<key>")
            yield from strings(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from strings(value, f"{path}[{index}]")


# -- gates -----------------------------------------------------------------


def test_an_unknown_tool_is_refused_as_an_invalid_argument() -> None:
    payload = make_router(armed_doubles()).call("pz_do_anything", {})

    assert payload["ok"] is False
    assert payload["reason_code"] == ReasonCode.INVALID_ARGUMENT.value


def test_a_read_tool_works_while_the_session_is_disarmed() -> None:
    doubles = armed_doubles()
    doubles.session.snapshot = replace(
        doubles.session.snapshot, armed=False, mode=SessionMode.OBSERVE
    )
    doubles.observations.observation = rich_observation()

    payload = make_router(doubles).call("pz_observe_snapshot", {"detail": "full"})

    assert payload["ok"] is True


def test_a_write_tool_on_a_disarmed_session_is_not_armed_and_submits_nothing() -> None:
    doubles = armed_doubles()
    doubles.session.snapshot = replace(
        doubles.session.snapshot, armed=False, mode=SessionMode.OBSERVE
    )

    payload = make_router(doubles).call(
        "pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "k1"}
    )

    assert payload["reason_code"] == ReasonCode.NOT_ARMED.value
    assert payload["retryable"] is False
    assert "OBSERVE" in payload["message"]
    assert doubles.actions.submitted == []


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("pz_safety_stop", {}),
        ("pz_session_disarm", {}),
        ("pz_action_cancel", {"idempotency_key": "k1"}),
    ],
)
def test_the_control_tools_work_while_disarmed(tool: str, arguments: dict[str, Any]) -> None:
    doubles = armed_doubles()
    doubles.session.snapshot = replace(
        doubles.session.snapshot, armed=False, mode=SessionMode.OBSERVE
    )

    payload = make_router(doubles).call(tool, arguments)

    assert payload["ok"] is True, payload


def test_stop_reaches_the_port_and_reports_only_mod_owned_clearing() -> None:
    doubles = armed_doubles()

    payload = make_router(doubles).call("pz_safety_stop", {})

    assert doubles.session.stops == 1
    assert payload["data"] == {"cleared": 2, "disarmed": True, "mode": SessionMode.OBSERVE.value}


def test_a_tool_whose_capability_is_unsupported_is_neither_listed_nor_callable() -> None:
    doubles = armed_doubles()
    doubles.capabilities.report_value = make_report(
        usable=[INVENTORY_TRANSFER], unsupported=[EAT_PERCENTAGE]
    )
    router = make_router(doubles)

    listed = {descriptor["name"] for descriptor in router.list_tools()}
    payload = router.call("pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "k1"})

    assert "pz_action_eat" not in listed
    assert payload["reason_code"] == ReasonCode.CAPABILITY_UNAVAILABLE.value
    assert EAT_PERCENTAGE in payload["message"]
    assert "NO_VERIFIED_API" in payload["message"]
    assert doubles.actions.submitted == []


def test_the_capability_gate_runs_before_the_arming_gate() -> None:
    # A withheld tool is not "offered and then refused for the session state";
    # it is not offered at all, and calling it says which capability is missing.
    doubles = armed_doubles()
    doubles.capabilities.report_value = make_report(unsupported=[EAT_PERCENTAGE])
    doubles.session.snapshot = replace(doubles.session.snapshot, armed=False)

    payload = make_router(doubles).call(
        "pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "k1"}
    )

    assert payload["reason_code"] == ReasonCode.CAPABILITY_UNAVAILABLE.value


def test_an_unverified_capability_is_published_but_warned_about() -> None:
    payload = make_router(armed_doubles()).call(
        "pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "k1"}
    )

    assert payload["ok"] is True
    assert any(EAT_PERCENTAGE in warning for warning in payload["warnings"])


# -- submitting ------------------------------------------------------------


def test_a_long_running_tool_answers_with_an_action_id_and_never_claims_success() -> None:
    doubles = armed_doubles()

    payload = make_router(doubles).call(
        "pz_action_read", {"item_ref": BOOK_REF, "pages": 5, "idempotency_key": "k1"}
    )

    assert payload["status"] == ActionStatus.ACCEPTED.value
    assert payload["data"]["terminal"] is False
    assert payload["action_id"]
    assert "evidence" not in payload["data"]


def test_the_arguments_reach_the_adapter_under_the_adapters_own_names() -> None:
    doubles = armed_doubles()

    make_router(doubles).call(
        "pz_action_move_to",
        {
            "target": {"x": 1210, "y": 3405, "z": 0},
            "radius": 1.5,
            "idempotency_key": "k1",
            "timeout_ms": 20_000,
        },
    )

    request = doubles.actions.submitted[0]
    assert request.action is ActionName.MOVEMENT_MOVE_TO
    assert request.session_id == DEFAULT_SESSION
    assert request.lease_ms == 20_000
    assert request.args == {
        "target": {"x": 1210, "y": 3405, "z": 0},
        "radius": 1.5,
        "max_distance": 20,
        "allow_doors": True,
        "allow_stairs": True,
    }


def test_the_envelope_fields_are_not_forwarded_as_adapter_arguments() -> None:
    doubles = armed_doubles()

    make_router(doubles).call("pz_action_wait", {"game_seconds": 30, "idempotency_key": "k1"})

    assert doubles.actions.submitted[0].args == {"game_seconds": 30}


def test_a_cancel_without_a_target_sends_no_argument_at_all() -> None:
    doubles = armed_doubles()

    make_router(doubles).call("pz_action_cancel", {"idempotency_key": "k1"})

    assert doubles.actions.submitted[0].args == {}
    assert doubles.actions.submitted[0].action is ActionName.PLAN_CANCEL


def test_a_terminal_success_carries_the_observed_postcondition() -> None:
    doubles = armed_doubles()
    doubles.actions.status_on_submit = ActionStatus.ACCEPTED
    router = make_router(doubles)
    first = router.call("pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "k1"})
    action_id = first["action_id"]
    doubles.actions.finish(action_id, succeeded_result(ActionName.CONSUME_EAT))

    replayed = router.call("pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "k1"})

    assert replayed["status"] == ActionStatus.SUCCEEDED.value
    assert replayed["data"]["evidence"]["observed"]["hunger_after"] == 0.2
    assert replayed["data"]["reason_code"] == ReasonCode.POSTCONDITION_MET.value


def test_a_terminal_failure_carries_the_code_and_the_retry_flag_from_the_table() -> None:
    doubles = armed_doubles()
    router = make_router(doubles)
    first = router.call(
        "pz_action_move_to", {"target": {"x": 1, "y": 2, "z": 0}, "idempotency_key": "k1"}
    )
    doubles.actions.finish(
        first["action_id"], failed_result(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_STUCK)
    )

    payload = router.call(
        "pz_action_move_to", {"target": {"x": 1, "y": 2, "z": 0}, "idempotency_key": "k1"}
    )

    assert payload["data"]["reason_code"] == ReasonCode.PATH_STUCK.value
    assert payload["data"]["retryable"] is True
    assert "evidence" not in payload["data"]


def test_a_port_that_answers_about_another_action_is_an_internal_error() -> None:
    doubles = armed_doubles()

    def wrong(request: Any) -> Any:
        return replace(doubles.actions.submit(request), action=ActionName.CONSUME_DRINK)

    doubles.actions.submit = wrong  # type: ignore[method-assign]

    payload = make_router(doubles).call(
        "pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "k1"}
    )

    assert payload["reason_code"] == ReasonCode.INTERNAL_ERROR.value


def test_a_refusal_from_the_action_port_keeps_the_engines_reason_code() -> None:
    doubles = armed_doubles()
    doubles.actions.submit_error = PreconditionFailed(
        "nothing safe to eat", reason_code=ReasonCode.NO_SAFE_FOOD
    )

    payload = make_router(doubles).call(
        "pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "k1"}
    )

    assert payload["reason_code"] == ReasonCode.NO_SAFE_FOOD.value


def test_a_crashing_port_becomes_an_internal_error_payload_not_a_raise() -> None:
    doubles = armed_doubles()
    doubles.actions.submit_error = ZeroDivisionError("boom")

    payload = make_router(doubles).call(
        "pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "k1"}
    )

    assert payload["ok"] is False
    assert payload["reason_code"] == ReasonCode.INTERNAL_ERROR.value


# -- idempotency -----------------------------------------------------------


def test_the_same_key_twice_does_not_act_twice() -> None:
    doubles = armed_doubles()
    router = make_router(doubles)
    arguments = {"item_ref": BEAN_REF, "idempotency_key": "k1"}

    first = router.call("pz_action_eat", arguments)
    second = router.call("pz_action_eat", arguments)

    assert len(doubles.actions.submitted) == 1
    assert second["action_id"] == first["action_id"]
    assert second["replayed"] is True
    assert first["replayed"] is False


def test_a_replay_survives_the_session_being_disarmed_in_between() -> None:
    # The retry that arrives after a panic stop must answer with the original
    # call, not with NOT_ARMED for a call that already happened.
    doubles = armed_doubles()
    router = make_router(doubles)
    arguments = {"item_ref": BEAN_REF, "idempotency_key": "k1"}
    first = router.call("pz_action_eat", arguments)
    doubles.session.snapshot = replace(doubles.session.snapshot, armed=False)

    second = router.call("pz_action_eat", arguments)

    assert second["ok"] is True
    assert second["action_id"] == first["action_id"]


def test_a_replay_of_an_action_the_core_forgot_returns_what_the_key_produced() -> None:
    doubles = armed_doubles()
    router = make_router(doubles)
    arguments = {"item_ref": BEAN_REF, "idempotency_key": "k1"}
    first = router.call("pz_action_eat", arguments)
    doubles.actions.forget = True

    second = router.call("pz_action_eat", arguments)

    assert second["replayed"] is True
    assert second["action_id"] == first["action_id"]
    assert len(doubles.actions.submitted) == 1


def test_a_replay_with_nothing_to_re_read_keeps_the_status_it_first_answered_with() -> None:
    # pz_plan_execute puts no action in flight, so the replay is rebuilt from the
    # cache. Rebuilding it as a bare "ok" would downgrade an accepted plan to a
    # different claim about the same work.
    doubles = armed_doubles()
    doubles.plans.record = PlanRecord(plan_id="plan-9", status=ActionStatus.ACCEPTED, step_index=0)
    router = make_router(doubles)
    arguments = {"goal": "eat then read", "idempotency_key": "k1"}
    first = router.call("pz_plan_execute", arguments)

    second = router.call("pz_plan_execute", arguments)

    assert first["status"] == ActionStatus.ACCEPTED.value
    assert second["status"] == ActionStatus.ACCEPTED.value
    assert second["replayed"] is True
    assert len(doubles.plans.requests) == 1


def test_a_replay_of_a_forgotten_action_keeps_the_status_and_the_capability_warning() -> None:
    doubles = armed_doubles()
    router = make_router(doubles)
    arguments = {"item_ref": BEAN_REF, "idempotency_key": "k1"}
    first = router.call("pz_action_eat", arguments)
    doubles.actions.forget = True

    second = router.call("pz_action_eat", arguments)

    assert second["status"] == first["status"] == ActionStatus.ACCEPTED.value
    assert any(EAT_PERCENTAGE in warning for warning in second["warnings"])


def test_reusing_a_key_across_two_tools_is_refused() -> None:
    doubles = armed_doubles()
    router = make_router(doubles)
    router.call("pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "k1"})

    payload = router.call("pz_action_drink", {"item_ref": BEAN_REF, "idempotency_key": "k1"})

    assert payload["reason_code"] == ReasonCode.INVALID_ARGUMENT.value
    assert len(doubles.actions.submitted) == 1


def test_the_replay_cache_is_bounded() -> None:
    doubles = armed_doubles()
    router = make_router(doubles, cache=IdempotencyCache(capacity=2))
    for n in range(4):
        router.call("pz_action_wait", {"game_seconds": 5, "idempotency_key": f"k{n}"})

    again = router.call("pz_action_wait", {"game_seconds": 5, "idempotency_key": "k0"})

    assert again["replayed"] is False
    assert len(doubles.actions.submitted) == 5


# -- observation -----------------------------------------------------------


def test_no_observation_is_reported_as_a_disconnected_game() -> None:
    doubles = armed_doubles()
    doubles.observations.observation = None

    payload = make_router(doubles).call("pz_observe_snapshot", {})

    assert payload["reason_code"] == ReasonCode.GAME_DISCONNECTED.value


def test_the_detail_levels_add_sections_and_never_return_a_raw_snapshot() -> None:
    doubles = armed_doubles()
    doubles.observations.observation = rich_observation()
    router = make_router(doubles)

    compact = router.call("pz_observe_snapshot", {"detail": "compact"})["data"]
    standard = router.call("pz_observe_snapshot", {"detail": "standard"})["data"]
    full = router.call("pz_observe_snapshot", {"detail": "full"})["data"]

    assert compact["view"] == "compact_observation"
    assert "nearby" not in compact
    assert "nearby" in standard and "inventory" not in standard
    assert "inventory" in full
    assert full["game"]["save_scope"] == save_scope(DEFAULT_SAVE)
    assert "save_id" not in full["game"]


def test_inventory_scope_and_nesting_filters_select_containers_and_their_items() -> None:
    doubles = armed_doubles()
    doubles.observations.observation = rich_observation()
    router = make_router(doubles)

    on_person = router.call("pz_observe_inventory", {"scope": "on_person"})["data"]
    world = router.call("pz_observe_inventory", {"scope": "world"})["data"]

    assert {c["ref"] for c in on_person["containers"]} == {
        main_container_ref(),
        backpack_container_ref(),
    }
    assert {i["ref"] for i in on_person["items"]} == {BEAN_REF, BOOK_REF}
    assert world["items"] == []


def test_the_category_filter_uses_the_compacted_token() -> None:
    doubles = armed_doubles()
    doubles.observations.observation = rich_observation()

    payload = make_router(doubles).call("pz_observe_inventory", {"category": "food"})["data"]

    # 'Food' is the category the mod reports; the filter is exact, so a lowered
    # token does not silently match and the caller learns their filter missed.
    assert payload["items"] == []


def test_an_observation_without_an_inventory_tier_is_a_missing_capability() -> None:
    doubles = armed_doubles()
    doubles.observations.observation = make_observation(inventory=None)

    payload = make_router(doubles).call("pz_observe_inventory", {})

    assert payload["reason_code"] == ReasonCode.CAPABILITY_UNAVAILABLE.value


def test_nearby_is_filtered_by_radius_and_by_type() -> None:
    doubles = armed_doubles()
    doubles.observations.observation = rich_observation()
    router = make_router(doubles)

    close = router.call("pz_observe_nearby", {"radius": 10})["data"]
    doors = router.call("pz_observe_nearby", {"radius": 30, "types": ["door"]})["data"]

    assert [z["ref"] for z in close["zombies"]] == [f"zombie:{DEFAULT_SESSION}:31:0"]
    assert [o["kind"] for o in close["objects"]] == ["container"]
    assert doors["zombies"] == []
    assert [o["kind"] for o in doors["objects"]] == ["door"]


def test_a_radius_beyond_the_bound_is_refused_rather_than_clamped() -> None:
    doubles = armed_doubles()
    doubles.observations.observation = rich_observation()

    payload = make_router(doubles).call("pz_observe_nearby", {"radius": 500})

    assert payload["reason_code"] == ReasonCode.INVALID_ARGUMENT.value


def test_a_radius_of_nan_is_refused_rather_than_emptying_the_world() -> None:
    # NaN sits outside no bound and inside none either: every `distance <=
    # radius` is false, so the surroundings tool would answer "no zombies, no
    # objects" for a world that has both — the worst wrong answer this surface
    # can give — and echo a bare NaN, which is not valid JSON for the client.
    doubles = armed_doubles()
    doubles.observations.observation = rich_observation()

    payload = make_router(doubles).call("pz_observe_nearby", {"radius": float("nan")})

    assert payload["ok"] is False
    assert payload["reason_code"] == ReasonCode.INVALID_ARGUMENT.value


# -- session, plans, memory, diagnostics -----------------------------------


def test_session_status_answers_even_when_the_game_is_not_connected() -> None:
    doubles = armed_doubles()
    doubles.observations.observation = None
    doubles.session.snapshot = replace(
        doubles.session.snapshot, connected=False, game_heartbeat_ok=False
    )

    data = make_router(doubles).call("pz_session_status", {})["data"]

    assert data["connected"] is False
    assert data["observation"] is None
    assert data["heartbeat"] == {"game_ok": False, "sidecar_ok": True}
    assert data["save_scope"] == save_scope(DEFAULT_SAVE)


def test_arming_forwards_the_mode_and_the_backup_confirmation() -> None:
    doubles = armed_doubles()

    payload = make_router(doubles).call(
        "pz_session_arm", {"mode": "AUTONOMOUS", "confirm_backup": True}
    )

    assert doubles.session.arms == [(SessionMode.AUTONOMOUS, True)]
    assert payload["data"]["mode"] == SessionMode.AUTONOMOUS.value


def test_arming_relays_the_cores_refusal_rather_than_deciding_for_itself() -> None:
    doubles = armed_doubles()
    doubles.session.arm_error = PreconditionFailed(
        "no backup exists for this save", reason_code=ReasonCode.PRECONDITION_FAILED
    )

    payload = make_router(doubles).call("pz_session_arm", {"mode": "AUTONOMOUS"})

    assert payload["reason_code"] == ReasonCode.PRECONDITION_FAILED.value


def test_arming_refuses_a_mode_outside_the_two_the_tool_offers() -> None:
    payload = make_router(armed_doubles()).call("pz_session_arm", {"mode": "OBSERVE"})

    assert payload["reason_code"] == ReasonCode.INVALID_ARGUMENT.value


def test_a_plan_goal_is_bounded_quarantined_and_echoed_back() -> None:
    doubles = armed_doubles()
    doubles.plans.record = PlanRecord(
        plan_id="plan-7",
        status=ActionStatus.FAILED,
        step_index=1,
        steps=(
            PlanStepRecord(index=0, action=ActionName.CONSUME_EAT, status=ActionStatus.SUCCEEDED),
            PlanStepRecord(
                index=1,
                action=ActionName.LITERATURE_READ,
                status=ActionStatus.FAILED,
                reason_code=ReasonCode.THREAT_INTERRUPTED,
            ),
        ),
        stopped_reason=ReasonCode.THREAT_INTERRUPTED,
    )

    data = make_router(doubles).call(
        "pz_plan_execute", {"goal": HOSTILE_NAME[:100], "idempotency_key": "k1"}
    )["data"]

    assert data["stopped_reason"] == ReasonCode.THREAT_INTERRUPTED.value
    assert data["retryable"] is False
    assert data["steps"][1]["reason_code"] == ReasonCode.THREAT_INTERRUPTED.value
    assert data["content_marker"] == CONTENT_MARKER
    assert "\n" not in data[UNTRUSTED_TEXT_KEY]["goal"]


def test_a_finished_plan_is_reported_without_borrowing_the_envelopes_success_word() -> None:
    """``PlanExecutor.run`` is synchronous, so a plan that worked comes back terminal.

    The envelope's ``succeeded`` promises the observed postcondition under
    ``data.evidence``, and a plan record carries none — its steps' evidence
    never reaches this layer. Claiming the word made ``ToolSuccess`` refuse the
    envelope, which turned every plan that *worked* into an INTERNAL_ERROR; and
    because the refusal landed after the port had already run the plan and
    before the idempotency key was recorded, the client's retry ran it twice.
    """
    doubles = armed_doubles()
    doubles.plans.record = PlanRecord(
        plan_id="plan-done",
        status=ActionStatus.SUCCEEDED,
        step_index=1,
        steps=(
            PlanStepRecord(index=0, action=ActionName.ACTION_WAIT, status=ActionStatus.SUCCEEDED),
        ),
    )
    router = make_router(doubles)
    arguments = {"goal": "wait a while", "idempotency_key": "k1"}

    first = router.call("pz_plan_execute", arguments)
    second = router.call("pz_plan_execute", arguments)

    assert first["ok"] is True
    assert first["status"] != ActionStatus.SUCCEEDED.value
    assert first["data"]["status"] == ActionStatus.SUCCEEDED.value
    assert first["data"]["terminal"] is True
    assert second["replayed"] is True
    assert len(doubles.plans.requests) == 1


def test_a_plan_with_raw_steps_or_lua_fails_validation_because_there_is_nowhere_to_put_it() -> None:
    router = make_router(armed_doubles())

    for extra in ({"steps": [{"lua": "getPlayer()"}]}, {"lua": "print(1)"}, {"path": "C:\\x"}):
        payload = router.call("pz_plan_execute", {"goal": "eat", "idempotency_key": "k1", **extra})
        assert payload["reason_code"] == ReasonCode.INVALID_ARGUMENT.value


def test_the_reported_plan_steps_are_bounded_and_the_true_count_is_still_told() -> None:
    doubles = armed_doubles()
    reported = router_module.MAX_PLAN_STEPS_REPORTED
    doubles.plans.record = PlanRecord(
        plan_id="plan-8",
        status=ActionStatus.STARTED,
        step_index=2,
        steps=tuple(
            PlanStepRecord(index=n, action=ActionName.ACTION_WAIT, status=ActionStatus.SUCCEEDED)
            for n in range(reported * 3)
        ),
    )

    data = make_router(doubles).call("pz_plan_status", {})["data"]

    assert len(data["steps"]) == reported
    assert data["step_count"] == reported * 3


def test_the_refs_on_one_memory_record_are_bounded() -> None:
    doubles = armed_doubles()
    doubles.memory.records = (
        MemoryRecord(
            kind="known_container",
            key="crate_1",
            refs=tuple(
                f"container:{DEFAULT_SESSION}:world:1200:{n}:0:crate:0"
                for n in range(router_module.MAX_REFS_PER_RECORD * 3)
            ),
        ),
    )

    data = make_router(doubles).call("pz_memory_query", {})["data"]

    assert len(data["records"][0]["refs"]) == router_module.MAX_REFS_PER_RECORD


def test_a_terminal_results_diagnostics_are_bounded_before_they_are_relayed() -> None:
    doubles = armed_doubles()
    router = make_router(doubles)
    arguments = {"game_seconds": 10, "idempotency_key": "k1"}
    first = router.call("pz_action_wait", arguments)
    doubles.actions.finish(
        first["action_id"],
        replace(
            failed_result(ActionName.ACTION_WAIT, ReasonCode.ACTION_TIMEOUT),
            diagnostics=[f"line {n}" for n in range(MAX_DIAGNOSTICS * 5)],
        ),
    )

    payload = router.call("pz_action_wait", arguments)

    assert len(payload["data"][UNTRUSTED_TEXT_KEY]["diagnostics"]) == MAX_DIAGNOSTICS


def test_a_terminal_refusal_quotes_the_item_name_only_inside_the_quarantine() -> None:
    """An adapter's refusal wording carries game text, so it leaves marked.

    ``consume.eat`` answers ``f"{item.display_name} has no portions left"`` and
    the display name is whatever a mod called the item. The terminal ack reaches
    a client through the replay path — re-calling with the same idempotency key
    is how a client polls — so this is the ordinary route, not a corner. Putting
    that sentence in a bare ``data.detail`` hands a client a string it has no
    way to tell from the protocol's own words.
    """
    doubles = armed_doubles()
    doubles.observations.observation = rich_observation()
    router = make_router(doubles)
    router.call("pz_session_arm", {"mode": "ASSISTED", "idempotency_key": "arm"})
    arguments = {"item_ref": BEAN_REF, "fraction": 1.0, "idempotency_key": "eat-1"}
    first = router.call("pz_action_eat", arguments)
    doubles.actions.finish(
        first["action_id"],
        replace(
            failed_result(ActionName.CONSUME_EAT, ReasonCode.PRECONDITION_FAILED),
            message=f"{HOSTILE_NAME} has no portions left",
        ),
    )

    data = router.call("pz_action_eat", arguments)["data"]

    # The name is present exactly once, and only under the quarantine key.
    assert "detail" not in data
    assert "diagnostics" not in data
    assert data["content_marker"] == CONTENT_MARKER
    # Redaction still applies inside the quarantine: bounded, and no line break
    # with which to end the sentence a client wrapped it in.
    assert "SYSTEM:" in data[UNTRUSTED_TEXT_KEY]["detail"]
    assert "\n" not in data[UNTRUSTED_TEXT_KEY]["detail"]
    unmarked = {key: value for key, value in data.items() if key != UNTRUSTED_TEXT_KEY}
    assert "SYSTEM:" not in json.dumps(unmarked)
    # `reason_code` is closed protocol vocabulary and stays where a client branches on it.
    assert data["reason_code"] == ReasonCode.PRECONDITION_FAILED.value


def test_plan_status_says_so_plainly_when_nothing_is_running() -> None:
    data = make_router(armed_doubles()).call("pz_plan_status", {})["data"]

    assert data == {"active": False}


def test_memory_records_carry_numbers_and_refs_and_quarantine_their_label() -> None:
    doubles = armed_doubles()
    doubles.memory.records = (
        MemoryRecord(
            kind="known_container",
            key="crate_1",
            label=HOSTILE_NAME,
            refs=(main_container_ref(), "not a ref"),
            data={"visits": 3, "empty": False},
        ),
        MemoryRecord(kind="a kind with spaces", key="bad"),
    )

    data = make_router(doubles).call("pz_memory_query", {"limit": 5})["data"]

    assert len(data["records"]) == 1
    record = data["records"][0]
    assert record["refs"] == [main_container_ref()]
    assert record["data"] == {"visits": 3, "empty": False}
    assert record["content_marker"] == CONTENT_MARKER
    assert "ignore previous instructions" in record[UNTRUSTED_TEXT_KEY]["label"]
    assert doubles.memory.calls == [((), 5)]


def test_a_record_the_boundary_cannot_name_is_omitted_out_loud_not_silently() -> None:
    # A shorter list with no explanation reads as "that is all there is", which
    # is the quiet half of a producer bug reaching a client as a fact.
    doubles = armed_doubles()
    doubles.memory.records = (
        MemoryRecord(kind="home_point", key="base", data={"x": 1}),
        MemoryRecord(kind="a kind with spaces", key="bad"),
        MemoryRecord(kind="known_container", key="a key with spaces"),
    )

    payload = make_router(doubles).call("pz_memory_query", {})

    assert len(payload["data"]["records"]) == 1
    assert payload["data"]["omitted"] == 2
    assert any("2 memory record(s) omitted" in warning for warning in payload["warnings"])


def test_a_doctor_that_could_not_report_every_check_is_not_reported_as_ok() -> None:
    doubles = armed_doubles()
    doubles.diagnostics.checks = (
        DoctorCheck(code="steam_library", ok=True),
        DoctorCheck(code="a code with spaces", ok=True),
    )

    payload = make_router(doubles).call("pz_debug_doctor", {})

    assert payload["data"]["failed"] == []
    assert payload["data"]["omitted"] == 1
    assert payload["data"]["ok"] is False
    assert any("environment check(s) omitted" in warning for warning in payload["warnings"])


def test_the_doctor_answer_is_bounded_even_though_the_tool_takes_no_limit() -> None:
    doubles = armed_doubles()
    doubles.diagnostics.checks = tuple(
        DoctorCheck(code=f"check_{n}", ok=True) for n in range(router_module.MAX_DOCTOR_CHECKS * 2)
    )

    payload = make_router(doubles).call("pz_debug_doctor", {})

    assert len(payload["data"]["checks"]) == router_module.MAX_DOCTOR_CHECKS
    assert payload["data"]["truncated"] is True
    # A partial report is not a clean bill of health.
    assert payload["data"]["ok"] is False
    assert any("cut off" in warning for warning in payload["warnings"])


def test_a_port_that_ignores_the_limit_does_not_get_to_set_the_answers_size() -> None:
    doubles = armed_doubles()
    doubles.diagnostics.lines = tuple(
        LogRecord(timestamp_ms=n, level="info", component="ipc", message=f"line {n}")
        for n in range(50)
    )

    payload = make_router(doubles).call("pz_debug_tail", {"limit": 5})

    assert len(payload["data"]["records"]) == 5


def test_doctor_reports_stable_codes_and_redacts_the_paths_in_its_detail() -> None:
    doubles = armed_doubles()
    doubles.diagnostics.checks = (
        DoctorCheck(code="steam_library", ok=False, detail=f"not found at C:\\Users\\{USERNAME}"),
        DoctorCheck(code="a code with spaces", ok=True),
    )

    data = make_router(doubles).call("pz_debug_doctor", {})["data"]

    assert [check["code"] for check in data["checks"]] == ["steam_library"]
    assert data["ok"] is False
    assert data["failed"] == ["steam_library"]
    assert data["checks"][0]["detail"] == f"not found at {REDACTED_PATH}"


def test_tail_forwards_its_filters_and_redacts_each_line() -> None:
    doubles = armed_doubles()
    doubles.diagnostics.lines = (
        LogRecord(
            timestamp_ms=1,
            level="error",
            component="ipc",
            message=f"cannot write /home/{USERNAME}/Zomboid/exchange",
            code="IPC_WRITE_FAILED",
        ),
        LogRecord(timestamp_ms=2, level="bad level", component="ipc", message="ignored"),
    )

    payload = make_router(doubles).call(
        "pz_debug_tail", {"limit": 5, "level": "error", "component": "ipc"}
    )

    assert doubles.diagnostics.tail_calls == [
        {"limit": 5, "level": "error", "component": "ipc", "action_id": None}
    ]
    records = payload["data"]["records"]
    assert len(records) == 1
    assert records[0]["message"] == f"cannot write {REDACTED_PATH}"


# -- the leak tests --------------------------------------------------------


def every_payload(router: ToolRouter) -> Iterator[tuple[str, Any]]:
    """One successful payload from every published tool.

    The two tools that disarm the session come last on purpose: run earlier,
    they would turn every write that follows into a ``NOT_ARMED`` refusal and
    the leak checks below would be inspecting error payloads instead of the
    successful ones they are about.
    """
    calls: list[tuple[str, dict[str, Any]]] = [
        ("pz_session_status", {}),
        ("pz_session_arm", {"mode": "ASSISTED"}),
        ("pz_observe_snapshot", {"detail": "full"}),
        ("pz_observe_inventory", {}),
        ("pz_observe_nearby", {"radius": 30}),
        ("pz_action_inspect_world", {"idempotency_key": "iw1"}),
        ("pz_action_inspect_container", {"container_ref": MAIN_REF, "idempotency_key": "ic1"}),
        ("pz_action_search_inventory", {"edible": True, "idempotency_key": "is1"}),
        ("pz_action_move_to", {"target": {"x": 1, "y": 2, "z": 0}, "idempotency_key": "m1"}),
        ("pz_action_move_near", {"object_ref": CRATE_REF, "idempotency_key": "mn1"}),
        ("pz_action_open_container", {"container_ref": CRATE_REF, "idempotency_key": "oc1"}),
        ("pz_action_open_door", {"door_ref": DOOR_REF, "idempotency_key": "od1"}),
        ("pz_action_close_door", {"door_ref": DOOR_REF, "idempotency_key": "cd1"}),
        ("pz_action_unlock_door", {"door_ref": DOOR_REF, "idempotency_key": "ud1"}),
        (
            "pz_action_transfer",
            {
                "item_ref": BEAN_REF,
                "destination_container_ref": backpack_container_ref(),
                "idempotency_key": "t1",
            },
        ),
        ("pz_action_ensure_main", {"item_ref": BOOK_REF, "idempotency_key": "em1"}),
        ("pz_action_eat", {"item_ref": BEAN_REF, "idempotency_key": "e1"}),
        ("pz_action_drink", {"item_ref": BEAN_REF, "idempotency_key": "d1"}),
        (
            "pz_action_drink_source",
            {"item_ref": BEAN_REF, "source_ref": SQUARE_REF, "idempotency_key": "ds1"},
        ),
        ("pz_action_read", {"item_ref": BOOK_REF, "idempotency_key": "r1"}),
        ("pz_action_equip", {"item_ref": BEAN_REF, "idempotency_key": "eq1"}),
        ("pz_action_unequip", {"hand": "primary", "idempotency_key": "uq1"}),
        (
            "pz_action_bandage",
            {"body_part": "ForeArm_L", "item_ref": BEAN_REF, "idempotency_key": "bd1"},
        ),
        ("pz_action_rest", {"target_endurance": 0.95, "idempotency_key": "rs1"}),
        ("pz_action_sleep", {"bed_ref": SQUARE_REF, "idempotency_key": "sl1"}),
        ("pz_action_wait", {"game_seconds": 10, "idempotency_key": "w1"}),
        ("pz_action_cancel", {"idempotency_key": "c1"}),
        ("pz_action_cancel_all", {"idempotency_key": "ca1"}),
        # An id the fake port never minted, on purpose: the unknown-here answer
        # is the leakiest-looking payload these two can produce (it echoes the
        # caller's id and names causes), and it returns without waiting.
        ("pz_action_status", {"action_id": str(uuid.UUID(int=0xDEAD))}),
        ("pz_action_await", {"action_id": str(uuid.UUID(int=0xDEAD)), "timeout_ms": 100}),
        ("pz_plan_execute", {"goal": "eat then read", "idempotency_key": "p1"}),
        ("pz_plan_status", {}),
        ("pz_memory_query", {}),
        ("pz_debug_doctor", {}),
        ("pz_debug_tail", {}),
    ]
    for name, arguments in calls:
        yield name, router.call(name, arguments)

    # The goal trio is swept separately because the cancel needs the id the
    # submit minted, and a cancel of an id the channel never issued answers with
    # a refusal — a thinner payload than the one a leak would hide in.
    submitted = router.call(
        "pz_goal_submit", {"kind": "satisfy_hunger", "idempotency_key": "goal-leak-1"}
    )
    yield "pz_goal_submit", submitted
    yield "pz_goal_status", router.call("pz_goal_status", {})
    minted = _goal_id_from(submitted)
    yield (
        "pz_goal_cancel",
        router.call("pz_goal_cancel", {"goal_id": minted, "idempotency_key": "goal-leak-2"}),
    )

    # Last on purpose: see the docstring.
    for name, arguments in (("pz_session_disarm", {}), ("pz_safety_stop", {})):
        yield name, router.call(name, arguments)


def _goal_id_from(payload: Any) -> str:
    """The id ``pz_goal_submit`` minted, or a loud failure.

    Falling back to a placeholder here would turn the cancel leg of the sweep
    into a refusal without anybody noticing, and a refusal is exactly the payload
    shape a leak does not appear in.
    """
    assert payload.get("ok") is True, f"the sweep's goal submit was refused: {payload}"
    identifier = payload["data"]["goal_id"]
    assert isinstance(identifier, str) and identifier, f"no goal id in {payload}"
    return identifier


def leaky_doubles() -> Doubles:
    doubles = armed_doubles()
    doubles.observations.observation = rich_observation()
    doubles.memory.records = (
        MemoryRecord(kind="home_point", key="base", label=HOSTILE_NAME, data={"x": 1200}),
    )
    doubles.diagnostics.checks = (
        DoctorCheck(code="user_dir", ok=True, detail=f"C:\\Users\\{USERNAME}\\Zomboid"),
    )
    doubles.diagnostics.lines = (
        LogRecord(
            timestamp_ms=1,
            level="info",
            component="session",
            message=f"attached at /home/{USERNAME}/Zomboid",
        ),
    )
    return doubles


def test_the_leak_sweep_covers_every_published_tool() -> None:
    # The checks below are only as good as this list; a tool added to the
    # catalogue and forgotten here would go unswept.
    doubles = leaky_doubles()

    swept = [name for name, _ in every_payload(make_router(doubles))]

    assert sorted(swept) == sorted(spec.name for spec in TOOLS)


def test_no_tool_result_carries_an_absolute_path_a_username_or_the_raw_save_id() -> None:
    doubles = leaky_doubles()
    router = make_router(doubles)

    for name, payload in every_payload(router):
        for path, value in strings(payload, name):
            assert not PATH_SHAPED.search(value), f"{path}: {value!r}"
            assert USERNAME not in value, f"{path}: {value!r}"
            assert DEFAULT_SAVE not in value, f"{path}: {value!r}"


def test_a_hostile_display_name_survives_only_as_quarantined_data() -> None:
    doubles = leaky_doubles()
    router = make_router(doubles)
    marker = "ignore previous instructions"

    seen = 0
    for name, payload in every_payload(router):
        for path, value in strings(payload, name):
            if marker not in value.lower():
                continue
            seen += 1
            # It reaches the client, because the user needs to know what the
            # item is called — but only ever inside the quarantine, so no field
            # a client reads as an instruction can carry it.
            assert UNTRUSTED_TEXT_KEY in path, f"{path}: {value!r}"
    assert seen > 0


def test_the_hostile_name_never_lands_in_a_field_a_client_would_obey() -> None:
    doubles = leaky_doubles()
    router = make_router(doubles)
    marker = "ignore previous instructions"
    directive_fields = ("message", "status", "tool", "request_id")

    for name, payload in every_payload(router):
        assert payload["ok"] is True, name
        for field in directive_fields:
            assert marker not in str(payload[field]).lower(), f"{name}.{field}"
        assert all(marker not in warning.lower() for warning in payload["warnings"]), name


def test_a_key_from_a_previous_session_is_not_replayed_into_this_one() -> None:
    # References are session-scoped, so replaying an answer minted under the old
    # session would report work done in a world these refs no longer describe.
    doubles = armed_doubles()
    router = make_router(doubles)
    arguments = {"item_ref": BEAN_REF, "idempotency_key": "k1"}
    router.call("pz_action_eat", arguments)
    doubles.session.snapshot = replace(doubles.session.snapshot, session_id=OTHER_SESSION)

    again = router.call("pz_action_eat", arguments)

    assert again["replayed"] is False
    assert len(doubles.actions.submitted) == 2
    assert doubles.actions.submitted[1].session_id == OTHER_SESSION


def test_the_content_marker_travels_with_every_quarantined_payload() -> None:
    doubles = leaky_doubles()
    router = make_router(doubles)

    for name, payload in every_payload(router):
        if not payload["ok"]:
            continue
        carries_quarantine = any(
            UNTRUSTED_TEXT_KEY in path for path, _ in strings(payload["data"], name)
        )
        if carries_quarantine:
            assert any(value == CONTENT_MARKER for _, value in strings(payload["data"], name)), name


def test_the_router_refuses_to_start_if_a_published_tool_has_no_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An advertised tool with no handler would answer INTERNAL_ERROR, which is
    # worse than not shipping it; the mismatch is caught at construction.
    extra = ToolSpec(
        name="pz_unhandled",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary="",
        input_schema={"type": "object", "additionalProperties": False, "properties": {}},
    )
    monkeypatch.setattr(router_module, "TOOLS", (*TOOLS, extra))

    with pytest.raises(ValueError, match="without a handler"):
        make_router(armed_doubles())


def test_items_are_reported_with_refs_and_never_with_a_raw_container_name() -> None:
    doubles = leaky_doubles()

    data = make_router(doubles).call("pz_observe_inventory", {})["data"]

    backpack = next(c for c in data["containers"] if c["ref"] == backpack_container_ref())
    assert "name" not in backpack
    assert backpack[UNTRUSTED_TEXT_KEY]["name"]
    bean = next(i for i in data["items"] if i["ref"] == BEAN_REF)
    assert bean["type"] == "Base.TinnedBeans"
    assert "display_name" not in bean
