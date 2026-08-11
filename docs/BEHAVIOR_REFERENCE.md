<!--
  GENERATED FILE - DO NOT EDIT BY HAND.
  Generator: pz_agent_core.knowledge.docgen.render_behavior_reference
  Corpus revision: 7e8169d348d5b434 (sha256/16 of the canonical corpus)
  Edit knowledge/gameplay/*.yaml and regenerate; the drift test
  byte-compares this file against a fresh render.
-->

# Behavior reference

Generated from `knowledge/gameplay/*.yaml` — the corpus the planner's bounded
retrieval reads. Grouped by domain; every rule shows what triggers it, what it
does, what proves it, and what happens when it fails. The status badge is the
honesty contract: `verified_script` restates behaviour this repository's tested
code enforces, `verified_live` was observed in a live session, `unverified` is
a hypothesis and drives nothing by itself.

## Containers and loot (`containers_loot`)

### `loot_batch_cap_eight`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/loot/policy.py::MAX_ITEMS_PER_CONTAINER` · **Build:** 42 · **Risk:** P3

One loot selection is one inventory.transfer_batch of at most 8 items — the transfer batch width — and a container holding more is re-selected after the batch lands, against its remaining contents, up to 4 batches per container.

- **Goals:** `loot_area`
- **Nearby:** `container`
- **Observed inputs:** `player.position`

**Preconditions**

- The container was opened and its contents enumerated

**Actions**

- `inventory.transfer_batch`
- `container.inspect`

**Decision** — loot.policy.select returns at most max_items_per_container picks; the mission issues one transfer_batch per selection and re-selects until the container yields nothing or the batch budget is spent.

**Postconditions**

- The batch is verified landing whole before anything is counted taken

**Fallback** — A partial or failed batch counts nothing; the stop reason is recorded and the mission moves on.

**Numbers**

- `max_items_per_container` = 8 items — `verified_script`
- `max_batch_items` = 8 items — `verified_script`
- `max_batches_per_container` = 4 batches — `verified_script`

**Proven by:** `tests/unit/test_loot_policy.py`, `tests/unit/test_loot_mission.py`, `tests/unit/test_adapters_inventory.py`

### `loot_capacity_stop_honesty`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_cli/src/pz_agent_cli/loot_mission.py::LootMission` · **Build:** 42 · **Risk:** P3

Taken means observed taken: items_taken counts only selections whose batch the engine verified landing whole; a partial CONTAINER_FULL stop records what stopped it and counts nothing, because the adapter's own verify refused to bless the partial state.

- **Goals:** `loot_area`
- **Needs:** `encumbrance`
- **Observed inputs:** `player.position`

**Preconditions**

- None recorded.

**Actions**

- `inventory.transfer_batch`

**Decision** — The mission reads the engine's terminal result per batch; only a verified whole landing increments the tally, and capacity known up front is spent greedily in category priority order.

**Postconditions**

- Every counted item is one a verified batch moved

**Fallback** — CONTAINER_FULL ends the container with the stop recorded; an encumbered character ends the mission as encumbered, never as complete.

**Proven by:** `tests/unit/test_loot_mission.py`

### `loot_container_memory_revision`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/memory/model.py::content_revision_of` · **Build:** 42 · **Risk:** P0

A visited container is remembered by the place-half of its reference and a digest of its enumerated contents; when the current observation's digest matches the remembered one the mission records an unchanged skip instead of walking back — and an unremembered or changed shelf is always visited.

- **Goals:** `loot_area`
- **Nearby:** `container`
- **Observed inputs:** `nearby.objects[].kind`

**Preconditions**

- Memory holds a revision for the container's tail

**Actions**

- `container.inspect`

**Decision** — SaveMemory.container_unchanged compares content_revision_of the current enumeration against the stored revision; memory stores derived facts keyed by world tail, never session-scoped references.

**Postconditions**

- Skipped containers carry a recorded skip reason in the mission report

**Fallback** — A digest mismatch or a missing record sends the mission to look; skips are first-class report entries, never silent drops.

**Proven by:** `tests/unit/test_memory_model.py`, `tests/unit/test_memory_store.py`, `tests/unit/test_loot_mission.py`

### `loot_default_wanted_categories`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/loot/policy.py::DEFAULT_WANTED` · **Build:** 42 · **Risk:** P3

The default loot policy takes what feeds, waters, heals, arms, fixes or teaches — food, water, medical, tools, weapons, materials, literature — and deliberately not clothing or unnameable items; take_all widens to every category but never overrides the user's reserves.

- **Goals:** `loot_area`

**Preconditions**

- None recorded.

**Actions**

- `inventory.transfer_batch`

**Decision** — LootPolicy.effective_wanted resolves an empty wanted set to DEFAULT_WANTED; respect_reserves defaults on and take_all does not override it, because the user's word outranks the flag.

**Postconditions**

- None recorded.

**Fallback** — Unwanted or reserved items are recorded as leaves with a single reason each; nothing is dropped silently.

**Proven by:** `tests/unit/test_loot_policy.py`

### `loot_open_is_timed_action`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/protocol/enums.py::READ_ONLY_ACTIONS` · **Build:** 42 · **Risk:** P3

container.open_nearby is deliberately not a read-only action: its name reads like a query, but opening a container is a timed action the character performs in the world, so an unarmed session may inspect but never open.

- **Goals:** `loot_area`
- **Nearby:** `container`

**Preconditions**

- The session is armed in a mutating mode

**Actions**

- `container.open_nearby`
- `container.inspect`

**Decision** — READ_ONLY_ACTIONS carries world.inspect, container.inspect, inventory.search and action.wait only; container.open_nearby needs an armed session like every other mutating command.

**Postconditions**

- None recorded.

**Fallback** — An unarmed open is refused NOT_ARMED before it reaches the mod.

**Proven by:** `tests/contract/test_mcp_action_coverage.py`

### `loot_corpse_observation_gap`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_cli/src/pz_agent_cli/loot_mission.py::LootMission` · **Build:** 42 · **Risk:** P0

Corpses are observation-only for the loot mission: discovery parses only world container references, so a corpse in nearby.objects is an ordinary neighbour, never a loot candidate — looting corpses is an honest gap, not a silent skip of something claimed supported.

- **Goals:** `loot_area`
- **Nearby:** `corpse`
- **Observed inputs:** `nearby.objects[].kind`

**Preconditions**

- None recorded.

**Actions**

- `container.inspect`

**Decision** — LootMission discovery attempts ContainerRef.parse on each nearby object and passes over everything that is not a world container, corpses included.

**Postconditions**

- None recorded.

**Fallback** — There is no failure: a corpse simply never enters the candidate map.

**Proven by:** `tests/unit/test_loot_mission.py`

### `loot_completion_criterion`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_cli/src/pz_agent_cli/loot_mission.py::MAX_CANDIDATES_PER_MISSION` · **Build:** 42 · **Risk:** P3

A loot mission finishes on a provable criterion — every reachable container in scope was inspected or carries a recorded skip reason — and is bounded to 24 candidates per mission with a default radius scope of 10 squares.

- **Goals:** `loot_area`
- **Nearby:** `container`
- **Observed inputs:** `nearby.objects[].kind`, `player.position`

**Preconditions**

- The scope was pinned from an observation at activation, never guessed

**Actions**

- `container.open_nearby`
- `container.inspect`
- `inventory.transfer_batch`
- `movement.move_to`

**Decision** — Scope room or building with the character's room unreadable is a typed PRECONDITION_FAILED naming scope radius as the alternative; candidates, failures, batches and completion probes each have a constant bound.

**Postconditions**

- The final report lists every candidate as inspected or skipped with a reason

**Fallback** — No-progress and consecutive-failure bounds end the mission with a typed refusal and a one-line report summary.

**Numbers**

- `max_candidates_per_mission` = 24 containers — `verified_script`
- `default_loot_radius` = 10 squares — `verified_script`

**Proven by:** `tests/unit/test_loot_mission.py`

## Doors and windows (`doors_windows`)

### `doors_tristate_honesty`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/doors.py::DoorOpenAdapter` · **Build:** 42 · **Risk:** P3

A door's open, locked and barricaded fields are tri-state: null means the build exposed no reader for that fact and is never conflated with an observed false — every door gate fires only on an observed true or false, and an absent field passes the command to the mod, which owns the last word.

- **Nearby:** `door`
- **Observed inputs:** `nearby.objects[].kind`

**Preconditions**

- The observation describes the object as a door

**Actions**

- `door.open`
- `door.close`
- `door.unlock`

**Decision** — Every gate in the doors adapters checks 'is True' or 'is False' against the observed field, never truthiness; an unreadable lock authorises nothing on this side.

**Postconditions**

- None recorded.

**Fallback** — When the field is unreadable the mod resolves the engine object itself and answers with its own observed result or refusal.

**Proven by:** `tests/unit/test_adapters_doors.py`

### `doors_locked_vs_barricaded`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/protocol/reason_codes.py::ReasonCode` · **Build:** 42 · **Risk:** P3

DOOR_LOCKED and DOOR_BARRICADED are distinct reason codes because they demand different replanning: a locked door needs a key the executor does not hunt, a barricaded one needs the planks off or a detour; the route executor refuses with the matching code when no route avoids the sealed door.

- **Goals:** `navigate_to`, `loot_area`
- **Nearby:** `door`
- **Observed inputs:** `nearby.objects[].kind`

**Preconditions**

- None recorded.

**Actions**

- `door.open`
- `movement.move_to`

**Decision** — The navigation executor treats doors known locked or barricaded as walls, and its second search pass walks through sealed doors only to name the one blocking the only route, never to plan through it.

**Postconditions**

- None recorded.

**Fallback** — DOOR_LOCKED or DOOR_BARRICADED ends the journey with the door named; the planner replans a key hunt or a detour, the executor does neither.

**Proven by:** `tests/unit/test_navigation_executor.py`, `tests/unit/test_adapters_doors.py`

### `doors_already_in_state`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/doors.py::DoorOpenAdapter` · **Build:** 42 · **Risk:** P3

A door already in the asked-for state is not pre-refused: the mod answers it as an unchanged success with identical before and after readings, for all three door actions alike, so a planner never special-cases which action treats already-done as failure — none does.

- **Nearby:** `door`
- **Observed inputs:** `nearby.objects[].kind`

**Preconditions**

- None recorded.

**Actions**

- `door.open`
- `door.close`
- `door.unlock`

**Decision** — Validation passes an observed-open door through door.open and an observed-unlocked one through door.unlock; the unchanged success comes back from the mod with the same semantics for open, close and unlock.

**Postconditions**

- The following observation describes the door in the asked-for state

**Fallback** — Nothing needed doing and that was observed; there is no failure branch to replan.

**Proven by:** `tests/unit/test_adapters_doors.py`

### `doors_postcondition_observed`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/doors.py::DoorOpenAdapter.verify` · **Build:** 42 · **Risk:** P3

A door action's postcondition is the following observation describing the door in the asked-for state; a door that left the observation radius proves nothing — out of view is not open — so verification declines and the engine reports the timeout or the mod's code honestly.

- **Nearby:** `door`
- **Observed inputs:** `nearby.objects[].kind`

**Preconditions**

- None recorded.

**Actions**

- `door.open`
- `door.close`

**Decision** — verify re-reads the door from the after observation and blesses only an observed state change; the mod proves its half by re-reading the IsoDoor after the toggle.

**Postconditions**

- The door is observed in the asked-for state after the action

**Fallback** — ACTION_TIMEOUT or the mod's own failure code surfaces unchanged; the caller re-observes or replans.

**Proven by:** `tests/unit/test_adapters_doors.py`

### `windows_never_auto`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/movement.py::SEMANTIC_CLOSED_WINDOW` · **Build:** 42 · **Risk:** P2

A closed window is never traversed automatically: the movement adapters refuse a target whose route semantics report a closed window, as an absolute behavioural restriction rather than a tunable, and no window-opening action exists in the protocol's action set.

- **Nearby:** `window`
- **Observed inputs:** `nearby.objects[].kind`

**Preconditions**

- None recorded.

**Actions**

- `movement.move_to`

**Decision** — MoveToAdapter reads the observer's closed_window semantic and refuses the move; ActionName carries door actions but no window action, so no plan can name one.

**Postconditions**

- None recorded.

**Fallback** — The refusal is final; a route through a window is a decision the user makes at the keyboard, not the agent.

**Proven by:** `tests/unit/test_adapters_movement.py`

## Food and water (`food_water`)

### `food_selection_is_code`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/food.py::select_food` · **Build:** 42 · **Risk:** P2

Which item the character eats is decided by deterministic policy code and never by a language model: the model may say satisfy_hunger, select_food says what, how much, and — on a refusal — exactly what was wrong with every candidate it looked at.

- **Goals:** `satisfy_hunger`
- **Needs:** `hunger`
- **Observed inputs:** `player.stats.hunger`, `player.stats.thirst`

**Preconditions**

- None recorded.

**Actions**

- `consume.eat`

**Decision** — Hard filters reject outright and are never traded off against a score; scoring ranks what is left and returns its factors, so the pick is explainable and reproducible.

**Postconditions**

- None recorded.

**Fallback** — A refusal carries NO_SAFE_FOOD plus one bounded rejection per candidate and the untruncated total.

**Proven by:** `tests/unit/test_policy_food.py`

### `food_rotten_refused`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/food.py::_filter_rotten` · **Build:** 42 · **Risk:** P2

Food labelled rotten, or past 60 percent rot progress even when unlabelled, is refused outright by the food policy; no freshness score can trade a rotten item back into the candidate set.

- **Goals:** `satisfy_hunger`
- **Needs:** `hunger`
- **Observed inputs:** `player.stats.hunger`

**Preconditions**

- None recorded.

**Actions**

- `consume.eat`

**Decision** — The rotten filter fires on the freshness label or on rot_progress above PolicyConfig.max_rot_progress; allow_rotten defaults off and is an explicit user override.

**Postconditions**

- None recorded.

**Fallback** — The item is rejected with reason ROTTEN and the next candidate is considered; nothing edible means NO_SAFE_FOOD.

**Numbers**

- `max_rot_progress` = 0.6 fraction — `verified_script`

**Proven by:** `tests/unit/test_policy_food.py`, `tests/unit/test_policy_config.py`

### `food_rotten_sickens_lore`

**Status:** `unverified` · **Source:** `wiki` — `https://pzwiki.net/wiki/Food — spoilage section, read for Build 41 lore` · **Build:** 41 · **Risk:** P0

Eating rotten food gives the character food sickness in the game — this is the mechanic the rotten refusal exists for, but nothing in this repository has observed the sickness itself, so it stays a wiki claim, not a fact.

- **Needs:** `sickness`

**Preconditions**

- None recorded.

**Actions**

- None — the rule drives no action.

**Decision** — No code decides on this claim; it is background for why the verified refusal in food_rotten_refused is worth its cost.

**Postconditions**

- None recorded.

**Fallback** — Not applicable: an unverified claim drives no action.

**Proven by:** nothing — which is why the status says so.

### `food_burnt_poison_tainted_refused`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/food.py::_filter_poisonous` · **Build:** 42 · **Risk:** P2

Poisonous, tainted and burnt food is refused outright — burnt includes anything past 25 percent burn progress — and the filters run in a fixed order so a refusal names the most fundamental problem with the item, not whichever check ran first.

- **Goals:** `satisfy_hunger`
- **Needs:** `hunger`

**Preconditions**

- None recorded.

**Actions**

- `consume.eat`

**Decision** — The filter chain runs edible, destroyed, poisonous, tainted, rotten, raw, requires-cooking, burnt, frozen, tool, reserves, reach, portions, relief in that order and reports the first hit; poison is read from the flag or a positive poison power.

**Postconditions**

- None recorded.

**Fallback** — Each rejected item carries its reason; the choice falls to the best survivor or the selection refuses NO_SAFE_FOOD.

**Numbers**

- `max_burn_progress` = 0.25 fraction — `verified_script`

**Proven by:** `tests/unit/test_policy_food.py`

### `food_raw_frozen_uncooked_refused`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/food.py::_filter_raw` · **Build:** 42 · **Risk:** P2

Raw food the game flags unsafe, food that requires cooking and is uncooked, and frozen food are all refused by default: the policy does not model cooking or thawing, so selecting such an item would queue a plan it cannot honour.

- **Goals:** `satisfy_hunger`
- **Needs:** `hunger`

**Preconditions**

- None recorded.

**Actions**

- `consume.eat`

**Decision** — allow_raw, allow_required_cooking and allow_frozen all default off in PolicyConfig; each is a deliberate user override, never a score trade-off.

**Postconditions**

- None recorded.

**Fallback** — The rejection names the flag that fired; cooking is a separate skill outside this policy.

**Proven by:** `tests/unit/test_policy_food.py`, `tests/unit/test_policy_config.py`

### `food_reserves_outrank_hunger`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/selection.py::is_user_reserved` · **Build:** 42 · **Risk:** P2

Items tagged reserved, keep or do_not_use — and in-game favourites — are never consumed automatically at any hunger; strategic-reserve items open up only when hunger reaches the critical 0.70, so ordinary hunger never eats the emergency stock.

- **Goals:** `satisfy_hunger`
- **Needs:** `hunger`
- **Observed inputs:** `player.stats.hunger`

**Preconditions**

- None recorded.

**Actions**

- `consume.eat`

**Decision** — is_user_reserved is an unconditional filter; the strategic-reserve filter passes only when PolicyConfig.is_hunger_critical, at hunger of at least critical_hunger.

**Postconditions**

- None recorded.

**Fallback** — Reserved items are rejected with USER_RESERVED or STRATEGIC_RESERVE naming the threshold; the user's word outranks the need.

**Numbers**

- `critical_hunger` = 0.7 fraction — `verified_script`

**Proven by:** `tests/unit/test_policy_food.py`, `tests/unit/test_policy_selection.py`

### `food_portion_logic`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/food.py::_choose_fraction` · **Build:** 42 · **Risk:** P2

A meal aims to bring hunger down to the satisfied 0.15: the portion covers the deficit, quantised up to 0.25 steps with a 0.25 minimum, and when the eat-percentage capability is unprobed or unavailable the choice falls back to a whole unit and says so in its rationale.

- **Goals:** `satisfy_hunger`
- **Needs:** `hunger`
- **Observed inputs:** `player.stats.hunger`

**Preconditions**

- None recorded.

**Actions**

- `consume.eat`

**Decision** — The fraction is ceil(deficit/relief) against eat_fraction_step, floored at min_eat_fraction; portions round up because under-eating costs a second action, which is worse than a little surplus.

**Postconditions**

- The chosen fraction is reproducible for the same inventory and stats

**Fallback** — Without the capability the whole item is eaten and the rationale names the probe state; the mod refuses a fraction it cannot honour.

**Numbers**

- `satisfied_hunger` = 0.15 fraction — `verified_script`
- `hunger_trigger` = 0.35 fraction — `verified_script`
- `eat_fraction_step` = 0.25 fraction — `verified_script`
- `min_eat_fraction` = 0.25 fraction — `verified_script`

**Proven by:** `tests/unit/test_policy_food.py`

### `drink_tainted_alcohol_refused`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/drink.py::_filter_tainted` · **Build:** 42 · **Risk:** P2

Tainted water is never drunk without explicit permission, and alcohol is refused as a thirst answer by default; poisonous, rotten-content, frozen and effectively-empty containers are refused on the same hard-filter terms as food.

- **Goals:** `satisfy_thirst`
- **Needs:** `thirst`
- **Observed inputs:** `player.stats.thirst`

**Preconditions**

- None recorded.

**Actions**

- `consume.drink`

**Decision** — allow_tainted_water and allow_alcohol_for_thirst default off; a container at or below min_remaining_units of 0.05 counts as empty and is rejected rather than raised to the lips.

**Postconditions**

- None recorded.

**Fallback** — Each rejection names its reason; nothing safe means NO_SAFE_DRINK with the bounded rejection list.

**Numbers**

- `min_remaining_units` = 0.05 fluid_units — `verified_script`

**Proven by:** `tests/unit/test_policy_drink.py`

### `drink_tainted_sickens_lore`

**Status:** `unverified` · **Source:** `wiki` — `https://pzwiki.net/wiki/Water — tainted water section, read for Build 41 lore` · **Build:** 41 · **Risk:** P0

Drinking tainted water risks making the character sick unless it is boiled first — the mechanic behind the tainted refusal; this repository has never observed the sickness, so the claim stays wiki-sourced and unverified.

- **Needs:** `sickness`, `thirst`

**Preconditions**

- None recorded.

**Actions**

- None — the rule drives no action.

**Decision** — No code decides on this claim; it is background for the verified refusal in drink_tainted_alcohol_refused.

**Postconditions**

- None recorded.

**Fallback** — Not applicable: an unverified claim drives no action.

**Proven by:** nothing — which is why the status says so.

### `drink_last_container_reserve`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/drink.py::_last_container_rejection` · **Build:** 42 · **Risk:** P2

The last usable drink container is protected while thirst is below the critical 0.70: without the drink-percentage capability the policy refuses to empty it, and with the capability the portion is capped at half the container, so the reserve survives an ordinary thirst.

- **Goals:** `satisfy_thirst`
- **Needs:** `thirst`
- **Observed inputs:** `player.stats.thirst`

**Preconditions**

- Exactly one usable container remains in reach

**Actions**

- `consume.drink`

**Decision** — LAST_CONTAINER_RESERVE fires only when partial drinking is unavailable and thirst is not critical; _choose_fraction otherwise caps the portion at max_last_container_fraction.

**Postconditions**

- None recorded.

**Fallback** — Critical thirst opens the container fully; below it the refusal or the cap stands and the rationale names the capability probe.

**Numbers**

- `critical_thirst` = 0.7 fraction — `verified_script`
- `max_last_container_fraction` = 0.5 fraction — `verified_script`

**Proven by:** `tests/unit/test_policy_drink.py`

## Medical (`medical`)

### `medical_triage_order`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/medical.py::select_treatment` · **Build:** 42 · **Risk:** P2

Triage is a fixed total order: bleeding outranks not bleeding whatever the severities, deeper outranks shallow among bleeding wounds, and the body part's name breaks the last tie — so a retry after an interruption picks the same wound instead of oscillating.

- **Goals:** `treat_wounds`
- **Needs:** `bleeding`, `pain`
- **Observed inputs:** `player.position`

**Preconditions**

- At least one wound is bleeding

**Actions**

- `medical.bandage`

**Decision** — Wounds sort on (not bleeding, negative severity, part name); only the head of that order is treated per selection, and the rationale lists the wounds waiting their turn.

**Postconditions**

- None recorded.

**Fallback** — Nothing bleeding is a PRECONDITION_FAILED refusal, because a dressing's effect on a non-bleeding wound is not observable.

**Proven by:** `tests/unit/test_policy_medical.py`

### `medical_dirty_dressing_refused`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/medical.py::select_treatment` · **Build:** 42 · **Risk:** P2

A dirty dressing is refused outright while any clean one is in reach, and is usable only when nothing clean survived the other filters — the rule is applied against the whole candidate set, not item by item.

- **Goals:** `treat_wounds`
- **Needs:** `bleeding`

**Preconditions**

- None recorded.

**Actions**

- `medical.bandage`

**Decision** — Candidates rank sterile before tagged-unknown before dirty; when any non-dirty candidate remains, every dirty one is converted to a rejection naming the infection trade.

**Postconditions**

- None recorded.

**Fallback** — With only dirty dressings left the best dirty one is chosen and the rationale says nothing sterile is in reach.

**Proven by:** `tests/unit/test_policy_medical.py`

### `medical_dirty_infects_lore`

**Status:** `unverified` · **Source:** `wiki` — `https://pzwiki.net/wiki/First_aid — dressing cleanliness, read for Build 41 lore` · **Build:** 41 · **Risk:** P0

A dirty dressing raises the wound's infection chance in the game — the mechanic the dirty-dressing refusal is priced against; this repository has never observed a wound infection, so the claim stays wiki-sourced and unverified.

- **Needs:** `sickness`

**Preconditions**

- None recorded.

**Actions**

- None — the rule drives no action.

**Decision** — No code decides on this claim; it is background for medical_dirty_dressing_refused.

**Postconditions**

- None recorded.

**Fallback** — Not applicable: an unverified claim drives no action.

**Proven by:** nothing — which is why the status says so.

### `medical_sterile_ranking`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/policy/medical.py::STERILE_DRESSING_TYPES` · **Build:** 42 · **Risk:** P2

The dressings the policy will choose on its own form a closed ordered list — bandage, ripped sheets, denim strips, leather strips — and a mod-added item tagged as a bandage ranks between clean and dirty, never above a named sterile dressing.

- **Goals:** `treat_wounds`

**Preconditions**

- None recorded.

**Actions**

- `medical.bandage`

**Decision** — Dressing.classify assigns rank sterile, tagged or dirty; the order key is rank, then position in the closed list, then the item reference, so no two candidates ever compare equal.

**Postconditions**

- None recorded.

**Fallback** — An item that is not on either list and carries no bandage tag is not a dressing at all and is never selected.

**Proven by:** `tests/unit/test_policy_medical.py`

### `medical_bleeding_verified_observed`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/medical.py::BandageAdapter` · **Build:** 42 · **Risk:** P2

medical.bandage succeeds only when a following observation shows the treated part's bleeding stopped: the bleeding flag clearing is the evidence, a vanished dressing is context, and another part clearing does not answer for the one that was dressed.

- **Goals:** `treat_wounds`
- **Needs:** `bleeding`
- **Observed inputs:** `player.position`

**Preconditions**

- The named part is observed bleeding before the action

**Actions**

- `medical.bandage`

**Decision** — verify compares the wound's bleeding flag across the before and after observations for the exact body part named; a part never observed bleeding is refused because success could not be shown.

**Postconditions**

- The treated part is no longer reported bleeding

**Fallback** — A mod ack without the observed change fails verification; the engine reports it honestly and the wound stays on the triage list.

**Proven by:** `tests/unit/test_adapters_medical.py`, `tests/unit/test_care_mission.py`

## Memory and places (`memory_places`)

### `memory_home_point`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/memory/store.py::SaveMemory.set_home` · **Build:** 42 · **Risk:** P2

Each save remembers at most one home point, a square set explicitly and stamped with when it was set; return_home is one deterministic journey to that square, and a save with no home set has nowhere to return to — the goal refuses rather than guessing.

- **Goals:** `return_home`
- **Observed inputs:** `player.position`

**Preconditions**

- Memory holds a home square for the current save

**Actions**

- `movement.move_to`

**Decision** — SaveMemory keeps home on save scope; the return-home server reads it and drives a single Journey at the remembered square.

**Postconditions**

- The observed position is within the arrival radius of the remembered home square

**Fallback** — No home in memory is a typed refusal naming the missing fact; nothing invents a home.

**Proven by:** `tests/unit/test_memory_store.py`, `tests/unit/test_return_home.py`

### `memory_safe_zones`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/memory/model.py::SafeZone` · **Build:** 42 · **Risk:** P2

A safe zone is a keyed centre square with a Chebyshev radius on one floor: containment requires the same z and distance within the radius, and the retreat mission falls back to the nearest remembered safe zone before open ground.

- **Goals:** `avoid_threat`, `return_home`
- **Observed inputs:** `player.position`

**Preconditions**

- None recorded.

**Actions**

- `movement.move_to`

**Decision** — SafeZone.contains checks floor equality and Chebyshev distance; the avoid mission prefers the nearest zone within its retreat range, ties broken on the centre square.

**Postconditions**

- None recorded.

**Fallback** — With no remembered zone in range the retreat targets open ground away from the observed threats instead.

**Proven by:** `tests/unit/test_memory_model.py`, `tests/unit/test_avoid_mission.py`

### `memory_no_session_refs`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/memory/model.py::container_tail` · **Build:** 42 · **Risk:** P0

Memory stores derived facts, never observations: a container is remembered by the place-half of its reference and a reservation by item type, because a reference minted last session is not stale, it is wrong — a store that persisted one would hand it back looking valid.

**Preconditions**

- None recorded.

**Actions**

- None — the rule drives no action.

**Decision** — container_tail strips the session half before anything is stored; Reservation is keyed by full_type so it still matches after a reload.

**Postconditions**

- None recorded.

**Fallback** — A record that cannot be reduced to a session-free fact is refused by the model's own validation, not stored approximately.

**Proven by:** `tests/unit/test_memory_model.py`, `tests/unit/test_memory_store.py`

### `memory_reservations_honoured`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/memory/model.py::Reservation` · **Build:** 42 · **Risk:** P1

A reservation names an item type the user set aside, with a reason and a timestamp; the loot selection asks the reserve list per item type and leaves reserved items behind, and the consumption policies refuse user-reserved items at any need level.

- **Goals:** `loot_area`, `satisfy_hunger`, `satisfy_thirst`

**Preconditions**

- None recorded.

**Actions**

- `inventory.transfer_batch`
- `consume.eat`
- `consume.drink`

**Decision** — loot.policy.select consults is_reserved per full_type when respect_reserves is on, which is the default; the food and drink filters refuse reserved and favourite items unconditionally.

**Postconditions**

- None recorded.

**Fallback** — A reserved item is a recorded leave or a named rejection, never a silent skip.

**Proven by:** `tests/unit/test_memory_model.py`, `tests/unit/test_loot_policy.py`, `tests/unit/test_policy_selection.py`

### `memory_strings_quarantined`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/memory/model.py::MAX_LABEL_LEN` · **Build:** 42 · **Risk:** P0

Every string memory stores is bounded and quarantined: container names and task details come from the game, which is untrusted data, so they are truncated — labels to 64 characters, details to 120 — and stripped of control characters and path shapes on the way in.

**Preconditions**

- None recorded.

**Actions**

- None — the rule drives no action.

**Decision** — The model runs every inbound string through the same redaction the MCP boundary uses, so a memory file cannot become a prompt-injection channel by being read back.

**Postconditions**

- None recorded.

**Fallback** — Observed strings are truncated and redacted on the way in; a record hand-built past the bounds raises the typed MemoryValueError instead of being stored.

**Numbers**

- `max_label_len` = 64 chars — `verified_script`
- `max_detail_len` = 120 chars — `verified_script`

**Proven by:** `tests/unit/test_memory_model.py`, `tests/unit/test_diagnostics_redaction.py`

## Movement (`movement`)

### `movement_single_leg_cap`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/movement.py::MAX_MOVE_DISTANCE_SQUARES` · **Build:** 42 · **Risk:** P2

A single movement.move_to command is capped at 30 squares; longer journeys are split into legs by the deterministic route executor, never sent as one walk.

- **Goals:** `navigate_to`, `return_home`, `explore_area`
- **Observed inputs:** `player.position`

**Preconditions**

- The target square is within 30 squares of the character

**Actions**

- `movement.move_to`

**Decision** — MoveToAdapter.validate refuses a longer single move with TARGET_OUT_OF_RANGE; Journey plans legs of at most JourneyLimits.leg_distance squares, itself capped at the same 30.

**Postconditions**

- The observed position is within the arrival radius on the target floor

**Fallback** — The executor replans the remaining route from the newest observation; the mod refuses longer single moves outright.

**Numbers**

- `max_move_distance_squares` = 30 squares — `verified_script`
- `default_move_distance_squares` = 20 squares — `verified_script`

**Proven by:** `tests/unit/test_adapters_movement.py`, `tests/unit/test_navigation_executor.py`

### `movement_arrival_is_observed`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/navigation/executor.py::Journey.next_step` · **Build:** 42 · **Risk:** P2

Arrival is decided only by an observation placing the character within the target radius on the target floor; a succeeded move ack is a statement about the queue, not the position.

- **Goals:** `navigate_to`
- **Observed inputs:** `player.position`

**Preconditions**

- None recorded.

**Actions**

- `movement.move_to`
- `movement.move_near`

**Decision** — Journey returns Arrived only after reading a fresh observation inside the radius; MoveToAdapter.verify compares the observed position, never the ack.

**Postconditions**

- A following observation reports the character inside the requested radius and floor

**Fallback** — Without the observed postcondition the engine reports timeout or the mod's own failure code honestly; the journey starts another leg or refuses.

**Numbers**

- `default_arrival_radius` = 0.75 world_units — `verified_script`
- `max_arrival_radius` = 3 world_units — `verified_script`

**Proven by:** `tests/unit/test_navigation_executor.py`, `tests/unit/test_adapters_movement.py`

### `movement_doors_opened_in_walk`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/navigation/executor.py::Journey` · **Build:** 42 · **Risk:** P3

movement.move_to carries allow_doors (default true) and the mod opens unlocked doors met mid-walk, so a route through a known closed door is still one move; the executor's own door.open is reserved for the retry path after a door-shaped failure.

- **Goals:** `navigate_to`, `loot_area`
- **Nearby:** `door`
- **Observed inputs:** `nearby.objects[].kind`

**Preconditions**

- The door on the route is not observed locked or barricaded

**Actions**

- `movement.move_to`
- `door.open`

**Decision** — The route search charges DOOR_STEP_COST 2.0 for stepping through a door not yet observed open, so short detours around it win; the walk itself opens it when no detour is worth it.

**Postconditions**

- The walk completes through the doorway, or the failed leg names the door for the retry

**Fallback** — A walk that failed with a door-shaped code replans with an explicit door.open on the remembered leg before routing around.

**Numbers**

- `door_step_cost` = 2 route_cost — `verified_script`

**Proven by:** `tests/unit/test_navigation_executor.py`, `tests/unit/test_adapters_movement.py`

### `movement_stuck_is_typed`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/navigation/executor.py::MAX_CONSECUTIVE_FAILURES` · **Build:** 42 · **Risk:** P2

A journey refuses as stuck, with reason code PATH_STUCK, after 3 completed legs without the Chebyshev distance to the target decreasing, or 3 consecutive failed legs; each single move retries at most once.

- **Goals:** `navigate_to`
- **Observed inputs:** `player.position`

**Preconditions**

- None recorded.

**Actions**

- `movement.move_to`

**Decision** — Journey counts legs without net progress and consecutive failures against MAX_CONSECUTIVE_FAILURES; MOVE_RETRY_POLICY grants one in-leg retry with interrupt allowed.

**Postconditions**

- None recorded.

**Fallback** — The refusal carries PATH_STUCK plus the last obstacle square and door where known; the caller replans or surrenders the goal, it does not loop.

**Numbers**

- `max_consecutive_failures` = 3 legs — `verified_script`
- `move_max_retries` = 1 retries — `verified_script`

**Proven by:** `tests/unit/test_navigation_executor.py`

### `movement_threat_tolls`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/navigation/executor.py::THREAT_STEP_COST` · **Build:** 42 · **Risk:** P2

Routes detour around remembered zombies: squares within Chebyshev radius 2 of a live sighting cost 8.0 extra, the square of a chasing sighting costs 64.0 — deliberately finite, so a cornered character still gets the least-bad way out instead of a fabricated NO_ROUTE.

- **Goals:** `navigate_to`, `avoid_threat`
- **Nearby:** `zombie`
- **Observed inputs:** `nearby.zombies[].distance`, `nearby.zombies[].chasing`

**Preconditions**

- JourneyLimits.avoid_threats is on, which is the default

**Actions**

- `movement.move_to`

**Decision** — The A* step cost adds THREAT_STEP_COST inside THREAT_AVOID_RADIUS of a live sighting and CHASING_STEP_COST on a chasing sighting's own square; sightings decay, so the tolls are preferences, never walls.

**Postconditions**

- None recorded.

**Fallback** — When every way out crosses a chaser the cheapest contaminated route is still returned rather than refusing NO_ROUTE.

**Numbers**

- `threat_avoid_radius` = 2 squares — `verified_script`
- `threat_step_cost` = 8 route_cost — `verified_script`
- `chasing_step_cost` = 64 route_cost — `verified_script`

**Proven by:** `tests/unit/test_navigation_executor.py`

### `movement_journey_budgets`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/navigation/executor.py::JourneyLimits` · **Build:** 42 · **Risk:** P2

Every journey is bounded: at most 64 move legs, 8 replans and 16384 expanded A* nodes; an exhausted budget is a typed refusal about this executor, never dressed as PATH_NOT_FOUND, which would claim a proof about the world.

- **Goals:** `navigate_to`, `explore_area`

**Preconditions**

- None recorded.

**Actions**

- `movement.move_to`

**Decision** — Journey tracks legs, replans and search expansions against JourneyLimits and refuses with LEG_BUDGET_EXHAUSTED, REPLAN_BUDGET_EXHAUSTED or SEARCH_BUDGET_EXHAUSTED, which map to no protocol reason code by design.

**Postconditions**

- None recorded.

**Fallback** — A journey this contested needs the planner, not another lap; the refusal ends it.

**Numbers**

- `max_legs` = 64 legs — `verified_script`
- `max_replans` = 8 replans — `verified_script`
- `max_expanded_nodes` = 16384 nodes — `verified_script`

**Proven by:** `tests/unit/test_navigation_executor.py`

### `movement_absolute_refusals`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/movement.py::MoveToAdapter` · **Build:** 42 · **Risk:** P2

Three movement refusals are absolute policy, not tunables: a square the mod has not reported loaded is never a target, a drop from height is never taken automatically, and a floor change without observed stairs is refused rather than attempted.

- **Goals:** `navigate_to`
- **Nearby:** `square`
- **Observed inputs:** `nearby.objects[].kind`, `player.position`

**Preconditions**

- None recorded.

**Actions**

- `movement.move_to`

**Decision** — MoveToAdapter.validate reads the observer's square semantics (loaded, blocked, drop, stairs) and refuses with TARGET_NOT_LOADED or PRECONDITION_FAILED before anything is queued.

**Postconditions**

- None recorded.

**Fallback** — The refusal is final for that target; the planner picks a different route or target, the adapter never guesses.

**Proven by:** `tests/unit/test_adapters_movement.py`

## Rest and sleep (`rest_sleep`)

### `sleep_refused_any_danger`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/survival.py::SleepAdapter.validate` · **Build:** 42 · **Risk:** P4

survival.sleep is refused outright while the safety snapshot reports any danger level above none — a stricter bar than the block threshold every other action gets, because a danger level the guard can see is a danger the character would sleep through.

- **Goals:** `sleep_until_rested`
- **Needs:** `fatigue`
- **Observed inputs:** `safety.danger_level`

**Preconditions**

- The observed danger level is exactly none

**Actions**

- `survival.sleep`

**Decision** — SleepAdapter.validate compares safety.danger_level against DangerLevel.NONE and refuses with the level named in the evidence; there is no override knob.

**Postconditions**

- None recorded.

**Fallback** — The refusal is PRECONDITION_FAILED with the danger level; the caller deals with the threat first or does not sleep.

**Proven by:** `tests/unit/test_adapters_survival.py`, `tests/unit/test_care_mission.py`

### `sleep_never_autonomous`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/survival.py::SleepAdapter` · **Build:** 42 · **Risk:** P4

Sleep is the one action in the adapter package rated P4: once the character is asleep the mod cannot wake them — no queue entry to cancel, no timed action to interrupt — so it is never taken on the agent's own initiative and needs explicit per-call consent.

- **Goals:** `sleep_until_rested`
- **Observed inputs:** `safety.danger_level`

**Preconditions**

- None recorded.

**Actions**

- `survival.sleep`

**Decision** — The adapter declares RiskClass.P4, which the permission layer maps to never-automatic; the reflex guard is consulted before the command is sent at all.

**Postconditions**

- None recorded.

**Fallback** — Without the consent tier the command is refused by policy before the mod ever sees it.

**Proven by:** `tests/unit/test_adapters_survival.py`, `tests/unit/test_policy_permissions.py`

### `rest_verified_against_endurance`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/survival.py::RestAdapter` · **Build:** 42 · **Risk:** P2

survival.rest is verified against the endurance stat it exists to move — the default target is 0.9 of full, ready to run again without waiting out the long tail — and never inferred from an idle queue, because resting is largely the absence of a queued action.

- **Goals:** `rest_until`
- **Needs:** `endurance`
- **Observed inputs:** `player.stats.endurance`

**Preconditions**

- None recorded.

**Actions**

- `survival.rest`

**Decision** — RestAdapter.verify reads the reported endurance and blesses only an observed rise to the target; a target outside 0.05 through 1.0 is refused at validation.

**Postconditions**

- Observed endurance reaches the requested target

**Fallback** — An unreached target within the bounded wait is an honest timeout, not a success; the mission may rest again or surrender.

**Numbers**

- `default_rest_target` = 0.9 fraction — `verified_script`
- `max_rest_wait_ms` = 900000 ms — `verified_script`

**Proven by:** `tests/unit/test_adapters_survival.py`, `tests/unit/test_care_mission.py`

### `sleep_verified_by_clock_and_fatigue`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/actions/adapters/survival.py::SleepAdapter.verify` · **Build:** 42 · **Risk:** P4

A slept night is proven by a pair of postconditions: fatigue fell and the game clock advanced, measured on the mod's world clock — fatigue alone would also fall during a quiet afternoon, and real seconds pass while the game is paused.

- **Goals:** `sleep_until_rested`
- **Needs:** `fatigue`
- **Observed inputs:** `player.stats.fatigue`

**Preconditions**

- None recorded.

**Actions**

- `survival.sleep`

**Decision** — SleepAdapter.verify requires both the fatigue drop and the elapsed game seconds for the requested hours, default 8 within 1 through 16.

**Postconditions**

- Observed fatigue fell after the sleep
- The mod's world clock advanced by the slept hours

**Fallback** — Either half missing fails verification; the engine reports timeout or the mod's own code, never an assumed night.

**Numbers**

- `default_sleep_hours` = 8 game_hours — `verified_script`
- `max_sleep_hours` = 16 game_hours — `verified_script`

**Proven by:** `tests/unit/test_adapters_survival.py`

## Threats and combat (`threat_combat`)

### `threat_chasing_outranks_close`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/safety/threat.py::assess_threat` · **Build:** 42 · **Risk:** P0

A distant zombie that is chasing outranks a closer one that has not noticed the player: the chasing ladder sits a full rung above the merely-visible one at every band, so a chaser anywhere inside the alert radius is at least medium while an unaware zombie only reaches medium at contact range.

- **Goals:** `avoid_threat`
- **Needs:** `panic`
- **Nearby:** `zombie`
- **Observed inputs:** `nearby.zombies[].chasing`, `nearby.zombies[].visible`, `nearby.zombies[].distance`

**Preconditions**

- None recorded.

**Actions**

- None — the rule drives no action.

**Decision** — assess_threat evaluates each zombie against chasing, visibility and distance, then takes the maximum; the ladders differ by one rung per band by construction.

**Postconditions**

- None recorded.

**Fallback** — Not applicable: assessment is observation-only and drives no action of its own.

**Proven by:** `tests/unit/test_safety_threat.py`

### `threat_distance_bands`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/safety/threat.py::ThreatConfig` · **Build:** 42 · **Risk:** P0

The threat bands are 2.0 tiles for contact, 6.0 for reaction range — a chasing zombie this close arrives before a long action finishes — and 15.0 for awareness; beyond that a zombie is a fact, not a situation. They are starting points for tests, tunable in ThreatConfig, not proven game balance.

- **Goals:** `avoid_threat`
- **Nearby:** `zombie`
- **Observed inputs:** `nearby.zombies[].distance`

**Preconditions**

- None recorded.

**Actions**

- None — the rule drives no action.

**Decision** — ThreatConfig validates the distances as positive and strictly increasing; every branch reads the config rather than a literal.

**Postconditions**

- None recorded.

**Fallback** — Not applicable: assessment is observation-only.

**Numbers**

- `critical_distance` = 2 tiles — `verified_script`
- `close_distance` = 6 tiles — `verified_script`
- `alert_distance` = 15 tiles — `verified_script`

**Proven by:** `tests/unit/test_safety_threat.py`

### `threat_pack_and_floor_rules`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/safety/threat.py::assess_threat` · **Build:** 42 · **Risk:** P0

A pack is worse than the sum of its members: 3 chasing zombies escalate the level one rung and 6 escalate it two, 8 in the alert radius escalate a crowd even when unaware; a zombie on another floor is capped at medium, and a bleeding character never assesses below low.

- **Needs:** `bleeding`, `panic`
- **Nearby:** `zombie`
- **Observed inputs:** `nearby.zombies[].chasing`, `nearby.zombies[].distance`

**Preconditions**

- None recorded.

**Actions**

- None — the rule drives no action.

**Decision** — Counts escalate on the ladder saturating at critical; bleeding raises the floor and bleeding while chased escalates once more, because the clock is already running.

**Postconditions**

- None recorded.

**Fallback** — Not applicable: assessment is observation-only.

**Numbers**

- `chasing_horde` = 3 zombies — `verified_script`
- `chasing_swarm` = 6 zombies — `verified_script`
- `crowd_count` = 8 zombies — `verified_script`

**Proven by:** `tests/unit/test_safety_threat.py`

### `reflex_bands`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/safety/reflex.py::ReflexConfig` · **Build:** 42 · **Risk:** P0

The reflex guard's rungs never decrease: danger at medium interrupts a vulnerable activity — eating, drinking, reading — danger at high starts nothing new, and danger at critical interrupts whatever is running, because at that point the only correct plan is the threat.

- **Needs:** `panic`
- **Nearby:** `zombie`
- **Observed inputs:** `safety.danger_level`

**Preconditions**

- None recorded.

**Actions**

- None — the rule drives no action.

**Decision** — ReflexConfig pins interrupt_at, block_at and flee_at with a validator refusing a decreasing ladder; the guard is a pure function of two observations and the out-of-band signals, so it works with the planner absent.

**Postconditions**

- None recorded.

**Fallback** — A fired event means start no new task; what more it authorises is stated on the event's own booleans, never inferred.

**Proven by:** `tests/unit/test_safety_reflex.py`

### `reflex_stall_detection`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/safety/reflex.py::ReflexConfig` · **Build:** 42 · **Risk:** P0

A command that has not progressed for 15 seconds is stuck — the bound sits well above the longest single timed action so a slow eat is not a stall — and stall events rank at user-command priority, pre-empting background work without outranking a real emergency.

**Preconditions**

- None recorded.

**Actions**

- None — the rule drives no action.

**Decision** — stall_ms is compared against each in-flight command's last progress; PATH_STUCK and NO_PROGRESS map to Priority.USER_COMMAND in the reflex ladder.

**Postconditions**

- None recorded.

**Fallback** — The stuck command is terminated through the event's command ids; the mod-owned queue is the only queue the guard touches.

**Numbers**

- `stall_ms` = 15000 ms — `verified_script`

**Proven by:** `tests/unit/test_safety_reflex.py`

### `avoid_retreat_criteria`

**Status:** `verified_script` · **Source:** `code` — `packages/pz_agent_cli/src/pz_agent_cli/avoid_mission.py::SAFE_DISTANCE` · **Build:** 42 · **Risk:** P2

A retreat succeeds only on the observed postcondition: the nearest zombie at 12.0 tiles or beyond — or none observed — or the character standing in a remembered safe zone with nothing chasing; arriving at the chosen square proves nothing by itself, because the zombies may have followed.

- **Goals:** `avoid_threat`
- **Needs:** `panic`
- **Nearby:** `zombie`
- **Observed inputs:** `nearby.zombies[].distance`, `nearby.zombies[].chasing`, `player.position`

**Preconditions**

- None recorded.

**Actions**

- `movement.move_to`

**Decision** — The target is the nearest remembered user safe zone within 30 squares, else the square on the 15-square ring maximising the minimum distance to the observed threats; the threat picture is re-read from every fresh observation, never pinned at activation.

**Postconditions**

- The newest observation reports the nearest zombie at or beyond the safe distance, or a safe zone reached with nothing chasing

**Fallback** — Cornered, unroutable or out of legs with zombies still observed ends as THREAT_INTERRUPTED naming the nearest observed threat distance.

**Numbers**

- `safe_distance` = 12 tiles — `verified_script`
- `max_retreat_distance` = 30 squares — `verified_script`
- `retreat_ring_radius` = 15 squares — `verified_script`
- `max_retreat_legs` = 8 legs — `verified_script`

**Proven by:** `tests/unit/test_avoid_mission.py`

### `combat_assisted_unverified`

**Status:** `unverified` · **Source:** `code` — `packages/pz_agent_core/src/pz_agent_core/combat/policy.py::assess_engagement` · **Build:** 42 · **Risk:** P4

Assisted combat exists in code and is unverified live: four bounded P4 actions (combat.equip_best, shove, engage — one attack window per command — retreat) and the engage_single_zombie mission ride the combat_assist capability, experimental until a live shove confirms the attack entry points.

- **Goals:** `avoid_threat`, `engage_single_zombie`
- **Nearby:** `zombie`
- **Observed inputs:** `nearby.zombies[].distance`, `nearby.zombies[].chasing`, `nearby.zombies[].state`, `player.stats.endurance`, `player.stats.panic`, `player.stats.health`, `player.wounds[]`, `inventory.items[].weapon`

**Preconditions**

- An explicit user-submitted engage_single_zombie goal or combat tool call: nothing on the agent's own initiative reaches these actions
- combat_assist usable on this install: on an unverified build every combat command is withheld or refused, never attempted

**Actions**

- `combat.equip_best`
- `combat.shove`
- `combat.engage`
- `combat.retreat`

**Decision** — assess_engagement re-reads group count, endurance, panic, injury and weapon condition from the current observation before every window and refuses with a closed token; the mission switches to mandatory retreat on any deterioration between windows, and each window is its own command, so the safety stop and reflex guard interrupt between them; autonomous_attack keeps its unsupported ceiling.

**Postconditions**

- The following observation reports the target down, honestly gone under the engage absence rule, or further away — the swing itself is never evidence

**Fallback** — A refusal or a window closing with the target standing ends typed (POLICY_DENIED, PRECONDITION_FAILED, POSTCONDITION_FAILED) with observed numbers as evidence; deterioration is answered by the mandatory retreat, never another window.

**Proven by:** nothing — which is why the status says so.

