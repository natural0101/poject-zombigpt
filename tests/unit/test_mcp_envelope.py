"""The two payload shapes: the retry table, the bounds, and the honesty rule."""

from __future__ import annotations

import pytest

from pz_agent_core.actions import PreconditionFailed
from pz_agent_core.capabilities.model import CapabilityError
from pz_agent_core.protocol import (
    PLAN_FATAL_CODES,
    RETRYABLE_CODES,
    ActionStatus,
    ProtocolError,
    ReasonCode,
    RefError,
)
from pz_agent_mcp.envelope import (
    MAX_DIAGNOSTICS,
    MAX_MESSAGE_CHARS,
    MAX_WARNINGS,
    ToolFailure,
    ToolOutcome,
    ToolSuccess,
    failure_from,
    is_retryable,
)


@pytest.mark.parametrize("code", sorted(ReasonCode))
def test_every_reason_code_serialises_with_the_retry_flag_from_the_table(code: ReasonCode) -> None:
    if code is ReasonCode.POSTCONDITION_MET:
        # The one code reserved for observed success cannot describe a failure.
        with pytest.raises(ValueError, match="POSTCONDITION_MET"):
            ToolFailure(code, "impossible")
        return

    payload = ToolFailure(code, "refused").to_payload(tool="pz_action_eat", request_id="req-1")

    assert payload["ok"] is False
    assert payload["reason_code"] == code.value
    assert payload["retryable"] is (code in RETRYABLE_CODES)
    assert payload["diagnostics"] == []
    assert set(payload) == {
        "ok",
        "tool",
        "request_id",
        "reason_code",
        "message",
        "retryable",
        "diagnostics",
    }


def test_a_plan_fatal_code_is_never_advertised_as_retryable() -> None:
    for code in PLAN_FATAL_CODES:
        assert is_retryable(code) is False


def test_message_and_diagnostics_are_bounded() -> None:
    failure = ToolFailure(
        ReasonCode.INTERNAL_ERROR,
        "x" * (MAX_MESSAGE_CHARS * 2),
        diagnostics=[f"line {n}" for n in range(MAX_DIAGNOSTICS * 3)],
    )

    assert len(failure.message) == MAX_MESSAGE_CHARS
    assert len(failure.diagnostics) == MAX_DIAGNOSTICS


def test_warnings_are_bounded() -> None:
    success = ToolSuccess(
        tool="pz_session_status",
        request_id="req-1",
        warnings=tuple(f"w{n}" for n in range(MAX_WARNINGS * 2)),
    )

    assert len(success.warnings) == MAX_WARNINGS


def test_a_succeeded_envelope_without_evidence_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="observed postcondition"):
        ToolSuccess(
            tool="pz_action_eat",
            request_id="req-1",
            status=ActionStatus.SUCCEEDED.value,
            data={"status": "succeeded"},
        )


def test_a_succeeded_envelope_with_evidence_is_allowed() -> None:
    success = ToolSuccess(
        tool="pz_action_eat",
        request_id="req-1",
        status=ActionStatus.SUCCEEDED.value,
        data={"evidence": {"observed": {"hunger_after": 0.1}}},
        action_id="act-1",
    )

    assert success.to_payload()["action_id"] == "act-1"
    assert success.to_payload()["replayed"] is False


def test_an_accepted_envelope_needs_no_evidence() -> None:
    # "Queued" is the honest word for work that has not been verified, so the
    # invariant must not force evidence onto it.
    success = ToolSuccess(
        tool="pz_action_eat",
        request_id="req-1",
        status=ActionStatus.ACCEPTED.value,
        data={"status": "accepted"},
    )

    assert "action_id" not in success.to_payload()


def test_outcome_identity_is_attached_by_the_router_not_the_handler() -> None:
    outcome = ToolOutcome(data={"a": 1}, message="done", action_id="act-9")

    success = ToolSuccess.of(outcome, tool="pz_action_wait", request_id="req-2", replayed=True)

    assert success.tool == "pz_action_wait"
    assert success.request_id == "req-2"
    assert success.replayed is True
    assert success.action_id == "act-9"


def test_precondition_failure_keeps_the_engines_own_reason_code() -> None:
    failure = failure_from(
        PreconditionFailed("no such square", reason_code=ReasonCode.TARGET_NOT_LOADED)
    )

    assert failure.reason_code is ReasonCode.TARGET_NOT_LOADED


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RefError("bad ref"), ReasonCode.INVALID_REF),
        (CapabilityError("no api"), ReasonCode.CAPABILITY_UNAVAILABLE),
        (ProtocolError("bad field"), ReasonCode.INVALID_ARGUMENT),
        (ZeroDivisionError("boom"), ReasonCode.INTERNAL_ERROR),
    ],
)
def test_core_exceptions_map_to_their_codes(exc: Exception, expected: ReasonCode) -> None:
    assert failure_from(exc).reason_code is expected


def test_a_tool_failure_passes_through_unchanged() -> None:
    original = ToolFailure(ReasonCode.NOT_ARMED, "disarmed")

    assert failure_from(original) is original
