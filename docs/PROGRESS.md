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
> | `P5-crafting-building` | todo |
> | `UX-one-command-play-and-docs` — one-command launch, goal CLI | **wave 1 done here, awaits live** — `pz-agent play` (validate → start → bounded wait for the game → arm confirmed by the game's own heartbeat, refuses in front of a panic latch, never touches the game process), `goal submit/status/cancel` over the same Core RPC link the MCP server and voice use (no `pause`/`resume` by design), `status --watch` HUD with the goal queue and an honest `unreachable`; the RPC codec tail is closed (suspension fields, `target_endurance`/`hours`, `progress`/`paused`/`report`) and `schemas/goal.schema.json` declares the four suspension keys; open: `goal submit` in a user-facing walkthrough, voice speakability of the newly wired kinds |
> | `stabilize/arm-session-confirmation` — the P0 stabilization pass | **35 reproduced defects fixed, awaits live** — seventeen on the sidecar, seventeen on the mod (the side every 2026-08-08 live finding came from): one family: evidence read without checking whose it was or when it was written. False success (`play` armed by another session's heartbeat; PZD010 active from an undated read; a future-stamped heartbeat fresh for the whole skew; combat reporting a kill it did not make; `avoid_threat` reading a missing nearby tier as safety; `status` printing a silent heartbeat as the state now), stale identity (snapshot sequences compared across sessions — the sidecar went blind after a game session change; a fresh reader adopting the dead session's slots; arm request and pid record stamped ahead of the clock), never terminal (a disarm countermanding nothing at the game; a suspended goal stranded past every timer; a mission wedged on an evicted step record; a journal renumbering silently). On the mod: a command polled for ever against a stopped clock; a raise in admission leaving a command with no ack at all and every redelivery classified a duplicate; an argument bound that raised at dispatch instead of refusing at load; a replay cache with no session dimension answering a live command with a dead session's `succeeded`; a closed session reopenable by re-presenting its document; the armed state surviving a session swap so a second sidecar inherited authority it never asked for; an unread body indistinguishable from an unhurt one; a nearby scan reporting an empty world it could not read; a rest succeeded with no departure reading; a bandage verified on a dressing the wound already wore. Each fix red-first; no P5, no refactor. Open and named, not fixed: the mission-cap eviction path banks a report without ending its goal. A final sweep of the tri-state rule itself found five more, the sharpest safety-critical: a zombie scan that could not run published an empty list, the danger floor read that as NONE, and an armed AUTONOMOUS agent was cleared to work on a count nobody took — the floor now answers HIGH with a `zombies_unknown` counter beside it. Two further flattenings (`player.alive`, `player.moodles`) were examined and left alone: every consumer refuses or proposes less, so an edit would have fixed nothing. The last sweep closed the neighbour that sweep had flagged and left: the danger floor is written only at the end of `Observe.context`, so a tick failing before that point leaves the old reading standing while the mod keeps heartbeating — reproduced with `getCore` removed, twelve ticks over sixty seconds and two chasing zombies, `mayStart("consume.eat")` answering `POSTCONDITION_MET` on a minute-old `none`; the floor now carries the clock that measured it and `mayStart` refuses a mutating action past 30 s with `PRECONDITION_FAILED` naming the missing measurement. And the pass's own evidence had the same defect: an earlier hand-merge put the zombie-scan group *after* `Harness.finish`, which calls `os.exit`, so eighty-eight assertions covering the safety-critical fix were present, named in this ledger and never executed — `test_observe` now runs 234 instead of 146. A seven-way audit then asked one question of every mission family and the core navigation paths: can a mission reach a state it never leaves without its goal ending? No family leaks — `GoalQueue.tick()` expires an abandoned active goal on `max_wall_ms` and `runtime.tick()` calls it unconditionally before any acting, `deadline_ms` being non-nil for every ACTIVE record — but the audit surfaced a silence that is not a leak: all six `_submit_*_step` handlers swallowed the channel's `LoopError` on a comment borrowed from the journey path ("the mission decides again from the next observation; its own bounds cap how often this can repeat"), which is false for a mission, because it sets `_pending_action` when it *emits* the step and only a terminal result clears it. A step the channel never admitted therefore wedged the mission into declining every later tick, and the goal ended `ACTION_TIMEOUT` at its wall budget — up to fifteen minutes for loot — naming a timeout instead of the refusal. Reachable two ways `ActionChannel.submit` names itself: the MCP router filling the channel from its own thread between the capacity check and the call, or a reused idempotency key after a restart. All six now end the goal `CAPABILITY_UNAVAILABLE`/`UNADMITTED_STEP_DETAIL`, as `fb8539a` already did for an evicted record; the journey handler is unchanged, because a journey really can replan. Two ledger items were verified rather than edited, neither being turnable red: arming on a **never**-measured danger floor is unreachable for mutating commands (the engine will not dispatch without a strictly newer observation, the mod publishes only after `setDanger`, and `dangerFloor` returns a `DANGER.*` constant on every path including `HIGH` for an unscanned world), and `_enforce_cap` evicting a live drive needs a fifth drive of one kind while `MAX_TRACKED_* = DEFAULT_MAX_OPEN = 4` and `app.py` overrides neither. A second audit then hunted the shape that had hidden the mission wedge for four passes -- a comment asserting a guarantee provided by other code -- across seven areas: thirteen candidates, three refuted on the spot, every survivor re-checked by hand before any edit, and **no behaviour changed**. Four were false. The sharpest is a gap, not just prose: `movement.move_near` refuses `object` references on both sides of the seam, justified by the claim that no scan mints one, but since `bf92ee2` the observer mints exactly one -- a door's, from its `object_index` -- and `doors.py` requires that kind for the `door_ref` it resolves out of the same `nearby` block, so **nothing walks the character up to a door**. The kinds were left alone and the gap recorded in `LIMITATIONS.md` beside the missing square tier, because widening a contract on both sides to no observable effect, while the destination square a walk resolves against is still absent, is a change nobody could confirm. `inventory.py` carried the mirror error (`move_near` does take a container reference). The fourth is in safety code: the reflex block rung called itself non-redundant because the mod fills `safety.danger_level` "from a value it never computes" -- it computes it, in the very `dangerFloor` this branch gave a measurement clock; the rung is still not redundant, for a true reason. The audit's remaining survivors were then verified by hand rather than taken on an agent's word, and three more were false, two in safety code. **`ReflexGuard`'s vulnerable-action rung has never fired**: `DEFAULT_VULNERABLE_ACTIONS` is matched against `ActionState.type`, and the shipped mod never fills it -- the observation's action block is `Ownership.describe`'s table, which has no `action_type`, the one field `ObserveModel.action` reads into `type` -- so §17.2's "interrupt a read or a meal when a zombie is near" is dead code; bounded rather than absent, because the flee rung above ignores the action type, so what is lost is the earlier reaction at `interrupt_at`. And **going stale does not disarm the mod**: `sidecarStale` is read in three places that only refuse, `Safety.disarm` comes only from a command, a new session or a panic stop, yet two sidecar sites promised a "stale-sidecar disarm" backstop -- one of them in the notice an operator reads. The residue is a stale mode reading, not a running agent, since `mayStart` refuses all but stop/disarm/cancel while stale. Both recorded in `LIMITATIONS.md`; neither behaviour touched, because both fixes live in safety-critical mod code no test here can exercise. A third, `combat_mission`'s seal, named a refused-admission history that cannot reach the line. The audit's last six survivors were then verified and corrected too -- all false, none needing a behaviour change. Two mattered: `ObserveModel.dangerFloor` claims to read the observation's fields and to count zombies on another floor as present but never closing, and does neither -- its caller passes the raw reader table with flat `x`/`y`/`z` while the floor test reads `zombie.position.z`, so every zombie counts as same-floor; the error runs toward caution, so the docstring was fixed and the code deliberately was not, since reading the flat `z` would make a safety guard less conservative on static reasoning alone. And the wound reference, documented as having no cross-language format, is split by `policy.medical.wound_body_part` for the body part -- a layout change would have silently disabled bandaging by location. The others: `Counters:reset` has no production caller, so sequence numbers are game-session scoped, not handshake scoped; `PZAgent.Json` escapes a corrupt byte rather than refusing it; and `autonomy.py` twice said "nothing in the protocol's action set" rests or treats a character, when `survival.rest`, `survival.sleep` and `medical.bandage` have been there since P3. Both autonomy omissions stand, now for true reasons. Both new dead gates were then added to `tests/contract/test_gates_without_producers.py`, which had three rows, all found by accident; these two were found on purpose, and each pattern was checked in both directions -- no match against the mod today, a match when a plausible producer is spliced in -- with the `action_type` window measured (return table ends at 784 characters, nearest unrelated occurrence at 2296) rather than guessed. Five rows now, two safety-relevant. A deliberate sweep for the dead-gate class then found the same shape one layer below the square tier: **the item-detail tier speaks two vocabularies**. Measured, not estimated -- `food` 22 keys read against 8 sent with 6 agreeing, `literature` 11 against 5 with 2, `fluid` 16 against 3 with 1 -- and `ObserveModel.domain` passes names through verbatim, so the sharpest cases are one fact under two names (`pages` vs `pages_total`, `amount`/`capacity` vs `remaining_units`/`capacity_units`, a boolean `rotten` against a `freshness == "rotten"` test). Nothing errors because every reader defaults a missing key, so `FoodView.is_rotten` is always false and the world reads uniformly bland. `poisonous` and `tainted` do cross, so the sharp hazards are still refused; what is lost is rot, portions, pages and alcohol. Recorded in `LIMITATIONS.md`, not repaired: choosing which side renames is a cross-language contract decision whose only real test is a live game. Verifying the sweep's remaining claims by hand then produced the third gap of the same shape, and the largest since the square tier: **a world container can be named but never resolved**. `InventoryView.container` searches `inventory.containers` alone and `resolve_container` refuses anything outside it, while the mod's inventory has exactly two roots -- main and worn, with carried nested inside items -- and no path adds a nearby crate. `buildObject` does mint the crate a container reference, so a planner sees it and names it; it just points into a list the crate was never added to, so `container.inspect` refuses `INVALID_REF` at its precondition and `inventory.transfer` refuses its source the same way. With the missing square tier above it, `loot_area` is blocked twice: cannot walk to the crate, could not open it standing there. Recorded in `LIMITATIONS.md` and added to the dead-gate ledger (six rows now) with a pattern checked both ways. The sweep's last three claims were then checked by hand: `ItemView.extra["weapon"]` is real (combat policy reads a weapon block the mod never builds, while the mod puts the condition in the player's stats where nothing reads it -- safe direction, since unreadable makes the policy refuse, and behind experimental `combat_assist` anyway); `player.present == False` is a **benign** dead gate worth a row so nobody mistakes the gate for the mechanism (with no character the mod publishes no observation at all, which the engine already handles); and `chain.on_person == False` was not counted, being a consequence of the world-container row rather than an independent root. Eight ledger rows, three of them one root -- the two sides spelling the same fact differently. The seam itself is then checked mechanically rather than field by field: `tests/contract/test_item_domain_vocabularies.py` re-derives both vocabularies from source, re-derives the counts `LIMITATIONS.md` quotes, and pins the exact set of keys the sidecar decides on without a producer, so a new mismatch fails a test instead of waiting to be found by hand. It caught its own author on the first run -- the `fluid` set was written from memory and wrong in three keys -- and the derivation sharpened a safety claim: each hazard key crosses in exactly one block (`poisonous` in `food`, `tainted` in `fluid`), so poisoned food and tainted water are refused but the crossed pairs read false. The same check was then pointed at the player's open stats map, expecting the item blocks over again -- and it is **clean**: every stat the sidecar reads is one `Observe.playerStats` sends, `observe.wounds_unknown` has its producer in ObserveModel's limit block, and nothing reads as a default for ever; the emptiness is now asserted, so the disease stays confined to the item-detail blocks. Reading the mod also corrected the `ItemView.extra["weapon"]` row written a commit earlier: the mod puts the weapon's wear in the stats map *deliberately*, saying why ("the item tier has no condition field in the schema") and refusing to fabricate one, so it is one bridge never built rather than two vocabularies drifting. The rest of the seam was then checked the same way, and **the divergence is local rather than the seam's nature**: every structural tier agrees exactly -- the item's own fields (12 keys), the container's (7), the zombie's (6), key for key, with the zombie block keeping `visible`/`chasing`/`state` tri-state on both sides. The three that diverged are precisely those passed through as raw `JsonDict` where nothing forced agreement, which is also why the schema declares them as objects and constrains no properties. Wherever a typed dataclass faces an explicit Lua table the two agree, so the repair is bounded -- give `food`, `literature` and `fluid` the treatment the other tiers already have, plus the one unbuilt bridge for the weapon's condition -- and still only confirmable live. An inventory of the 47 contract tests then placed all of this: agreement is already machine-checked for adapter arguments, capability declarations, capability evidence, the engine API inventory, the MCP tool surface, four wire schemas and the documented CLI -- and the one seam with **no** such check was the observation document's field vocabularies, which is exactly where the eight dead gates accumulated. The map now lives in `tests/contract/__init__.py`, which was empty, so the next person looks there before writing a duplicate checker as this branch once did. The handoff the game machine reads was then found to have fallen behind: `LOCAL_GAME_HANDOFF.md` §4 still opened with "three parts of the sidecar are wired to a mod that cannot drive them" while the ledger had held eight for several commits, and the three most consequential findings of this branch were missing from the one document a live session is planned from -- the same stale-document defect this pass exists to remove, aimed at the handoff. All eight are now listed with what each looks like from the chair, and the priority list gained the one experiment that would change what the agent may do: start a read or a meal, let a zombie approach, confirm the character stops at all, because §17.2's earlier interrupt is dead while the flee rung above it is not. Recorded, not closed: `nearbyObjects`' silent failure is a wart — all six sidecar consumers of `nearby.objects` abstain on an empty list, no local map banks it as knowledge, no goal completes on the absence. **Reproduced, unfixed, and larger than a stabilization fix: the agent cannot walk.** `movement.move_to`, `move_near`, `world.inspect` and the local map all find the destination square by scanning `nearby.objects` for `kind == "square"`, and the mod emits no such entry — object kinds come from the container type, `getObjectName` or the literal `corpse`, `Refs.KIND.SQUARE` only mints reference strings, `nearbyFields` has no square tier, and `"loaded"`/`"blocked"` appear nowhere in the mod's Lua. A one-square walk east refuses `TARGET_NOT_LOADED`; every navigation leg goes through it. Invisible until now because the sidecar's own fixtures mint the square objects the mod never sends — the same green-that-does-not-cover shape as the dead test group, across a contract boundary. Relaxing the sidecar is the forbidden direction. Building the mod's half was attempted and **rejected under adversarial verification**: it fixed walking by breaking drinking (square entries collide with the `Refs.buildSquare` refs `buildObject` already mints for world objects, so `nearby_object` resolved a sink to the ground under it and `consume.drink_source` went accepted -> `NO_SAFE_DRINK`), it published a glass wall as open ground on a build exposing `isSolid` but not `isSolidTrans`, and it starved the planner's 24-slot compact view one layer below the fix (a furnished room lost nine real objects including a door; a warehouse aisle delivered zero squares). The three blockers are recorded in `LIMITATIONS.md` with their measurements as the specification for a real fix; the attempt is not in the branch. One consequence of that reference scheme was fixed independently, because it bites without any tier: a square reference denotes the place **and everything on it** (`source_ref` is a `RefKind.SQUARE` by contract and the mod reads it back the same way), the mod scans several objects per square by design, and `consume.drink_source` asked only `nearby_object` — the first match — so a tree scanned before a sink had the square refused `NO_SAFE_DRINK` with a sink standing on it. It now asks every object carrying that reference, and a square with nothing watery on it is still refused |

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
had not been started. `docs/control/PLAN.md` and `STATUS.json` are historical.

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
- `scripts/master_report.py` prints the mandated block.
- `scripts/verify_carryover.py` re-runs a task's regression test before its
  `PASS` is believed. Nothing inherits a pass from the old model.
- `tests/unit/test_master_plan.py` proves each of those refusals by planting the
  violation it exists to catch, and includes a control asserting a clean plan is
  admitted — without which a gate that rejected everything would satisfy them
  all.

### The figures, as of `521f1e4`

Run `.venv/bin/python scripts/master_report.py` for the current ones; these are
not maintained by hand and will drift.

```
OVERALL WEIGHTED PROGRESS: 40.1%  (1244/3104 weight)
TOTAL TASKS 480 · PASS 218 · IN_PROGRESS 33 · FAIL 0 · BLOCKED 0 · NOT_STARTED 229
CHECKS: 0/54 PASS

CODE IMPLEMENTATION    56.2%   (682/1213)
WINDOWS COMPATIBILITY  89.3%   (259/290)
MCP OPERABILITY        58.3%   (154/264)
VOICE OPERABILITY       0.0%   (0/324)
RC PACKAGING           17.3%   (35/202)
LIVE GAME VALIDATION    0.0%   (0/599)
FINAL RELEASE           0.0%   (0/200)
```

Three of those seven are at zero. **Live game validation is 599 of 3104 weight —
a fifth of the project — and nothing in this environment can move it**, because
nothing here can start Project Zomboid. Every task in those epics is owned
`local`, and the gate refuses to mark one `PASS` from here at all. "Done" is not
a word that applies to this build.

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
sixteen definitions in `tests/game-smoke/` name what closes each one.

Two non-scenario items also need a real installation:

- **Which file Build 42.20 ships its version in.** Only the `versionNumber=`
  header in `console.txt` is confirmed. The install-side candidates
  (`version.txt`, `version`, `media/version.txt`) are unverified guesses. The
  behaviour is honest either way — an unreadable or absent file reports
  `known=False` with the reason recorded, never a substituted `TARGET_BUILD` —
  but which path actually exists is unknown until someone runs `pz-agent
  doctor` against the game.
- **A real "is the game running" probe.** `BackupManager.restore` requires
  `game_running` as a keyword with no default and no override, so the rule
  cannot be bypassed by accident. Nothing yet supplies it from an actual
  process check; whoever wires the CLI must, and a wrong answer here is the one
  that corrupts a save.

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
| S01–S20 live scenarios | **live** |
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

## Deviations from the blueprint

| Blueprint | Here | Why |
| --- | --- | --- |
| Python 3.12+ | `requires-python = ">=3.11"` | The build environment runs 3.11; CI tests both 3.11 and 3.12 so the 3.12 target stays honest. No 3.12-only syntax is used. |
| A single `setup` command (blueprint §14.2) that detects, backs up an existing same-id mod, installs, creates config, runs doctor and prints launch steps | `install-mod`, with `validate-config` and `doctor` as separate steps that `docs/QUICKSTART.md` sequences | The composition is a preference; **the backup step is a deliberate refusal**. `install-mod` audits the destination before writing anything and raises `ForeignFileError` on the first file pz-agent did not install, or on any installed file whose hash has changed. Backing up and overwriting would still have overwritten; refusing and naming the file does not. Nothing is written when the audit fails. |
| A `support-bundle` command (blueprint §14.7) | `logs --bundle`, with `--verify` | Same subsystem, reached through the command a user is already in when they need it. `docs/TROUBLESHOOTING.md` gives the real invocation. |
