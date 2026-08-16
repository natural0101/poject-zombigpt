# Progress

The handover point between work sessions: read it first, update it last.

> **2026-08-10, the game-agent directive.** The first live session
> (2026-08-08, Build 42.20.2) both confirmed the design — the mod loaded,
> published structured observation, moved the character, opened a door — and
> produced thirteen concrete defect findings, recorded in
> [`GAME_API_VERIFICATION.md`](GAME_API_VERIFICATION.md). The product goal has
> been raised from "a set of MCP commands" to "an agent you can tell
> «облутай квартиру»". The work is cut into reviewable epics; each lands as
> its own PR and is not called done without live evidence:
>
> | Epic | Status |
> | --- | --- |
> | `P0-build42-live-compat` — the 13 live-confirmed fixes | **done here, awaits live** — PR #1 |
> | `P0-windows-ipc-arm-recovery` — single-producer queues, pointer contention, two-phase arm, action status/await tools | **done here, awaits live** — stacked on PR #1; latency p95 instrumentation still open |
> | `P1-doors-navigation` — door state/actions, local navigation executor | **done here, awaits live** — doors observable/operable, `allow_doors` real, `pz-agent latency`, `navigate_to` goal on a deterministic A* executor over a bounded local map (no LLM per square); remote-RPC wire for `navigate_to` deliberately deferred |
> | `P1-loot-area` — container primitives + `loot_area` goal | **done here, awaits live** — LIST args, `inventory.transfer_batch` with honest partial stops, container memory fed, rooms/buildings/corpses observed, deterministic loot policy, `loot_area` mission with provable termination and honest report; deferred: transfer-from-corpse (needs a new ref surface), vehicle/floor containers, voice speakability of `loot_area` |
> | `P2-continuous-goal-controller` — long-lived goals, needs arbitration | **done here, awaits live** — goal persistence across restart; `return_home`/`explore_area`; phase/paused/report surface; the mandatory hunger chain deterministic; `treat_wounds`/`rest_until`/`sleep_until_rested`; needs-preemption (suspend/resume with stopped budgets, edge-triggered arbiter, bounded ledger); threat-aware routing + `avoid_threat` — 13 kinds, 10 deterministic |
> | `P3-survival-knowledge` — machine-readable gameplay knowledge base | **wave 1 done here** — 50-rule corpus (honesty-gated loader, schema, one-file-per-domain), bounded retrieval into the planner prompt with UNVERIFIED markers, three generated docs with a byte-drift gate; open: the six unfilled domains (crafting/building/clothing/literature/utilities/inventory), live promotion of rule statuses |
> | `P4-assisted-combat` — bounded defensive combat under `combat_assist` | **done here, awaits live** — 4 actions (`equip_best`/`shove`/`engage`/`retreat`), one bounded engage window (≤3 swings, 4s wall), sidecar `CombatPolicy` refusals (group/endurance/panic/health/weapon), parameterless `engage_single_zombie` (14th kind, ≤4 windows), zombie tri-state + weapon condition observed; `combat_assist` EXPERIMENTAL until a live shove, `autonomous_attack` untouched and pinned unsupported; arbiter/initiative pinned to never mint combat |
> | `P4-autonomous-combat-live-gated` | todo — not to be built without live certification of `combat_assist` first |
> | `P5-crafting-building` | **both waves done here, awaits live** — crafting: `crafting.inspect` (published) + `crafting.craft` (EXPERIMENTAL capability, withheld), one command one item, `craft_item` on a bounded mission, P3→P4 by recipe; building: `building.inspect` (published) + `building.build` (**P4 flat**, EXPERIMENTAL capability), `build_structure` at most one attempt, typed refusals `SQUARE_OCCUPIED` / `RECIPE_MATERIALS_MISSING` / **`WOULD_TRAP_PLAYER`** (bounded flood fill; an unreadable map refuses rather than passes. A previous revision of this row claimed that was the *only* outcome against the shipped mod; **that was wrong and is retracted** — `ObserveModel.mergeNearby` folds the square entries into `nearby.objects`, which is exactly where `read_window` scans, so the enclosure check runs on real squares. See the retraction at the end of the stabilize row for how the checker fooled itself), no demolition action by design, arbiter/planner/initiative all pinned unable to mint it; the mod now describes a bounded square window; `learn_recipe` repaired; S21/S22 give both capabilities a live route. Open: **live promotion is blocked by design** — an `experimental` capability cannot be confirmed by a run the engine refuses to issue (documented, affects five capabilities); the five remaining knowledge domains |
> | `UX-one-command-play-and-docs` — one-command launch, goal CLI | **wave 1 done here, awaits live** — `pz-agent play` (validate → start → bounded wait for the game → arm confirmed by the game's own heartbeat, refuses in front of a panic latch, never touches the game process), `goal submit/status/cancel` over the same Core RPC link the MCP server and voice use (no `pause`/`resume` by design), `status --watch` HUD with the goal queue and an honest `unreachable`; the RPC codec tail is closed (suspension fields, `target_endurance`/`hours`, `progress`/`paused`/`report`) and `schemas/goal.schema.json` declares the four suspension keys; open: `goal submit` in a user-facing walkthrough, voice speakability of the newly wired kinds |
> | `stabilize/arm-session-confirmation` — the P0 stabilization pass | **35 reproduced defects fixed, awaits live** — seventeen on the sidecar, seventeen on the mod (the side every 2026-08-08 live finding came from): one family: evidence read without checking whose it was or when it was written. False success (`play` armed by another session's heartbeat; PZD010 active from an undated read; a future-stamped heartbeat fresh for the whole skew; combat reporting a kill it did not make; `avoid_threat` reading a missing nearby tier as safety; `status` printing a silent heartbeat as the state now), stale identity (snapshot sequences compared across sessions — the sidecar went blind after a game session change; a fresh reader adopting the dead session's slots; arm request and pid record stamped ahead of the clock), never terminal (a disarm countermanding nothing at the game; a suspended goal stranded past every timer; a mission wedged on an evicted step record; a journal renumbering silently). On the mod: a command polled for ever against a stopped clock; a raise in admission leaving a command with no ack at all and every redelivery classified a duplicate; an argument bound that raised at dispatch instead of refusing at load; a replay cache with no session dimension answering a live command with a dead session's `succeeded`; a closed session reopenable by re-presenting its document; the armed state surviving a session swap so a second sidecar inherited authority it never asked for; an unread body indistinguishable from an unhurt one; a nearby scan reporting an empty world it could not read; a rest succeeded with no departure reading; a bandage verified on a dressing the wound already wore. Each fix red-first; no P5, no refactor. Open and named, not fixed: the mission-cap eviction path banks a report without ending its goal. A final sweep of the tri-state rule itself found five more, the sharpest safety-critical: a zombie scan that could not run published an empty list, the danger floor read that as NONE, and an armed AUTONOMOUS agent was cleared to work on a count nobody took — the floor now answers HIGH with a `zombies_unknown` counter beside it. Two further flattenings (`player.alive`, `player.moodles`) were examined and left alone: every consumer refuses or proposes less, so an edit would have fixed nothing. The last sweep closed the neighbour that sweep had flagged and left: the danger floor is written only at the end of `Observe.context`, so a tick failing before that point leaves the old reading standing while the mod keeps heartbeating — reproduced with `getCore` removed, twelve ticks over sixty seconds and two chasing zombies, `mayStart("consume.eat")` answering `POSTCONDITION_MET` on a minute-old `none`; the floor now carries the clock that measured it and `mayStart` refuses a mutating action past 30 s with `PRECONDITION_FAILED` naming the missing measurement. And the pass's own evidence had the same defect: an earlier hand-merge put the zombie-scan group *after* `Harness.finish`, which calls `os.exit`, so eighty-eight assertions covering the safety-critical fix were present, named in this ledger and never executed — `test_observe` now runs 234 instead of 146. A seven-way audit then asked one question of every mission family and the core navigation paths: can a mission reach a state it never leaves without its goal ending? No family leaks — `GoalQueue.tick()` expires an abandoned active goal on `max_wall_ms` and `runtime.tick()` calls it unconditionally before any acting, `deadline_ms` being non-nil for every ACTIVE record — but the audit surfaced a silence that is not a leak: all six `_submit_*_step` handlers swallowed the channel's `LoopError` on a comment borrowed from the journey path ("the mission decides again from the next observation; its own bounds cap how often this can repeat"), which is false for a mission, because it sets `_pending_action` when it *emits* the step and only a terminal result clears it. A step the channel never admitted therefore wedged the mission into declining every later tick, and the goal ended `ACTION_TIMEOUT` at its wall budget — up to fifteen minutes for loot — naming a timeout instead of the refusal. Reachable two ways `ActionChannel.submit` names itself: the MCP router filling the channel from its own thread between the capacity check and the call, or a reused idempotency key after a restart. All six now end the goal `CAPABILITY_UNAVAILABLE`/`UNADMITTED_STEP_DETAIL`, as `fb8539a` already did for an evicted record; the journey handler is unchanged, because a journey really can replan. Two ledger items were verified rather than edited, neither being turnable red: arming on a **never**-measured danger floor is unreachable for mutating commands (the engine will not dispatch without a strictly newer observation, the mod publishes only after `setDanger`, and `dangerFloor` returns a `DANGER.*` constant on every path including `HIGH` for an unscanned world), and `_enforce_cap` evicting a live drive needs a fifth drive of one kind while `MAX_TRACKED_* = DEFAULT_MAX_OPEN = 4` and `app.py` overrides neither. A second audit then hunted the shape that had hidden the mission wedge for four passes -- a comment asserting a guarantee provided by other code -- across seven areas: thirteen candidates, three refuted on the spot, every survivor re-checked by hand before any edit, and **no behaviour changed**. Four were false. The sharpest is a gap, not just prose: `movement.move_near` refuses `object` references on both sides of the seam, justified by the claim that no scan mints one, but since `bf92ee2` the observer mints exactly one -- a door's, from its `object_index` -- and `doors.py` requires that kind for the `door_ref` it resolves out of the same `nearby` block, so **nothing walks the character up to a door**. The kinds were left alone and the gap recorded in `LIMITATIONS.md` beside the missing square tier, because widening a contract on both sides to no observable effect, while the destination square a walk resolves against is still absent, is a change nobody could confirm. `inventory.py` carried the mirror error (`move_near` does take a container reference). The fourth is in safety code: the reflex block rung called itself non-redundant because the mod fills `safety.danger_level` "from a value it never computes" -- it computes it, in the very `dangerFloor` this branch gave a measurement clock; the rung is still not redundant, for a true reason. The audit's remaining survivors were then verified by hand rather than taken on an agent's word, and three more were false, two in safety code. **`ReflexGuard`'s vulnerable-action rung has never fired**: `DEFAULT_VULNERABLE_ACTIONS` is matched against `ActionState.type`, and the shipped mod never fills it -- the observation's action block is `Ownership.describe`'s table, which has no `action_type`, the one field `ObserveModel.action` reads into `type` -- so §17.2's "interrupt a read or a meal when a zombie is near" is dead code; bounded rather than absent, because the flee rung above ignores the action type, so what is lost is the earlier reaction at `interrupt_at`. And **going stale does not disarm the mod**: `sidecarStale` is read in three places that only refuse, `Safety.disarm` comes only from a command, a new session or a panic stop, yet two sidecar sites promised a "stale-sidecar disarm" backstop -- one of them in the notice an operator reads. The residue is a stale mode reading, not a running agent, since `mayStart` refuses all but stop/disarm/cancel while stale. Both recorded in `LIMITATIONS.md`; neither behaviour touched, because both fixes live in safety-critical mod code no test here can exercise. A third, `combat_mission`'s seal, named a refused-admission history that cannot reach the line. The audit's last six survivors were then verified and corrected too -- all false, none needing a behaviour change. Two mattered: `ObserveModel.dangerFloor` claims to read the observation's fields and to count zombies on another floor as present but never closing, and does neither -- its caller passes the raw reader table with flat `x`/`y`/`z` while the floor test reads `zombie.position.z`, so every zombie counts as same-floor; the error runs toward caution, so the docstring was fixed and the code deliberately was not, since reading the flat `z` would make a safety guard less conservative on static reasoning alone. And the wound reference, documented as having no cross-language format, is split by `policy.medical.wound_body_part` for the body part -- a layout change would have silently disabled bandaging by location. The others: `Counters:reset` has no production caller, so sequence numbers are game-session scoped, not handshake scoped; `PZAgent.Json` escapes a corrupt byte rather than refusing it; and `autonomy.py` twice said "nothing in the protocol's action set" rests or treats a character, when `survival.rest`, `survival.sleep` and `medical.bandage` have been there since P3. Both autonomy omissions stand, now for true reasons. Both new dead gates were then added to `tests/contract/test_gates_without_producers.py`, which had three rows, all found by accident; these two were found on purpose, and each pattern was checked in both directions -- no match against the mod today, a match when a plausible producer is spliced in -- with the `action_type` window measured (return table ends at 784 characters, nearest unrelated occurrence at 2296) rather than guessed. Five rows now, two safety-relevant. A deliberate sweep for the dead-gate class then found the same shape one layer below the square tier: **the item-detail tier speaks two vocabularies**. Measured, not estimated -- `food` 22 keys read against 8 sent with 6 agreeing, `literature` 11 against 5 with 2, `fluid` 16 against 3 with 1 -- and `ObserveModel.domain` passes names through verbatim, so the sharpest cases are one fact under two names (`pages` vs `pages_total`, `amount`/`capacity` vs `remaining_units`/`capacity_units`, a boolean `rotten` against a `freshness == "rotten"` test). Nothing errors because every reader defaults a missing key, so `FoodView.is_rotten` is always false and the world reads uniformly bland. `poisonous` and `tainted` do cross, so the sharp hazards are still refused; what is lost is rot, portions, pages and alcohol. Recorded in `LIMITATIONS.md`, not repaired: choosing which side renames is a cross-language contract decision whose only real test is a live game. Verifying the sweep's remaining claims by hand then produced the third gap of the same shape, and the largest since the square tier: **a world container can be named but never resolved**. `InventoryView.container` searches `inventory.containers` alone and `resolve_container` refuses anything outside it, while the mod's inventory has exactly two roots -- main and worn, with carried nested inside items -- and no path adds a nearby crate. `buildObject` does mint the crate a container reference, so a planner sees it and names it; it just points into a list the crate was never added to, so `container.inspect` refuses `INVALID_REF` at its precondition and `inventory.transfer` refuses its source the same way. With the missing square tier above it, `loot_area` is blocked twice: cannot walk to the crate, could not open it standing there. Recorded in `LIMITATIONS.md` and added to the dead-gate ledger (six rows now) with a pattern checked both ways. The sweep's last three claims were then checked by hand: `ItemView.extra["weapon"]` is real (combat policy reads a weapon block the mod never builds, while the mod puts the condition in the player's stats where nothing reads it -- safe direction, since unreadable makes the policy refuse, and behind experimental `combat_assist` anyway); `player.present == False` is a **benign** dead gate worth a row so nobody mistakes the gate for the mechanism (with no character the mod publishes no observation at all, which the engine already handles); and `chain.on_person == False` was not counted, being a consequence of the world-container row rather than an independent root. Eight ledger rows, three of them one root -- the two sides spelling the same fact differently. The seam itself is then checked mechanically rather than field by field: `tests/contract/test_item_domain_vocabularies.py` re-derives both vocabularies from source, re-derives the counts `LIMITATIONS.md` quotes, and pins the exact set of keys the sidecar decides on without a producer, so a new mismatch fails a test instead of waiting to be found by hand. It caught its own author on the first run -- the `fluid` set was written from memory and wrong in three keys -- and the derivation sharpened a safety claim: each hazard key crosses in exactly one block (`poisonous` in `food`, `tainted` in `fluid`), so poisoned food and tainted water are refused but the crossed pairs read false. The same check was then pointed at the player's open stats map, expecting the item blocks over again -- and it is **clean**: every stat the sidecar reads is one `Observe.playerStats` sends, `observe.wounds_unknown` has its producer in ObserveModel's limit block, and nothing reads as a default for ever; the emptiness is now asserted, so the disease stays confined to the item-detail blocks. Reading the mod also corrected the `ItemView.extra["weapon"]` row written a commit earlier: the mod puts the weapon's wear in the stats map *deliberately*, saying why ("the item tier has no condition field in the schema") and refusing to fabricate one, so it is one bridge never built rather than two vocabularies drifting. The rest of the seam was then checked the same way, and **the divergence is local rather than the seam's nature**: every structural tier agrees exactly -- the item's own fields (12 keys), the container's (7), the zombie's (6), key for key, with the zombie block keeping `visible`/`chasing`/`state` tri-state on both sides. The three that diverged are precisely those passed through as raw `JsonDict` where nothing forced agreement, which is also why the schema declares them as objects and constrains no properties. Wherever a typed dataclass faces an explicit Lua table the two agree, so the repair is bounded -- give `food`, `literature` and `fluid` the treatment the other tiers already have, plus the one unbuilt bridge for the weapon's condition -- and still only confirmable live. An inventory of the 47 contract tests then placed all of this: agreement is already machine-checked for adapter arguments, capability declarations, capability evidence, the engine API inventory, the MCP tool surface, four wire schemas and the documented CLI -- and the one seam with **no** such check was the observation document's field vocabularies, which is exactly where the eight dead gates accumulated. The map now lives in `tests/contract/__init__.py`, which was empty, so the next person looks there before writing a duplicate checker as this branch once did. The handoff the game machine reads was then found to have fallen behind: `LOCAL_GAME_HANDOFF.md` §4 still opened with "three parts of the sidecar are wired to a mod that cannot drive them" while the ledger had held eight for several commits, and the three most consequential findings of this branch were missing from the one document a live session is planned from -- the same stale-document defect this pass exists to remove, aimed at the handoff. All eight are now listed with what each looks like from the chair, and the priority list gained the one experiment that would change what the agent may do: start a read or a meal, let a zombie approach, confirm the character stops at all, because §17.2's earlier interrupt is dead while the flee rung above it is not. Recorded, not closed: `nearbyObjects`' silent failure is a wart — all six sidecar consumers of `nearby.objects` abstain on an empty list, no local map banks it as knowledge, no goal completes on the absence. **Reproduced, unfixed, and larger than a stabilization fix: the agent cannot walk.** `movement.move_to`, `move_near`, `world.inspect` and the local map all find the destination square by scanning `nearby.objects` for `kind == "square"`, and the mod emits no such entry — object kinds come from the container type, `getObjectName` or the literal `corpse`, `Refs.KIND.SQUARE` only mints reference strings, `nearbyFields` has no square tier, and `"loaded"`/`"blocked"` appear nowhere in the mod's Lua. A one-square walk east refuses `TARGET_NOT_LOADED`; every navigation leg goes through it. Invisible until now because the sidecar's own fixtures mint the square objects the mod never sends — the same green-that-does-not-cover shape as the dead test group, across a contract boundary. Relaxing the sidecar is the forbidden direction. Building the mod's half was attempted and **rejected under adversarial verification**: it fixed walking by breaking drinking (square entries collide with the `Refs.buildSquare` refs `buildObject` already mints for world objects, so `nearby_object` resolved a sink to the ground under it and `consume.drink_source` went accepted -> `NO_SAFE_DRINK`), it published a glass wall as open ground on a build exposing `isSolid` but not `isSolidTrans`, and it starved the planner's 24-slot compact view one layer below the fix (a furnished room lost nine real objects including a door; a warehouse aisle delivered zero squares). The three blockers are recorded in `LIMITATIONS.md` with their measurements as the specification for a real fix; the attempt is not in the branch. One consequence of that reference scheme was fixed independently, because it bites without any tier: a square reference denotes the place **and everything on it** (`source_ref` is a `RefKind.SQUARE` by contract and the mod reads it back the same way), the mod scans several objects per square by design, and `consume.drink_source` asked only `nearby_object` — the first match — so a tree scanned before a sink had the square refused `NO_SAFE_DRINK` with a sink standing on it. It now asks every object carrying that reference, and a square with nothing watery on it is still refused. The mirror branch was then read rather than described — six commits, 19 097 insertions, all P5 crafting and building except one file, and that one is the first of the vocabulary rows to be **repaired**: `unread_recipes` was read through a defaulting helper and published by no mod reader, so every magazine reported zero and `_filter_recipes` refused all of them for "nothing new in it" — `LEARN_RECIPE` could not be served by any item on any build, and the refusal blamed the magazine instead of the missing reader. Carried over red-first (four of its tests fail here before the fix, pass after), it depends on no P5 code and `literature.py` had not been touched on `dev` since the branches parted. Absent is now *unknown*: the recipe goal refuses saying the count could not be read, a real positive count beats an unreadable one, and a boredom goal still scores the item on what *was* readable. This is the pattern for the rest of that table — answer a missing key with unknown and let each consumer say what unknown means — which recovers no fact but stops the sidecar stating one it never measured. The ledger caught the change and had to be fixed too: its extractor saw only the `read_*` helpers, so a field going tri-state (which requires bypassing them) read as a *dropped* key, and left alone it would have retired one row per honest fix; membership now means "read without a producer", which had been the same thing as "decided on a default" and no longer is. The crafting/building wave was then **merged into the line** rather than left on its branch: four conflicts, each resolved by keeping both sides, and two of them would otherwise have dropped a property this branch added — the mod's `nearby` error capture (an unreachable world must cost the section, not publish an empty one the threat assessment reads as no zombies) and the three-state `_Pending`. That second one was not cosmetic: the arriving craft and build handlers read `_collect_mission_pending` as a boolean, every enum member is truthy, and both missions would have returned `None` on every tick for ever — two dead goal kinds, reported by `mypy --strict` as two unreachable statements. Their submit paths carried the original wedge too, same borrowed comment and same falsity, and both now end the goal typed. Two ledger extractors had to be repaired in the same pass, both blind in the same direction — *the seam getting better read as the seam disappearing*: the gate ledger matched `kind = "square"` inside the new section's own **comment** and would have retired the largest gap in the build on a sentence, while `movement.py` still scans `nearby.objects` and the wave publishes a separate `nearby.squares` tier, so **the agent still cannot walk** and the row stands; and `_mod_keys` pinned readers to exactly `(item)`, so `itemLiterature(item, isKnown)` — grown a second argument precisely in order to answer honestly — read as a reader that had vanished. What the merge does close is the literature block: 6 of 6 keys the mod sends are now read, the first of the three divergent blocks repaired and the worked example for `food` and `fluid`. What it opens is a fourth free-form block, `crafting`, riding `ItemView.extra` with nothing forcing the two vocabularies to agree — recorded before it drifts rather than after. **Then the branch's largest standing claim was found to be false, by its own ledger, and retracted: the agent can walk.** `ObserveModel.buildSquare` mints `kind = ObserveModel.SQUARE_KIND` (`SQUARE_KIND = "square"`) with a square reference, a position and a semantics list, and `mergeNearby` folds the bounded square list into `objects` before `ObserveModel.nearby` returns — so `movement._find_square` and `policy/building.read_window` both find what they scan for, and "`build_structure` refuses every placement" was wrong for the same reason. The producer arrived with the crafting/building wave and was missed twice, because the ledger row searched the mod for a *literal* `kind = "square"` while the mod declares the token once as a constant and refers to it; the row's both-directions check spliced in a producer written the same literal way the pattern was, so it tested the pattern against itself. The ledger now carries a second positive control asserting the mod's own idiom is visible — the semantics it demonstrably sends, and the `SQUARE_KIND` declaration — because a checker blind to the producer's spelling cannot report an absence. What survives is narrower and is about **where** a semantic lives: three of movement's five square tokens cross (`loaded`, `blocked`, `drop`) plus `occupied` for the build policy, while `closed_window` and `stairs` are read off the square and put by the mod on the *object* standing there, on purpose ("emitting them here would be the same fact in two places, free to disagree"). So a floor-changing move always refuses `PATH_NOT_FOUND` — toward caution — and a closed-window square is refused as `blocked` rather than under its own name. `LIMITATIONS.md`, the report's §9 and §10, and the ledger were all corrected together; the world-container gap is now the only one of the three that still costs a whole goal kind |

> **2026-08-08, the today-finalization push.** The integration branch of
> record is `rescue/today-finalization`; `docs/control/TODAY_SWARM.yaml`,
> `TODAY_EVIDENCE.md` and `MERGE_QUEUE.json` carry the swarm's ledger. Every
> remotely-completable band reads 100.0 (`scripts/master_report.py`: 74.3%
> overall, 2305/3104), the tree is green on both platforms at `ff63b38`, and
> that Windows run certified `v1.0.0-rc1` — including the packaged pair
> completing an MCP initialize over the real RPC link. What remains is
> LIVE_GAME_VALIDATION (599) and FINAL_RELEASE (200), which need a machine
> with Project Zomboid; `docs/LOCAL_AGENT_PROMPT.md` is the handoff.

> **This file is no longer where progress is decided.** The plan of record is
> [`docs/control/MASTER_PLAN.yaml`](https://github.com/natural0101/poject-zombigpt/blob/main/docs/control/MASTER_PLAN.yaml), and the current figure
> comes from `.venv/bin/python scripts/master_report.py`, never from a number
> written here. What follows is the closed T001–T030 graph, kept as history,
> and the defect record — which is the part still worth reading.

> **The live-test runner reported success having run nothing.**
> `pz-agent live-test run --scenario ""` — an unexpanded shell variable — printed
> `nothing to run: every scenario is PASS.` and exited 0 with all twenty-two
> `NOT_RUN`. `resolve()` dropped blank tokens and then answered an explicit
> request with an empty selection, which `_selection`'s `if only:` could not tell
> apart from "nothing left to do". It refuses now, and the unknown-scenario
> message counts the catalogue instead of saying "twenty-two". This is the
> command that produces the evidence for all 84 live tasks, so a green exit from
> it is the most expensive false success in the tree;
> `tests/contract/test_live_test_selection.py` holds both halves.

> **A safety postcondition could pass on a character nobody read.** Ten `Check`
> members claim that none of them succeeds on an unobserved value; driving the
> real `evaluate` over all ten found nine holding and `UNCHANGED` not. It decided
> presence by key alone, so `player.health` published as `null` in both snapshots
> was equal to itself and `S05_BLOCKED_PATH`'s *"the character took no damage"*
> passed — the one postcondition in the catalogue that uses `UNCHANGED`, a safety
> statement, feeding the manifest `--release` reads. The observation path already
> had the right rule (`_is_non_empty`); the snapshot path now uses it too, and
> not truthiness, because `0` and `False` are readings.
> `tests/unit/test_postcondition_needs_a_reading.py` holds both directions.

> **The release bar could certify `v1.0.0` on evidence from other code.**
> `--release` enforced *"evidence from a different build is evidence about that
> build"* on the product version — a literal that does not move for hundreds of
> commits — and compared no commit at all: the manifest's own was printed and
> never checked, and the per-scenario ones were missing from the manifest, though
> `result.json` has always carried them. Reachable by design: the ledger derives
> *PASS if any attempt passed*, and `E14-M04` expects fixes and re-runs mid
> campaign, so a week of live testing naturally ends with passes spread across
> commits. The manifest now carries each scenario's commit and `evidence.commit`
> refuses a disagreement, naming what to re-run.

> **The release bar never checked which game the evidence came from.** The
> runner's `UNOBSERVED_BUILD` states the rule — *"evidence that cannot name the
> game it ran against closes nothing"* — and nothing enforced it: `game_build`
> appeared in `check_release.py` once, in a printed detail, and no code compared
> it to `SUPPORTED_BUILDS` (`['42.20']`). Measured: 21 of 22 scenarios declare no
> postcondition about the build and record `(not observed)`; the 22nd asks only
> that it be `observed`, so `"41.78"` and `"banana"` both pass. `evidence.game_build`
> now refuses an unnamed, unobserved or unsupported build, and imports the
> marker from the runner rather than re-spelling it.

> **Two of the five versions the evidence records were read by nobody.** The
> changelog's own rule is that five versions move independently; the manifest
> records three and `--release` compared one. `mod_version` describes the Lua
> that ran inside the game and did the observing, `schema_version` the shape of
> the documents postconditions read by dotted path — `evidence.components` now
> compares both against the checkout. Found by enumerating every manifest key
> against every key the gate reads, after two consecutive fixes of the same
> shape; the same enumeration is now a test, and it also records which of the
> remaining keys honestly need nothing.

> **The gate's headline was asserted from the checkout, not read off the
> artefact.** `CERTIFIED v1.0.0-rc1` came from `build_rc.RELEASE_VERSION`; the
> archive's own `release_version` was recorded and never read, in a file whose
> rule is *"A claim is checked against the artefact, never accepted from it."*
> `archive.release` closes it. Smaller than the preceding entries and said so:
> D-012 puts the gate in the workflow that built, so it cannot fire on the real
> path — it is a tightening, not a reachable false success. Found by enumerating
> the archive manifest's keys against the `--rc` path, the same method as the
> evidence manifest; the enumeration's first pass gave a false positive by
> scanning the whole file instead of the archive functions.

**Legend** — `done` implementation + tests + docs complete and `scripts/check.sh`
green · `wip` in progress · `todo` not started · `live` blocked on a step that
physically requires a running game.

The T001–T030 graph: 28 of 30 closed; T029 and T030 are blocked on a live game,
not deferred. That measurement was 3677 Python tests and 2875 Lua assertions
across 26 suites with mypy strict over 271 files, taken under Python 3.11.15,
the only interpreter with the suite installed in this container. It is a
historical reading and has not been re-taken; the tree has grown by roughly
1500 tests since. CI declares a 3.11/3.12 matrix and both legs are now observed
green, which was configuration rather than a result when that line was written.
See FINAL_IMPLEMENTATION_REPORT.md — noting that it, too, predates the master
plan and describes the release candidate as it stood then.

Work beyond the original graph is complete on `feature/playable-agent-1.0`:
the protocol grew from fifteen actions to twenty-two, the mod gained a real
command executor and seventeen game adapters, and the sidecar gained the
adapters, providers, live-test harness and Windows release candidate that go
with them. See [the playable-agent section](#the-playable-agent-branch) below,
and [`LOCAL_GAME_HANDOFF.md`](LOCAL_GAME_HANDOFF.md) for what still needs a
machine with the game on it.

**Thirty-two defects that branch found are worth reading before any further work**,
because they are one family and the family is not closed. Every subsystem was
written, tested and green; what nothing tested was whether the subsystems were
*connected*.

| # | The defect | What it cost |
| --- | --- | --- |
| 1 | Adapters published under `name`; the dispatcher reads `action` | Thirteen of sixteen game actions unreachable |
| 2 | The two halves of the wire named arguments differently | Every movement and transfer command refused |
| 3 | `move_near` demanded a reference kind the mod never mints | The action could not be called from a real observation |
| 4 | `build_loop` passed no capability check, so it kept `deny_capability` | The assembled sidecar refused *every* action |
| 5 | `build_loop` passes no planner | Autonomous mode proposes nothing |
| 6 | Nothing mapped a backup to the save id the mod reports | Autonomy asked instead of acting; closed by recording the id at backup time |
| 7 | The memory store was complete and connected to nothing | `reserves_item` always answered False, so §7.9 rested on tag rules alone, and no home point could exist |
| 8 | `pz_agent_voice` was imported by nothing and had no entry point | Russian voice control was complete, tested, and impossible to start |
| 9 | The mod could drink from a sink; the sidecar had no argument for it, and the path it did have ran under the wrong capability | Two faults in one place: a working mod feature unreachable from Python, *and* `drink_world_source` — which §12.4 caps at `experimental` — reachable through `drink_carried`, which a scan verifies |
| 32 | **AGENTS.md and CONTRIBUTING.md both said CI rejects an empty exception handler; no such check existed** | The rule AGENTS.md declares binding and CONTRIBUTING lists first, unenforced for exactly the handler style this codebase writes — so nobody reviews for one. It cannot be scanned honestly either: the tree has an `except (OSError, UnicodeDecodeError): pass` that falls through to a second lookup and several `except OSError: return` that deliberately trade a diagnostic for a session in flight. The untyped `except:` *can* be scanned and now is; the swallow is stated as a review rule with the reason. `check_forbidden.py` had no tests at all — it does now, including both directions of what the documents promise |
| 31 | **The standalone installer in `installer/` is reachable from nothing** | 927 tested lines plus a guide whose title reads as the install instructions, in no shipped artefact, run by nothing, and read as *the* install instructions by anyone who opens the directory. The shipped path is `install.bat` → `pz-agent install-mod`. Kept — it is the only path that works before anything is installed — and both the guide and the module now open by saying which of the three cases they are for |
| 30 | **`voice run` wrote nothing to `logs/`** | Defect 18's shape, one package over. `docs/LOCAL_DEBUG_MAP.md` names `logs/` for both voice symptoms it lists, including «стоп» heard while the character kept going, and the companion had never written a byte there: its turn history and synthesiser failures sat in two bounded rings in a process that then exited, with `speech_failures` saying in its own docstring that they are kept because "the companion went quiet" is what a bundle cannot explain. It writes intents and outcomes, never transcripts |
| 29 | **`docs/QUICKSTART.md` told a new user to command the agent by voice** | Section 7 named two routes and one of them is refused: this build carries arm, disarm and stop from a second process and no channel carries a goal, so a spoken "eat something" gets «Не получилось.» The CLI's own `voice check` says so for any phrase; the quickstart did not |
| 28 | **`pz-agent start` printed an MCP config setting `PZ_AGENT_STATE_DIR`, which nothing reads** | The name occurred exactly once in the repository — in the literal that printed it. Meanwhile `configs/mcp/README.md` carries a section titled "Why `env` is empty" saying that naming an unread variable "would look like configuration and be decoration, and the first person to change it would spend an evening finding out that it does nothing", all three shipped configs carry `"env": {}`, and a test pinned that — over the checked-in files only, stopping exactly where the product started handing one out |
| 27 | **Seven links in the archive's own README resolved to nothing** | Defect 13's general case, left open by fixing the two instances. An operator on Windows has no repository, so a relative link is a file beside the one they are reading or nothing. `ARCHITECTURE.md` and `PROTOCOL.md` ship now; the links about building the project became absolute. `tests/contract/test_archive_documents_resolve.py` builds the archive and follows every link in it |
| 26 | **`safety.panic_hotkey` had a validator, an error message and no consumer** | The mod binds DirectInput scancode 88 directly and reads no configuration, so any other value bound nothing — and this is the stop button. A user rebinding away from F12 (Steam's screenshot key, so there is a real reason to) was told "configuration is valid" and had bound nothing. Any value but `F12` is a hard error now, naming the two routes that do work; rebinding for real needs the mod to read a published keycode *and* a live run to prove the new key reaches the stop |
| 25 | **`game.install_dir` and `game.user_dir` were read by nothing** | `doctor`'s own remediation for `PZD001`, `TROUBLESHOOTING.md` for `PZD001` and `PZD003`, and `configs/mcp/README.md` all send a blocked user to set them. Those two failures brick every other command — a GOG or manual copy Steam does not list, a profile moved by OneDrive or `-cachedir` — and the escape hatch did nothing: the user got "configuration is valid" and the identical failure telling them to do what they had just done |
| 24 | **No configuration could produce `disabled_by_policy`** | The state existed, the mod guarded on it, `PermissionEngine` refused on it with a message written for a user, and `docs/COMPATIBILITY.md` — which ships in the Windows archive — listed it as "available, but configuration forbids it" three rows above its own warning that a panic stop cannot reach a sleeping character. There was no key to write, and unknown keys are hard errors. Closed by implementing the switch, the way defect 10 was |
| 23 | **The mod could never publish `experimental`** | `CapabilityRuntime` reads `adapter.experimental`; `Toolkit.declare` never carried the field, so it was read in one place and written in none. Two adapters carried comments saying "the probe caps this at experimental" and both published as ordinary unverified, while `docs/PROTOCOL.md` documents `capabilities.json` with an example showing a state its own writer could not emit |
| 22 | **`docs/TROUBLESHOOTING.md` sent a user to `pz-agent status --explain`** | No such flag. The paragraph also said the food thresholds are "in configuration" — `[safety]` holds four keys and none of them is one. Found by the new guard rather than by review |
| 21 | **`PRIVACY.md` documented data deletion under a command the CLI does not have** | It named a `memory` subcommand with a `--forget` flag. There is no such command; it is `remember forget`, which appeared in no document at all. A user reading the privacy policy to erase what the agent keeps about their save got exit 2 and a usage error |
| 20 | **`SECURITY.md` told a vulnerability reporter to redact with a flag that does not exist** | "Do **not** attach a raw support bundle to a public issue until you have checked it with `pz-agent logs --redact --verify`" — there is no `--redact`. The single gate between a reporter and an unredacted archive on a public issue was an instruction that exits 2 |
| 19 | **`TraceWriter` was constructed nowhere, so `pz-agent replay` had nothing to replay** | The same shape as 18, one layer in. `docs/QUICKSTART.md` printed `pz-agent replay <trace>` under "When something goes wrong", `logs --bundle` packed `traces/*.jsonl`, and `replay` was a shipped, parsed, documented command reading a format the product never produced. Closing it needed a seam rather than a call: the engine returns a *result* and never lets go of the command it sent, so `ActionEngine.on_dispatch` was added and the loop pairs the two. Writing the trace then exposed a second fault in the format itself — a rotation could leave an observation *diff* as the first line of the new file, and `replay_observations` refuses a diff with no baseline, so every long run's trace would have read back as unreplayable. Found by a test that rotates for real |
| 18 | **`DiagnosticLog` was constructed nowhere, so `logs/pz-agent.log` could not exist** | Written, tested, rotated, redacted, level-filtered — and never built outside the test suite. Nineteen of the twenty live scenarios name that file among the logs to collect and three name `pz-agent.jsonl`; `LOCAL_DEBUG_MAP.md` sends an operator to it; `pz-agent logs` reads it; the support bundle packs its directory. `live-test collect` reported "copied 0 file(s), skipped 15" — the honest answer, and the line read past twice before anyone asked why the sidecar's own log was among the missing |
| 17 | The support bundle's verifier flagged its own successful redaction | `credential_assignment` matched `api_key=<REDACTED>`, so `logs --bundle --verify` printed "REVIEW BEFORE SHARING" and exited 1 over a line whose secret had been correctly removed. Not a leak — the training of a habit that would let one through, in the one artefact designed to be attached to a public issue |
| 16 | `configs/mcp/README.md` documented one of the two refusals a client meets | It said `pz-agent-mcp` "exits with status 1" for missing services. On a plain install the SDK gate fires first and returns 3, with a message about a missing package rather than a missing sidecar — so a client author would have gone looking for the wrong cause on their first launch |
| 15 | **`pz-agent start` reported success on the strength of `Popen` returning** | A fork succeeding says nothing about whether the program ran. A sidecar that died on its first import left `start` printing "sidecar started as pid N" and exiting 0, `arm` then failing for reasons that named nothing, and `stop` reporting "no such process" — also exiting 0. Found by running the lifecycle, not by reading it |
| 14 | **`live-test run` never consulted the prepare record** | `prepare` verifies a test save is named and a backup *reads back*, then writes `prepare.json`. Nothing read it. The one check standing between twenty deliberately destructive scenarios and a main save produced a record nobody consulted |
| 13 | The release archive omitted the two documents its own shipped documents told the operator to open | `GAME_API_VERIFICATION.md` and `LOCAL_AGENT_PROMPT.md` were not in `DOC_NAMES`. Introduced by fixing defect 12: the correction pointed five documents at a file the archive did not carry. Found only by opening the ZIP | It returns six lines against 52 `requires_live` rows — about an eighth. The sentence was in `LOCAL_AGENT_PROMPT.md`, the file the agent starts from, so it would have checked six places and believed the surface covered |
| 11 | Five Lua adapters declared no capability, with comments saying no probe existed for them | Probes exist for all five. The action gate was never open — the mod enforces by required symbols, the sidecar by the ledger — but the mod's capability document named six capabilities where the system knows twelve, so five were absent from the report a person consults when something is refused. A census of all twenty-six registered adapters then found `DrinkSourceAdapter` registered, dispatched, exported, offered over MCP and **built by no test anywhere** — its refusals and postcondition had never run. It was the only one, and it was found by hand while chasing something else, so the census is now a standing check (`tests/contract/test_registered_adapters_are_tested.py`, non-vacuous and self-excluding) and the adapter's postcondition is covered. Auditing the rest of the reference resolutions then **closed the first of the three square-tier blockers**: every other site resolving a ref against `nearby.objects` asks a *position* question, not a property one (`move_near` x4, the critic's `_destination`), and position is shared by construction; the square lookups match on kind and position, never on the ref. A regression test pins the case that killed the tier attempt — a `kind="square"` entry listed ahead of a sink at the same ref, drink still accepted — and is verified non-vacuous. Two blockers remain: the partial solidity read, and the planner's 24-slot compact view. Auditing that compact view — the only picture a planner ever gets — then found the sharpest remaining false absence: the mod publishes eighteen `observe.*` counters in `player.stats` saying what it could not read, they arrive intact, and **nothing in the sidecar read one of them**, so a zombie scan that could not run reached the model as `zombie_count: 0, zombies_truncated: false` — the street is empty and the reading is complete, about a scan that never happened. The document now carries an `unread` block and `nearby.zombies_unscanned`, generic so the next counter is not silently dropped. The same sweep then found the deterministic half: the mod omits `chasing` when the build has no `getTarget` — deliberately, and the schema requires only `ref` and `distance` — and the parser read that absence as `chasing=False`. `_zombie_level`'s ladders differ by a full rung at every band, so a chaser at contact range was assessed as an unaware zombie, and the navigation executor skipped `CHASING_STEP_COST` around it. `chasing` is now tri-state end to end; the ladder and the route cost treat "could not say" as a possible chase; `chasing_count` stays a count of observed chasers with `chasing_unknown_count` beside it, so the spoken reason never claims chasers nobody saw. Sweeping that same question across every parser then found two more gates with no producer: `container.accessible` is **always true** (five sidecar refusals dead, a locked container presented as reachable) and `observation.full` is always true (three merge branches never run). Neither is fixable without an engine reader of the kind that sank the tier, so all three are now a ledger — `tests/contract/test_gates_without_producers.py` fails the day a producer appears, asking for the row to be moved. Finishing the `*_unknown` sweep on the deterministic side then found the last one: an empty wound list means both "nothing is wrong" and "the body could not be read" (`observe.wounds_unknown`), and three files cite "a missing `is_bleeding` never means 'not bleeding'" while arguing for other gates — the gate they name was the one not applying it. `assess_threat` skipped the bleeding floor and `combat.policy` answered `SHOVE_FIRST`, on a body nobody read; both now key on `PlayerState.wounds_unread`, with the spoken reason keeping observed and unread apart. `policy.autonomy` left alone: it proposes less, not more. The live handoff was then corrected: `LOCAL_GAME_HANDOFF.md` told its reader nothing about the walk outage, so a tester would have burned the session on movement scenarios that cannot pass and had no way to tell a structural gap from a broken install. It now leads with the three producerless gates and an ordered list of what a session is actually worth spending on, starting with the 52 engine symbols — `isSolid`/`isSolidTrans` are still not rows there, and confirming them turns the tier's blocker 2 from a guess into a fact. One loose end in the `unread` work was then closed: the fields reached the planner with no instruction attached, and an empty `zombies` list beside `zombie_count: 0` reads as an empty street to a language model whatever else the document says. `plan_instructions()` now states the rule — an empty list beside an unread flag means nobody looked — and forbids claiming absence in the summary for a reading never taken. Last: **CI had never run on this branch at all** — both workflows trigger on `epic/**` and friends but not `stabilize/**`, and PR #10 targets `epic/ux-one-command-play` rather than `main` or `dev`, so neither trigger fired and the PR shows zero check runs across 43 commits, while every STATUS entry recorded both platforms `PENDING` for a verdict that was never coming. `stabilize/**` added to both workflows so the Linux suite and the Windows package build actually run and `PENDING` means what it says — and the first verdict came back **GREEN on both platforms**, with the Windows job building and certifying `pz-agent-windows-v1.0.0-rc1.zip` (sha256 `9e84a4e5…` at that commit — later RCs have their own digests, and `docs/control/EVIDENCE_INDEX.md` names the current one; `CERTIFIED v1.0.0-rc1: 8 checks`, including both executables in `bin/`, which no container-built ZIP could satisfy). STATUS now records GREEN/GREEN and the RC as CURRENT at this commit; `live_game` stays `NOT_RUN` |
| 10 | **Multiplayer was documented as refused twice and refused nowhere** | `safety.allow_multiplayer` sat in `_advisories`, whose contract is "Never errors", carrying the sentence "multiplayer is refused at the handshake regardless of this setting". No such refusal existed in `packages/` or `pz-mod/`. The setting was the bypass it claimed not to be |

Every one was found by a test that crosses a seam rather than covering a unit,
and each of those tests now exists: `tests/lua/test_adapter_registry.lua`,
`tests/contract/test_adapter_args_agreement.py`,
`tests/contract/test_capability_evidence_agreement.py`,
`tests/contract/test_mcp_action_coverage.py`,
`tests/contract/test_sidecar_capability_wiring.py`,
`tests/contract/test_sidecar_planner_wiring.py`,
`tests/contract/test_backup_attribution.py`,
`tests/contract/test_sidecar_memory_wiring.py`,
`tests/contract/test_voice_wiring.py` and
`tests/contract/test_multiplayer_refusal.py` and
`tests/contract/test_capability_declaration_agreement.py`,
`tests/contract/test_game_api_inventory.py`,
`tests/contract/test_doctor_codes_documented.py`,
`tests/contract/test_sidecar_writes_its_log.py`,
`tests/contract/test_sidecar_writes_a_replayable_trace.py`,
`tests/contract/test_configured_game_paths.py`,
`tests/contract/test_disabled_capabilities.py` and
`tests/contract/test_documented_commands_parse.py` — the last of which is a
general guard rather than one defect's test: it puts every `pz-agent` command
line any shipped document prints through the real parser, and it found number 22
by itself.

Number ten is the one to read first, because it is not a wiring defect at all —
it is a documented safety guarantee that was never implemented. It is closed by
two gates: the configuration key is a hard error, and
`ActionEngine._multiplayer_abort` refuses every mutating command unless the mod
positively reported single player, with an absent reading refused exactly as
`true` is. `tests/contract/test_multiplayer_refusal.py` holds it, and both
halves were mutation-checked. Nobody has watched it refuse a real server.

Number nine was closed by splitting the action: `consume.drink_source` is its
own action with its own adapter on both sides, so the capability is checked by
the engine before the adapter is entered rather than inside it. The tests that
hold it are `test_the_two_drink_actions_do_not_share_a_capability` and
`test_the_world_source_adapter_will_not_verify_without_the_source_it_names`, and
`drink_world_source` moved out of that file's excuse list into its exercised
table — where every other probe has always had to be.

Each was mutation-checked rather than trusted: the wiring was removed and the
failures counted. A seam test that would not have failed is not evidence that
the seam holds.

The pattern is worth naming, because it will recur. A unit test written beside
the code it covers cannot fail for the reason these failed: both sides were
correct in isolation and the assumption connecting them was never stated
anywhere a test could read it. **The live run is the next seam of the same
kind**, and it is the only one that cannot be closed from here.

## Where the current work is tracked

**The plan of record is
[`docs/control/MASTER_PLAN.yaml`](https://github.com/natural0101/poject-zombigpt/blob/main/docs/control/MASTER_PLAN.yaml).** The T001–T030 graph
below is closed and is kept as history. Everything after it lives in the master
plan, and nothing else in this repository states how far along the project is.

**State at the last update (2026-08-08):** `main` is the working branch, and it
is green — CI and `windows package` both passed against `276b9d9`, the first
commit where every claim in `STATUS.json` describes the commit that carries it,
and the release candidate was built from it (run 31214158408). On that
foundation `scripts/verify_carryover.py` confirmed the accumulated work into
the plan by running each task's named regression test: weighted progress stands
at **59.66%** — after a criterion-coverage audit of the 75 heaviest claims
reopened 22 whose tests pass without observing their criteria (R-009), and
found that the shipped sidecar never served the Core RPC router (R-008,
critical — closed the same day: `pz-agent start` now serves the link over
the real loop; the goal channel is served for real the day after — a
client's goal reaches the loop's planner over the link — and the RPC
transport's every wait on the peer is bounded per family, the Windows
pipe asymmetry documented rather than denied). Before the audit the figure read 74.26%; the drop is the
audit working. Earlier it stood at 53.25%, with MCP_OPERABILITY, VOICE_OPERABILITY and the goal channel now
counted because their tests ran, not because their code exists. Still at zero
and honestly so: LIVE_GAME_VALIDATION (599 weight, needs a machine with the
game) and FINAL_RELEASE. Still open besides them: all 54 integration `CHECK`s,
E01-M01/M03 (baseline records, control instrumentation), most of E03 evidence
canonicalisation, E05-M02..M06 statuses, and E14/E15 entirely. The number to
trust is `scripts/master_report.py`'s, never this paragraph's.

The hundred-step model that used to be described here has been **retired**. It
counted steps and called one step one percentage point, which meant a paragraph
of documentation and a Project Zomboid scenario running on a real machine were
the same size — so `STEP 51/100` read as "half done" while several subsystems
had not been started. `docs/control/PLAN.md` is historical.

Retiring the model left its two scripts behind, and that had a cost that went
unnoticed until it was looked for. `STATUS.json` was regenerated in the new
shape — no `steps` key at all — while `scripts/progress_report.py` reads every
field through a `.get` with a default, so it printed a complete and entirely
false report: `PROGRESS: 0%`, `STEP: 1/100`, `STATUS: NOT_STARTED`, `RC
ARTIFACT: None`, `LIVE SCENARIOS: 0/20`, `EVIDENCE: 0 path(s)` — against a file
recording 73.31%, 400 of 484 tasks `PASS`, an archive identified by commit, run
and sha256, and a catalogue of 22 scenarios. `--write`, which
`docs/control/COMMAND_LOG.md` told an operator to run to "recount and store",
stored `overall_percent: 0` and six more zeroed keys beside the correct figure,
into the file whose own `$comment` forbids exactly that. `check_progress.py`
did refuse, but as `'steps' must be a list`, which reads as a corrupt file
rather than as the retired gate.

Both now ask `STATUS.json` which plan it describes and refuse when the answer
is not theirs, naming that plan and its successor. `STATUS.json` is therefore
**not** historical — it is the current control-plane record, written by
`scripts/reconcile_status.py`, and the pair above no longer pretends to read it.
`tests/unit/test_control_plane_reporters.py` runs all four scripts as
subprocesses and holds `master_report.py`'s printed figures against the recorded
ones, in both directions; nothing had run any of them before.

What replaced it:

- **Five levels.** `EPIC → MILESTONE → TASK → CHECK → EVIDENCE`. A `TASK` is one
  action. A `CHECK` is a claim about a milestone that no single task
  establishes — the integration claim, true of the whole and of none of the
  parts. `EVIDENCE` is a path that exists on disk; everything above it is a
  claim.
- **Weight, not count.** `progress = Σ weight(PASS) / Σ weight(all) × 100`,
  derived on every read. No percentage is stored anywhere, so none can go stale;
  `tests/unit/test_master_plan.py` asserts that the file contains no stored
  figure at all. Weights sit in validated, disjoint bands: `doc` 1–2, `audit`
  1–3, `portability` and `packaging` 3–5, `evidence` 4–6, `transport` 5–6,
  `integration` 7–8, `security` 7–9, `live` 9–10. Documentation, RPC transport,
  MCP end-to-end and live tests cannot share a weight — a rule the bands broke
  until `transport` and `integration` were narrowed apart.
- **An epic does not close because its tasks did.** Five conditions, all
  required: every task `PASS`, every check `PASS`, evidence present, the
  required workflow green, and a declared integration scenario that has run.
  Nineteen of twenty tasks is open. Every task passing with the check open is
  open, because that is the exact shape of the recurring defect in this
  repository — a subsystem complete, tested, green, and connected to nothing.
- **Seven metrics, reported separately**, because one number hides a subsystem
  at zero. Two of them are at zero right now, and the overall figure is not.

The tooling, all of it gates rather than reports:

- `scripts/build_master_plan.py` emits the YAML from the task definitions and
  refuses a weight outside its band. `--check` detects a hand-edited plan.
- `scripts/check_master_plan.py` — eleven refusals: a `PASS` with no evidence,
  with no commit, with an evidence path that does not exist, with an unmet
  dependency, with a regression test that does not exist; a `local` task marked
  `PASS` from this environment, which cannot honestly claim it; a Windows claim
  on a red Windows workflow; a `PASS` check with no evidence; an epic recorded
  closed that `epic_closed` disagrees with; an unknown status; and drift from
  the generator.
- `scripts/master_report.py` prints the mandated block, and `--json` prints the
  same figures structured. `tests/unit/test_control_plane_reporters.py` holds
  both against what `STATUS.json` records, by running the command rather than
  reading its source — the check whose absence let the retired counter print
  `0%` for a tree at 73.31%.
- `scripts/_process.py` is how any of them runs a child. `text=True` decodes the
  pipe with the host's locale, which is cp1252 on the Windows runner against a
  tree that is UTF-8 Russian — `audit_pass.py` therefore could not run there at
  all, and took the release build red one commit after it was wired in. The
  decoding is pinned to UTF-8 with `errors="replace"`;
  `tests/unit/test_script_output_decoding.py` runs the gates under `LC_ALL=C`,
  where the same failure reproduces on Linux. The two platforms fail
  *differently*, which is the part worth carrying: POSIX decodes on the calling
  thread and dies, Windows decodes in a reader thread and returns `returncode`
  0 with **empty output**. So on Windows the audit would not have crashed — it
  would have read every file as empty and reported 82 of 400 claims as unproven,
  measured by simulating exactly that. The shipped `packages/` were checked at
  the same time and are clean: all three of their subprocess calls are
  byte-mode.
- `scripts/audit_pass.py` asks the *historical* questions, which are the ones
  the gate above cannot: did the regression test a task names exist at the commit
  the task records as its verification, did the named test node exist in it, is
  the evidence on disk, is every dependency `PASS`. Two seconds over 400 claims,
  a step of `check.sh`, and it refuses outright on a shallow clone rather than
  answering a historical question with no history. It had never been run by
  anything, and the first run returned eight invalid claims — seven real
  (E11's packaging tasks named a proof added in a later commit than the one they
  recorded; `verification_commit` was corrected, no `PASS` withdrawn) and one a
  false accusation by the audit, which read `commit` where the plan records
  `verification_commit`. Both halves are held by
  `tests/unit/test_pass_audit.py`, which plants each question.
- `scripts/verify_carryover.py` re-runs a task's regression test before its
  `PASS` is believed. Nothing inherits a pass from the old model. It refuses a
  green run over zero executed tests — pytest exits 0 when every test in a target
  skips — and it decides nothing at all about an `owner: local` task, which is
  the rule the gate above enforces and which this script did not know: a live
  task whose test happened to pass here came back `PASS`, exactly what
  `check_master_plan.py` refuses. Held by
  `tests/unit/test_carryover_verification.py`, whose central assertion is that
  anything this script would confirm, that gate would accept.
- `tests/unit/test_master_plan.py` proves each of those refusals by planting the
  violation it exists to catch, and includes a control asserting a clean plan is
  admitted — without which a gate that rejected everything would satisfy them
  all.

### The figures, as of `5fcab5d`

Run `.venv/bin/python scripts/master_report.py` for the current ones; these are
not maintained by hand and will drift.

```
OVERALL WEIGHTED PROGRESS: 73.3%  (2305/3144 weight)
TOTAL TASKS 484 · PASS 400 · IN_PROGRESS 0 · FAIL 0 · BLOCKED 0 · NOT_STARTED 84
CHECKS: 48/54 PASS

CODE IMPLEMENTATION   100.0%   (1213/1213)
WINDOWS COMPATIBILITY 100.0%   (290/290)
MCP OPERABILITY       100.0%   (264/264)
VOICE OPERABILITY     100.0%   (324/324)
RC PACKAGING          100.0%   (202/202)
LIVE GAME VALIDATION    0.0%   (0/639)
FINAL RELEASE           0.0%   (0/200)
```

Five of those seven are at 100% and two are at zero, which is the whole shape of
this build. **Live game validation is 639 of 3144 weight — a fifth of the
project — and nothing in this environment can move it**, because nothing here
can start Project Zomboid. Every task in those epics is owned `local`, and the
gate refuses to mark one `PASS` from here at all. "Done" is not a word that
applies to this build.

The live total grew from 599 to 639 when `E14` was regenerated from the
runner's scenario catalogue: the plan had been carrying twenty hand-written
scenarios, two fewer than exist, and eighteen of them named a different
scenario from the id they select. The percentage went *down* as a result, which
is what a corrected denominator does.

## Status

| Task | Title | Phase | Depends on | Status |
| --- | --- | --- | --- | --- |
| T001 | Initialize repository and quality toolchain | 1 | — | **done** |
| T005 | Define protocol domain models and JSON schemas | 1 | T001 | **done** |
| T002 | Detect Project Zomboid installation and user directory | 0 | T001 | **done** |
| T003 | Build local API compatibility scanner | 0 | T002 | **done** |
| T004 | Implement doctor CLI | 0 | T002, T003 | **done** |
| T006 | Implement Lua mod skeleton and heartbeat | 2 | T003, T005 | **done** |
| T007 | Implement sidecar handshake and locks | 2 | T005, T006 | **done** |
| T008 | Implement command queue and acknowledgements | 2 | T007 | **done** |
| T009 | Implement panic stop and manual takeover | 2 | T006, T008 | **done** |
| T010 | Implement save backup subsystem | 2 | T002 | **done** |
| T011 | Observe player scalar state | 3 | T006, T008 | **done** |
| T012 | Observe nested inventory with stable refs | 3 | T011 | **done** |
| T013 | Observe nearby world and threats | 3 | T011 | **done** |
| T014 | Implement action lifecycle framework | 4 | T008, T011 | **done** |
| T015 | Implement movement adapter | 4 | T013, T014 | **done** |
| T016 | Implement inventory transfer adapter | 4 | T012, T014 | **done** |
| T017 | Implement safe food selection and eat adapter | 4 | T016 | **done** |
| T018 | Implement safe drink selection and drink adapter | 4 | T016 | **done** |
| T019 | Implement literature selection and read adapter | 4 | T016 | **done** |
| T020 | Implement deterministic reflex guard | 6 | T009, T013, T014 | **done** |
| T021 | Implement MCP server | 5 | T011, T014 | **done** |
| T022 | Implement permission and autonomy policy | 6 | T017–T020 | **done** |
| T023 | Implement typed planner and critic | 7 | T021, T022 | **done** |
| T024 | Implement memory store | 7 | T012, T013, T023 | **done** |
| T025 | Implement TeamON voice adapter interface | 8 | T021, T023 | **done** |
| T026 | Implement installer and launcher | 9 | T004, T006, T007, T010 | **done** |
| T027 | Implement diagnostics and support bundle | 9 | T004, T008, T014 | **done** |
| T028 | Build live game smoke harness | 9 | T015–T019 | **done** |
| T029 | Run endurance and recovery tests | 9 | T020, T022, T028 | **live** |
| T030 | Produce release artifact and final report | 9 | T021, T025–T027, T029 | **live** |

## Completed in detail

### T001 — repository and quality toolchain

Package layout under `packages/`, `pyproject.toml` with a dependency-free core,
ruff (format + lint, security and stub rules on), mypy in strict mode, pytest
with contract/integration/game-smoke markers, `.luacheckrc` with the engine
globals enumerated, and a GitHub Actions matrix over Python 3.11/3.12 plus a
Lua job.

Three gates beyond the usual linting, all runnable standalone and all wired
into `scripts/check.sh`:

- `scripts/check_forbidden.py` — AST-level scan for stub bodies, `TODO` markers
  in shipped code, `eval`/`exec`/`shell=True`/`loadstring`, plus a secret
  scanner over every tracked text file.
- `scripts/check_versions.py` — the five versions must agree across
  `version.py`, `pyproject.toml`, `mod.info`, the schema consts and the
  changelog.
- `scripts/check_schemas.py` — every schema must itself compile as Draft 2020-12.

### T005 — protocol domain models and schemas

`pz_agent_core.protocol` holds the shared vocabulary:

- `enums.py` — closed vocabularies mirrored by the schemas: action names,
  ack statuses with a terminal set, session modes, danger levels with an
  ordering, container kinds, capability states, risk classes, and the interrupt
  priority ladder from the master prompt.
- `refs.py` — session-scoped references with generation tracking. Parsing is
  done from both ends because a container reference itself contains colons; a
  naive split would corrupt world refs. `belongs_to_session` is what turns a
  reference from a previous session into `INVALID_REF` rather than a silent
  mis-resolve.
- `messages.py` — strict, total parsers for `Command`, `ActionResult` and
  `Observation`. Optional fields are omitted rather than emitted as null,
  because the observation schema is `additionalProperties: false` throughout.

The central safety invariant is encoded here: constructing an `ActionResult`
with `status = succeeded` and any reason other than `POSTCONDITION_MET` raises,
and `ActionResult.succeeded()` refuses to build without evidence. "Queued" can
therefore not be reported as "done" by construction, not merely by convention.

### T002 — installation and user-directory discovery

Walks every Steam library from `libraryfolders.vdf` rather than assuming the
default one; a second library on another drive is the common case. The VDF
parser accepts the modern `path` key and the legacy numeric form, comments and
CRLF, because that file is written by many Steam versions over the years.

Every entry point takes an injectable filesystem root and environment mapping
instead of reading `os.environ` at call time. That is what makes a Windows-only
code path testable on Linux CI, and it is why the tests genuinely cover a
Cyrillic username and a relocated home directory instead of assuming them away.

Build detection reports what it read and where. When the metadata is absent it
says so rather than falling back to `42.20` — a wrong build assumption silently
invalidates every capability probe downstream, which is worse than an honest
unknown.

### T010 — save backup and restore

Manifest with per-file sha256, sizes, total bytes and the source directory with
the user's home redacted to a placeholder. Restore verifies every hash before
writing anything and stages through a temp directory, so an abort cannot leave
a half-save.

`restore` refuses while the game is running — an exception, not a warning, with
no override flag. `prune` is the only deletion path and never removes the
newest backup. A source directory over the configured size cap is refused with
a clear error rather than filling the disk.

### T007 + T008 — session, IPC journal, queue

`ipc/layout.py` exposes the fixed filenames as properties plus a predicate
asserting nothing writes outside them; filenames are constants on both sides,
so a command can never name a file.

`journal.py` appends one record per line and reads by byte offset. A trailing
line without a newline is ignored and re-read next tick rather than parsed
half-written; a complete but unparseable line is reported and skipped so one
bad record cannot stall the stream. Rotation is signalled, not silent.

`snapshot.py` uses alternating a/b slots with the pointer written last, because
atomic rename is not guaranteed from inside Kahlua. A torn read falls back to
the other slot, so the worst case is one stale snapshot rather than a
half-parsed one.

`queue.py` tracks sequences with gap detection, enforces leases at both check
points, and caches only terminal results, bounded by entry count.

The session nonce rule carries real weight: `session.json` left by a crashed
sidecar is perfectly well-formed and would otherwise read as a fresh request to
attach on the next save load. Requiring a nonce different from the previously
accepted session distinguishes "a sidecar is asking to connect" from "a sidecar
asked once and nothing cleaned up".

### T006 + T009 — Lua mod

`shared/PZAgent/` holds the pure logic, tested under a plain interpreter with
no engine present: `Json` (deterministic key order, correct control-character
escaping, no `loadstring` anywhere — decoding is a hand-written scanner),
`Refs`, `Protocol` (version constants and the closed action whitelist checked
before dispatch), `Sequence`, `Ownership`.

`client/PZAgent/` holds the engine-coupled half: `Ipc`, `Heartbeat`, `Session`,
`Safety`, `Hud`, `Runtime`. `PZAgent_Main.lua` wires them and holds no logic of
its own.

Cross-verified directly rather than assumed: an item reference into a world
container — which carries five colons of its own — parses to the same container
tail, runtime id and generation on the Lua and Python sides, and rebuilds to
the identical string. A parser splitting left-to-right would not error here; it
would resolve to a *different container*, which is the whole reason both
implementations parse from the ends.

Lua builders return `nil, reason` rather than raising, following the language's
convention. Every call site must therefore check, and the client modules that
consume them are new — this is the most likely place for a swallowed failure to
hide, and is worth attention in review.

## What `wip` and `todo` mean for each open task

Recording which half is missing matters: a task reading as done when only one
side exists hides the gap rather than closing it.

| Task | Built | Missing |
| --- | --- | --- |
| T029 endurance | `S99_endurance.yaml` and every bound it asserts | The run itself, which needs a live game |
| T030 release | The artefact, its checksums and `FINAL_IMPLEMENTATION_REPORT.md` | The smoke and endurance evidence T029 would produce |

Seventeen game action adapters are registered on both sides:
`movement.move_to`, `movement.move_near`, `world.inspect`, `container.inspect`,
`container.open_nearby`, `inventory.search`, `inventory.transfer`,
`inventory.ensure_main`, `consume.eat`, `consume.drink`, `consume.drink_source`, `literature.read`,
`equipment.equip`, `equipment.unequip`, `medical.bandage`, `survival.rest` and
`survival.sleep`. The control plane is **five** — `session.arm`,
`session.disarm`, `safety.stop`, `action.wait` and `plan.cancel` — and is served
by `PZAgent.ActionRuntime` itself, so a stop can never be queued behind the
thing it is stopping. `plan.cancel` was listed above as an eighteenth adapter
for a while; it is a Python builtin with no Lua adapter, and putting it on the
adapter side made the count agree with the membership only by accident.

`tests/lua/test_adapter_registry.lua` asserts that every one of them reaches
the dispatcher through the real install path, and that the registry holds
exactly the protocol's actions and nothing else. That test exists because the
count above was true of the source and false of the running mod: the adapters
were published under a key the dispatcher does not read.

The CLI exposes seventeen commands: `arm`, `backup-save`, `disarm`, `doctor`,
`install-mod`, `live-test`, `logs`, `remember`, `replay`, `restore-save`,
`smoke`, `start`, `status`, `stop`, `uninstall-mod`, `validate-config` and
`voice`. The `live-test` group carries `prepare`, `run`, `status`, `resume`,
`collect` and `finalize`. This sentence listed fourteen and omitted `remember`,
`voice` and `smoke` — the same rot the paragraph below apologises for, one
revision later. It is derived from `pz_agent_cli.app.COMMANDS`, which
`tests/contract/test_cli_docs_agreement.py` now pins to the parser in both
directions. An earlier revision of this file said
`start`/`stop`/`arm`/`disarm` were deliberately absent because the sidecar loop
was not written. The loop is written and they are in the parser; the note was
left standing after the code moved past it.

## Deviations found by verification

Recorded here rather than quietly closed, because each is a place the
implementation and the blueprint differ for a reason.

**Reference generation is session-scoped, not save-scoped.** Blueprint §3.7 says
the generation in a reference increments after a save/load transition, so a
reference minted before it fails validation. The mod emits `generation = 0`
throughout and never increments it; in-session save/load invalidation instead
works by the sidecar noticing `game.save_id` changed, raising `SAVE_CHANGED`,
and closing the session.

That is coarser and stronger: ending the session invalidates every reference
*and* closes in-flight commands as `lost`, where a generation bump alone would
leave a command mid-flight against refs that had just become meaningless. The
counter was left at zero rather than wired to `Session.generation`, which
increments per handshake — a different quantity, and threading it in would put
a number in the field that does not track what the field claims to.

Revisit if a save/load transition is ever made survivable without a new
session; until then the coarse invalidation is the safer reading.

**`Refs.KIND.OBJECT` has no builder on either side.** Two non-container world
objects standing on the same square therefore share one `square:` reference.
Harmless for diffing — `diff.py` degrades to a whole-list diff when references
repeat — but it turned out not to be harmless everywhere.

`movement.move_near` required exactly that kind. `PZAgent.ObserveModel` mints a
`container:` reference for a nearby thing that holds a container and a `square:`
reference for everything else, so the adapter refused every reference the mod is
capable of producing, and the action could not be reached from a real
observation at all. Both the adapter and the plan parser now accept
`container`, `square` or `item`, which is what the mod actually emits.

The note above had been sitting in this file, correct and unread, while the
defect it describes was live. A deviation recorded is not a deviation handled.

**Build 42.20 accessor names are unverified.** The mod's probes degrade to an
absent field rather than guessing, so the behaviour is honest, but which
accessors actually exist needs a live session.

## Known gaps and caveats

- **Both entries that used to stand here are out of date and are recorded as
  such rather than deleted.** One said T003 was unfinished and that any action
  adapter had to wait for it; the scanner has since closed and seventeen
  adapters are registered. The other said T028 was partial because the scenario
  definitions existed without a runner; `pz-agent live-test` now drives twenty
  of them and refuses to finalize without evidence. Both notes outlived the work
  they described, which is the failure mode this section is most prone to — a
  caveat nobody deletes reads as a caveat that still applies.
- **`tests/lua/` proves logic, not compatibility.** It runs under mocked engine
  globals. It cannot demonstrate that `ISInventoryTransferAction` or
  `ISEatFoodAction` behave as expected in Build 42.20, and nothing in this repo
  claims otherwise.

## Requires a live game session

No scenario has been run — there is no installed game in this environment. The
sixteen definitions in `tests/game-smoke/`, driven by `pz-agent smoke`, name what
closes each of the claims in the table below.

**And they are not the catalogue the release depends on.** `pz_agent_cli.livetest`
holds **22** scenarios, driven by `pz-agent live-test`, and *that* is what
`scripts/check_release.py --release` reads: a `PASS` and hashed artefacts for
every id in `SCENARIO_IDS`, so v1.0.0 cannot ship until all 22 have run against a
real Build 42.20 session. The two number their scenarios independently and the
numbers collide — `S14` is `backup / restore` in one and `SLEEP_REST` in the
other — so a reader who took the table below for the whole of what the game is
needed for was reading the wrong list *and* a bar three times smaller than the
real one. `docs/LIVE_TEST_PLAYBOOK.md` and `docs/LOCAL_GAME_HANDOFF.md` describe
the 22 in the order an operator runs them; this section named neither until now,
which matters because `docs/RELEASE.md` requires
`FINAL_IMPLEMENTATION_REPORT.md` to list exactly the steps that physically need
the game, and this is the section such a list would be assembled from.

Two non-scenario items also need a real installation:

- **Which file Build 42.20 ships its version in.** Only the `versionNumber=`
  header in `console.txt` is confirmed. The install-side candidates
  (`version.txt`, `version`, `media/version.txt`) are unverified guesses. The
  behaviour is honest either way — an unreadable or absent file reports
  `known=False` with the reason recorded, never a substituted `TARGET_BUILD` —
  but which path actually exists is unknown until someone runs `pz-agent
  doctor` against the game.
- **Whether `"zomboid"` is what the running game calls itself.** This entry
  used to read *"nothing yet supplies it from an actual process check; whoever
  wires the CLI must"*. That is no longer true and had stopped being true some
  time ago: `supervisor.probe_game_running` asks the game heartbeat first and
  falls back to the process table, `game_running_for_restore` collapses its
  three-valued verdict toward refusing, and `saves.py` passes that boolean to
  `BackupManager.restore` — which still takes `game_running` as a keyword with
  no default and no override, so the rule cannot be bypassed by accident.

  What a live install is still owed is narrower and worth stating exactly: the
  fallback matches a process whose name contains `GAME_PROCESS_MARKER`, which is
  the literal `"zomboid"`, case-folded. Nobody has read the process table of a
  machine with Build 42.20 open, so whether the real name contains it is
  unconfirmed. The direction of a wrong answer is the safe one — an unreadable,
  truncated or non-matching listing all yield `MAY_BE_RUNNING`, and a restore is
  refused rather than allowed — so the cost of the marker being wrong is a
  refusal a person has to work around, not a corrupted save.

| Scenario | Status | Blocked on |
| --- | --- | --- |
| S01 heartbeat | not run | a live session |
| S02 panic stop | not run | a live session |
| S03–S08 actions | not run | T015–T019 adapters, then a live session |
| S09 manual takeover | not run | a live session |
| S10 stale sidecar | not run | a live session |
| S11 invalid ref | not run | a live session |
| S12 path blocked | not run | T015, then a live session |
| S13 zombie interruption | not run | T020, then a live session |
| S14 backup / restore | not run | a live session (the code and its tests exist) |
| S15 restart recovery | not run | a live session |
| S99 endurance | not run | everything above |

"Not run" is the honest status and stays until an evidence artefact exists.

## The playable-agent branch

`feature/playable-agent-1.0` takes the build from "every subsystem exists" to
"the mod can execute a command and prove what it did". It is not merged, and it
must not be merged before the live evidence exists.

### The protocol grew

Fifteen action names became twenty-two, and the mod's adapter files own
seventeen of them (the other five are the control plane the runtime serves
itself). Six were missing outright — `container.inspect`, `container.open_nearby`,
`inventory.search`, `medical.bandage`, `survival.rest`, `survival.sleep` — and
two were renamed rather than aliased: `inventory.equip`/`inventory.unequip` are
`equipment.equip`/`equipment.unequip`, because the dispatcher's whitelist decides
what may reach an adapter at all and two keys for one action is a second door.

`PROTOCOL_VERSION` is `1.1`. `SCHEMA_VERSION` stays `1.0`: the document shapes
did not change, only an enum inside them gained members, and a schema bump would
have invalidated every stored plan and observation for a change they read fine.

### The mod can now execute

`CommandReader` → `CommandDispatcher` → `ActionRuntime` → an adapter, with an
acknowledgement written at every transition. One command in flight, one waiting,
lease checked before each step, TTL, idempotent replay, session validation,
panic stop, manual takeover and heartbeat-loss stop.

`ActionRuntime` holds the invariant everything else rests on: there is a single
constructor for a success ack, it requires a non-empty evidence table, and an
adapter that finishes with nothing to show yields `POSTCONDITION_FAILED`.

### What running it found that reading it had not

Four defects, each caught by executing the code rather than reviewing it:

- **Thirteen of sixteen game actions were unreachable.** The adapters were
  written and individually tested; they named themselves under `name` while the
  runtime looks up `adapter.action`, so they registered nowhere. Every adapter
  test passed against code wired to nothing. `tests/lua/test_adapter_registry.lua`
  is the question none of them asked, and it went red immediately.
- **Arguments were silently dropped.** The dispatcher builds the argument table
  it hands an adapter *from the adapter's declaration*. An adapter that declared
  nothing was not refused — it ran with every argument gone. Declarations are now
  mandatory, asserted at load, and carry real bounds.
- **`RUNTIME_OWNED` was referenced and never defined**, in the branch deciding
  whether a published adapter supersedes a built-in one. `install` raised on any
  build where `adapters/` had published anything.
- **A lease expiring mid-flight reported `ACTION_TIMEOUT`**, telling the sidecar
  its adapter was slow when its own grant had lapsed.

### Status of the new work

| Block | Status |
| --- | --- |
| Protocol extension to 22 actions | **done** |
| Lua command executor and capability runtime | **done** |
| Seventeen Lua game adapters | **done** |
| Adapter-registry integration test | **done** |
| Python adapters for the new actions | **done** |
| Medical triage policy | **done** |
| `openai_compatible` and `teamon` plan providers | **done** |
| Live-test runner and evidence structure | **done** — its own commands were run before handover |
| Windows release candidate and CI | **done** — RC built; the two `.exe` files need a Windows PyInstaller run |
| `consume.drink_source`, and the capability gate under it | **done** |
| Handoff documentation | **done** |
| the catalogue's live scenarios | **live** |
| `v1.0.0` tag and release | **live** |

### Handoff documents

Written for the machine that has the game, because that is the only place the
remaining work can happen:

- [`LOCAL_GAME_HANDOFF.md`](LOCAL_GAME_HANDOFF.md) — what exists, what was
  verified, what was not, exact paths, and what not to rewrite.
- [`LOCAL_DEBUG_MAP.md`](LOCAL_DEBUG_MAP.md) — symptom → module → log → action.
- [`GAME_API_VERIFICATION.md`](GAME_API_VERIFICATION.md) — every engine symbol
  the mod assumes, all of them `requires_live`.
- [`LOCAL_AGENT_PROMPT.md`](LOCAL_AGENT_PROMPT.md) — the prompt itself.
- [`LIVE_TEST_PLAYBOOK.md`](LIVE_TEST_PLAYBOOK.md) — generated from `SCENARIOS`
  by `scripts/generate_playbook.py`, one section per scenario: preconditions,
  the command, the postconditions, and the observations file to hand back.

That last document told the operator to pass `--observations <file>` twenty-two
times and never said what the file had to contain. It published each
postcondition's key and its prose — "the player is standing within a tile of the
target" — and dropped the `field`, which is the only part the runner uses. The
catalogue's 83 postconditions read 66 distinct dotted paths, and those paths
existed nowhere outside `scenarios.py`: `grep "field" docs/LIVE_TEST_PLAYBOOK.md`
returned nothing. Someone working from the document alone could not write a file
the runner would read, and a wrong guess is not a soft failure — the path is
absent, the postcondition is unread, and the scenario fails, correctly and with
nothing to act on.

The generator now prints each postcondition's path and check beside its
statement and emits a per-scenario JSON skeleton with every value `null`,
carrying exactly the fields that scenario reads and no others. `null` is a form
to fill rather than a value to accept: every check refuses an unread reading, so
an untouched skeleton fails. Proved by running the producer rather than by
matching over it — `tests/contract/test_playbook_observations_skeleton.py` lifts
the JSON back out of the published markdown, feeds it through the real
`parse_observations`, and asks `read_field`, the reader `evaluate` itself calls,
whether each path is there; and the reverse, so a skeleton listing everything
cannot pass by breadth.

Filling that form exposed the next one. `game_build` is the single field a live
result carries that no postcondition covers: twenty-one scenarios say nothing
about the build, and `S01_INSTALL`'s `build_string` postcondition reads
`observations.game.build`, which is a different value from the top-level
`game_build` the result records and `finalize` gathers. So every scenario could
reach PASS with the build unread, the manifest would carry `(not observed)`, and
`check_release.py --rc` would refuse the archive — correctly, and only after all
twenty-two live sessions had been spent on a machine this project does not have,
over a string the operator could have typed at the first scenario. The skeleton
made it likely: it emits `"game_build": ""` and the instruction said "fill every
`null`". `decide` now refuses that PASS with a `BUILD_NOT_OBSERVED` code, the
result schema refuses the document under its PASS branch, and the playbook names
the field. Held by `tests/unit/test_pass_names_its_game_build.py` over all 22
scenarios, with the control that says the fixture really does satisfy them.

The same question asked of the *other* per-scenario declaration found the worse
one. `measures_latency` is set by `S04_MOVE`, `S19_AUTONOMOUS_30_MIN` and
`S20_AUTONOMOUS_2_HOURS`, and it was read by two things that only described it:
the playbook, which prints "**latency measured** (p50/p95 recorded in
`result.json`)", and `latency_summary`, which writes `"measured": false,
"samples": 0` when no samples were supplied. The document promised a
measurement, the evidence recorded that none was made, and the verdict said
PASS — and unlike the build, **no gate anywhere would ever have asked**:
neither `finalize` nor `check_release.py` reads the latency block. The skeleton
had no `latencies_ms` field, so an operator following the playbook supplied none
by construction. `decide` now refuses that PASS under `LATENCY_NOT_MEASURED`,
the skeleton carries the field for exactly those three scenarios, and the prose
names a real source — `pz-agent latency --json`, the `traces` array,
`terminal_at_ms - issued_at_ms` — because a required field with no honest source
invites invented numbers. Held by
`tests/unit/test_latency_scenarios_measure_latency.py`, with the control that
the other nineteen scenarios are not held to a rule they never declared.

Then the same question asked one level out, of the file the operator actually
double-clicks. `packaging/windows/bat/` is the entire interface of the release —
nobody on the Windows machine types `pz-agent` — and its `rem` blocks are the
manual. `run-live-tests.bat` opened by naming twenty live scenarios and a range
ending two short, and `finalize-release.bat` with "only when all twenty scenarios
are PASS", against a catalogue of twenty-two; the two an operator would have dropped are
S21 and S22, the craft and the placement, the only irreversible ones. The same
wrapper advertised `run-live-tests.bat --observations obs.json`, which exits 1
with "--observations describes one scenario, but 22 were selected" — so it
published the combination that never passes and omitted the only one that can,
`--scenario` together with `--observations`. The playbook's hand-written
"Running them" block had the same gap and now leads with the working form.

The counts are gone rather than corrected: a `.bat` cannot import the catalogue,
and this was the third stale scenario count found here. What holds it now is
`tests/contract/test_wrapper_comments_match_the_catalogue.py` — every scenario
id a wrapper names must exist, no wrapper may state a count or a range endpoint,
and an `--observations` example's own tokens go through the real `resolve`.

The same question asked of `docs/LOCAL_AGENT_PROMPT.md` — the text pasted into a
session on the machine with the game — found the most expensive one yet. §5
collected evidence after a FAIL and §7 collected everything once the run was
done, but `finalize` requires each scenario's declared logs **whether it passed
or not**: measured, a scenario driven to PASS with nothing collected is refused
over five missing files, and every one of the twenty-two declares some. Unlike
the build string and the latency samples, this cannot be repaired afterwards —
`console.txt` is rewritten on every game launch and the session trace rotates,
so by the end of a day the early scenarios' logs are gone and the only remedy is
to play them again. `LOCAL_GAME_HANDOFF.md` carried the correct rule six hundred
lines in, justified by trace rotation rather than by the refusal: two documents
in one bundle disagreeing about the order of operations, with the instruction
sheet wrong. The prompt now carries §4a. It also said "двадцать сценариев
a range ending two short" twice while its sibling was correct, and showed
`run-live-tests.bat` bare. Held by
`tests/contract/test_handoff_instructions_match_the_run.py`, whose load-bearing
assertion is the measured one — the real runner and the real `finalize` — with
the prose checks resting on it.

Separately, `evidence.commit`, `evidence.game_build` and `evidence.components`
had only ever been tested against hand-built manifests; they now also run over
one `finalize` really wrote. All three already agreed — a tightening, recorded
as such.

The logs have a sibling, and it is worse. Seven scenarios declare
`screenshots_required`, and the whole of what the operator was told was
`— screenshots required` appended to the **Evidence** line: no directory, no
moment, no mention that `finalize` refuses without one. The three handoff
documents mentioned screenshots zero times between them. Measured —
`S11_CONTAINER` driven to PASS with every declared log written, and `finalize`
still refuses. A log survives until the next game launch; a screenshot is a
moment, and **no command produces one** (`collect` gathers logs and journals and
never touches that directory, which is now asserted against the source rather
than recalled). The generator prints the directory, the moment and the refusal
under each requiring scenario, and says plainly that the runner only checks a
file is there — whether it shows the right thing is the operator's judgement,
which is why the scenario asks for one. The prompt gains §4b. Held by
`tests/contract/test_screenshots_are_asked_for_in_time.py`, both directions.

The last document of the handoff bundle to be checked, `LOCAL_DEBUG_MAP.md`,
turned out to disagree with the other three about a number all four state: how
much of the engine surface is unverified. `GAME_API_VERIFICATION.md` said "nine
lines, in two files" and "159 symbol rows"; the debug map and the playbook said
"52 symbols"; the prompt said "сто двадцать четыре". Measured: the grep returns
**10 lines in 3 files** and the table carries **167 rows**, every one
`requires_live`. Two documents presented the unverified surface as under a third
of its real size, and the inventory — the one that calls itself the list — was
wrong about its own table. The figure now lives in that one document and the
other three point at it; `tests/contract/test_unverified_surface_is_counted.py`
runs the grep and parses the table, so a new symbol row fails the suite until
the sentence is updated.

Then the same literal turned up in six more places, and the reason nobody had
seen them was my own two guards: each had been scoped to the file where the
defect was last caught rather than to the fact. Tree-wide, the survivors were
`check_release.py`'s module docstring, a description in
`schemas/gameplay-knowledge.schema.json`, a task string in `plan_epics_d.py`
(and so a `pass_criterion` in the generated plan), a status row here, and two
claims in `docs/RELEASE.md`. None was reachable — the release gate imports
`SCENARIO_IDS` and the schema constrains `proven_by` by length — so the cost was
a reader of the release gate being told the catalogue ends two early. The narrow
checks are gone, replaced by `tests/contract/test_scenario_ranges_match_the_catalogue.py`
over the whole tree. It has to know that **two catalogues exist and collide**:
`S01-S15` in `RELEASE.md` describes `tests/game-smoke/` and is right, so both
ends are derived — one from `SCENARIO_IDS`, one by listing that directory — and
a further test asserts they still differ. The plan was regenerated, not
hand-edited: one line, statuses preserved, progress unmoved.

`docs/LOCAL_DEBUG_MAP.md`, the last handoff document never checked for content,
came out clean: every module its triage table names exists, all seven reason
codes it sends the agent to look for are emitted somewhere, and all eleven
exchange filenames it lists appear in the code that writes them.

Then that guard took the Windows release build red — it shelled out to grep and
recovered filenames with `split(":", 1)[0]`, which on a `D:\a\...` runner
returns the drive letter, so three files counted as one. Green here, red there.
The measurement now reads `pz-mod/` directly; grep was only ever a reader of the
tree, and the tree is the producer.

Asking whether that class existed elsewhere found the more serious one.
`modinstall._check_relative` — whose stated job is *"refusing anything that
could escape the mod directory"* — split on `/` alone, so `../../evil.txt` was
refused and `..\..\evil.txt` was **admitted**: the traversal never became a
part, and `joinpath` on Windows resolves it outside the destination. Not remote
— the paths come from the ledger `pz-agent` writes — but that ledger sits in the
user's Zomboid directory, is read on the next `install-mod`, and the entries the
audit calls stale are `unlink`ed. `platform/backup.py` already normalised both
separators; the fix is that idiom where it was missing. Held by
`tests/unit/test_install_path_traversal.py`, built on `PureWindowsPath` per
D-004 so it fails on any platform.

Asking the question once more found the third, and the worst placed. The release
gate's own first rule is *"a claim is checked against the artefact, never
accepted from it"* — and it re-hashes every artefact the manifest records, which
is that rule applied to the contents. The **path** it took at face value:
`evidence_root / path`. Measured, `../outside.txt` came back verified, and an
absolute path came back verified without needing a traversal at all, because
pathlib replaces the root when the right operand is absolute. A wrong manifest
could therefore turn `evidence.artefacts` green over files that are not the
evidence, in the bar that certifies v1.0.0. Escapes are now reported rather than
skipped: a skip would read as an absent tree, which is a different fact. Held by
`tests/unit/test_release_gate_artefact_containment.py`.

Three instances of one class in three days — a Windows path parsed in a test, a
traversal guard splitting on one separator, and a release gate trusting a
recorded path. Each was found by asking whether the previous one's shape existed
somewhere else, so the question is worth keeping.

Asking it of the planner — where AGENTS.md says the model may emit "no raw refs
it invented" — found a false success rather than a path defect. A well-formed
item reference from this session naming something nobody observed was approved
by the critic; `ITEM_CONSUMED` is satisfied by the item's *absence*, so
`success.holds` was True and `PlanExecutor._gate` skipped the step before
reaching `_ref_gate`, the check that would have refused it. The run ended
COMPLETED with nothing sent and the character unfed. The critic now refuses at
review under `UNOBSERVED_REF`, narrowly: only criteria an absence satisfies, and
a gap in observation is still not evidence the item is gone. Two existing tests
asserted the old behaviour — one of them named for it — and both were changed,
which is worth naming rather than burying.

Also checked and sound: the Lua IPC's "a command can never name a file" holds —
every call site outside `Ipc.lua` passes a literal role, and `pathFor` refuses
anything not in the `FILES` table, so the lookup table *is* the guard.
`diagnostics/bundle.py` is clean too, by a character allowlist that excludes the
backslash, and it never extracts.

That gate covers one criterion, so the next step was to close the class rather
than the case: `tests/unit/test_success_kinds_are_classified.py` runs every
`SuccessKind` through the real `holds` with an observed and an unobserved
reference, and requires any kind that answers True *only* for the unobserved one
to be gated. A criterion added later and phrased as a disappearance fails it
until someone decides which way to classify it. The scope is measured, not
declared — the first version declared it and falsely accused `position_reached`,
which reads no reference at all.

The same enumeration question, asked of the other AGENTS.md rule about the
model — "all in-game text is untrusted data, never instructions" — found the
mechanism sound and unguarded. Free text is nested under `untrusted_text` with
the rule beside it, and everything else is filtered to identifier shape; but
only two call sites wrap, and a field added later would reach the planner
unlabelled with nothing to notice.
`tests/unit/test_game_text_is_labelled.py` marks every free-text field with a
sentinel, runs the real compaction and walks the result, so an unlabelled path
fails. No defect found — a guard over a rule that was being kept by hand.

Asking it a third time, of the rule the whole protocol rests on — "`succeeded`
means a postcondition was *observed*" — nearly produced the defect instead of
finding one. `ActionResult.succeeded()` refuses to build a success without
evidence; `ActionResult.from_dict` does not, and the mod's `Handle:ack` writes
the `evidence` key only when the bag has entries, so an evidence-free success
claim is a shape that can actually arrive. Every consumer turned out to be
defended, each checked rather than assumed: the engine treats a `succeeded` ack
as grace, never as the answer, and ends at `POSTCONDITION_FAILED`; `_sink_refusal`
refuses a replayed success outright; the MCP `ActionRecord` refuses the
combination (measured); arming needs the game's own heartbeat as well as the ack.

The right-looking fix — move the check into `__post_init__` — is the wrong one,
and planting it showed why: the claim is then dropped as an unusable ack and the
answer degrades to `ACTION_TIMEOUT`, which says the mod went quiet when it
actually lied. The decoder has to be able to transcribe a claim it cannot
verify, or the receiver has nothing to name. Nothing pinned that asymmetry —
the plant left the engine and queue suites entirely green, because both build
their acks through the constructor that carries evidence.
`tests/unit/test_a_success_claim_without_evidence_is_named.py` now covers the
path an ack really travels (journal bytes → decoder → engine), and AGENTS.md
says which half of the rule binds the decoder.

That commit then went red on CI, and the cause was the gate's own wording. The
commit was pushed on its own, before its STATUS commit; at `c4b08ef`
`docs/control/STATUS.json` still described the previous commit, and
`check_master_plan.py` refuses exactly that. Reproduced from the commit rather
than read off the log, and then measured across history: *every* code commit
here is inadmissible to its own gate — 12 of the last 25 on `dev`, precisely the
code ones — because a commit cannot carry a STATUS describing itself. The design
accepts that and compensates by pushing the code commit and its STATUS commit
together; nothing had ever said so, and nothing had needed to, because until now
they always travelled as a pair.

What let the push feel safe was `scripts/check.sh` ending in a bare `All checks
passed.`, over a working tree that still had uncommitted work. The checks did
pass — on a tree that never became a commit. The header claimed a green run here
means a green run on CI, which holds only for a clean tree.
`scripts/check_tree_identity.py` now names the subject of every verdict, first
and last: the commit, or the pending STATUS commit whose tree this is, or no
commit at all. It never fails the gate; running checks over uncommitted work is
ordinary, and calling that a verdict about a commit was the defect. Its own
test found a defect in it — `git status` collapses untracked directories, so a
new file under `docs/control/` read as `?? docs/` and landed in the wrong
branch; `--untracked-files=all` fixes it.

The same enumeration question, asked of the capability-honesty rule, found the
last sentence of it unguarded: "never simulate the effect by writing stats".
That is the one point where success-only-by-observation can fail silently.
Measured rather than argued: every engine access goes through
`Toolkit.call(owner, name, ...)`, a generic dispatcher with varargs, and
`ActionRuntime.observedPairs` asks only that some `x_before` differ from its
`x_after` — both readings the adapter took. An adapter that wrote the player's
endurance would produce a `succeeded` ack carrying evidence of its own change,
pass every check here, and mutate the save.
`tests/contract/test_state_changing_calls_are_declared.py` now requires each
state-changing name in shipped Lua to carry a row in
`GAME_API_VERIFICATION.md`. Five exist today — the two input-press tables in
`Combat.lua` — and all five are documented. A planted
`Toolkit.call(stats, "setEndurance", 1.0)` fails it by file and line while
`check_forbidden.py` still says "No forbidden patterns found". The guard does
not judge whether a mutating call fabricates an effect; no scanner can, and that
stays a review question.

Two things were checked in the same pass and found sound. The method-name
surface is closed: all 74 dispatcher call sites take a string literal or an
entry of a module-level literal table, so no name can arrive from a command or
the model — the same shape as the `FILES` lookup that keeps a command from
naming a file. And the inventory covers every engine name the code reaches;
that took three attempts at the comparison, and each apparent gap (39, then 11,
then 2 names) was the parser reading one side too narrowly rather than a missing
row. The one real defect found this pass was in the guard itself: compiled with
`re.IGNORECASE`, its pattern also relaxed the required capital and accused
`settings` and `address`. Its own control test caught that.

Asking the same enumeration question of "bounded everything" found a real leak
on the session-long path. `QueueCommandSink` remembers when each shipped command
was last heard from; entries went in on every send and every ack and came out
only on a terminal ack, so a command that never got one stayed for the whole
session. The queue guarantees there are such commands — its `pending_limit`
sheds the oldest evictable entry, and a shed command's terminal ack is filed
against nothing. Measured with the real classes at `pending_limit=8`: two
hundred accepted commands left the queue tracking eight and the sink holding two
hundred, 192 of them unreadable and unremovable. The sink now prunes to the
queue's own pending set — not to a second limit of its own, since a duplicate
bound drifts — and `tests/unit/test_sink_progress_is_bounded.py` fails both when
the pruning is removed and when it is made greedy enough to drop the entry the
reflex guard reads.

Two rules were checked in the same pass and found sound, reported rather than
changed. "The model never picks the sandwich": the goal path runs `select_food`
/ `select_drink` in the provider and in the consume mission, and although
`consume.eat` is plannable — so a model may name any *observed* item — the mod
refuses rotten, burnt and poisonous items in `Consumption.lua` before acting,
which is defence in depth and is pinned by Lua tests (removing those lines fails
two of them). No claim is made here about whether the mod's boolean gate and the
policy's richer freshness model agree; those symbols are `requires_live`, and
inventing a defect out of unverified engine semantics is the same error the
capability-honesty rule forbids. And every `policy/*.py` module has shipped
importers, so none of the deterministic selectors is a subsystem nobody
connected.

The architectural line in the repository map — `pz_agent_cli` is the only
package allowed to `print` — turned out to be load-bearing in a way the map does
not say. An MCP client parses `pz-agent-mcp`'s stdout as JSON-RPC, and
`pz_agent_mcp/__main__.py` imports `pz_agent_cli.context` on purpose, so the
state directory is derived once rather than by two copies that drift. Measured:
that import loads 36 CLI modules into the serving process, `output` and `status`
among them — every `print` in the repository. `redirect_stdout` there wraps only
argparse; the serving path writes to the real descriptor, as it must. Nothing on
the crossed path prints today (zero bytes, measured in a real child process) and
nothing but discipline kept it so. `check_forbidden.py` now refuses a terminal
write outside the CLI, and
`tests/contract/test_mcp_stdout_belongs_to_the_protocol.py` crosses the boundary
in a subprocess and reads the pipe. Planting shows they are complementary: the
static rule catches a `print` in core, mcp or voice and a `sys.stdout.write` in
the entry point; a `print` added to `pz_agent_cli.context` is invisible to it and
fails the subprocess test.

Checked in the same pass and found sound, reported rather than changed. The rest
of "bounded memory" holds after last iteration's leak: the reflex event list and
the state-problem list are `deque`s with `maxlen`, `_fresh_problems` is emptied
every tick, the memory store has hard ceilings with eviction and a refusal past
`max_preferences`, the sequence tracker is keyed by a four-value enum, and
journal rotation deletes the oldest generation rather than accumulating on disk.
The scan that found these also produced 35 candidates, most of them false: a
container evicted through a helper that takes it as an argument does not look
shrunk from the call site.

The repository map's two rules about the domain layer — zero third-party runtime
dependencies, and core at the bottom of the stack — had no check either, and
measuring them changed what the check should say. Importing all 109 core modules
in one process pulls in nothing outside the standard library; a static scan of
the same source finds `yaml` in `knowledge/loader.py`, imported inside a
function, which never runs at import time. The runtime probe would have
confirmed "zero" and been wrong. The import itself is handled well — inside a
`try` whose `ImportError` handler raises `CorpusError(YAML_UNAVAILABLE)` and
stops planning rather than continuing without the configured rules — so
`tests/contract/test_core_carries_no_dependency.py` encodes the true rule: a
third-party name only when listed with its reason *and* guarded so its absence
is a typed refusal, plus no import of `pz_agent_cli`, `pz_agent_mcp` or
`pz_agent_voice`. Three plants fail it: a deferred `import httpx` in a provider,
the guard removed from around `yaml`, and `import pz_agent_cli` in the action
engine.

Sound and reported rather than changed: `pz-agent-mcp.exe` does not bundle
PyYAML and does not need to. The provider that loads the corpus is built in
`pz_agent_cli` and runs in `pz-agent.exe`, whose spec does list `yaml` among its
hidden imports; the MCP process only compacts an observation for the planner.
Chasing that to a conclusion is what kept it from becoming a false accusation
against the packaging.

The last unguarded line of the repository map was the one that matters most to
every claim in this file: `docs/blueprint/` is read-only, and it is what
conformance is measured against. Measured — 22 files, one commit, zero
modifications, deletions or renames since — so the discipline has held perfectly
and nothing enforced it. `tests/contract/test_the_blueprint_is_the_baseline.py`
now does, at both moments that matter: history, which CI judges and whose
failure names the offending commit, and the working tree, which `git log` cannot
see and where an edit still costs nothing to undo. Both planted, both fired. It
also pins that the directory still holds its files, because an emptied baseline
would satisfy the history check forever.

**Open for the user, not decided here.** AGENTS.md says of `schemas/`:
"Changing one is a protocol change; update `version.py` and the sync test."
Read strictly, roughly fourteen commits break it — every one that added a name
to the wire (`Two crafting names join the wire`, `Doors observed, addressed and
operated`, and so on) touched a schema and left `version.py` alone. The practice
those commits follow is coherent: `SCHEMA_VERSION` stays at `1.0` for additive
changes, `PROTOCOL_VERSION` has moved once to `1.1`, an old peer refuses an
unknown name by its own enum check rather than misreading it, and
`check_versions.py` keeps every restatement of the constants in step. So this is
reported rather than acted on. Rewriting the rule so that past commits stop
looking like violations is the same move as editing the blueprint to match what
was built, and it is the user's call, not mine.

Turning back to the blocked work itself, the 84 open tasks were re-counted from
the plan rather than from memory: 64 in E14 (live validation) and 20 in E15
(endurance and release), every one `owner: local`, every one verified by running
the game or by reading a workflow the live evidence has to exist for. That
standing report is accurate.

What is preparable is the surface the operator meets when they finally run them.
The plan puts a `verify_command` on all 484 tasks, 150 distinct, and nothing
checked that those commands exist — a renamed test or a removed flag would
surface after a two-hour endurance run, on a machine this repository cannot
reach. Measured: all 150 resolve, all 33 `pz-agent` lines parse against the real
parser with their flags, and the 22 scenario ids the plan names are exactly the
22 the catalogue defines.
`tests/contract/test_the_plan_names_things_that_exist.py` now holds that, and
its own classification check caught the first version dropping eight `grep`
commands whose paths were globs or bare directory names.

Checked and sound, reported rather than changed: `pz_agent_mcp` names
`pz_agent_core.policy` only in prose — it imports no policy module, which is the
"thin adapter, never re-implements policy" rule in its checkable form — and the
catalogue's specific claims about policy are already pinned (`MODE_LIMITS` in
`test_policy_permissions.py`, the P4 tier by name in `test_mcp_catalog_actions.py`,
`allow_windows` in `test_mcp_catalog.py`). Both hold: no mode carries a P4
ceiling, and `BuildingBuildAdapter` declares P4 flat with no `risk_for`.

Continuing outward along the same path — what the operator meets when they
finally run the 84 blocked tasks — the next question was whether the evidence
they produce can clear the release bar at all. It cannot, and the reason is not
the evidence. Running the real gate over the release tests' own passing fixture:
fifteen of sixteen checks pass; the sixteenth is `evidence.version`, because
`finalize` stamps `PRODUCT_VERSION` (`0.1.0`) into the manifest and the gate
requires the version being released (`1.0.0`). The gate's remediation says "bump
version.py … then re-run the scenarios" — the whole session, twenty-two
scenarios plus a thirty-minute and a two-hour run.

The defect is the ordering and the silence around it: neither
`LIVE_TEST_PLAYBOOK.md` nor `LOCAL_AGENT_PROMPT.md` mentioned the version, and
the open tasks say "follow the playbook". Both now state the rule and its cost
before the first scenario, and `live-test prepare` prints the number it will
stamp — the last cheap moment, and the one command every session passes through
because `run` refuses without its record.

The version was deliberately **not** bumped. What this repository declares
itself to be is a product decision; taking it inside a test would be the same
yardstick-moving the blueprint guard exists to prevent.
`tests/contract/test_the_release_bar_is_reachable.py` holds that the bar is
otherwise reachable — so a future check no real run could satisfy fails here
rather than on the operator's machine — and fails on purpose once the bump
happens, so whoever does it rewrites the file rather than inheriting a green
test that has outlived its reason.

**Requires live session, and now also requires a decision first:** bump
`PRODUCT_VERSION` and everything `scripts/check_versions.py` lists to the
release version *before* the live run, or accept that the session's evidence
cannot certify v1.0.0.

The same lesson arrived again, about the same defect, and this time the fault
was in the guard I wrote for it. The rule — the inventory states the size of the
unverified engine surface, everything else points at it — was enforced over
three documents, the three the original sweep had found. Measured now: ten
documents name the inventory, and two outside that list had been carrying the
wrong figures ever since. `LOCAL_GAME_HANDOFF.md` still said "the 52 engine
symbols" and "finds six of them" against a real 167 rows and 10 marker lines in
3 files; `LIMITATIONS.md` said "168 symbol rows", the legend-row miscount fixed
in the inventory and never propagated outward. Both size the risk an operator
takes on before the first live session.

Two failures, not one. The set was listed rather than derived — the same
guard-scoping mistake recorded here before — and the pattern demanded the noun
immediately after the number, so "52 engine symbols" passed even once the
document was in scope. The second only surfaced because the first plant was
tried and did *not* fail.

Both fixed at the fact: the satellite set is derived from "names the inventory",
`PROGRESS.md` is exempt by name because it is this record and must be able to
quote the wrong numbers, and one qualifier is allowed between the number and the
noun. Nine satellites checked, no false accusation among them, both stale
sentences fail when planted back.

Sound and reported rather than changed: the seam between the runner's manifest
and the release gate is already covered by
`tests/contract/test_evidence_manifest_round_trip.py`, which states its own
scope honestly — two scenarios, because observations for the other twenty would
have to be invented, and an invented observation is what this project refuses on
the critical path. The gap that leaves — a manifest `finalize` wrote for all
twenty-two, read by the gate — cannot be closed without inventing that evidence,
so it stays open and named rather than filled. `docs/VOICE.md`'s self-check
(`UnroutedPlanPort` absent, `services_over_core_rpc` present) holds: 0 and 4.

Having found the same guard-scoping mistake twice, the next pass asked it of the
guards written since — and found it a third time, in the newest one. The version
warning had reached `LIVE_TEST_PLAYBOOK.md` and `LOCAL_AGENT_PROMPT.md`, the two
documents open when the defect was found, while this repository already defines
the operator's instruction set in
`test_handoff_instructions_match_the_run.INSTRUCTIONS` — and it has three.
`LOCAL_GAME_HANDOFF.md`, the document that hands the whole job over, said nothing
about the version; its one mention of `version.py` described a `check.sh` step.
The set is now imported from that definition rather than re-listed.

Planting then found something worse than the omission. Cutting the entire
warning out of `LOCAL_GAME_HANDOFF.md` left the test green: it asked for the
substrings `version.py` and `re-run`, and both appear in these documents for
unrelated reasons, so the assertion had been passing for the wrong reason from
the start. The anchors are now `PRODUCT_VERSION` and the gate's own remediation
phrase — measured to occur exactly once per document, only inside the warning —
and neutralising it in any of the three fails that document's case. A guard
whose plant does not fail is not a guard, and only planting says which one it
is.

Having learned that a guard whose plant does not fail is not a guard, the next
pass planted against the guards themselves rather than reasoning about them. The
first proxy — "does this assertion's literal occur elsewhere in the document" —
produced 342 hits, nearly all noise, and was abandoned; planting found the real
one in three tries.

Cutting section `## 4a` out of `LOCAL_AGENT_PROMPT.md` left the whole contract
suite green. That section is the instruction that decides whether a live session
has to be repeated: `finalize` requires every scenario's declared logs, and
`console.txt` is rewritten on each game launch while the session trace rotates,
so logs gathered at the end of the day are missing the early scenarios'
entirely. Worse than unguarded — the playbook and the handoff named
`collect-evidence.bat` only in a command table and never said when to run it, so
two of the three instruction documents did not carry the rule at all.

All three now show the per-scenario form and the reason.
`tests/contract/test_logs_are_collected_per_scenario.py` holds it over the
imported instruction set, anchored on `collect-evidence.bat --scenario` —
mechanical and language-independent, since one document is in Russian — with its
limits stated: it proves the call is shown, not that the prose explains it. A
second check confirms the wrapper accepts the flag, so the documents cannot
agree on a spelling that fails. Four plants, four failures.

Those four plants were the convenient kind — replace every occurrence of the
string — and the next pass showed how much they flattered the guard. Deleting a
*section*, which is what a rewrite does, left it green: `LOCAL_AGENT_PROMPT.md`
spells the command twice, in the timing rule and again in "what to do after a
FAIL". A sweep that deleted each section of the two long documents in turn found
**24 of 28 removable with the suite still green**, `## 4a` among them. The guard
now also requires the half a command cannot carry — that the scenarios which
*passed* owe their logs too — through a per-document phrase table whose
completeness is asserted against the imported instruction set rather than
trusted, since a mapping that silently missed a document would be this guard's
third scoping failure. Most of the other 23 sections are prose that should not
be pinned to a magic string; that is a judgement, recorded as one, not coverage.

The same method was then turned on code rather than documents.
`live-test prepare` is the subcommand that proves a world is safe to experiment
on before twenty scenarios wound the character and end in restores, and it makes
six refusals. Each was neutralised in turn and the **full** suite re-run — seven
runs of about 9 400 tests, twenty-three minutes — and three passed unnoticed:

- `manager.verify(...)` replaced by `pass`. This is the distinction the prose
  and `_unprepared`'s docstring both draw — a backup that *reads back* rather
  than merely existing — and it was the one refusal in `prepare` with nothing
  behind it. The new test corrupts one backed-up file in place at the same
  length: the file exists, the listing is unchanged, only the manifest's SHA-256
  disagrees. That is the damage an existence check cannot see.
- the missing-save-directory refusal. A backup record outlives the save it came
  from, so a world renamed after the backup leaves a machine where the backup
  verifies and the save does not exist — and `prepare` would write `ready`,
  unlock `run`, and name that backup as if it covered a save id resolving to
  nothing.
- the no-Zomboid-directory refusal, whose absence is not a worse message but a
  `TypeError` two lines later, where a refusal naming `pz-agent doctor` belongs.

No product behaviour changed: all three refusals were correct and unguarded,
which is a defect in the suite rather than in the CLI. The other three refusals
— the missing schema, the absent `--save`, the save name without "test" — and
the absence of any backup were already guarded, and are reported sound. What
this iteration measured is the coverage of one subcommand's refusals; the same
sweep over the 1 179 refusal sites in `packages/` has not been run, and no claim
is made about them.

That sweep was then run, as a multi-agent fan-out: four agents, a disjoint area
of shipped code each, every one in its own git worktree so the plants could not
collide. The rule they were held to is the one this file has been learning all
month — a refusal counts as unguarded only when its plant leaves the **full**
suite green, a scoped run being enough to prove the opposite and nothing more.
Isolation was verified before the fan-out started rather than assumed: pytest's
`pythonpath` is relative to the rootdir, so a worktree run imports the
worktree's sources, and a plant made in one was shown to fail a test there while
the main checkout stayed clean.

`safety/` came back sound — five plants, five caught, including the panic-stop
path and the rung that lets the mod's reported danger raise but never lower this
process's own assessment. `platform/` returned three findings, each re-planted
here in the main tree before any test was written for it: the verify-side
traversal bound (suite green at 9484 passed under the plant), `_plan`'s "not a
regular file" refusal (9463), and `restore`'s postcondition (9464). All three are
now guarded, and each guard was shown to fail under its plant. A fourth followed
— `create`'s check that the manifest is where the backup was moved to, whose
absence lets `create` return a record naming a directory that is not there,
which `live-test prepare` then reads as its licence to arm twenty destructive
scenarios.

The two remaining areas came back with three findings each. The adapters: the
generation half of `find_by_identity`, which is the recogniser every
postcondition in that layer runs on and whose loss makes a pre-save/load
reference match whatever object now holds that runtime id; and two refusals on
`consume.drink_source`, one of them the only thing in the adapter layer standing
between the character and drinking out of a vessel that already holds tainted or
poisonous fluid. The CLI loop: the freshness bound on the arm-confirming
heartbeat, an arm request racing a disarming safety event, and a panic stop
arriving while an arm is in flight.

Two of those are worth recording for what the *measurement* said rather than the
finding. The racing-arm refusal already had a test, and that test passed with the
arbitration deleted: since the arm became two-phase, `armed is False` one tick
after a request is true whether the guard refused it or the loop merely submitted
`session.arm` and is waiting, so the assertion had been passing for the wrong
reason. And the panic-during-arm case turned out to be genuinely doubled — the
panic level's disarm and a branch in `_watch_pending_arm` each suffice, so
planting them one at a time made each look unguarded while the protection was
never absent. Planting both together does break it, and now fails a test. The
sweep's report was taken as a lead, not as a verdict; one of its suspicions
(`create`'s staging cleanup, thought unguarded because the tests glob for `*`
and staging directories start with a dot) was refuted outright — `pathlib`
globbing is not shell globbing and does match leading dots.

The sweep also found a defect in the *tests* rather than the code, and it is the
more interesting one. `test_the_records_own_message_does_not_survive_into_the_traceback[-1]`
asserts that `-1` does not appear in a rendered traceback; a rendered traceback
quotes every frame's file path; a git worktree is named `…-1`. So the agent
working in it opened with a failure that was neither its own nor the
repository's, had to stash its work to establish that, and qualified every
finding it made afterwards. A checker that accuses correct work is argued with
once and then switched off — this repository has written that sentence about its
own gates twice, and this time it was a unit test doing the accusing.

The sweep then covered the five areas it had not reached — `goals/`,
`planner/`, `ipc/`, `memory/` and the voice surface — with one instruction added
after the last round's mistakes: before calling a refusal unguarded, look for a
*second lever* that delivers the same protection, and say so. That instruction
paid for itself. `memory/` came back sound (five plants, five caught), and of
the twelve findings the other four returned, the reports themselves narrowed
three by naming the lever that covers them.

Nine are closed here, each re-planted in the main tree before a test was written
and each guard shown to fail under its own plant: the journal's oversized-header
diagnostic (a stream that cannot be read was reported as a stream with nothing
in it), the per-poll read bound, the queue's uncommitted-tail damage signal —
which is half of the live Build 42.20.2 second-producer finding — both
cross-record checks in `GoalQueue.restore`, the stored terminal cap, and the
transport's response ceiling, read-timeout re-arm and `Content-Length` shape
check.

Two of those deserve recording for *why* the suite could not see them. The
transport's byte ceiling had a test that passed with the bound deleted, because
the size check two lines below it still fires — after the whole body is on the
heap, which is the thing the bound exists to prevent. And the read timeout is
only distinct from the connect timeout because the live socket is re-armed; the
existing timeout test sets both numbers small, so it could not tell them apart.
Neither was a missing test. Both were tests that asserted the outcome and not
the bound.

The three findings in `pz_agent_cli/voice.py` that were carried as open — the
panic latch's post-write size check, `_log_safely`, and the redaction of the
line `voice run` prints — are now closed, and all three turned out to be real
even after the lever analysis had narrowed them. The latch check covers the one
failure `Path.write_text` cannot produce: a write that returns while the file
stays empty, which the mod reads as no stop at all. `_log_safely` is the only
thing standing between a logs directory that fills mid-session and a traceback
in place of the companion's ending sentence, since `_companion_log` catches only
at construction. And the printed line was the one sink nothing redacted: the
record, the log and the support bundle each keep their own redaction, so a path
in a backend exception reached the terminal and the `--json` payload and nowhere
else. That last one is why `test_voice_privacy.py` did not see it — it scans for
a transcript canary, not for paths.

With those closed, the sweep has covered every area of shipped code it set out
to: `platform/`, `safety/`, `actions/adapters/`, the CLI, `goals/`, `planner/`,
`ipc/`, `memory/` and the voice surface. What it did not cover is stated rather
than implied: within each area only a handful of refusal sites were planted —
five per agent, against surfaces holding dozens — so "swept" here means "sampled
under a stated budget", never "exhausted".

## Deviations from the blueprint

| Blueprint | Here | Why |
| --- | --- | --- |
| Python 3.12+ | `requires-python = ">=3.11"` | The build environment runs 3.11; CI tests both 3.11 and 3.12 so the 3.12 target stays honest. No 3.12-only syntax is used. |
| A single `setup` command (blueprint §14.2) that detects, backs up an existing same-id mod, installs, creates config, runs doctor and prints launch steps | `install-mod`, with `validate-config` and `doctor` as separate steps that `docs/QUICKSTART.md` sequences | The composition is a preference; **the backup step is a deliberate refusal**. `install-mod` audits the destination before writing anything and raises `ForeignFileError` on the first file pz-agent did not install, or on any installed file whose hash has changed. Backing up and overwriting would still have overwritten; refusing and naming the file does not. Nothing is written when the audit fails. |
| A `support-bundle` command (blueprint §14.7) | `logs --bundle`, with `--verify` | Same subsystem, reached through the command a user is already in when they need it. `docs/TROUBLESHOOTING.md` gives the real invocation. |
