"""The shared selection primitives, especially their refusals.

These are small values, but three of them carry guarantees the policies rest
on: a rejection always explains itself, a ranking is a total order, and a
capability that was never probed never reports as usable.
"""

from __future__ import annotations

import pytest

from pz_agent_core.capabilities.model import (
    Capability,
    CapabilityReport,
    Evidence,
    EvidenceKind,
)
from pz_agent_core.policy import (
    CAPABILITY_EAT_PERCENTAGE,
    CapabilityLookup,
    CapabilitySnapshot,
    RejectionReason,
    select_food,
)
from pz_agent_core.policy.selection import (
    NO_CAPABILITIES,
    Rejection,
    ScoreBreakdown,
    ScoredCandidate,
    ScoreFactor,
    rank_candidates,
    read_bool,
    read_float,
    read_int,
    read_str,
    read_str_tuple,
    truncation_note,
)
from pz_agent_core.protocol import CapabilityState
from tests.fixtures.policy_items import food_item, hungry_player, inventory


def factor(name: str, raw: float, weight: float = 1.0) -> ScoreFactor:
    return ScoreFactor(name=name, raw=raw, weight=weight)


def candidate(ref: str, total: float) -> ScoredCandidate:
    return ScoredCandidate(
        item_ref=ref, display_name=ref, breakdown=ScoreBreakdown((factor("t", total),))
    )


def test_a_rejection_must_explain_itself() -> None:
    with pytest.raises(ValueError, match="explain itself"):
        Rejection(
            item_ref="item:x",
            display_name="x",
            reason=RejectionReason.ROTTEN,
            detail="   ",
        )


def test_a_factor_weight_cannot_carry_the_sign() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ScoreFactor(name="freshness", raw=1.0, weight=-1.0)


def test_a_breakdown_cannot_hold_the_same_factor_twice() -> None:
    with pytest.raises(ValueError, match="duplicate score factor"):
        ScoreBreakdown((factor("freshness", 1.0), factor("freshness", 0.5)))


def test_the_total_is_the_weighted_sum_and_the_explanation_is_ordered() -> None:
    breakdown = ScoreBreakdown(
        (
            factor("small", 0.1),
            factor("large", -2.0),
            factor("medium", 1.0),
            factor("zero", 0.0),
        )
    )

    assert breakdown.total == pytest.approx(-0.9)
    assert breakdown.explain(limit=2) == "large -2.00, medium +1.00"
    assert breakdown.factor("missing") is None


def test_explain_needs_a_positive_limit() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ScoreBreakdown((factor("a", 1.0),)).explain(limit=0)


def test_ranking_is_a_total_order_that_ends_on_the_reference() -> None:
    tied_low = candidate("item:b", 1.0)
    tied_high = candidate("item:a", 1.0)
    winner = candidate("item:z", 2.0)

    forward = rank_candidates([tied_low, tied_high, winner])
    backward = rank_candidates([winner, tied_high, tied_low])

    assert [c.item_ref for c in forward] == ["item:z", "item:a", "item:b"]
    assert forward == backward


def test_scores_differing_below_the_precision_floor_count_as_tied() -> None:
    noisy = candidate("item:b", 1.0 + 1e-9)
    clean = candidate("item:a", 1.0)

    assert [c.item_ref for c in rank_candidates([noisy, clean])] == ["item:a", "item:b"]


def test_an_unprobed_capability_is_never_usable() -> None:
    snapshot = CapabilitySnapshot()

    assert snapshot.state(CAPABILITY_EAT_PERCENTAGE) is CapabilityState.UNSUPPORTED
    assert not snapshot.usable(CAPABILITY_EAT_PERCENTAGE)


@pytest.mark.parametrize(
    ("state", "usable"),
    [
        (CapabilityState.VERIFIED, True),
        (CapabilityState.AVAILABLE_UNVERIFIED, True),
        (CapabilityState.EXPERIMENTAL, False),
        (CapabilityState.UNSUPPORTED, False),
        (CapabilityState.DISABLED_BY_POLICY, False),
    ],
)
def test_only_verified_and_available_probes_count_as_usable(
    state: CapabilityState, usable: bool
) -> None:
    snapshot = CapabilitySnapshot.from_mapping({CAPABILITY_EAT_PERCENTAGE: state})

    assert snapshot.usable(CAPABILITY_EAT_PERCENTAGE) is usable


