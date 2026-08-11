<!--
  GENERATED FILE - DO NOT EDIT BY HAND.
  Generator: pz_agent_core.knowledge.docgen.render_guide_ru
  Corpus revision: d4258d6e19fc5d33 (sha256/16 of the canonical corpus)
  Edit knowledge/gameplay/*.yaml and regenerate; the drift test
  byte-compares this file against a fresh render.
-->

# Что агент умеет в игре: обзор корпуса знаний

Этот обзор сгенерирован из `knowledge/gameplay/*.yaml` — того же корпуса,
который читает планировщик. Здесь нет ни одного утверждения, которого нет в
корпусе: рамочный текст фиксирован, всё содержимое по правилам — сгенерировано.
Формулировки правил (claim) оставлены на английском намеренно: это дословные
утверждения корпуса, и пересказ был бы вторым источником правды.

## Как читать маркеры

Каждое правило и каждое число несёт ровно один маркер доверия:

- **проверено кодом** — правило пересказывает то, что делает код этого
  репозитория, и закреплено его тестами (пути в `proven_by`).
- **проверено в игре** — постусловие наблюдалось в живой сессии; указатель на
  свидетельство лежит в `proven_by`.
- **не проверено (гипотеза)** — источник wiki или официальные материалы;
  планировщик может учитывать это как фон, но не как факт.

Число без маркера в этом документе не появляется. Если рядом с числом стоит
«не проверено (гипотеза)» — это фольклор Build 41/42, а не измеренный факт, и
код на него не опирается.

## Сводка

Правил всего: 63. Проверено кодом: 55, проверено в игре: 0, гипотез: 8.

## Контейнеры и лут (`containers_loot`)

Проверенные правила:

- `loot_batch_cap_eight` — **проверено кодом** (источник: код этого репозитория). One loot selection is one inventory.transfer_batch of at most 8 items — the transfer batch width — and a container holding more is re-selected after the batch lands, against its remaining contents, up to 4 batches per container.
  - `max_items_per_container = 8 items` — проверено кодом
  - `max_batch_items = 8 items` — проверено кодом
  - `max_batches_per_container = 4 batches` — проверено кодом
- `loot_capacity_stop_honesty` — **проверено кодом** (источник: код этого репозитория). Taken means observed taken: items_taken counts only selections whose batch the engine verified landing whole; a partial CONTAINER_FULL stop records what stopped it and counts nothing, because the adapter's own verify refused to bless the partial state.
- `loot_container_memory_revision` — **проверено кодом** (источник: код этого репозитория). A visited container is remembered by the place-half of its reference and a digest of its enumerated contents; when the current observation's digest matches the remembered one the mission records an unchanged skip instead of walking back — and an unremembered or changed shelf is always visited.
- `loot_default_wanted_categories` — **проверено кодом** (источник: код этого репозитория). The default loot policy takes what feeds, waters, heals, arms, fixes or teaches — food, water, medical, tools, weapons, materials, literature — and deliberately not clothing or unnameable items; take_all widens to every category but never overrides the user's reserves.
- `loot_open_is_timed_action` — **проверено кодом** (источник: код этого репозитория). container.open_nearby is deliberately not a read-only action: its name reads like a query, but opening a container is a timed action the character performs in the world, so an unarmed session may inspect but never open.
- `loot_corpse_observation_gap` — **проверено кодом** (источник: код этого репозитория). Corpses are observation-only for the loot mission: discovery parses only world container references, so a corpse in nearby.objects is an ordinary neighbour, never a loot candidate — looting corpses is an honest gap, not a silent skip of something claimed supported.
- `loot_completion_criterion` — **проверено кодом** (источник: код этого репозитория). A loot mission finishes on a provable criterion — every reachable container in scope was inspected or carries a recorded skip reason — and is bounded to 24 candidates per mission with a default radius scope of 10 squares.
  - `max_candidates_per_mission = 24 containers` — проверено кодом
  - `default_loot_radius = 10 squares` — проверено кодом

## Крафт (`crafting`)

Проверенные правила:

- `crafting_recipe_is_chosen_deterministically` — **проверено кодом** (источник: код этого репозитория). A craft goal names a product, never a recipe: which recipe spends which materials is deterministic policy, ranked known-first, then no-surface-first, then the shorter requirement list, then the name — so two runs over one observation always weigh the same recipe first.
  - `max_candidate_recipes = 8 recipes` — проверено кодом
  - `max_recipes_per_item = 16 entries` — проверено кодом
  - `max_materials_per_recipe = 8 lines` — проверено кодом
- `crafting_unreadable_is_never_the_convenient_reading` — **проверено кодом** (источник: код этого репозитория). Both crafting flags are tri-state and neither absence is read as the convenient answer: an unreported 'known' refuses the recipe as unknown, and an unreported 'needs_surface' is treated as needing one — which costs a permission tier rather than a craft queued where it cannot run.
- `crafting_reserved_materials_get_their_own_refusal` — **проверено кодом** (источник: код этого репозитория). A user-reserved item is never spent by a craft, and when reserves are the only reason a recipe is short the refusal says so with its own token — so the user answers 'release the reserve?' instead of being told the materials do not exist.
  - `max_reported_shortfalls = 6 lines` — проверено кодом
- `crafting_one_command_runs_one_recipe_once` — **проверено кодом** (источник: код этого репозитория). One crafting.craft command runs one recipe once. The count is on the wire rather than defaulted by the mod, both halves cap it at one, and there is no loop and no retry behind the command id: a recipe that could run again is a report, and running it again is another command.
  - `max_craft_count = 1 runs` — проверено кодом
  - `max_recipe_name_len = 64 characters` — проверено кодом
  - `craft_timeout_ms = 60000 ms` — проверено кодом
- `crafting_success_is_the_product_observed` — **проверено кодом** (источник: код этого репозитория). A craft succeeds only when the re-observed inventory holds more of the recipe's product than before; the mod requires the second half too — that something the recipe consumes is measurably gone — because a product that appeared while nothing was spent did not come out of this recipe.
- `crafting_risk_escalates_from_the_arguments` — **проверено кодом** (источник: код этого репозитория). Spending what the character carries is P3 because it is irreversible; the same command becomes P4 — never automatic — when the named recipe may need a surface or is only afforded by items in a world container, which is the escalate-by-arguments rule move_to applies off-radius.
- `crafting_reading_a_recipe_spends_nothing` — **проверено кодом** (источник: код этого репозитория). crafting.inspect is read-only in fact as well as in the protocol: the character does not move and touches nothing, the answer comes off the crafting readout the observer already produces, and a recipe that was not found is a finding rather than a failure.
- `crafting_goal_bounds_what_one_submission_spends` — **проверено кодом** (источник: код этого репозитория). A craft_item goal names a product and at most four runs, one command each, and the mission will not go and fetch what is missing: a known recipe short of materials ends the goal naming the shortfall, and whether to loot for it is the user's next submission.
  - `max_craft_goal_count = 4 runs` — проверено кодом
  - `max_craft_attempts = 6 attempts` — проверено кодом
  - `max_recipes_tried = 3 recipes` — проверено кодом
- `crafting_capability_is_withheld_until_a_live_run` — **проверено кодом** (источник: код этого репозитория). The crafting capability declares itself experimental, so pz_action_craft is withheld on every install this project can ship to and only a live craft — the recipe's product observed in the inventory afterwards — promotes it; the recipe reading is not withheld with it, because reading spends nothing.

Гипотезы (фон для проверенных отказов, не руководство к действию):

- `crafting_build42_rewrote_the_system` — **не проверено (гипотеза)** (источник: официальные материалы). Build 42 replaced Build 41's crafting model with a new recipe and script system, so every recipe accessor this mod probes is a guess: none of the spellings has been seen answering in a live session, and each is probed through a short closed candidate list.
- `crafting_recipes_are_learned_before_they_can_be_run` — **не проверено (гипотеза)** (источник: wiki). A character can only craft recipes it has learned — some known from the start or from an occupation, others taught by reading a magazine or skill book — and this repository has never observed how Build 42 reports that knowledge.
- `crafting_some_recipes_need_a_surface_or_a_station` — **не проверено (гипотеза)** (источник: wiki). Some Project Zomboid recipes require the character to be at or near something — a workbench, a fire, a specific nearby item — rather than merely holding the materials, and how Build 42 expresses that requirement has not been observed from this repository.
- `crafting_yield_and_skill_effects_are_unmeasured` — **не проверено (гипотеза)** (источник: wiki). How much a recipe yields, whether a craft can fail or produce a lower-quality result, and what a skill level changes about any of it are unmeasured here: no number about Project Zomboid's own crafting outcomes appears anywhere in this repository.

## Двери и окна (`doors_windows`)

Проверенные правила:

- `doors_tristate_honesty` — **проверено кодом** (источник: код этого репозитория). A door's open, locked and barricaded fields are tri-state: null means the build exposed no reader for that fact and is never conflated with an observed false — every door gate fires only on an observed true or false, and an absent field passes the command to the mod, which owns the last word.
- `doors_locked_vs_barricaded` — **проверено кодом** (источник: код этого репозитория). DOOR_LOCKED and DOOR_BARRICADED are distinct reason codes because they demand different replanning: a locked door needs a key the executor does not hunt, a barricaded one needs the planks off or a detour; the route executor refuses with the matching code when no route avoids the sealed door.
- `doors_already_in_state` — **проверено кодом** (источник: код этого репозитория). A door already in the asked-for state is not pre-refused: the mod answers it as an unchanged success with identical before and after readings, for all three door actions alike, so a planner never special-cases which action treats already-done as failure — none does.
- `doors_postcondition_observed` — **проверено кодом** (источник: код этого репозитория). A door action's postcondition is the following observation describing the door in the asked-for state; a door that left the observation radius proves nothing — out of view is not open — so verification declines and the engine reports the timeout or the mod's code honestly.
- `windows_never_auto` — **проверено кодом** (источник: код этого репозитория). A closed window is never traversed automatically: the movement adapters refuse a target whose route semantics report a closed window, as an absolute behavioural restriction rather than a tunable, and no window-opening action exists in the protocol's action set.

## Еда и вода (`food_water`)

Проверенные правила:

- `food_selection_is_code` — **проверено кодом** (источник: код этого репозитория). Which item the character eats is decided by deterministic policy code and never by a language model: the model may say satisfy_hunger, select_food says what, how much, and — on a refusal — exactly what was wrong with every candidate it looked at.
- `food_rotten_refused` — **проверено кодом** (источник: код этого репозитория). Food labelled rotten, or past 60 percent rot progress even when unlabelled, is refused outright by the food policy; no freshness score can trade a rotten item back into the candidate set.
  - `max_rot_progress = 0.6 fraction` — проверено кодом
- `food_burnt_poison_tainted_refused` — **проверено кодом** (источник: код этого репозитория). Poisonous, tainted and burnt food is refused outright — burnt includes anything past 25 percent burn progress — and the filters run in a fixed order so a refusal names the most fundamental problem with the item, not whichever check ran first.
  - `max_burn_progress = 0.25 fraction` — проверено кодом
- `food_raw_frozen_uncooked_refused` — **проверено кодом** (источник: код этого репозитория). Raw food the game flags unsafe, food that requires cooking and is uncooked, and frozen food are all refused by default: the policy does not model cooking or thawing, so selecting such an item would queue a plan it cannot honour.
- `food_reserves_outrank_hunger` — **проверено кодом** (источник: код этого репозитория). Items tagged reserved, keep or do_not_use — and in-game favourites — are never consumed automatically at any hunger; strategic-reserve items open up only when hunger reaches the critical 0.70, so ordinary hunger never eats the emergency stock.
  - `critical_hunger = 0.7 fraction` — проверено кодом
- `food_portion_logic` — **проверено кодом** (источник: код этого репозитория). A meal aims to bring hunger down to the satisfied 0.15: the portion covers the deficit, quantised up to 0.25 steps with a 0.25 minimum, and when the eat-percentage capability is unprobed or unavailable the choice falls back to a whole unit and says so in its rationale.
  - `satisfied_hunger = 0.15 fraction` — проверено кодом
  - `hunger_trigger = 0.35 fraction` — проверено кодом
  - `eat_fraction_step = 0.25 fraction` — проверено кодом
  - `min_eat_fraction = 0.25 fraction` — проверено кодом
- `drink_tainted_alcohol_refused` — **проверено кодом** (источник: код этого репозитория). Tainted water is never drunk without explicit permission, and alcohol is refused as a thirst answer by default; poisonous, rotten-content, frozen and effectively-empty containers are refused on the same hard-filter terms as food.
  - `min_remaining_units = 0.05 fluid_units` — проверено кодом
- `drink_last_container_reserve` — **проверено кодом** (источник: код этого репозитория). The last usable drink container is protected while thirst is below the critical 0.70: without the drink-percentage capability the policy refuses to empty it, and with the capability the portion is capped at half the container, so the reserve survives an ordinary thirst.
  - `critical_thirst = 0.7 fraction` — проверено кодом
  - `max_last_container_fraction = 0.5 fraction` — проверено кодом

Гипотезы (фон для проверенных отказов, не руководство к действию):

- `food_rotten_sickens_lore` — **не проверено (гипотеза)** (источник: wiki). Eating rotten food gives the character food sickness in the game — this is the mechanic the rotten refusal exists for, but nothing in this repository has observed the sickness itself, so it stays a wiki claim, not a fact.
- `drink_tainted_sickens_lore` — **не проверено (гипотеза)** (источник: wiki). Drinking tainted water risks making the character sick unless it is boiled first — the mechanic behind the tainted refusal; this repository has never observed the sickness, so the claim stays wiki-sourced and unverified.

## Медицина (`medical`)

Проверенные правила:

- `medical_triage_order` — **проверено кодом** (источник: код этого репозитория). Triage is a fixed total order: bleeding outranks not bleeding whatever the severities, deeper outranks shallow among bleeding wounds, and the body part's name breaks the last tie — so a retry after an interruption picks the same wound instead of oscillating.
- `medical_dirty_dressing_refused` — **проверено кодом** (источник: код этого репозитория). A dirty dressing is refused outright while any clean one is in reach, and is usable only when nothing clean survived the other filters — the rule is applied against the whole candidate set, not item by item.
- `medical_sterile_ranking` — **проверено кодом** (источник: код этого репозитория). The dressings the policy will choose on its own form a closed ordered list — bandage, ripped sheets, denim strips, leather strips — and a mod-added item tagged as a bandage ranks between clean and dirty, never above a named sterile dressing.
- `medical_bleeding_verified_observed` — **проверено кодом** (источник: код этого репозитория). medical.bandage succeeds only when a following observation shows the treated part's bleeding stopped: the bleeding flag clearing is the evidence, a vanished dressing is context, and another part clearing does not answer for the one that was dressed.

Гипотезы (фон для проверенных отказов, не руководство к действию):

- `medical_dirty_infects_lore` — **не проверено (гипотеза)** (источник: wiki). A dirty dressing raises the wound's infection chance in the game — the mechanic the dirty-dressing refusal is priced against; this repository has never observed a wound infection, so the claim stays wiki-sourced and unverified.

## Память о местах (`memory_places`)

Проверенные правила:

- `memory_home_point` — **проверено кодом** (источник: код этого репозитория). Each save remembers at most one home point, a square set explicitly and stamped with when it was set; return_home is one deterministic journey to that square, and a save with no home set has nowhere to return to — the goal refuses rather than guessing.
- `memory_safe_zones` — **проверено кодом** (источник: код этого репозитория). A safe zone is a keyed centre square with a Chebyshev radius on one floor: containment requires the same z and distance within the radius, and the retreat mission falls back to the nearest remembered safe zone before open ground.
- `memory_no_session_refs` — **проверено кодом** (источник: код этого репозитория). Memory stores derived facts, never observations: a container is remembered by the place-half of its reference and a reservation by item type, because a reference minted last session is not stale, it is wrong — a store that persisted one would hand it back looking valid.
- `memory_reservations_honoured` — **проверено кодом** (источник: код этого репозитория). A reservation names an item type the user set aside, with a reason and a timestamp; the loot selection asks the reserve list per item type and leaves reserved items behind, and the consumption policies refuse user-reserved items at any need level.
- `memory_strings_quarantined` — **проверено кодом** (источник: код этого репозитория). Every string memory stores is bounded and quarantined: container names and task details come from the game, which is untrusted data, so they are truncated — labels to 64 characters, details to 120 — and stripped of control characters and path shapes on the way in.
  - `max_label_len = 64 chars` — проверено кодом
  - `max_detail_len = 120 chars` — проверено кодом

## Движение (`movement`)

Проверенные правила:

- `movement_single_leg_cap` — **проверено кодом** (источник: код этого репозитория). A single movement.move_to command is capped at 30 squares; longer journeys are split into legs by the deterministic route executor, never sent as one walk.
  - `max_move_distance_squares = 30 squares` — проверено кодом
  - `default_move_distance_squares = 20 squares` — проверено кодом
- `movement_arrival_is_observed` — **проверено кодом** (источник: код этого репозитория). Arrival is decided only by an observation placing the character within the target radius on the target floor; a succeeded move ack is a statement about the queue, not the position.
  - `default_arrival_radius = 0.75 world_units` — проверено кодом
  - `max_arrival_radius = 3 world_units` — проверено кодом
- `movement_doors_opened_in_walk` — **проверено кодом** (источник: код этого репозитория). movement.move_to carries allow_doors (default true) and the mod opens unlocked doors met mid-walk, so a route through a known closed door is still one move; the executor's own door.open is reserved for the retry path after a door-shaped failure.
  - `door_step_cost = 2 route_cost` — проверено кодом
- `movement_stuck_is_typed` — **проверено кодом** (источник: код этого репозитория). A journey refuses as stuck, with reason code PATH_STUCK, after 3 completed legs without the Chebyshev distance to the target decreasing, or 3 consecutive failed legs; each single move retries at most once.
  - `max_consecutive_failures = 3 legs` — проверено кодом
  - `move_max_retries = 1 retries` — проверено кодом
- `movement_threat_tolls` — **проверено кодом** (источник: код этого репозитория). Routes detour around remembered zombies: squares within Chebyshev radius 2 of a live sighting cost 8.0 extra, the square of a chasing sighting costs 64.0 — deliberately finite, so a cornered character still gets the least-bad way out instead of a fabricated NO_ROUTE.
  - `threat_avoid_radius = 2 squares` — проверено кодом
  - `threat_step_cost = 8 route_cost` — проверено кодом
  - `chasing_step_cost = 64 route_cost` — проверено кодом
- `movement_journey_budgets` — **проверено кодом** (источник: код этого репозитория). Every journey is bounded: at most 64 move legs, 8 replans and 16384 expanded A* nodes; an exhausted budget is a typed refusal about this executor, never dressed as PATH_NOT_FOUND, which would claim a proof about the world.
  - `max_legs = 64 legs` — проверено кодом
  - `max_replans = 8 replans` — проверено кодом
  - `max_expanded_nodes = 16384 nodes` — проверено кодом
- `movement_absolute_refusals` — **проверено кодом** (источник: код этого репозитория). Three movement refusals are absolute policy, not tunables: a square the mod has not reported loaded is never a target, a drop from height is never taken automatically, and a floor change without observed stairs is refused rather than attempted.

## Отдых и сон (`rest_sleep`)

Проверенные правила:

- `sleep_refused_any_danger` — **проверено кодом** (источник: код этого репозитория). survival.sleep is refused outright while the safety snapshot reports any danger level above none — a stricter bar than the block threshold every other action gets, because a danger level the guard can see is a danger the character would sleep through.
- `sleep_never_autonomous` — **проверено кодом** (источник: код этого репозитория). Sleep is the one action in the adapter package rated P4: once the character is asleep the mod cannot wake them — no queue entry to cancel, no timed action to interrupt — so it is never taken on the agent's own initiative and needs explicit per-call consent.
- `rest_verified_against_endurance` — **проверено кодом** (источник: код этого репозитория). survival.rest is verified against the endurance stat it exists to move — the default target is 0.9 of full, ready to run again without waiting out the long tail — and never inferred from an idle queue, because resting is largely the absence of a queued action.
  - `default_rest_target = 0.9 fraction` — проверено кодом
  - `max_rest_wait_ms = 900000 ms` — проверено кодом
- `sleep_verified_by_clock_and_fatigue` — **проверено кодом** (источник: код этого репозитория). A slept night is proven by a pair of postconditions: fatigue fell and the game clock advanced, measured on the mod's world clock — fatigue alone would also fall during a quiet afternoon, and real seconds pass while the game is paused.
  - `default_sleep_hours = 8 game_hours` — проверено кодом
  - `max_sleep_hours = 16 game_hours` — проверено кодом

## Угрозы и бой (`threat_combat`)

Проверенные правила:

- `threat_chasing_outranks_close` — **проверено кодом** (источник: код этого репозитория). A distant zombie that is chasing outranks a closer one that has not noticed the player: the chasing ladder sits a full rung above the merely-visible one at every band, so a chaser anywhere inside the alert radius is at least medium while an unaware zombie only reaches medium at contact range.
- `threat_distance_bands` — **проверено кодом** (источник: код этого репозитория). The threat bands are 2.0 tiles for contact, 6.0 for reaction range — a chasing zombie this close arrives before a long action finishes — and 15.0 for awareness; beyond that a zombie is a fact, not a situation. They are starting points for tests, tunable in ThreatConfig, not proven game balance.
  - `critical_distance = 2 tiles` — проверено кодом
  - `close_distance = 6 tiles` — проверено кодом
  - `alert_distance = 15 tiles` — проверено кодом
- `threat_pack_and_floor_rules` — **проверено кодом** (источник: код этого репозитория). A pack is worse than the sum of its members: 3 chasing zombies escalate the level one rung and 6 escalate it two, 8 in the alert radius escalate a crowd even when unaware; a zombie on another floor is capped at medium, and a bleeding character never assesses below low.
  - `chasing_horde = 3 zombies` — проверено кодом
  - `chasing_swarm = 6 zombies` — проверено кодом
  - `crowd_count = 8 zombies` — проверено кодом
- `reflex_bands` — **проверено кодом** (источник: код этого репозитория). The reflex guard's rungs never decrease: danger at medium interrupts a vulnerable activity — eating, drinking, reading — danger at high starts nothing new, and danger at critical interrupts whatever is running, because at that point the only correct plan is the threat.
- `reflex_stall_detection` — **проверено кодом** (источник: код этого репозитория). A command that has not progressed for 15 seconds is stuck — the bound sits well above the longest single timed action so a slow eat is not a stall — and stall events rank at user-command priority, pre-empting background work without outranking a real emergency.
  - `stall_ms = 15000 ms` — проверено кодом
- `avoid_retreat_criteria` — **проверено кодом** (источник: код этого репозитория). A retreat succeeds only on the observed postcondition: the nearest zombie at 12.0 tiles or beyond — or none observed — or the character standing in a remembered safe zone with nothing chasing; arriving at the chosen square proves nothing by itself, because the zombies may have followed.
  - `safe_distance = 12 tiles` — проверено кодом
  - `max_retreat_distance = 30 squares` — проверено кодом
  - `retreat_ring_radius = 15 squares` — проверено кодом
  - `max_retreat_legs = 8 legs` — проверено кодом

Гипотезы (фон для проверенных отказов, не руководство к действию):

- `combat_assisted_unverified` — **не проверено (гипотеза)** (источник: код этого репозитория). Assisted combat exists in code and is unverified live: four bounded P4 actions (combat.equip_best, shove, engage — one attack window per command — retreat) and the engage_single_zombie mission ride the combat_assist capability, experimental until a live shove confirms the attack entry points.

