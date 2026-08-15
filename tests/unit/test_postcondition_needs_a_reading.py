"""No postcondition may pass on a value nobody read.

``pz_agent_cli.livetest.scenarios`` states the rule its whole verdict rests on:

    A postcondition can only pass on a value that was *observed*. There is no
    check that succeeds on an absent field, and none that a driver can satisfy
    by reporting that it tried.

and ``runner.evaluate`` repeats it: *"There is no branch that passes on a
missing value."* Ten checks make that claim. Nine held. ``UNCHANGED`` did not.

The snapshot path decided presence by key alone — ``found_before and
found_after`` — so a field present in both snapshots with the value ``null``
(or ``""``) compared equal to itself and passed. The observation path had
always applied a second rule, ``_is_non_empty``, and the two had drifted apart.

That mattered in exactly one place, and it is the worst one it could have been.
``UNCHANGED`` is used by a single postcondition in the whole catalogue:

    S05_BLOCKED_PATH · health_unchanged · player.health
    "the character took no damage"

A safety statement, in one of the twenty-two scenarios whose ``result.json``
becomes the evidence manifest that ``check_release.py --release`` reads before
``v1.0.0``. With the mod failing to read the character and publishing ``null``,
the runner would have recorded that the character took no damage — from a
reading nobody took. This repository already carries the same shape one layer
down, in its own defect ledger: *a zombie scan that could not run published an
empty list, and the danger floor read that as NONE*.

The fix is the module's own rule, applied on both paths rather than one. The
tests below drive the real ``evaluate`` over every member of ``Check`` against
every way a value can be missing, and — the half that stops the fix from being
"refuse everything" — over the readings that must still decide normally, ``0``
and ``False`` among them, because health at zero is a fact about a character and
not a failure to look.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from pz_agent_cli.livetest.runner import ObservedRun, evaluate
from pz_agent_cli.livetest.scenarios import SCENARIOS, Check, Postcondition

#: An ``expected`` that makes the comparing checks well formed. The value never
#: matters below — every case is about a field that was not read.
_EXPECTED: Final[dict[Check, Any]] = {
    Check.EQUALS: "anything",
    Check.AT_LEAST: 1,
    Check.AT_MOST: 1,
}

#: The ways a value fails to be a reading. ``absent`` is the key missing
#: entirely; the other two are the key present and carrying nothing, which is
#: what a producer that could not read publishes.
_NOT_A_READING: Final = ("absent", "empty string", "null")

#: Values that *are* readings and must keep deciding normally. Falsy on purpose:
#: truthiness would have been the easy fix and the wrong one.
_REAL_READINGS: Final = (1.0, 0.75, 0, 0.0, False, "closed")


def _condition(check: Check, field: str = "player.health") -> Postcondition:
    return Postcondition(
        key="k", statement="s", check=check, field=field, expected=_EXPECTED.get(check)
    )


def _document(value: Any, field: str) -> dict[str, Any]:
    head, _, tail = field.partition(".")
    return {head: {tail: value}} if tail else {head: value}


def _unread(shape: str, field: str = "player.health") -> ObservedRun:
    """An observed run in which *field* was not read, one of three ways."""
    if shape == "absent":
        return ObservedRun()
    document = _document("" if shape == "empty string" else None, field)
    return ObservedRun(observations=document, before=document, after=document)


def _both(value: Any, field: str = "player.health") -> ObservedRun:
    document = _document(value, field)
    return ObservedRun(observations=document, before=document, after=document)


# ---------------------------------------------------------------------------
# the rule, over every check and every way of not reading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", _NOT_A_READING)
@pytest.mark.parametrize("check", list(Check))
def test_no_check_passes_on_a_value_that_was_never_read(check: Check, shape: str) -> None:
    outcome = evaluate(_condition(check), _unread(shape))

    assert outcome.passed is False, (
        f"{check.value} passed on a field that was {shape}; the module's rule is that "
        f"no check succeeds on a value nobody observed"
    )
    assert outcome.present is False
    assert outcome.detail, f"{check.value} refused without saying why"


def test_the_check_that_broke_the_rule_is_named_in_its_failure() -> None:
    """``UNCHANGED`` on two nulls: equal to itself, and read from nothing."""
    outcome = evaluate(_condition(Check.UNCHANGED), _unread("null"))

    assert outcome.passed is False
    assert "present but empty" in outcome.detail
    assert "player.health" in outcome.detail


# ---------------------------------------------------------------------------
# ... without refusing the readings that are real
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", _REAL_READINGS)
def test_unchanged_still_passes_on_a_value_that_really_was_read(value: Any) -> None:
    """The other direction. ``0`` and ``False`` are readings, not absences.

    Health at zero is a fact about a character. A fix built on truthiness would
    have satisfied every test above and quietly made a dead character
    unobservable.
    """
    assert evaluate(_condition(Check.UNCHANGED), _both(value)).passed is True


def test_unchanged_still_fails_when_the_value_actually_moved() -> None:
    hurt = ObservedRun(
        before={"player": {"health": 1.0}},
        after={"player": {"health": 0.6}},
    )

    outcome = evaluate(_condition(Check.UNCHANGED), hurt)

    assert outcome.passed is False
    assert "differs" in outcome.detail


@pytest.mark.parametrize(
    ("check", "before", "after", "expected"),
    [
        (Check.CHANGED, 1.0, 0.6, True),
        (Check.CHANGED, 1.0, 1.0, False),
        (Check.INCREASED, 1, 2, True),
        (Check.INCREASED, 2, 1, False),
        (Check.DECREASED, 2, 1, True),
        (Check.DECREASED, 1, 2, False),
    ],
)
def test_the_other_snapshot_checks_still_decide_normally(
    check: Check, before: Any, after: Any, expected: bool
) -> None:
    run = ObservedRun(before={"player": {"health": before}}, after={"player": {"health": after}})

    assert evaluate(_condition(check), run).passed is expected


@pytest.mark.parametrize(
    ("check", "value"),
    [
        (Check.OBSERVED, 0),
        (Check.OBSERVED, False),
        (Check.IS_TRUE, True),
        (Check.IS_FALSE, False),
        (Check.EQUALS, "anything"),
        (Check.AT_LEAST, 5),
        (Check.AT_MOST, 0),
    ],
)
def test_the_observation_checks_still_decide_normally(check: Check, value: Any) -> None:
    condition = Postcondition(
        key="k", statement="s", check=check, field="a", expected=_EXPECTED.get(check)
    )

    assert evaluate(condition, ObservedRun(observations={"a": value})).passed is True


# ---------------------------------------------------------------------------
# and the postcondition this was actually about
# ---------------------------------------------------------------------------


def test_the_catalogue_still_has_the_postcondition_this_protects() -> None:
    """If ``UNCHANGED`` ever leaves the catalogue, this file should say so loudly.

    Not "some scenario uses it" — the specific one, because the reason the bug
    mattered is what that postcondition claims. A rewrite that drops it has not
    made the rule less important; it has moved where it applies.
    """
    using = [
        (scenario.id, condition.key, condition.field)
        for scenario in SCENARIOS
        for condition in scenario.postconditions
        if condition.check is Check.UNCHANGED
    ]

    assert ("S05_BLOCKED_PATH", "health_unchanged", "player.health") in using, (
        f"the UNCHANGED postconditions are now {using}; re-read this module's docstring "
        f"before adjusting it"
    )


def test_that_postcondition_cannot_pass_on_an_unread_character() -> None:
    """End to end on the real scenario's own postcondition, not a stand-in."""
    scenario = next(s for s in SCENARIOS if s.id == "S05_BLOCKED_PATH")
    condition = scenario.postcondition("health_unchanged")
    assert condition is not None

    unread = evaluate(condition, _unread("null", condition.field))
    genuine = evaluate(condition, _both(0.82, condition.field))

    assert unread.passed is False, "S05 would record 'took no damage' from an unread character"
    assert genuine.passed is True
