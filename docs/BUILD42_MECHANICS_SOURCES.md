<!--
  GENERATED FILE - DO NOT EDIT BY HAND.
  Generator: pz_agent_core.knowledge.docgen.render_sources
  Corpus revision: 0a01c0921b02a5a0 (sha256/16 of the canonical corpus)
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
| `building_a_placement_that_would_seal_the_character_in_is_refused` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/building.py::enclosure_after` | `verified_script` | `tests/unit/test_policy_building.py`, `tests/unit/test_adapters_building.py` |
| `building_a_raised_wall_changes_where_anything_can_walk` | 42 | `wiki` | `https://pzwiki.net/wiki/Construction — walls, doorframes and how built tiles block movement` | `unverified` | — |
| `building_an_occupied_square_is_never_cleared_to_make_room` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/building.py::assess_placement` | `verified_script` | `tests/unit/test_policy_building.py`, `tests/lua/test_adapter_building.lua` |
| `building_blueprints_must_be_known_before_they_can_be_raised` | 42 | `wiki` | `https://pzwiki.net/wiki/Carpentry — construction recipes, carpentry level and what unlocks them` | `unverified` | — |
| `building_build42_rewrote_construction` | 42 | `official` | `https://projectzomboid.com/blog/news/2024/12/build-42-unstable-released/ — Build 42's new crafting and construction system` | `unverified` | — |
| `building_capability_is_withheld_until_a_live_run` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/capabilities/probes.py::PROBES` | `verified_script` | `tests/unit/test_mcp_catalog_actions.py` |
| `building_material_costs_and_placement_rules_are_unmeasured` | 42 | `wiki` | `https://pzwiki.net/wiki/Construction — material costs, tools required and where a tile may be placed` | `unverified` | — |
| `building_materials_are_counted_by_the_crafting_rule` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/building.py::assess_placement` | `verified_script` | `tests/unit/test_policy_building.py` |
| `building_nothing_in_this_build_takes_a_structure_down` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/protocol/enums.py::ActionName` | `verified_script` | `tests/unit/test_mcp_catalog.py`, `tests/lua/test_adapter_registry.lua` |
| `building_one_command_raises_one_structure_once` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/building.py::MAX_BLUEPRINT_NAME_LEN` | `verified_script` | `tests/unit/test_adapters_building.py`, `tests/lua/test_adapter_building.lua` |
| `building_placement_is_p4_and_never_the_agents_own_idea` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/building.py::BuildingBuildAdapter` | `verified_script` | `tests/unit/test_adapters_building.py`, `tests/unit/test_mcp_catalog_actions.py` |
| `building_reading_a_square_places_nothing` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/building.py::BuildingInspectAdapter` | `verified_script` | `tests/unit/test_adapters_building.py` |
| `building_success_is_the_structure_observed_on_the_square` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/building.py::BuildingBuildAdapter` | `verified_script` | `tests/unit/test_adapters_building.py`, `tests/lua/test_adapter_building.lua` |
| `building_the_enclosure_claim_is_bounded_by_what_was_observed` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/building.py::EnclosureCheck` | `verified_script` | `tests/unit/test_policy_building.py` |
| `building_the_goal_names_both_the_blueprint_and_the_square` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/goals/model.py::GOAL_SPECS` | `verified_script` | `tests/unit/test_mcp_catalog_goals.py`, `tests/unit/test_voice_intents.py` |
| `combat_assisted_unverified` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/combat/policy.py::assess_engagement` | `unverified` | — |
| `crafting_build42_rewrote_the_system` | 42 | `official` | `https://projectzomboid.com/blog/news/2024/12/build-42-unstable-released/ — Build 42 crafting and the new recipe/script model` | `unverified` | — |
| `crafting_capability_is_withheld_until_a_live_run` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/capabilities/probes.py::PROBES` | `verified_script` | `tests/unit/test_mcp_catalog_actions.py` |
| `crafting_goal_bounds_what_one_submission_spends` | 42 | `code` | `packages/pz_agent_cli/src/pz_agent_cli/craft_mission.py::CraftItemMission` | `verified_script` | `tests/unit/test_craft_mission.py`, `tests/unit/test_goal_channel.py` |
| `crafting_one_command_runs_one_recipe_once` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/crafting.py::MAX_CRAFT_COUNT` | `verified_script` | `tests/unit/test_adapters_crafting.py`, `tests/lua/test_adapter_crafting.lua` |
| `crafting_reading_a_recipe_spends_nothing` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/crafting.py::CraftingInspectAdapter` | `verified_script` | `tests/unit/test_adapters_crafting.py` |
| `crafting_recipe_is_chosen_deterministically` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/crafting.py::recipes_for_product` | `verified_script` | `tests/unit/test_policy_crafting.py` |
| `crafting_recipes_are_learned_before_they_can_be_run` | 42 | `wiki` | `https://pzwiki.net/wiki/Crafting — recipe knowledge, skill books and magazines` | `unverified` | — |
| `crafting_reserved_materials_get_their_own_refusal` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/crafting.py::assess_recipe` | `verified_script` | `tests/unit/test_policy_crafting.py` |
| `crafting_risk_escalates_from_the_arguments` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/crafting.py::CraftingCraftAdapter` | `verified_script` | `tests/unit/test_adapters_crafting.py` |
| `crafting_some_recipes_need_a_surface_or_a_station` | 42 | `wiki` | `https://pzwiki.net/wiki/Crafting — recipes requiring a nearby item, workstation or heat source` | `unverified` | — |
| `crafting_success_is_the_product_observed` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/actions/adapters/crafting.py::CraftingCraftAdapter` | `verified_script` | `tests/unit/test_adapters_crafting.py`, `tests/lua/test_adapter_crafting.lua` |
| `crafting_unreadable_is_never_the_convenient_reading` | 42 | `code` | `packages/pz_agent_core/src/pz_agent_core/policy/crafting.py::assess_recipe` | `verified_script` | `tests/unit/test_policy_crafting.py` |
| `crafting_yield_and_skill_effects_are_unmeasured` | 42 | `wiki` | `https://pzwiki.net/wiki/Crafting — outputs, success chance and skill effects` | `unverified` | — |
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
