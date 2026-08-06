"""``plan.cancel`` as the game-adapter package implements it.

The postcondition is negative — "no entry this session owns is still running" —
and the tests below are mostly about the ways that sentence can be true without
anything having been cleared. A manual action satisfies it immediately, because
the agent must never take the player's work out of the queue; an ``ambiguous``
entry does not, because the mod saying it cannot tell whose entry it is is not
the same as the entry being gone.

The last two tests pin the relationship with the API-free cancel in
:mod:`pz_agent_core.actions.builtin`: both implement the action, and a registry
that has the builtin keeps it, because stopping must work before anything has
been probed.
"""

from __future__ import annotations

import uuid

import pytest

from pz_agent_core.actions import AdapterRegistry, PreconditionFailed, register_builtins
from pz_agent_core.actions.adapters import PlanCancelAdapter, register_game_adapters
from pz_agent_core.actions.builtin import CancelAdapter
from pz_agent_core.protocol import (
    ActionName,
    ActionOwnership,
    ActionState,
    Command,
    Observation,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import DEFAULT_SESSION, make_action_state, make_observation
from tests.fixtures.adapter_worlds import a_command, prepare

OTHER_SESSION = str(uuid.UUID(int=0xBEEF))
TARGET_ID = str(uuid.UUID(int=0xC0DE))


def queue(
    *,
    ownership: ActionOwnership = ActionOwnership.NONE,
    busy: bool = False,
    action_id: str | None = None,
    seq: int = 1,
    session_id: str = DEFAULT_SESSION,
) -> Observation:
    return make_observation(
        seq=seq,
        session_id=session_id,
        action=make_action_state(ownership=ownership, busy=busy, action_id=action_id),
    )


def cancel_command(**args: object) -> Command:
    return a_command(ActionName.PLAN_CANCEL, dict(args))


# --------------------------------------------------------------------------
# the postcondition
# --------------------------------------------------------------------------


def test_an_idle_queue_is_the_evidence() -> None:
    adapter = PlanCancelAdapter()
    before = queue(ownership=ActionOwnership.MOD, busy=True)
    command = prepare(adapter, cancel_command(), before)

    evidence = adapter.verify(command, before, queue(seq=2))

    assert evidence is not None
    assert evidence.kind == "no_session_owned_action_in_flight"
    assert evidence.observed["busy"] is False


def test_a_mod_owned_action_still_running_is_not_a_cancelled_plan() -> None:
    adapter = PlanCancelAdapter()
    before = queue(ownership=ActionOwnership.MOD, busy=True)
    command = prepare(adapter, cancel_command(), before)

    still_going = queue(ownership=ActionOwnership.MOD, busy=True, seq=2)

    assert adapter.verify(command, before, still_going) is None


def test_an_ambiguous_entry_is_not_proof_that_ours_is_gone() -> None:
    adapter = PlanCancelAdapter()
    before = queue(ownership=ActionOwnership.MOD, busy=True)
    command = prepare(adapter, cancel_command(), before)

    unsure = queue(ownership=ActionOwnership.AMBIGUOUS, busy=True, seq=2)

    assert adapter.verify(command, before, unsure) is None


def test_the_players_own_action_satisfies_the_cancel_immediately() -> None:
    """§4.3: a cancel must never reach for something the player queued."""
    adapter = PlanCancelAdapter()
    before = queue(ownership=ActionOwnership.MANUAL, busy=True)
    command = prepare(adapter, cancel_command(), before)

    evidence = adapter.verify(
        command, before, queue(ownership=ActionOwnership.MANUAL, busy=True, seq=2)
    )

    assert evidence is not None
    assert evidence.observed["ownership"] == ActionOwnership.MANUAL.value


def test_a_targeted_cancel_is_satisfied_once_a_different_entry_is_running() -> None:
    adapter = PlanCancelAdapter()
    before = queue(ownership=ActionOwnership.MOD, busy=True, action_id=TARGET_ID)
    command = prepare(adapter, cancel_command(command_id=TARGET_ID), before)

    other = queue(
        ownership=ActionOwnership.MOD, busy=True, action_id=str(uuid.UUID(int=0xFEED)), seq=2
    )

    evidence = adapter.verify(command, before, other)

    assert evidence is not None
    assert evidence.observed["cancelled_command_id"] == TARGET_ID


def test_a_targeted_cancel_is_not_satisfied_by_an_unidentified_entry() -> None:
    adapter = PlanCancelAdapter()
    before = queue(ownership=ActionOwnership.MOD, busy=True, action_id=TARGET_ID)
    command = prepare(adapter, cancel_command(command_id=TARGET_ID), before)

    nameless = queue(ownership=ActionOwnership.MOD, busy=True, seq=2)

    assert adapter.verify(command, before, nameless) is None


def test_another_sessions_observation_cannot_clear_this_sessions_queue() -> None:
    """Its ``mod`` tag is somebody else's ownership."""
    adapter = PlanCancelAdapter()
    before = queue(ownership=ActionOwnership.MOD, busy=True)
    command = prepare(adapter, cancel_command(), before)

    assert adapter.verify(command, before, queue(seq=2, session_id=OTHER_SESSION)) is None


# --------------------------------------------------------------------------
# refusals and shape
# --------------------------------------------------------------------------


def test_an_observation_from_another_session_is_refused_before_anything_is_sent() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        PlanCancelAdapter().validate(cancel_command(), queue(session_id=OTHER_SESSION))
    assert caught.value.reason_code is ReasonCode.STALE_SESSION


def test_a_command_id_that_is_not_a_uuid_is_refused() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        PlanCancelAdapter().validate(cancel_command(command_id="everything"), queue())
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_an_unknown_argument_is_refused_rather_than_dropped() -> None:
    with pytest.raises(PreconditionFailed) as caught:
        PlanCancelAdapter().validate(cancel_command(scope="all"), queue())
    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_cancelling_everything_sends_no_target() -> None:
    assert PlanCancelAdapter().build_args(cancel_command(), queue()) == {}


def test_stopping_never_depends_on_a_probe() -> None:
    adapter = PlanCancelAdapter()

    assert adapter.required_capability is None
    assert adapter.risk is RiskClass.P1


# --------------------------------------------------------------------------
# who owns the action in a composed registry
# --------------------------------------------------------------------------


def test_a_registry_of_game_adapters_alone_still_covers_the_cancel() -> None:
    registry = register_game_adapters(AdapterRegistry())

    assert isinstance(registry.get(ActionName.PLAN_CANCEL), PlanCancelAdapter)


def test_the_api_free_cancel_keeps_the_action_when_both_are_registered() -> None:
    """A stop that a missing probe could displace is not a stop."""
    registry = register_game_adapters(register_builtins(AdapterRegistry()))

    assert isinstance(registry.get(ActionName.PLAN_CANCEL), CancelAdapter)


def test_the_two_cancels_agree_on_what_counts_as_cleared() -> None:
    before = queue(ownership=ActionOwnership.MOD, busy=True)
    after = queue(ownership=ActionOwnership.MOD, busy=True, seq=2)
    command = prepare(PlanCancelAdapter(), cancel_command(), before)

    assert PlanCancelAdapter().verify(command, before, after) is None
    assert CancelAdapter().verify(command, before, after) is None


def test_an_action_state_the_mod_reports_as_idle_needs_no_ownership_check() -> None:
    idle = ActionState(ownership=ActionOwnership.MOD, busy=False)
    before = queue(ownership=ActionOwnership.MOD, busy=True)
    command = prepare(PlanCancelAdapter(), cancel_command(), before)

    assert (
        PlanCancelAdapter().verify(command, before, make_observation(seq=2, action=idle))
        is not None
    )
