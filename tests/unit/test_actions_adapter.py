"""The adapter contract: evidence, refusals and the registry.

Everything here is about the seam that makes "no evidence, no success"
structural rather than a convention someone has to remember.
"""

from __future__ import annotations

import pytest

from pz_agent_core.actions import (
    ActionAdapter,
    AdapterRegistry,
    CancelAdapter,
    DuplicateAdapterError,
    Evidence,
    PreconditionFailed,
    ProgressReporter,
    UnknownActionError,
    WaitAdapter,
    register_builtins,
)
from pz_agent_core.protocol import ActionName, ActionResult, ActionStatus, ReasonCode

SESSION = "00000000-0000-0000-0000-0000005e5510"
COMMAND = "00000000-0000-0000-0000-00000000c0de"


def test_evidence_requires_an_observed_payload() -> None:
    with pytest.raises(ValueError, match="at least one observed value"):
        Evidence(kind="anything", observation_seq=3, observed={})


def test_evidence_requires_a_label_and_a_real_sequence() -> None:
    with pytest.raises(ValueError, match="non-empty label"):
        Evidence(kind="   ", observation_seq=3, observed={"x": 1})
    with pytest.raises(ValueError, match="non-negative"):
        Evidence(kind="k", observation_seq=-1, observed={"x": 1})


def test_evidence_payload_satisfies_the_success_constructor() -> None:
    """The only bridge between an adapter and a successful result."""
    evidence = Evidence(kind="hunger_decreased", observation_seq=9, observed={"hunger": 0.1})
    result = ActionResult.succeeded(
        session_id=SESSION,
        seq=0,
        command_id=COMMAND,
        action=ActionName.CONSUME_EAT.value,
        timestamp_ms=1,
        evidence=evidence.to_payload(),
    )
    assert result.status is ActionStatus.SUCCEEDED
    assert result.reason_code is ReasonCode.POSTCONDITION_MET
    assert result.evidence["observed"] == {"hunger": 0.1}
    assert result.evidence["observation_seq"] == 9


def test_precondition_failure_cannot_claim_the_success_code() -> None:
    with pytest.raises(ValueError, match="cannot explain a refusal"):
        PreconditionFailed("no", reason_code=ReasonCode.POSTCONDITION_MET)


def test_precondition_failure_carries_code_and_evidence() -> None:
    exc = PreconditionFailed(
        "item is gone",
        reason_code=ReasonCode.INVALID_REF,
        evidence={"item_ref": "item:x:1:0"},
    )
    assert exc.reason_code is ReasonCode.INVALID_REF
    assert exc.evidence == {"item_ref": "item:x:1:0"}
    assert str(exc) == "item is gone"


def test_registry_rejects_a_second_adapter_for_one_action() -> None:
    registry = AdapterRegistry()
    registry.register(WaitAdapter())
    with pytest.raises(DuplicateAdapterError) as caught:
        registry.register(WaitAdapter(timeout_ms=1_000))
    assert caught.value.action is ActionName.ACTION_WAIT
    assert len(registry) == 1


def test_registry_reports_an_unregistered_action_as_a_missing_capability() -> None:
    registry = AdapterRegistry()
    with pytest.raises(UnknownActionError) as caught:
        registry.get(ActionName.CONSUME_EAT)
    assert caught.value.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert caught.value.action is ActionName.CONSUME_EAT
    assert "consume.eat" in str(caught.value)


def test_registry_is_open_and_ordered() -> None:
    registry = register_builtins(AdapterRegistry())
    assert registry.names() == (ActionName.ACTION_WAIT, ActionName.PLAN_CANCEL)
    assert ActionName.ACTION_WAIT in registry
    assert ActionName.MOVEMENT_MOVE_TO not in registry
    assert isinstance(registry.get(ActionName.PLAN_CANCEL), CancelAdapter)


def test_builtin_adapters_satisfy_the_protocol() -> None:
    wait: ActionAdapter = WaitAdapter()
    cancel: ActionAdapter = CancelAdapter()
    assert wait.name is ActionName.ACTION_WAIT
    assert cancel.required_capability is None
    # Only the adapter that can measure its own advance reports progress.
    assert isinstance(wait, ProgressReporter)
    assert not isinstance(cancel, ProgressReporter)
