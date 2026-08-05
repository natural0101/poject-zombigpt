"""Wire-format conformance.

Two directions are checked, and both matter:

* **outbound** — what `to_dict()` emits must validate against the schema, or the
  mod will reject our commands at runtime;
* **inbound** — what the schema permits must parse, or we will reject the mod's
  observations at runtime.

The pair is what keeps `schemas/` and `pz_agent_core.protocol` from drifting
into two different definitions of the same protocol.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    ContainerKind,
    DangerLevel,
    InventoryView,
    Observation,
    ReasonCode,
    RiskClass,
    SessionMode,
)
from pz_agent_core.protocol.enums import ActionOwnership, CapabilityState
from pz_agent_core.version import PROTOCOL_VERSION, SCHEMA_VERSION
from tests.fixtures import (
    backpack_container_ref,
    item_ref,
    main_container_ref,
    make_container,
    make_item,
    make_observation,
)

pytestmark = pytest.mark.contract

SESSION = str(uuid.UUID(int=0x5E5510))
COMMAND = str(uuid.UUID(int=0xC0DE))


def validator_for(schemas: dict[str, dict[str, Any]], name: str) -> Draft202012Validator:
    return Draft202012Validator(schemas[name])


def assert_valid(schemas: dict[str, dict[str, Any]], name: str, payload: dict[str, Any]) -> None:
    errors = sorted(validator_for(schemas, name).iter_errors(payload), key=lambda e: e.path)
    assert not errors, "\n".join(f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors)


class TestCommandContract:
    def test_minimal_command_validates(self, schemas: dict[str, Any]) -> None:
        command = Command(
            session_id=SESSION,
            seq=0,
            command_id=COMMAND,
            idempotency_key="k",
            issued_at_ms=0,
            lease_ms=1000,
            action=ActionName.SESSION_ARM,
            args={},
        )
        assert_valid(schemas, "command.schema", command.to_dict())

    def test_fully_populated_command_validates(self, schemas: dict[str, Any]) -> None:
        command = Command(
            session_id=SESSION,
            seq=12,
            command_id=COMMAND,
            idempotency_key="goal-1:step-2:attempt-1",
            issued_at_ms=1_700_000_000_000,
            lease_ms=10_000,
            expected_observation_seq=55,
            action=ActionName.CONSUME_EAT,
            args={"item_ref": item_ref("4210", "player-main"), "fraction": 0.5},
            policy=CommandPolicy(allow_interrupt=True, max_retries=1, risk_permission=RiskClass.P2),
        )
        assert_valid(schemas, "command.schema", command.to_dict())

    def test_every_action_name_is_in_the_schema_enum(self, schemas: dict[str, Any]) -> None:
        schema_actions = set(schemas["command.schema"]["properties"]["action"]["enum"])
        code_actions = {a.value for a in ActionName}

        assert code_actions == schema_actions, (
            f"only in code: {sorted(code_actions - schema_actions)}; "
            f"only in schema: {sorted(schema_actions - code_actions)}"
        )

    def test_schema_pins_our_protocol_version(self, schemas: dict[str, Any]) -> None:
        const = schemas["command.schema"]["properties"]["protocol_version"]["const"]
        assert const == PROTOCOL_VERSION

    def test_schema_rejects_an_unknown_action(self, schemas: dict[str, Any]) -> None:
        payload = Command(
            session_id=SESSION,
            seq=0,
            command_id=COMMAND,
            idempotency_key="k",
            issued_at_ms=0,
            lease_ms=1000,
            action=ActionName.SESSION_ARM,
            args={},
        ).to_dict()
        payload["action"] = "shell.exec"

        assert not validator_for(schemas, "command.schema").is_valid(payload)

    def test_schema_rejects_an_extra_field(self, schemas: dict[str, Any]) -> None:
        payload = Command(
            session_id=SESSION,
            seq=0,
            command_id=COMMAND,
            idempotency_key="k",
            issued_at_ms=0,
            lease_ms=1000,
            action=ActionName.SESSION_ARM,
            args={},
        ).to_dict()
        payload["lua"] = "getPlayer():setHunger(0)"

        assert not validator_for(schemas, "command.schema").is_valid(payload)


class TestActionResultContract:
    def test_success_validates(self, schemas: dict[str, Any]) -> None:
        result = ActionResult.succeeded(
            session_id=SESSION,
            seq=4,
            command_id=COMMAND,
            action="consume.eat",
            timestamp_ms=1_700_000_050_000,
            started_at_ms=1_700_000_040_000,
            evidence={"hunger_before": 0.72, "hunger_after": 0.31},
        )
        assert_valid(schemas, "action_result.schema", result.to_dict())

    @pytest.mark.parametrize("status", list(ActionStatus))
    def test_every_status_is_in_the_schema_enum(
        self, schemas: dict[str, Any], status: ActionStatus
    ) -> None:
        assert status.value in schemas["action_result.schema"]["properties"]["status"]["enum"]

    @pytest.mark.parametrize("code", list(ReasonCode))
    def test_every_reason_code_serialises(self, schemas: dict[str, Any], code: ReasonCode) -> None:
        status = (
            ActionStatus.SUCCEEDED if code is ReasonCode.POSTCONDITION_MET else ActionStatus.FAILED
        )
        result = (
            ActionResult.succeeded(
                session_id=SESSION,
                seq=1,
                command_id=COMMAND,
                action="action.wait",
                timestamp_ms=1,
                evidence={"waited_ms": 1000},
            )
            if status is ActionStatus.SUCCEEDED
            else ActionResult.failure(
                session_id=SESSION,
                seq=1,
                command_id=COMMAND,
                action="action.wait",
                timestamp_ms=1,
                reason_code=code,
            )
        )
        assert_valid(schemas, "action_result.schema", result.to_dict())

    def test_schema_pins_our_schema_version(self, schemas: dict[str, Any]) -> None:
        const = schemas["action_result.schema"]["properties"]["schema_version"]["const"]
        assert const == SCHEMA_VERSION


class TestObservationContract:
    def test_minimal_observation_validates(self, schemas: dict[str, Any]) -> None:
        observation = make_observation(inventory=None, nearby=None)
        assert_valid(schemas, "observation.schema", observation.to_dict())

    def test_nested_inventory_observation_validates(self, schemas: dict[str, Any]) -> None:
        main = main_container_ref()
        back = backpack_container_ref()
        inventory = InventoryView(
            containers=[
                make_container(main, ContainerKind.PLAYER_MAIN, name="Inventory"),
                make_container(back, ContainerKind.WORN, name="Big Hiking Bag", parent_ref=main),
            ],
            items=[
                make_item(item_ref("4210", "worn:Back:99001"), back, food={"hungerChange": -30}),
                make_item(
                    item_ref("4211", "player-main"),
                    main,
                    full_type="Base.Book",
                    category="Literature",
                    literature={"pagesRead": 12, "pageCount": 220},
                ),
            ],
        )
        assert_valid(schemas, "observation.schema", make_observation(inventory=inventory).to_dict())

    def test_every_enum_matches_the_schema(self, schemas: dict[str, Any]) -> None:
        props = schemas["observation.schema"]["properties"]
        defs = schemas["observation.schema"]["$defs"]

        assert {m.value for m in SessionMode} == set(props["safety"]["properties"]["mode"]["enum"])
        assert {d.value for d in DangerLevel} == set(
            props["safety"]["properties"]["danger_level"]["enum"]
        )
        assert {o.value for o in ActionOwnership} == set(
            props["action"]["properties"]["ownership"]["enum"]
        )
        assert {k.value for k in ContainerKind} == set(
            defs["container"]["properties"]["kind"]["enum"]
        )

    def test_inbound_payload_from_the_schema_parses(self, schemas: dict[str, Any]) -> None:
        # A payload the mod is allowed to send, written by hand rather than
        # produced by our own serialiser, so this direction is a real check.
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": SESSION,
            "seq": 91,
            "timestamp_ms": 1_700_000_000_000,
            "full": True,
            "game": {"build": "42.20", "save_id": "save-1", "paused": False, "speed": 1.0},
            "player": {
                "present": True,
                "alive": True,
                "position": {"x": 1.0, "y": 2.0, "z": 0},
                "stats": {"hunger": 0.8, "unknownFutureStat": 3},
                "moodles": {"Hungry": 3},
            },
            "safety": {
                "armed": False,
                "mode": "OBSERVE",
                "danger_level": "low",
                "manual_takeover": False,
            },
            "action": {"ownership": "manual", "busy": True},
        }
        assert_valid(schemas, "observation.schema", payload)

        observation = Observation.from_dict(payload)
        assert observation.player.hunger == 0.8
        assert observation.player.stats["unknownFutureStat"] == 3
        assert not observation.can_act  # not armed, and the player is busy

    def test_capability_states_match_the_config_vocabulary(self) -> None:
        # Capability states are referenced by name in capabilities.json; the set
        # is small enough that an accidental rename would be easy to miss.
        assert {s.value for s in CapabilityState} == {
            "verified",
            "available_unverified",
            "experimental",
            "unsupported",
            "disabled_by_policy",
        }
        assert CapabilityState.VERIFIED.usable
        assert CapabilityState.AVAILABLE_UNVERIFIED.usable
        assert not CapabilityState.UNSUPPORTED.usable
        assert not CapabilityState.EXPERIMENTAL.usable
        assert not CapabilityState.DISABLED_BY_POLICY.usable
