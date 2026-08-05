"""Message parsing and the honest-success invariant.

The invariant under test — a `succeeded` result cannot exist without observed
evidence — is the single rule this whole project is organised around, so it is
tested at the constructor level rather than trusted to reviewer discipline.
"""

from __future__ import annotations

import uuid

import pytest

from pz_agent_core.protocol import (
    ActionName,
    ActionOwnership,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    DangerLevel,
    PlayerState,
    Position,
    ProtocolError,
    ReasonCode,
    RiskClass,
    SessionMode,
    Wound,
)
from pz_agent_core.protocol.messages import MAX_LEASE_MS, MIN_LEASE_MS
from tests.fixtures import make_action_state, make_observation, make_player, make_safety

SESSION = str(uuid.UUID(int=0x5E5510))
COMMAND = str(uuid.UUID(int=0xC0DE))


def make_command(**overrides: object) -> Command:
    base: dict[str, object] = {
        "session_id": SESSION,
        "seq": 7,
        "command_id": COMMAND,
        "idempotency_key": "goal-1:step-2:attempt-1",
        "issued_at_ms": 1_700_000_000_000,
        "lease_ms": 10_000,
        "action": ActionName.CONSUME_EAT,
        "args": {"item_ref": f"item:{SESSION}:player-main:4210:0", "fraction": 0.5},
    }
    base.update(overrides)
    return Command(**base)  # type: ignore[arg-type]


class TestCommand:
    def test_roundtrip(self) -> None:
        command = make_command(
            policy=CommandPolicy(
                allow_interrupt=False, max_retries=2, risk_permission=RiskClass.P2
            ),
            expected_observation_seq=55,
        )
        assert Command.from_dict(command.to_dict()) == command

    def test_optional_fields_are_omitted_not_nulled(self) -> None:
        payload = make_command().to_dict()

        # The schema is additionalProperties:false with no null allowed on these.
        assert "policy" not in payload
        assert "expected_observation_seq" not in payload

    @pytest.mark.parametrize("lease", [MIN_LEASE_MS - 1, MAX_LEASE_MS + 1, 0, -1])
    def test_lease_bounds_enforced(self, lease: int) -> None:
        with pytest.raises(ProtocolError, match="lease_ms"):
            make_command(lease_ms=lease)

    def test_idempotency_key_must_be_present_and_bounded(self) -> None:
        with pytest.raises(ProtocolError, match="idempotency_key"):
            make_command(idempotency_key="")
        with pytest.raises(ProtocolError, match="idempotency_key"):
            make_command(idempotency_key="k" * 129)

    def test_expiry_is_computed_from_the_lease(self) -> None:
        command = make_command(issued_at_ms=1000, lease_ms=5000)

        assert command.deadline_ms() == 6000
        assert not command.is_expired(6000)
        assert command.is_expired(6001)

    def test_unknown_action_is_a_protocol_error(self) -> None:
        payload = make_command().to_dict()
        payload["action"] = "consume.everything"

        with pytest.raises(ProtocolError, match="unknown value"):
            Command.from_dict(payload)

    def test_missing_required_field_names_the_field(self) -> None:
        payload = make_command().to_dict()
        del payload["idempotency_key"]

        with pytest.raises(ProtocolError, match="idempotency_key"):
            Command.from_dict(payload)

    def test_booleans_are_not_accepted_as_integers(self) -> None:
        payload = make_command().to_dict()
        payload["seq"] = True

        with pytest.raises(ProtocolError, match="seq"):
            Command.from_dict(payload)

    def test_retry_budget_is_capped(self) -> None:
        with pytest.raises(ProtocolError, match="max_retries"):
            CommandPolicy.from_dict({"max_retries": 9})


