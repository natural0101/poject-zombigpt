<!--
  GENERATED FILE - DO NOT EDIT BY HAND.
  Generator: pz_agent_core.knowledge.docgen.render_sources
  Corpus revision: 49494ec57fe7ded0 (sha256/16 of the canonical corpus)
  Edit knowledge/gameplay/*.yaml and regenerate; the drift test
  byte-compares this file against a fresh render.
-->

# Build 42 mechanics: the provenance ledger

Every rule in the corpus with where its claim comes from and how far it has
been verified, sorted by rule id. Source tiers, most authoritative first:
`code` (this repository's shipped policy or adapter), `live_probe` (a live
session or capability probe), `official` (The Indie Stone's published
material), `wiki` (PZwiki — secondary by policy, never sufficient for a
verified status on its own). A `verified_script` row's proofs are repo test
paths; a `verified_live` row's proofs include a live evidence pointer; an
`unverified` row proves nothing and says so.

| Rule | Build | Source | Source detail | Status | Proven by |
| --- | --- | --- | --- | --- | --- |
| `avoid_retreat_criteria` | 42 | `code` | `packages/pz_agent_cli/src/pz_agent_cli/avoid_mission.py::SAFE_DISTANCE` | `verified_script` | `tests/unit/test_avoid_mission.py` |
| `combat_not_implemented` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/protocol/enums.py::ActionName` | `unverified` | — |
| `doors_already_in_state` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/doors.py::DoorOpenAdapter` | `verified_script` | `tests/unit/test_adapters_doors.py` |
| `doors_locked_vs_barricaded` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/protocol/reason_codes.py::ReasonCode` | `verified_script` | `tests/unit/test_navigation_executor.py`, `tests/unit/test_adapters_doors.py` |
| `doors_postcondition_observed` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/doors.py::DoorOpenAdapter.verify` | `verified_script` | `tests/unit/test_adapters_doors.py` |
| `doors_tristate_honesty` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/doors.py::DoorOpenAdapter` | `verified_script` | `tests/unit/test_adapters_doors.py` |
| `drink_last_container_reserve` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/drink.py::_last_container_rejection` | `verified_script` | `tests/unit/test_policy_drink.py` |
| `drink_tainted_alcohol_refused` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/drink.py::_filter_tainted` | `verified_script` | `tests/unit/test_policy_drink.py` |
| `drink_tainted_sickens_lore` | 41 | `wiki` | `https://pzwiki.net/wiki/Water — tainted water section, read for Build 41 lore` | `unverified` | — |
| `food_burnt_poison_tainted_refused` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/food.py::_filter_poisonous` | `verified_script` | `tests/unit/test_policy_food.py` |
| `food_portion_logic` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/food.py::_choose_fraction` | `verified_script` | `tests/unit/test_policy_food.py` |
| `food_raw_frozen_uncooked_refused` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/food.py::_filter_raw` | `verified_script` | `tests/unit/test_policy_food.py`, `tests/unit/test_policy_config.py` |
| `food_reserves_outrank_hunger` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/selection.py::is_user_reserved` | `verified_script` | `tests/unit/test_policy_food.py`, `tests/unit/test_policy_selection.py` |
| `food_rotten_refused` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/food.py::_filter_rotten` | `verified_script` | `tests/unit/test_policy_food.py`, `tests/unit/test_policy_config.py` |
| `food_rotten_sickens_lore` | 41 | `wiki` | `https://pzwiki.net/wiki/Food — spoilage section, read for Build 41 lore` | `unverified` | — |
| `food_selection_is_code` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/food.py::select_food` | `verified_script` | `tests/unit/test_policy_food.py` |
| `loot_batch_cap_eight` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/loot/policy.py::MAX_ITEMS_PER_CONTAINER` | `verified_script` | `tests/unit/test_loot_policy.py`, `tests/unit/test_loot_mission.py`, `tests/unit/test_adapters_inventory.py` |
| `loot_capacity_stop_honesty` | 42 | `code` | `packages/pz_agent_cli/src/pz_agent_cli/loot_mission.py::LootMission` | `verified_script` | `tests/unit/test_loot_mission.py` |
| `loot_completion_criterion` | 42 | `code` | `packages/pz_agent_cli/src/pz_agent_cli/loot_mission.py::MAX_CANDIDATES_PER_MISSION` | `verified_script` | `tests/unit/test_loot_mission.py` |
| `loot_container_memory_revision` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/memory/model.py::content_revision_of` | `verified_script` | `tests/unit/test_memory_model.py`, `tests/unit/test_memory_store.py`, `tests/unit/test_loot_mission.py` |
| `loot_corpse_observation_gap` | 42 | `code` | `packages/pz_agent_cli/src/pz_agent_cli/loot_mission.py::LootMission` | `verified_script` | `tests/unit/test_loot_mission.py` |
| `loot_default_wanted_categories` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/loot/policy.py::DEFAULT_WANTED` | `verified_script` | `tests/unit/test_loot_policy.py` |
| `loot_open_is_timed_action` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/protocol/enums.py::READ_ONLY_ACTIONS` | `verified_script` | `tests/contract/test_mcp_action_coverage.py` |
| `medical_bleeding_verified_observed` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/medical.py::BandageAdapter` | `verified_script` | `tests/unit/test_adapters_medical.py`, `tests/unit/test_care_mission.py` |
| `medical_dirty_dressing_refused` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/medical.py::select_treatment` | `verified_script` | `tests/unit/test_policy_medical.py` |
| `medical_dirty_infects_lore` | 41 | `wiki` | `https://pzwiki.net/wiki/First_aid — dressing cleanliness, read for Build 41 lore` | `unverified` | — |
| `medical_sterile_ranking` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/medical.py::STERILE_DRESSING_TYPES` | `verified_script` | `tests/unit/test_policy_medical.py` |
| `medical_triage_order` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/medical.py::select_treatment` | `verified_script` | `tests/unit/test_policy_medical.py` |
| `memory_home_point` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/memory/store.py::SaveMemory.set_home` | `verified_script` | `tests/unit/test_memory_store.py`, `tests/unit/test_return_home.py` |
| `memory_no_session_refs` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/memory/model.py::container_tail` | `verified_script` | `tests/unit/test_memory_model.py`, `tests/unit/test_memory_store.py` |
| `memory_reservations_honoured` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/memory/model.py::Reservation` | `verified_script` | `tests/unit/test_memory_model.py`, `tests/unit/test_loot_policy.py`, `tests/unit/test_policy_selection.py` |
| `memory_safe_zones` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/memory/model.py::SafeZone` | `verified_script` | `tests/unit/test_memory_model.py`, `tests/unit/test_avoid_mission.py` |
| `memory_strings_quarantined` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/memory/model.py::MAX_LABEL_LEN` | `verified_script` | `tests/unit/test_memory_model.py`, `tests/unit/test_diagnostics_redaction.py` |
| `movement_absolute_refusals` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/movement.py::MoveToAdapter` | `verified_script` | `tests/unit/test_adapters_movement.py` |
| `movement_arrival_is_observed` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/navigation/executor.py::Journey.next_step` | `verified_script` | `tests/unit/test_navigation_executor.py`, `tests/unit/test_adapters_movement.py` |
| `movement_doors_opened_in_walk` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/navigation/executor.py::Journey` | `verified_script` | `tests/unit/test_navigation_executor.py`, `tests/unit/test_adapters_movement.py` |
| `movement_journey_budgets` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/navigation/executor.py::JourneyLimits` | `verified_script` | `tests/unit/test_navigation_executor.py` |
| `movement_single_leg_cap` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/movement.py::MAX_MOVE_DISTANCE_SQUARES` | `verified_script` | `tests/unit/test_adapters_movement.py`, `tests/unit/test_navigation_executor.py` |
| `movement_stuck_is_typed` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/navigation/executor.py::MAX_CONSECUTIVE_FAILURES` | `verified_script` | `tests/unit/test_navigation_executor.py` |
| `movement_threat_tolls` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/navigation/executor.py::THREAT_STEP_COST` | `verified_script` | `tests/unit/test_navigation_executor.py` |
| `reflex_bands` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/safety/reflex.py::ReflexConfig` | `verified_script` | `tests/unit/test_safety_reflex.py` |
| `reflex_stall_detection` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/safety/reflex.py::ReflexConfig` | `verified_script` | `tests/unit/test_safety_reflex.py` |
| `rest_verified_against_endurance` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/survival.py::RestAdapter` | `verified_script` | `tests/unit/test_adapters_survival.py`, `tests/unit/test_care_mission.py` |
| `sleep_never_autonomous` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/survival.py::SleepAdapter` | `verified_script` | `tests/unit/test_adapters_survival.py`, `tests/unit/test_policy_permissions.py` |
| `sleep_refused_any_danger` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/survival.py::SleepAdapter.validate` | `verified_script` | `tests/unit/test_adapters_survival.py`, `tests/unit/test_care_mission.py` |
| `sleep_verified_by_clock_and_fatigue` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/survival.py::SleepAdapter.verify` | `verified_script` | `tests/unit/test_adapters_survival.py` |
| `threat_chasing_outranks_close` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/safety/threat.py::assess_threat` | `verified_script` | `tests/unit/test_safety_threat.py` |
| `threat_distance_bands` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/safety/threat.py::ThreatConfig` | `verified_script` | `tests/unit/test_safety_threat.py` |
| `threat_pack_and_floor_rules` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/safety/threat.py::assess_threat` | `verified_script` | `tests/unit/test_safety_threat.py` |
| `windows_never_auto` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/movement.py::SEMANTIC_CLOSED_WINDOW` | `verified_script` | `tests/unit/test_adapters_movement.py` |
