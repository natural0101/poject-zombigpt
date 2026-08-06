"""The record invariants the boundary refuses to let a port break."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pz_agent_core.protocol import ActionName, ActionStatus, ReasonCode, SessionMode
from pz_agent_mcp.ports import ActionRecord, PlanRecord, StopReport, evidence_payload
from tests.fixtures.mcp_doubles import failed_result, succeeded_result


def test_an_accepted_record_needs_no_result() -> None:
    record = ActionRecord(
        action_id="act-1",
        action=ActionName.CONSUME_EAT,
        status=ActionStatus.ACCEPTED,
        idempotency_key="k1",
    )

    assert record.terminal is False


def test_a_succeeded_record_without_a_result_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="observed postcondition"):
        ActionRecord(
            action_id="act-1",
            action=ActionName.CONSUME_EAT,
            status=ActionStatus.SUCCEEDED,
            idempotency_key="k1",
        )


def test_a_succeeded_record_whose_result_carries_no_evidence_cannot_be_built() -> None:
    ack = failed_result(ActionName.CONSUME_EAT, ReasonCode.POSTCONDITION_FAILED)

    with pytest.raises(ValueError, match="observed postcondition"):
        ActionRecord(
            action_id="act-1",
            action=ActionName.CONSUME_EAT,
            status=ActionStatus.SUCCEEDED,
            idempotency_key="k1",
            result=ack,
        )


def test_a_succeeded_record_whose_result_proves_something_else_cannot_be_built() -> None:
    # Evidence alone is not enough: a result that looked at the world and did
    # not find the postcondition carries a payload too, and reporting it as
    # success would put the one code reserved for observed success on it.
    ack = replace(
        succeeded_result(ActionName.CONSUME_EAT),
        status=ActionStatus.FAILED,
        reason_code=ReasonCode.POSTCONDITION_FAILED,
    )

    with pytest.raises(ValueError, match="POSTCONDITION_MET"):
        ActionRecord(
            action_id="act-1",
            action=ActionName.CONSUME_EAT,
            status=ActionStatus.SUCCEEDED,
            idempotency_key="k1",
            result=ack,
        )


def test_a_succeeded_record_with_observed_evidence_is_allowed() -> None:
    ack = succeeded_result(ActionName.CONSUME_EAT)

    record = ActionRecord(
        action_id="act-1",
        action=ActionName.CONSUME_EAT,
        status=ActionStatus.SUCCEEDED,
        idempotency_key="k1",
        result=ack,
    )

    assert record.terminal is True
    assert evidence_payload(ack)["kind"] == "stub_postcondition"


def test_progress_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValueError, match="progress must be within"):
        ActionRecord(
            action_id="act-1",
            action=ActionName.ACTION_WAIT,
            status=ActionStatus.STARTED,
            idempotency_key="k1",
            progress=1.5,
        )


def test_a_record_without_an_id_is_refused() -> None:
    with pytest.raises(ValueError, match="id the caller waits on"):
        ActionRecord(
            action_id="",
            action=ActionName.ACTION_WAIT,
            status=ActionStatus.ACCEPTED,
            idempotency_key="k1",
        )


def test_evidence_of_a_failed_result_is_empty_not_the_world_snapshot() -> None:
    # The failure evidence explains a refusal; it never proves a postcondition,
    # so a caller reading 'evidence' must get nothing from it.
    ack = failed_result(ActionName.MOVEMENT_MOVE_TO, ReasonCode.PATH_NOT_FOUND)

    assert evidence_payload(ack) == {}


def test_a_stop_report_cannot_claim_a_negative_number_of_cleared_entries() -> None:
    with pytest.raises(ValueError, match="cleared must be non-negative"):
        StopReport(cleared=-1, disarmed=True, mode=SessionMode.OBSERVE)


def test_a_plan_record_needs_an_id_and_a_non_negative_step() -> None:
    with pytest.raises(ValueError, match="id the caller polls"):
        PlanRecord(plan_id="", status=ActionStatus.ACCEPTED, step_index=0)
    with pytest.raises(ValueError, match="step_index must be non-negative"):
        PlanRecord(plan_id="plan-1", status=ActionStatus.ACCEPTED, step_index=-1)