class TestActionResultHonesty:
    def test_success_requires_postcondition_evidence(self) -> None:
        with pytest.raises(ProtocolError, match="evidence"):
            ActionResult.succeeded(
                session_id=SESSION,
                seq=1,
                command_id=COMMAND,
                action="consume.eat",
                timestamp_ms=1,
                evidence={},
            )

    def test_success_cannot_carry_a_failure_reason(self) -> None:
        with pytest.raises(ProtocolError, match="POSTCONDITION_MET"):
            ActionResult(
                session_id=SESSION,
                seq=1,
                command_id=COMMAND,
                action="consume.eat",
                status=ActionStatus.SUCCEEDED,
                reason_code=ReasonCode.ACTION_TIMEOUT,
                timestamp_ms=1,
            )

    def test_failure_helper_refuses_to_forge_a_success(self) -> None:
        with pytest.raises(ProtocolError, match="succeeded"):
            ActionResult.failure(
                session_id=SESSION,
                seq=1,
                command_id=COMMAND,
                action="consume.eat",
                timestamp_ms=1,
                reason_code=ReasonCode.POSTCONDITION_MET,
                status=ActionStatus.SUCCEEDED,
            )

    def test_a_verified_success_roundtrips(self) -> None:
        result = ActionResult.succeeded(
            session_id=SESSION,
            seq=3,
            command_id=COMMAND,
            action="consume.eat",
            timestamp_ms=1_700_000_050_000,
            started_at_ms=1_700_000_040_000,
            evidence={"hunger_before": 0.72, "hunger_after": 0.31, "item_uses_left": 1},
        )

        assert result.status is ActionStatus.SUCCEEDED
        assert result.reason_code is ReasonCode.POSTCONDITION_MET
        assert result.progress == 1.0
        assert result.is_terminal
        assert ActionResult.from_dict(result.to_dict()) == result

    def test_accepted_is_not_terminal(self) -> None:
        result = ActionResult(
            session_id=SESSION,
            seq=1,
            command_id=COMMAND,
            action="consume.eat",
            status=ActionStatus.ACCEPTED,
            reason_code=ReasonCode.POSTCONDITION_MET,
            timestamp_ms=1,
        )
        assert not result.is_terminal

    @pytest.mark.parametrize(
        "status",
        [
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.CANCELLED,
            ActionStatus.REJECTED,
            ActionStatus.LOST,
        ],
    )
    def test_terminal_statuses(self, status: ActionStatus) -> None:
        assert status.is_terminal

    def test_unknown_reason_code_is_rejected_rather_than_coerced(self) -> None:
        result = ActionResult.failure(
            session_id=SESSION,
            seq=1,
            command_id=COMMAND,
            action="movement.move_to",
            timestamp_ms=1,
            reason_code=ReasonCode.PATH_STUCK,
        )
        payload = result.to_dict()
        payload["reason_code"] = "SOMETHING_NEW"

        with pytest.raises(ProtocolError, match="unknown reason_code"):
            ActionResult.from_dict(payload)

    def test_progress_is_bounded(self) -> None:
        with pytest.raises(ProtocolError, match="progress"):
            ActionResult(
                session_id=SESSION,
                seq=1,
                command_id=COMMAND,
                action="literature.read",
                status=ActionStatus.PROGRESS,
                reason_code=ReasonCode.POSTCONDITION_MET,
                timestamp_ms=1,
                progress=1.5,
            )


class TestObservation:
    def test_roundtrip(self) -> None:
        observation = make_observation()
        assert type(observation).from_dict(observation.to_dict()) == observation

    def test_can_act_requires_every_gate(self) -> None:
        assert make_observation().can_act

        assert not make_observation(safety=make_safety(armed=False)).can_act
        assert not make_observation(safety=make_safety(manual_takeover=True)).can_act
        assert not make_observation(safety=make_safety(sidecar_stale=True)).can_act
        assert not make_observation(player=make_player(alive=False)).can_act
        assert not make_observation(player=make_player(present=False)).can_act

    def test_a_manual_action_blocks_automation(self) -> None:
        busy_manual = make_action_state(busy=True, ownership=ActionOwnership.MANUAL)
        assert not make_observation(action=busy_manual).can_act

    def test_an_ambiguous_queue_is_treated_as_the_players(self) -> None:
        ambiguous = make_action_state(busy=True, ownership=ActionOwnership.AMBIGUOUS)
        assert ambiguous.blocks_automation
        assert not make_observation(action=ambiguous).can_act

    def test_our_own_action_does_not_block_us(self) -> None:
        ours = make_action_state(busy=True, ownership=ActionOwnership.MOD)
        assert not ours.blocks_automation
        assert make_observation(action=ours).can_act

    def test_unknown_stats_survive_the_boundary(self) -> None:
        observation = make_observation(player=make_player(stats={"someNewBuild42Stat": 0.5}))
        payload = observation.to_dict()

        assert payload["player"]["stats"]["someNewBuild42Stat"] == 0.5

    def test_named_stat_accessors_have_safe_defaults(self) -> None:
        # Built bare rather than through the fixture, which merges over
        # defaults: the point is what the mod reporting no stats at all does.
        player = PlayerState(
            present=True, alive=True, position=Position(x=0.0, y=0.0, z=0), stats={}
        )

        # An absent need must read as "satisfied", never as "critical" — a
        # default that leans the other way would let a parsing gap trigger an
        # autonomous eat.
        assert player.hunger == 0.0
        assert player.thirst == 0.0
        assert player.fatigue == 0.0
        assert player.health == 1.0
        assert player.endurance == 1.0

    def test_a_boolean_stat_does_not_masquerade_as_a_level(self) -> None:
        player = PlayerState(
            present=True,
            alive=True,
            position=Position(x=0.0, y=0.0, z=0),
            stats={"hunger": True},
        )
        assert player.hunger == 0.0

    def test_bleeding_is_derived_from_wounds(self) -> None:
        assert not make_player().is_bleeding
        bleeding = make_player(
            wounds=[Wound(ref="wound:1", kind="laceration", severity=0.6, bleeding=True)]
        )
        assert bleeding.is_bleeding

    def test_mode_and_danger_ordering(self) -> None:
        assert DangerLevel.CRITICAL > DangerLevel.HIGH > DangerLevel.NONE
        assert make_safety(mode=SessionMode.OBSERVE).mode is SessionMode.OBSERVE
