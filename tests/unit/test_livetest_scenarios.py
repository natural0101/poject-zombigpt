"""The scenario catalogue.

``docs/LIVE_TEST_PLAYBOOK.md`` is generated from this data and an operator on a
Windows machine follows it with the game open, so the assertions here are about
completeness: a scenario missing its world preparation, or declaring a check
that carries no threshold, is a gap somebody discovers at eleven at night with
a zombie in the room.
"""

from __future__ import annotations

import json

import pytest

from pz_agent_cli.livetest.scenarios import (
    SCENARIO_IDS,
    SCENARIOS,
    Check,
    LiveScenario,
    UnknownScenarioError,
    by_id,
    catalogue,
    resolve,
)

#: The ids the harness contract fixes. Spelled out rather than derived, so a
#: rename shows up here as a failing test instead of a silently renamed
#: directory in the evidence tree.
EXPECTED_IDS = (
    "S01_INSTALL",
    "S02_HEARTBEAT",
    "S03_ARM_DISARM",
    "S04_MOVE",
    "S05_BLOCKED_PATH",
    "S06_MANUAL_TAKEOVER",
    "S07_NESTED_INVENTORY",
    "S08_UNSAFE_FOOD",
    "S09_DRINK",
    "S10_READ",
    "S11_CONTAINER",
    "S12_EQUIPMENT",
    "S13_MEDICAL",
    "S14_SLEEP_REST",
    "S15_ZOMBIE_INTERRUPT",
    "S16_STALE_SIDECAR",
    "S17_RESTART_RECOVERY",
    "S18_PANIC",
    "S19_AUTONOMOUS_30_MIN",
    "S20_AUTONOMOUS_2_HOURS",
)

#: Checks that compare against a declared value, and therefore need one.
_NEEDS_EXPECTED = {Check.EQUALS, Check.AT_LEAST, Check.AT_MOST}


class TestCatalogue:
    def test_exactly_the_twenty_declared_ids_in_run_order(self) -> None:
        assert SCENARIO_IDS == EXPECTED_IDS
        assert len(SCENARIOS) == 20

    def test_every_scenario_is_completely_specified(self) -> None:
        for scenario in SCENARIOS:
            assert scenario.title.strip()
            assert scenario.goal.strip()
            assert scenario.world_preparation, scenario.id
            assert scenario.required_state, scenario.id
            assert scenario.command.strip(), scenario.id
            assert scenario.operator_steps, scenario.id
            assert scenario.expected_result.strip(), scenario.id
            assert scenario.logs, scenario.id
            assert scenario.suspect_module.strip(), scenario.id
            assert scenario.time_budget_s > 0, scenario.id

    def test_no_scenario_can_pass_vacuously(self) -> None:
        """A scenario with nothing to observe would pass by declaring nothing."""
        for scenario in SCENARIOS:
            assert scenario.postconditions, scenario.id

    def test_postcondition_keys_are_unique_within_a_scenario(self) -> None:
        # The key indexes into result.json; two of them would make one
        # unreadable and the other unreachable.
        for scenario in SCENARIOS:
            keys = [condition.key for condition in scenario.postconditions]
            assert len(keys) == len(set(keys)), scenario.id

    def test_a_comparison_check_carries_the_value_it_compares_against(self) -> None:
        for scenario in SCENARIOS:
            for condition in scenario.postconditions:
                if condition.check in _NEEDS_EXPECTED:
                    assert condition.expected is not None, f"{scenario.id}.{condition.key}"

    def test_a_check_that_needs_no_threshold_declares_none(self) -> None:
        for scenario in SCENARIOS:
            for condition in scenario.postconditions:
                if condition.check not in _NEEDS_EXPECTED:
                    assert condition.expected is None, f"{scenario.id}.{condition.key}"

    def test_every_postcondition_names_a_field(self) -> None:
        for scenario in SCENARIOS:
            for condition in scenario.postconditions:
                assert condition.field.strip()
                assert condition.statement.strip()

    def test_only_the_endurance_and_repeated_scenarios_measure_latency(self) -> None:
        measured = {scenario.id for scenario in SCENARIOS if scenario.measures_latency}

        assert measured == {"S04_MOVE", "S19_AUTONOMOUS_30_MIN", "S20_AUTONOMOUS_2_HOURS"}

    def test_the_two_endurance_scenarios_budget_their_real_duration(self) -> None:
        assert by_id("S19_AUTONOMOUS_30_MIN").time_budget_s >= 1800
        assert by_id("S20_AUTONOMOUS_2_HOURS").time_budget_s >= 7200

    def test_the_catalogue_serialises(self) -> None:
        """The playbook generator reads JSON, not Python objects."""
        document = catalogue()
        round_tripped = json.loads(json.dumps(document))

        assert round_tripped["scenario_count"] == 20
        assert len(round_tripped["scenarios"]) == 20
        assert round_tripped["checks"] == [check.value for check in Check]
        first = round_tripped["scenarios"][0]
        assert first["id"] == "S01_INSTALL"
        assert first["postconditions"][0]["key"]


class TestLookup:
    def test_by_id_returns_the_scenario(self) -> None:
        assert by_id("S07_NESTED_INVENTORY").title.startswith("Transfer an item")

    def test_an_unknown_id_lists_the_ones_that_exist(self) -> None:
        with pytest.raises(UnknownScenarioError) as caught:
            by_id("S07")

        assert "S07_NESTED_INVENTORY" in str(caught.value)

    def test_resolve_none_selects_everything_in_run_order(self) -> None:
        assert tuple(s.id for s in resolve(None)) == EXPECTED_IDS

    def test_resolve_returns_run_order_regardless_of_argument_order(self) -> None:
        selected = resolve(["S09_DRINK", "S04_MOVE"])

        assert [s.id for s in selected] == ["S04_MOVE", "S09_DRINK"]

    def test_resolve_rejects_an_unknown_id_rather_than_ignoring_it(self) -> None:
        """Silently dropping it would run a smaller set than the operator asked for."""
        with pytest.raises(UnknownScenarioError):
            resolve(["S04_MOVE", "S99_NOT_A_SCENARIO"])

    def test_postcondition_lookup_misses_return_none(self) -> None:
        scenario: LiveScenario = by_id("S04_MOVE")

        assert scenario.postcondition("position_changed") is not None
        assert scenario.postcondition("no_such_key") is None


def test_snapshot_checks_are_the_ones_that_read_before_and_after() -> None:
    reads = {check for check in Check if check.reads_snapshots}

    assert reads == {Check.INCREASED, Check.DECREASED, Check.CHANGED, Check.UNCHANGED}
