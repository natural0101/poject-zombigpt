"""The permission ladder: every cell that answers differently, and why.

The table-driven tests below walk mode x risk x initiative and mode x capability
because the ladder is a table — a single example per mode would leave the cells
that matter most (the ones that refuse) unexercised.
"""

from __future__ import annotations

import pytest

from pz_agent_core.policy.permissions import (
    MODE_LIMITS,
    Initiative,
    ModeLimits,
    PermissionConfig,
    PermissionDecision,
    PermissionRequest,
    check_permission,
    is_mutating,
)
from pz_agent_core.policy.selection import CapabilitySnapshot
from pz_agent_core.protocol import (
    ALWAYS_ALLOWED_ACTIONS,
    MUTATING_MODES,
    READ_ONLY_ACTIONS,
    SELF_DIRECTED_MODES,
    ActionName,
    CapabilityState,
    ReasonCode,
    RiskClass,
    SessionMode,
)

CAPABILITY = "eat_percentage"


def _request(
    *,
    action: ActionName = ActionName.CONSUME_EAT,
    risk: RiskClass = RiskClass.P2,
    mode: SessionMode = SessionMode.ASSISTED,
    initiative: Initiative = Initiative.USER,
    capability: str | None = None,
    grant: RiskClass | None = None,
) -> PermissionRequest:
    return PermissionRequest(
        action=action,
        risk=risk,
        mode=mode,
        initiative=initiative,
        capability=capability,
        grant=grant,
    )


def _snapshot(state: CapabilityState) -> CapabilitySnapshot:
    return CapabilitySnapshot.from_mapping({CAPABILITY: state})


# --------------------------------------------------------------------------
# the table agrees with the protocol's own sets
# --------------------------------------------------------------------------


def test_every_session_mode_has_a_row() -> None:
    assert set(MODE_LIMITS) == set(SessionMode)


def test_the_mutating_modes_are_exactly_those_with_a_ceiling_above_p0() -> None:
    mutating = {
        mode
        for mode, limits in MODE_LIMITS.items()
        if limits.user_max is not None and limits.user_max.rank > RiskClass.P0.rank
    }

    assert mutating == set(MUTATING_MODES)


def test_the_self_directed_modes_are_exactly_those_with_an_autonomous_ceiling() -> None:
    self_directed = {
        mode for mode, limits in MODE_LIMITS.items() if limits.self_directed_max is not None
    }

    assert self_directed == set(SELF_DIRECTED_MODES)


def test_no_mode_may_be_given_a_p4_ceiling() -> None:
    with pytest.raises(ValueError, match="P4 may never be a mode ceiling"):
        ModeLimits(user_max=RiskClass.P4, self_directed_max=None)


def test_a_mode_cannot_permit_autonomy_it_forbids_to_the_user() -> None:
    with pytest.raises(ValueError, match="cannot permit autonomous"):
        ModeLimits(user_max=None, self_directed_max=RiskClass.P1)


def test_mutating_is_derived_from_the_protocol_sets() -> None:
    assert not any(is_mutating(action) for action in READ_ONLY_ACTIONS)
    assert not any(is_mutating(action) for action in ALWAYS_ALLOWED_ACTIONS)
    assert is_mutating(ActionName.CONSUME_EAT)


# --------------------------------------------------------------------------
# stopping is never gated
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(SessionMode))
@pytest.mark.parametrize(
    "action", [ActionName.SAFETY_STOP, ActionName.SESSION_DISARM, ActionName.PLAN_CANCEL]
)
def test_stopping_and_disarming_are_permitted_in_every_mode(
    mode: SessionMode, action: ActionName
) -> None:
    decision = check_permission(_request(action=action, risk=RiskClass.P1, mode=mode))

    assert decision.allowed
    assert decision.reason_code is None


def test_a_stop_is_not_refused_by_a_missing_capability() -> None:
    decision = check_permission(
        _request(
            action=ActionName.SAFETY_STOP,
            risk=RiskClass.P1,
            mode=SessionMode.OFF,
            capability=CAPABILITY,
        ),
        capabilities=_snapshot(CapabilityState.UNSUPPORTED),
    )

    assert decision.allowed