def test_a_snapshot_does_not_track_later_edits_to_the_source_mapping() -> None:
    source = {CAPABILITY_EAT_PERCENTAGE: CapabilityState.VERIFIED}
    snapshot = CapabilitySnapshot.from_mapping(source)

    source[CAPABILITY_EAT_PERCENTAGE] = CapabilityState.UNSUPPORTED

    assert snapshot.usable(CAPABILITY_EAT_PERCENTAGE)


def test_a_snapshot_cannot_be_edited_through_its_probes_mapping() -> None:
    snapshot = CapabilitySnapshot({CAPABILITY_EAT_PERCENTAGE: CapabilityState.UNSUPPORTED})

    with pytest.raises(TypeError):
        snapshot.probes[CAPABILITY_EAT_PERCENTAGE] = CapabilityState.VERIFIED  # type: ignore[index]

    assert not snapshot.usable(CAPABILITY_EAT_PERCENTAGE)


def test_the_shared_empty_default_cannot_be_granted_a_capability() -> None:
    # NO_CAPABILITIES is one process-wide object every policy call falls back
    # on; a writable one would let any caller grant a probe to all later calls.
    with pytest.raises(TypeError):
        NO_CAPABILITIES.probes[CAPABILITY_EAT_PERCENTAGE] = (  # type: ignore[index]
            CapabilityState.VERIFIED
        )

    assert NO_CAPABILITIES.state(CAPABILITY_EAT_PERCENTAGE) is CapabilityState.UNSUPPORTED


def test_the_real_capability_report_satisfies_the_lookup_protocol() -> None:
    """The capability subsystem's report must drive a policy with no adapter."""
    report = CapabilityReport(
        build="42.0.0",
        capabilities=(
            Capability(
                name=CAPABILITY_EAT_PERCENTAGE,
                state=CapabilityState.AVAILABLE_UNVERIFIED,
                build="42.0.0",
                evidence=(
                    Evidence(
                        kind=EvidenceKind.STATIC_SCAN,
                        symbol="ISEatFoodAction",
                        observed_at="2026-01-01T00:00:00+00:00",
                        file="media/lua/client/TimedActions/ISEatFoodAction.lua",
                        file_sha256="0" * 64,
                    ),
                ),
            ),
        ),
    )
    lookup: CapabilityLookup = report

    assert isinstance(report, CapabilityLookup)
    assert lookup.state(CAPABILITY_EAT_PERCENTAGE) is CapabilityState.AVAILABLE_UNVERIFIED
    assert lookup.usable(CAPABILITY_EAT_PERCENTAGE)

    selection = select_food(
        inventory(food_item("big", hunger_change=-1.0)),
        hungry_player(0.5),
        capabilities=report,
    )

    assert selection.choice is not None
    assert selection.choice.portioned


def test_a_truncation_note_is_silent_only_when_nothing_was_dropped() -> None:
    assert truncation_note(3, 3) == ""
    assert "1 further rejected candidate " in truncation_note(3, 4)
    assert "7 further rejected candidates " in truncation_note(3, 10)
    with pytest.raises(ValueError, match="out of a total of"):
        truncation_note(5, 4)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"flag": True}, True),
        ({"flag": 1}, True),
        ({"flag": 0}, False),
        ({"flag": "yes"}, False),
        ({"flag": None}, False),
        ({}, False),
        (None, False),
    ],
)
def test_boolean_reads_are_total(payload: dict[str, object] | None, expected: bool) -> None:
    assert read_bool(payload, "flag") is expected


def test_numeric_reads_fall_back_instead_of_raising() -> None:
    payload: dict[str, object] = {"n": "not a number", "b": True, "ok": 3}

    assert read_float(payload, "n", 1.5) == 1.5
    assert read_float(payload, "b", 1.5) == 1.5
    assert read_float(payload, "ok") == 3.0
    assert read_int(payload, "missing", 7) == 7
    assert read_int(payload, "ok") == 3


def test_string_reads_fall_back_instead_of_raising() -> None:
    payload: dict[str, object] = {"s": 5, "tags": ["a", 2, "b"], "bad": "x"}

    assert read_str(payload, "s", "default") == "default"
    assert read_str_tuple(payload, "tags") == ("a", "b")
    assert read_str_tuple(payload, "bad") == ()
    assert read_str_tuple(None, "tags") == ()
