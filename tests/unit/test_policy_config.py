"""The policy configuration validates itself.

A config is user-editable, so a nonsensical one has to fail loudly at
construction rather than quietly changing what "critical hunger" means three
call frames later.
"""

from __future__ import annotations

from typing import Any

import pytest

from pz_agent_core.policy import DEFAULT_POLICY_CONFIG, PolicyConfig


def test_the_documented_defaults_match_the_decision_table() -> None:
    config = PolicyConfig()

    assert config.critical_hunger == 0.70
    assert config.hunger_trigger == 0.35
    assert config.critical_thirst == 0.70
    assert config.thirst_trigger == 0.35
    assert config.boredom_moodle_trigger == 3
    assert not config.allow_raw
    assert not config.allow_rotten
    assert not config.allow_tainted_water
    assert not config.allow_alcohol_for_thirst


def test_the_shared_default_is_the_plain_default() -> None:
    assert PolicyConfig() == DEFAULT_POLICY_CONFIG


@pytest.mark.parametrize(
    "overrides",
    [
        {"critical_hunger": 0.2},  # below the trigger
        {"hunger_trigger": 0.05},  # below the satisfied level
        {"satisfied_hunger": -0.1},
        {"critical_thirst": 1.5},
        {"thirst_trigger": 0.9},  # above the critical level
    ],
)
def test_a_broken_need_ladder_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="thresholds must satisfy"):
        PolicyConfig(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_eat_fraction": 0.0},
        {"eat_fraction_step": 1.5},
        {"max_last_container_fraction": -0.5},
        {"max_rot_progress": 0.0},
        {"max_burn_progress": 2.0},
    ],
)
def test_a_fraction_outside_zero_to_one_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"must be within \(0, 1\]"):
        PolicyConfig(**overrides)


def test_a_negative_weight_is_rejected() -> None:
    # Weights are non-negative by convention so a penalty is visible as a
    # negative raw value in the breakdown instead of a sign flip in the config.
    with pytest.raises(ValueError, match="must be non-negative"):
        PolicyConfig(food_weight_freshness=-1.0)


def test_a_zero_normalisation_reference_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        PolicyConfig(calorie_reference=0.0)


def test_the_rejection_report_must_be_able_to_hold_something() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        PolicyConfig(max_reported_rejections=0)


def test_deficits_and_criticality_follow_the_thresholds() -> None:
    config = PolicyConfig()

    assert config.hunger_deficit(0.5) == pytest.approx(0.35)
    assert config.hunger_deficit(0.1) == 0.0
    assert config.thirst_deficit(0.6) == pytest.approx(0.45)
    assert config.is_hunger_critical(0.70)
    assert not config.is_hunger_critical(0.69)
    assert config.is_thirst_critical(0.95)