def test_a_stop_is_not_refused_on_the_agents_own_initiative() -> None:
    decision = check_permission(
        _request(
            action=ActionName.SAFETY_STOP,
            risk=RiskClass.P1,
            mode=SessionMode.OBSERVE,
            initiative=Initiative.SELF_DIRECTED,
        )
    )

    assert decision.allowed


# --------------------------------------------------------------------------
# OFF and OBSERVE
# --------------------------------------------------------------------------


def test_off_refuses_even_a_read_and_says_automation_is_off() -> None:
    decision = check_permission(
        _request(action=ActionName.WORLD_INSPECT, risk=RiskClass.P0, mode=SessionMode.OFF)
    )

    assert decision.denied
    assert decision.reason_code is ReasonCode.NOT_ARMED
    assert not decision.requires_grant


@pytest.mark.parametrize("action", sorted(READ_ONLY_ACTIONS))
def test_read_only_actions_are_permitted_in_observe(action: ActionName) -> None:
    decision = check_permission(
        _request(action=action, risk=RiskClass.P0, mode=SessionMode.OBSERVE)
    )

    assert decision.allowed


def test_a_mutating_action_is_refused_in_observe_even_at_p0() -> None:
    decision = check_permission(
        _request(action=ActionName.CONSUME_EAT, risk=RiskClass.P0, mode=SessionMode.OBSERVE)
    )

    assert decision.denied
    assert decision.reason_code is ReasonCode.POLICY_DENIED
    assert "observation mode" in decision.detail


def test_a_read_only_action_above_p0_is_still_refused_in_observe() -> None:
    decision = check_permission(
        _request(action=ActionName.WORLD_INSPECT, risk=RiskClass.P3, mode=SessionMode.OBSERVE)
    )

    assert decision.denied
    assert decision.reason_code is ReasonCode.POLICY_DENIED
    assert "allows at most P0" in decision.detail


def test_reflex_only_refuses_mutation_like_observe() -> None:
    decision = check_permission(_request(mode=SessionMode.REFLEX_ONLY))

    assert decision.denied
    assert decision.reason_code is ReasonCode.POLICY_DENIED


# --------------------------------------------------------------------------
# the risk ceiling, mode by mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "risk", "allowed"),
    [
        (SessionMode.OBSERVE, RiskClass.P0, False),  # eat is mutating
        (SessionMode.ASSISTED, RiskClass.P0, True),
        (SessionMode.ASSISTED, RiskClass.P1, True),
        (SessionMode.ASSISTED, RiskClass.P2, True),
        (SessionMode.ASSISTED, RiskClass.P3, True),
        (SessionMode.ASSISTED, RiskClass.P4, False),
        (SessionMode.AUTONOMOUS, RiskClass.P3, True),
        (SessionMode.AUTONOMOUS, RiskClass.P4, False),
        (SessionMode.EXPERIMENTAL_INPUT, RiskClass.P3, True),
        (SessionMode.EXPERIMENTAL_INPUT, RiskClass.P4, False),
    ],
)
def test_user_directed_ceiling_per_mode(mode: SessionMode, risk: RiskClass, allowed: bool) -> None:
    decision = check_permission(_request(risk=risk, mode=mode))

    assert decision.allowed is allowed


@pytest.mark.parametrize(
    ("risk", "allowed"),
    [
        (RiskClass.P0, True),
        (RiskClass.P1, True),
        (RiskClass.P2, True),
        (RiskClass.P3, False),
        (RiskClass.P4, False),
    ],
)
def test_autonomous_initiative_stops_at_p2(risk: RiskClass, allowed: bool) -> None:
    decision = check_permission(
        _request(risk=risk, mode=SessionMode.AUTONOMOUS, initiative=Initiative.SELF_DIRECTED)
    )

    assert decision.allowed is allowed


def test_a_p3_action_may_be_whitelisted_for_autonomy() -> None:
    config = PermissionConfig(autonomous_p3_actions=frozenset({ActionName.INVENTORY_TRANSFER}))
    request = _request(
        action=ActionName.INVENTORY_TRANSFER,
        risk=RiskClass.P3,
        mode=SessionMode.AUTONOMOUS,
        initiative=Initiative.SELF_DIRECTED,
    )

    assert check_permission(request, config=config).allowed
    assert check_permission(request).denied


