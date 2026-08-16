"""Every success kind is classified, so a new one cannot reopen the hole quietly.

The critic refuses an unobserved item reference under ``ITEM_CONSUMED``, because
that criterion is satisfied by the item's *absence*: an item nobody ever
reported is absent, ``holds`` returns True, and ``PlanExecutor._gate`` skips the
step before reaching the reference check that would have refused it. A
hallucinated reference produced a finished plan and a silent nothing.

Closing that instance is not the same as closing the class. ``_ITEM_READING_CRITERIA``
is a set with one member today, and a seventh :class:`SuccessKind` added later —
``item_dropped``, ``container_emptied``, anything phrased as a disappearance —
would be satisfied by the same absence and would not be in it.

So this does not compare two lists. For **every** kind the enum declares, it
runs the real ``SuccessCriterion.holds`` against a real observation with a
reference the observation does not carry, and asserts the classification the
critic depends on:

* the kind is gated by the critic, **or**
* an unobserved reference does not make it hold.

A kind that is satisfied by an absence and is not gated fails here, and the
failure names the choice to make. That cannot be satisfied by editing a literal;
it has to be satisfied by the behaviour.
"""

from __future__ import annotations

from typing import Final

import pytest

from pz_agent_core.planner.critic import _ITEM_READING_CRITERIA
from pz_agent_core.planner.plan import (
    ConsumeArgs,
    MoveToArgs,
    ReadArgs,
    StepArgs,
    SuccessCriterion,
    SuccessKind,
)
from pz_agent_core.protocol import Observation
from tests.fixtures.planner_worlds import item_ref, planner_observation

#: A reference of the right shape for this session that names nothing observed.
INVENTED: Final = item_ref("999999")

#: Thresholds the two stat criteria need; the value is irrelevant to the
#: question here, which is only whether an unobserved *reference* satisfies them.
_THRESHOLDS: Final = {SuccessKind.HUNGER_AT_MOST: 0.15, SuccessKind.THIRST_AT_MOST: 0.15}


def _args_for(kind: SuccessKind, ref: str) -> StepArgs:
    """Arguments carrying *ref* wherever the kind could read one."""
    if kind is SuccessKind.POSITION_REACHED:
        return MoveToArgs(x=1200, y=3400, z=0)
    if kind is SuccessKind.ITEM_IN_MAIN_INVENTORY:
        return ReadArgs(item_ref=ref)
    return ConsumeArgs(item_ref=ref)


def _criterion(kind: SuccessKind) -> SuccessCriterion:
    value = _THRESHOLDS.get(kind)
    return (
        SuccessCriterion(kind=kind, value=value)
        if value is not None
        else SuccessCriterion(kind=kind)
    )


def test_the_enum_has_kinds_to_classify() -> None:
    """A vacuous enum would make every case below pass over nothing."""
    assert len(list(SuccessKind)) >= 6


def test_the_gated_set_names_only_real_kinds() -> None:
    """A stale member would gate nothing and read as protection."""
    assert set(SuccessKind) >= _ITEM_READING_CRITERIA
    assert _ITEM_READING_CRITERIA, "the critic gates no criterion at all"


@pytest.mark.parametrize("kind", list(SuccessKind), ids=lambda k: k.value)
def test_a_kind_satisfied_by_an_unobserved_reference_is_gated(kind: SuccessKind) -> None:
    """The load-bearing one, measured through the real criterion.

    Which kinds are even in scope is *measured* rather than declared: a kind is
    reference-reading when its answer changes with the reference it is given.
    ``position_reached`` reads the player's position and no reference at all, so
    it answers the same either way and is not in scope — a first version of this
    test declared the scope by hand instead and accused it, which would have
    been a false accusation of the kind this repository has already been taught
    to avoid.

    For a kind that *is* in scope, the dangerous shape is exact: a reference
    nobody observed makes the criterion hold while an observed one does not.
    That is what lets ``PlanExecutor._gate`` skip the step before the reference
    check runs.
    """
    observation: Observation = planner_observation()
    criterion = _criterion(kind)
    invented = criterion.holds(_args_for(kind, INVENTED), observation)
    observed = criterion.holds(_args_for(kind, item_ref("beans")), observation)

    if invented == observed:
        return  # the criterion does not read the reference; nothing to classify
    if kind in _ITEM_READING_CRITERIA:
        return  # gated at the critic; the executor never sees it
    assert invented is not True, (
        f"{kind.value} is satisfied by a reference nobody observed while an observed "
        f"one does not satisfy it, and the critic does not gate it. Either add it to "
        f"_ITEM_READING_CRITERIA, or make holds() answer None for a reference the "
        f"observation does not carry."
    )


def test_the_gated_kind_really_is_satisfied_by_an_absence() -> None:
    """The control for the skip above: without it the parametrized test could
    pass by gating kinds that never needed gating, and would say nothing."""
    holds = _criterion(SuccessKind.ITEM_CONSUMED).holds(
        ConsumeArgs(item_ref=INVENTED), planner_observation()
    )

    assert holds is True, (
        "item_consumed is no longer satisfied by an unobserved reference, so the "
        "critic's gate is now belt-and-braces rather than load-bearing — worth "
        "knowing before it is removed as redundant"
    )


def test_an_observed_reference_does_not_read_as_already_consumed() -> None:
    """The other direction, so the gate cannot be 'satisfied' by breaking the check."""
    holds = _criterion(SuccessKind.ITEM_CONSUMED).holds(
        ConsumeArgs(item_ref=item_ref("beans")), planner_observation()
    )

    assert holds is False