def test_the_p3_whitelist_is_empty_by_default() -> None:
    assert not PermissionConfig().autonomous_p3_actions


def test_the_whitelist_refuses_a_read_only_action() -> None:
    with pytest.raises(ValueError, match="permitted without a P3 grant already"):
        PermissionConfig(autonomous_p3_actions=frozenset({ActionName.WORLD_INSPECT}))


@pytest.mark.parametrize("mode", sorted(set(SessionMode) - set(SELF_DIRECTED_MODES)))
def test_no_other_mode_acts_on_its_own_initiative(mode: SessionMode) -> None:
    decision = check_permission(
        _request(risk=RiskClass.P1, mode=mode, initiative=Initiative.SELF_DIRECTED)
    )

    assert decision.denied


# --------------------------------------------------------------------------
# P4
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(SessionMode))
@pytest.mark.parametrize("grant", [None, RiskClass.P3, RiskClass.P4])
def test_p4_is_unreachable_on_the_agents_own_initiative(
    mode: SessionMode, grant: RiskClass | None
) -> None:
    """No mode, and no grant, lets the agent take a P4 action unasked."""
    decision = check_permission(
        _request(
            action=ActionName.INVENTORY_TRANSFER,
            risk=RiskClass.P4,
            mode=mode,
            initiative=Initiative.SELF_DIRECTED,
            grant=grant,
        )
    )

    assert decision.denied
    assert decision.reason_code in (ReasonCode.POLICY_DENIED, ReasonCode.NOT_ARMED)


def test_p4_refused_autonomously_says_it_would_need_the_user() -> None:
    decision = check_permission(
        _request(
            risk=RiskClass.P4,
            mode=SessionMode.AUTONOMOUS,
            initiative=Initiative.SELF_DIRECTED,
            grant=RiskClass.P4,
        )
    )

    assert decision.reason_code is ReasonCode.POLICY_DENIED
    assert decision.requires_grant
    assert "on my own initiative" in decision.detail


def test_p4_needs_an_explicit_per_call_grant_even_from_the_user() -> None:
    decision = check_permission(_request(risk=RiskClass.P4, mode=SessionMode.ASSISTED))

    assert decision.denied
    assert decision.requires_grant
    assert "explicit permission for this one call" in decision.detail


def test_a_p3_grant_does_not_unlock_p4() -> None:
    decision = check_permission(
        _request(risk=RiskClass.P4, mode=SessionMode.ASSISTED, grant=RiskClass.P3)
    )

    assert decision.denied


def test_p4_with_its_grant_runs_for_the_user_in_a_mutating_mode() -> None:
    decision = check_permission(
        _request(risk=RiskClass.P4, mode=SessionMode.ASSISTED, grant=RiskClass.P4)
    )

    assert decision.allowed


def test_a_p4_grant_does_not_work_in_an_observation_mode() -> None:
    decision = check_permission(
        _request(risk=RiskClass.P4, mode=SessionMode.OBSERVE, grant=RiskClass.P4)
    )

    assert decision.denied
    assert not decision.requires_grant


# --------------------------------------------------------------------------
# grants below P4
# --------------------------------------------------------------------------


def test_a_grant_does_not_lift_an_observation_mode() -> None:
    decision = check_permission(
        _request(
            action=ActionName.WORLD_INSPECT,
            risk=RiskClass.P3,
            mode=SessionMode.OBSERVE,
            grant=RiskClass.P3,
        )
    )

    assert decision.denied
    assert "allows at most P0" in decision.detail


def test_a_grant_is_ignored_on_a_self_directed_call() -> None:
    decision = check_permission(
        _request(
            risk=RiskClass.P3,
            mode=SessionMode.AUTONOMOUS,
            initiative=Initiative.SELF_DIRECTED,
            grant=RiskClass.P3,
        )
    )

    assert decision.denied
    assert decision.requires_grant


# --------------------------------------------------------------------------
# capability
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "allowed"),
    [
        (CapabilityState.VERIFIED, True),
        (CapabilityState.AVAILABLE_UNVERIFIED, True),
        (CapabilityState.EXPERIMENTAL, False),
        (CapabilityState.UNSUPPORTED, False),
        (CapabilityState.DISABLED_BY_POLICY, False),
    ],
)
def test_capability_state_gates_a_user_directed_command(
    state: CapabilityState, allowed: bool
) -> None:
    decision = check_permission(_request(capability=CAPABILITY), capabilities=_snapshot(state))

    assert decision.allowed is allowed
    if not allowed:
        assert decision.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE


def test_an_unprobed_capability_blocks_rather_than_being_attempted() -> None:
    decision = check_permission(_request(capability="never_probed"))

    assert decision.denied
    assert decision.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert "no probe has shown it working" in decision.detail


def test_an_unverified_capability_blocks_autonomy_but_not_the_user() -> None:
    capabilities = _snapshot(CapabilityState.AVAILABLE_UNVERIFIED)
    autonomous = check_permission(
        _request(
            capability=CAPABILITY, mode=SessionMode.AUTONOMOUS, initiative=Initiative.SELF_DIRECTED
        ),
        capabilities=capabilities,
    )
    user = check_permission(_request(capability=CAPABILITY), capabilities=capabilities)

    assert autonomous.denied
    assert autonomous.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert autonomous.requires_grant
    assert user.allowed


def test_the_autonomy_verification_requirement_can_be_relaxed() -> None:
    decision = check_permission(
        _request(
            capability=CAPABILITY, mode=SessionMode.AUTONOMOUS, initiative=Initiative.SELF_DIRECTED
        ),
        capabilities=_snapshot(CapabilityState.AVAILABLE_UNVERIFIED),
        config=PermissionConfig(require_verified_capability_for_autonomy=False),
    )

    assert decision.allowed


def test_an_experimental_capability_asks_rather_than_refusing_outright() -> None:
    decision = check_permission(
        _request(capability=CAPABILITY), capabilities=_snapshot(CapabilityState.EXPERIMENTAL)
    )

    assert decision.requires_grant


def test_a_disabled_capability_does_not_ask() -> None:
    decision = check_permission(
        _request(capability=CAPABILITY),
        capabilities=_snapshot(CapabilityState.DISABLED_BY_POLICY),
    )

    assert decision.denied
    assert not decision.requires_grant


def test_the_mode_is_refused_before_the_capability_is_consulted() -> None:
    decision = check_permission(
        _request(mode=SessionMode.OFF, capability=CAPABILITY),
        capabilities=_snapshot(CapabilityState.VERIFIED),
    )

    assert decision.reason_code is ReasonCode.NOT_ARMED


def test_an_action_needing_no_capability_is_not_gated_on_one() -> None:
    decision = check_permission(_request(capability=None))

    assert decision.allowed


# --------------------------------------------------------------------------
# the decision value itself
# --------------------------------------------------------------------------


def test_a_decision_cannot_be_allowed_and_carry_a_reason() -> None:
    with pytest.raises(ValueError, match="refusal carries a reason code"):
        PermissionDecision(allowed=True, detail="fine", reason_code=ReasonCode.POLICY_DENIED)


def test_a_refusal_must_carry_a_reason() -> None:
    with pytest.raises(ValueError, match="refusal carries a reason code"):
        PermissionDecision(allowed=False, detail="no")


def test_a_decision_must_explain_itself() -> None:
    with pytest.raises(ValueError, match="must explain itself"):
        PermissionDecision(allowed=True, detail="   ")


def test_an_allowed_command_is_not_waiting_on_a_grant() -> None:
    with pytest.raises(ValueError, match="not waiting on a grant"):
        PermissionDecision(allowed=True, detail="fine", requires_grant=True)


def test_postcondition_met_cannot_explain_a_refusal() -> None:
    with pytest.raises(ValueError, match="cannot explain a refusal"):
        PermissionDecision.refuse(ReasonCode.POSTCONDITION_MET, "no")


def test_a_capability_name_may_not_be_blank() -> None:
    with pytest.raises(ValueError, match="probe name"):
        _request(capability="  ")
