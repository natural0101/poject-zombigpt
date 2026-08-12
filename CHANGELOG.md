# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Five versions move independently — product, protocol, schema, mod and the
supported build range. `scripts/check_versions.py` fails the build when they
drift out of sync with `pz_agent_core.version`.

## [Unreleased]

### Fixed

- **Thirty-three defects of one family: evidence read without checking whose it
  was, or when it was written** (`stabilize/arm-session-confirmation`). A static
  audit along the P0 causal chain — mod visibility, session identity, heartbeat
  freshness, the two-phase arm, terminality, pointer and sequence recovery, one
  action, goals made of several — starting from one found in `pz-agent play`,
  generalising its shape across the sidecar, and then running the same three
  families against the mod, which is where every one of the 2026-08-08 live
  findings had lived, and finally against the tri-state rule itself. Sixteen on
  the sidecar, seventeen on the mod. Every fix was
  watched red first; a hypothesis that could not be turned red was reported as a
  hypothesis and left alone.

  *Claims resting on evidence that did not prove them.* `play` confirmed its arm
  from any fresh heartbeat reporting armed in the requested mode, without
  comparing the session — so a heartbeat left by an **earlier** sidecar could
  confirm an arm this session never got, and the command exited 0. `doctor`
  PZD010 called a session active from an undated read, telling the owner of a
  game that crashed an hour ago that it was live, two lines under PZD006 saying
  that same heartbeat was stale. `HeartbeatMonitor` read a *future*-stamped
  heartbeat as fresh for the whole of the skew, so a peer whose clock ran ahead
  could publish once and stop forever while every reader saw "fresh" — the
  handshake and the snapshot reader already refuse a document stamped past their
  window; the heartbeat was the one with no ceiling in that direction. The
  `engage_single_zombie` mission reported a kill it had not made: a failed window
  plus a succeeded `combat.shove` — whose adapter verifies "down **or strictly
  further away**" — plus the zombie leaving the nearby tier closed the goal as
  done, the shove that pushed it away serving as the evidence. `avoid_threat`
  read a *missing* nearby tier as an open horizon, completing a retreat with a
  chaser four tiles out. `status` printed a silent heartbeat's armed/mode/player
  as the state now, three lines under the word "stale".

  *A previous run's document taken as current.* The snapshot reader compared
  session-scoped sequence numbers **across** sessions, so after a game session
  change it refused every new snapshot as a rewind and the sidecar went blind
  while handshake and heartbeat both looked healthy. A fresh reader also adopted
  the previous session's slots as its first observation — the picture an attach
  and an arm decision are made against. Both are now scoped to the session the
  sidecar handshook. An arm request stamped ahead of the tick's clock was
  consumed and armed the run, and a pid record stamped ahead read as a ticking
  sidecar forever: the same one-sided subtraction of two processes' clocks.

  *Work that reached no end.* A disarm superseding a pending arm countermanded
  nothing at the game, so the mod could finish arming into a mode the sidecar had
  abandoned. `disarm` stranded a suspended goal for ever — exempt from the
  pending TTL because activation was supposed to resume it, and activation
  refuses once disarmed — holding an open slot no timer could reach. A mission
  whose step record aged out of the channel's bounded history left the wrapper
  proposing nothing tick after tick, the goal idling to its wall clock; it now
  ends `CAPABILITY_UNAVAILABLE` on the next tick, identically across all six
  goal kinds. A journal recreated at an earlier serial now reports the loss
  instead of silently renumbering.

  *And the same three families on the mod side.* A running command had two
  bounds and both read one clock — the lease and the adapter timeout — while
  `now()` returns a constant on a build without `getTimestampMs`, which is one
  more Kahlua gap of the kind that live run hit; against a frozen clock neither
  can fire, so the adapter was polled without end and the sidecar waited on an
  ack nobody would write. A raise anywhere in admission killed the whole tick
  and left that command with **no ack at all, ever** — the reader has already
  tracked it, so every redelivery classifies as a duplicate, and duplicates are
  deliberately never acked. The trigger that made that reachable: registration
  validated an argument's type and enum values but not its numeric bounds, which
  are used only in a comparison against the arriving value, so a non-number
  raised at dispatch in a file whose contract is that malformed declarations are
  caught at load.

  The replay cache had no session dimension while outliving any one session, so
  a restarted sidecar reusing an idempotency key met its predecessor's stored
  result: `succeeded` replayed on evidence about objects whose runtime ids no
  longer denote the same things, **addressed to a session nobody is listening
  on** — so the live command went unanswered too. A session the mod had already
  closed could be reopened by re-presenting its own document. And the mod's
  armed state survived a session swap entirely: `Safety.disarm` was reached by
  an explicit disarm and by a panic stop and by nothing on a session change, so
  a second sidecar inherited authority it never asked for and could not see it
  held — it believes it is in OBSERVE while the mod accepts mutating commands.

  On the observation side: an unread body and an unhurt one produced the
  identical document (`wounds` is omitted when empty, and `treat_wounds`
  completes on exactly `bleeding_observed == 0`); the nearby scan reported a
  complete scan of an empty world when it could not read the world at all;
  `survival.rest` promoted a rest to succeeded with no departure reading; and
  `medical.bandage` verified on a dressing the wound was already wearing.

  *An absent reader is not a false answer — five more places.* The sharpest is
  safety-critical: the zombie scan returned an empty list on three failure paths
  (`getCell` missing, raising, or answering nil; `getZombieList` missing), all
  indistinguishable from an empty street, so the danger floor counted zero and
  answered `DANGER.NONE`. Three deterministic consumers act on that with no model
  in the loop — the mod's gate, the sidecar's threat abort, and the reflex guard,
  whose only "nobody could tell" channel is a table the mod always supplies. On
  any build where `getZombieList` is absent or renamed, an armed AUTONOMOUS agent
  was cleared to work, reason `POSTCONDITION_MET`, on a scan that never happened
  — the README's threat-interruption guarantee, on a count nobody took. The
  schema requires `danger_level` and has no absent form, so the floor now answers
  HIGH when the zombies could not be read (the lowest rung reaching both block
  thresholds: it starts nothing and interrupts a vulnerable long action without
  claiming an emergency nobody saw), with a `zombies_unknown` counter beside it
  so the HIGH cannot be mistaken for a measurement.

  Four more, one layer down, all from the same shared snapshot helper publishing
  an inventory nobody could walk as an empty one: `inventory.search` answered
  "you carry no bandage" about a character nobody read; `consume.eat`/`drink`
  read an item's absence from an unreadable inventory as it having been eaten;
  `equipment.unequip` read a double absence across a worn set and a hand that had
  stopped answering as a garment taken off; and `snapshotBody` flattened
  `bleeding` to false while `medical.bandage`'s whole postcondition is
  `bleeding == false` — the exact ack that file's own header says it exists to
  prevent. Each ends in the same place: the mod mints `verified` from any
  succeeded ack carrying evidence, so a verify concluding from a flattened
  absence promotes the capability in the very document the sidecar gates its
  write tools on.

  *A floor nobody has measured recently is not calm.* Closing the zombie-scan
  gap left its neighbour standing, and the neighbour turned out to be reachable.
  `Safety.setDanger` is called from exactly one place — the end of
  `Observe.context` — so every tick that fails *before* that point leaves the
  previous reading in `agent.safety.danger_level` while the mod keeps
  heartbeating and keeps accepting commands. It was reproduced end to end: arm an
  agent, observe once against a calm street (floor `none`), then remove `getCore`
  so `Heartbeat.detectBuild` fails, and tick twelve more times across sixty
  seconds with the sidecar heartbeat renewed and two chasing zombies now present.
  `Safety.mayStart(safety, "consume.eat", …)` answered **`POSTCONDITION_MET`** —
  cleared to act, on a reading a minute old, taken before the horde arrived. The
  floor now carries the clock that measured it (`danger_seen_ms`), and `mayStart`
  refuses a mutating action whose floor has not been re-measured within
  `DANGER_MAX_AGE_MS` (30 s, six sidecar heartbeat windows) with
  `PRECONDITION_FAILED` naming the missing *measurement* rather than a threat
  nobody saw. The refusal sits after the always-allowed and read-only returns, so
  `world.inspect` is never blocked and the state clears itself the moment one
  observation succeeds.

  *A square is asked about, not the first thing standing on it.* A water source
  is addressed by its **square** — `source_ref` is parsed as `RefKind.SQUARE` and
  the mod's `Consumption` adapter reads it back the same way and looks for water
  on that square — and `ObserveModel.buildObject` accordingly mints the square's
  reference for everything that is not a container or a door. One reference
  therefore denotes a place and everything on it, and the mod scans several
  objects per square by design. `consume.drink_source` resolved it with
  `nearby_object`, which is `next(o for o in nearby.objects if o.ref == ref)` —
  the *first* match. A tree scanned before the sink answered for the sink, and a
  square with a sink on it was refused `NO_SAFE_DRINK`, "nothing at
  square:…:1201:3400:0 reports water". The question is now asked of every object
  carrying that reference, and the refusal's evidence lists all their kinds
  instead of one; a square with nothing watery on it is still refused, which is
  the test that keeps the fix from becoming a formality. `nearby_object` keeps
  its single-match meaning for the callers that want it and now says in its own
  docstring why a property question needs `nearby_objects`.

  *A registered adapter that no test had ever built.* Chasing the reference
  defect above turned up `DrinkSourceAdapter` in a state nothing was watching
  for: registered on the dispatcher, exported from the package, offered as an MCP
  action — and constructed by no test anywhere in the repository. Its refusals
  and its postcondition had never run. A census of the registry found it was the
  only one of the twenty-six, so the gap is closed rather than wide, but it was
  found by hand while looking for something else, which is not a method.
  `tests/contract/test_registered_adapters_are_tested.py` now fails when a
  registered adapter is built by no test; it is checked non-vacuously (drop the
  consume tests and `DrinkSourceAdapter` is reported while `MoveToAdapter` is
  not), and it excludes itself from its own corpus so its failure message cannot
  count as the coverage it is complaining about. The adapter's own postcondition
  is now covered too — thirst falling proves the drink, an unchanged *or risen*
  thirst proves nothing, and the evidence still carries `source_ref` so an
  ordinary sip from a bottle cannot stand as confirmation of a capability nobody
  has seen work.

  *The planner was told a street was empty on a scan that never ran.* The mod
  publishes its own accounting of every reading it could not complete — eighteen
  counters under the `observe.` prefix in `player.stats`, which is an open scalar
  map, so all of them arrive on this side intact. **Nothing in the sidecar read a
  single one.** An earlier wave noted the declaration had no listener and left it
  as contract-shaped; the consumer that makes it matter is
  `compact_for_planner`, the only picture a planner is ever given, and it builds
  `stats` from a whitelist of five. So a zombie scan that could not run — the
  safety-critical case fixed on the mod side earlier in this pass, where the
  floor now answers HIGH — reached the model as `zombies: []`, `zombie_count: 0`,
  `zombies_truncated: false`: a positive claim that the street is empty *and the
  reading complete*, about a scan that never happened. The counts and truncation
  flags there are about what **this** side dropped, and cannot say what the mod
  could not read. The compact document now carries an `unread` block and the
  nearby tier a `zombies_unscanned` flag. The block is generic on purpose —
  enumerating the counters would leave the next one silently dropped, which is
  the state all eighteen were in — and it gets the treatment `moodles` already
  gets for the same reason, token-checked names and a cap, because it is an open
  map arriving from the game side. The compact document is serialised to the
  model wholesale, so this needed no prompt change to become visible.

  *A zombie whose intent nobody could read was assessed as calm.* The mod omits
  `chasing` when the build exposes no `getTarget`, and says so in its own
  comment: an absent accessor "must stay absent so the sidecar is told it could
  not be read". The schema agrees — only `ref` and `distance` are required. The
  sidecar's parser read that absence as `chasing=False`, a positive claim that
  the zombie is not hunting. `NearbyZombie` states the rule it broke in the file
  itself: `state` is `str | None` with a comment that an unreadable body state
  must never read as "standing", while `chasing` — which that class's own
  docstring calls more important than distance for the reflex guard — defaulted
  to `False`. The cost lands in `_zombie_level`, whose three ladders "differ by a
  full rung at every band": a chaser at contact range was assessed as an unaware
  zombie, one rung down, on every build without that accessor. The navigation
  executor lost the same fact — it adds `CHASING_STEP_COST` around a chaser's
  square, and an unread intent silently skipped it, routing the character past a
  zombie nobody could rule out. `chasing` is now `bool | None` end to end,
  omitted from `to_dict` rather than sent as a schema-invalid null; the threat
  ladder and the route cost both treat "could not say" as a possible chase, the
  same cost this pass already accepted for the failed zombie scan; and
  `chasing_count` stays a count of *observed* chasers, with
  `chasing_unknown_count` beside it, so the reason spoken to the player never
  says "three chasing" about one chaser and two the reader could not answer for.
  The local map's same-tick merge over the three states is explicit for the same
  reason: an observed chase wins, but an unread intent survives rather than
  collapsing to the calm neither reading claimed.

  *A test that never ran.* An earlier hand-merge in this same pass appended the
  zombie-scan group to `tests/lua/test_observe.lua` **after** `Harness.finish`,
  which calls `os.exit`. Eighty-eight assertions — the ones covering the
  safety-critical fix above — were present, readable, named in the ledger and
  executed never; the suite reported 146 passing and exited before reaching them.
  This is the pass's own defect family turned on its evidence: a green count that
  did not cover what it appeared to cover. `finish` now sits at the end of the
  file and the suite runs 234.

  Two flattenings were examined and deliberately left: `player.alive` is false
  for both a corpse and a build without `isDead`, but every consumer refuses or
  stops on it and none acts, so the cost is a wrong sentence rather than a wrong
  action; `player.moodles` flattens an absent reader into "no moodles", whose
  only consumers propose *less*. Editing for either would have fixed nothing.

  Recorded and deliberately **not** fixed, a third door onto the same silence:
  the mission-cap eviction path marks a drive abandoned and banks its report
  without ending the goal. It fired in no test, and reproducing it needs a
  concurrency the queue does not currently produce.

  **One finding is reproduced, unfixed, and larger than a stabilization fix: the
  agent cannot walk.** `movement.move_to`, `movement.move_near`, `world.inspect`
  and the local map all locate the destination square by scanning
  `nearby.objects` for an entry whose `kind` is `square`, reading `loaded` /
  `blocked` / `closed_window` / `drop` from its semantics. The mod has no code
  path that emits one. `Observe.nearbyObjects` sets each entry's `kind` from the
  container type, from `getObjectName` lowercased, or to the literal `corpse`;
  `Refs.KIND.SQUARE` appears only where reference *strings* are minted and
  parsed; `nearbyFields` exports `objects` and `zombies` and no square tier; and
  the strings `"loaded"`, `"blocked"` and `"closed_window"` occur nowhere in the
  mod's Lua at all. Driven against a document assembled the way
  `Observe.nearbyObjects` assembles it, a one-square walk east and a walk up to a
  container the mod *did* report both refuse with `TARGET_NOT_LOADED` — "no
  loaded square was reported at (1201, 3400, 0)". Every navigation leg goes
  through this, so it is a total functional outage of movement, and it was
  invisible because the sidecar's fixtures mint the square objects
  (`tests/fixtures/adapter_worlds.py:a_square`) that the mod never sends: the
  same green-that-does-not-cover shape as the dead test group above, this time
  across a contract boundary. It is **not** fixed here. The sidecar side must not
  be relaxed — that is the forbidden direction, and it would walk the character
  onto squares nothing assessed. The mod side means building the half of the
  interface that was never built: a square record per scanned square, with a
  solidity read behind `blocked`, since emitting `loaded` alone would assert
  passability nobody measured. That is a scope decision, not a minimal edit, and
  it is written down for the owner rather than taken unilaterally.
  `TROUBLESHOOTING.md` carries the symptom so a live tester does not spend the
  session hunting their install.

  An implementation of the missing tier was built and adversarially verified, and
  it is **not** in this branch: three regressions were proven end to end against
  the mod's own published bytes. It fixed walking by breaking drinking —
  `buildObject` mints a non-container, non-door world object's ref with
  `Refs.buildSquare`, so every tree and water source inside the tier shared a
  reference with the ground under it, the square sorted first, and `common.nearby_object`'s
  `next(o for o in … if o.ref == ref)` returned the square: a sink one step away
  went from `consume.drink_source` accepted to refused `NO_SAFE_DRINK` on the same
  document. It published a wall as open ground on a build exposing `isSolid` and
  not `isSolidTrans`, because "at least one reader answered" was treated as "the
  question was answered" — the glass wall the two-reader design was justified by
  is published `['loaded']` and the walk into it is accepted. And it starved the
  planner one layer below the fix: `compact_for_planner` keeps the nearest 24
  objects in one merged list with one cap, so a separate square budget in the mod
  does not survive it — a furnished room lost nine real objects including a real
  door two squares east, and a warehouse aisle delivered zero squares to the
  planner. All three are recorded in `LIMITATIONS.md` with their measurements,
  because they are what the real fix has to solve; shipping the attempt would have
  traded a named refusal for two silent wrong answers.

  That fix closes the first of the three square-tier blockers, which changes what
  a tier now has to solve. Every other reference resolution against
  `nearby.objects` was audited and each asks a *position* question rather than a
  property one — `movement.move_near` in all four of its uses and the planner
  critic's `_destination` — and position is shared by construction, since
  everything answering to a square's reference stands on that square. The square
  lookups themselves match on kind and position, never on the reference. A
  regression test pins the exact case that killed the attempt (a `kind="square"`
  entry listed ahead of a sink at the same reference, drink still accepted) and is
  verified non-vacuous: restore the first-match resolution and it fails with the
  original `NO_SAFE_DRINK`. Two blockers remain — the partial solidity read and
  the planner's compact view.

  Two more are recorded with their reasoning rather than closed. The first is a
  **decision**, not a stabilisation fix: the age check above refuses a *stale*
  floor but accepts a floor that was **never** measured, because
  `Safety.newState` starts at `DANGER.NONE` with no timestamp and three suites
  arm without observing at all (`tests/lua/support/command_support.lua:201`,
  `test_action_runtime.lua`, `test_movement_runtime.lua`). Deleting one clause —
  `dangerAge ~= nil and` — closes it and breaks those three, which is a contract
  change about whether arming requires an observation. The options are (a) refuse
  a nil age like a stale one, (b) gate *arming* on a successful observation
  instead of the gate, or (c) accept it on the record; nobody has picked one, so
  it is written down here rather than chosen unilaterally. The second is a
  **wart** with its trace, so it need not be re-investigated: `nearbyObjects`
  fails the same silent way the zombie scan did, but all six sidecar consumers of
  `nearby.objects` abstain on an empty list rather than concluding from it —
  there is no local map that banks a scan as knowledge, and no explore or loot
  goal completes on the absence of a frontier or a container. The honest end has
  no listener, so declaring the gap would fix nothing today.

### Added

- **A terminal is enough to play** (`epic/ux-one-command-play`, wave 1). The
  agent had a goal channel with two ways in — an MCP tool call, which needs a
  language model on a stdio pipe, and a Russian phrase, which needs a
  microphone — and a user with a keyboard could arm it and then had no way to
  tell it anything. Three commands close that: `pz-agent play` runs the whole
  cold-start sequence (validate, start the sidecar, wait for the game, arm) as
  one command, where every wait is bounded twice — a deadline *and* a poll
  count, so a stopped clock cannot hang it — and arming is granted only when
  the game's own heartbeat reports it in the mode that was asked for; a wait
  that runs out is a failure carrying what the heartbeat actually said, never a
  success in a quieter tone. It refuses in front of a panic latch with the arm
  path's remedy and never clears one, and it never touches the game process:
  launching Zomboid stays the user's, which is why the wait comes with
  instructions rather than a spawn. `pz-agent goal submit/status/cancel` sends
  the same typed `GoalRequest` over the same Core RPC link the MCP server and
  the voice companion use — the terminal is not a privileged caller, it meets
  the same 14 kinds, the same parameter ranges and the same refusals, with the
  valid values printed by the command that refused. There are deliberately no
  `pause`/`resume` verbs: touching the controls *is* the pause, and parking a
  goal so another may run belongs to the arbiter, which decides from an
  observation rather than from a command line. `pz-agent status --watch` keeps
  a compact HUD on screen — ANSI on a terminal, separators into a pipe, never
  an escape byte down a redirect.
- **The goal wire caught up with the goal model.** The remote codec now carries
  the suspension bookkeeping (`suspended_by`, `suspensions`,
  `active_ms_before_suspend`, `front_rank`), the two care parameters
  (`target_endurance`, `hours` — so `rest_until` and `sleep_until_rested` are
  submittable over the link for the first time), and the channel status tails
  (`progress`, `paused`, `report`). All of it decodes absence to the model's own
  defaults rather than inventing state, and every surface that renders it prints
  `unreported` for what the wire did not say — never `no` and never `0`, because
  "the agent is not paused" and "this build could not tell me" are different
  sentences and only one is safe to act on. `schemas/goal.schema.json` declares
  the four suspension keys it was closed against, with a conformance test on a
  record that actually carries them.

- **The character can defend itself — assisted, bounded, and never on its own
  initiative** (`epic/p4-assisted-combat`, wave 1). Four new protocol actions
  (`combat.equip_best`, `combat.shove`, `combat.engage`, `combat.retreat` —
  ActionName 27–30) under a new `combat_assist` capability that starts
  EXPERIMENTAL and can only be promoted by a live shove observed against the
  real game; the pre-existing `autonomous_attack` ceiling stays `unsupported`
  and is pinned untouched on both sides. `combat.engage` is one bounded
  window: 1–3 swings, hard 4-second wall clock, terminal with an honest
  reason either way — there is no loop in the mod. Sidecar-side
  `CombatPolicy` gates every engagement before a command is minted (group
  size over `max_group` — default 1, low endurance, panic, critical health,
  broken weapon are each a typed refusal, not a worse fight). The
  `engage_single_zombie` goal (14th kind) is deliberately parameterless —
  it re-selects the nearest live target every window rather than accepting a
  `target_ref` that could go stale into a kill order — and runs at most 4
  windows before honestly failing. Zombies now report a tri-state `state`
  (moving/prone/unknown) and the player's stats carry weapon condition, so
  "the swing landed" is an observed postcondition, not an assumption. The
  needs arbiter and autonomous initiative are pinned by test to never mint a
  combat action: in AUTONOMOUS mode threat still means avoid, never engage.
  (`epic/p3-survival-knowledge`, wave 1). `knowledge/gameplay/*.yaml` — 50
  rules across 8 domains, validated by `schemas/gameplay-knowledge.schema.json`
  and a loader that refuses claims dressed above their evidence:
  `verified_script` requires a code source whose repo path exists and test
  paths that exist (a deleted test demotes the rule loudly at load, not
  silently), `verified_live` requires a live evidence pointer, and PZwiki can
  never carry a verified status. The first corpus is distilled from the
  shipped code — 46 rules citing exact symbols and pinning tests, plus the
  honest split the directive demanded: "the code refuses rotten food" is
  verified_script, "rotten food sickens the character" is a separate
  wiki-sourced hypothesis. Bounded retrieval feeds the planner prompt only
  the rules relevant to the current goal, active needs and nearby objects
  (cap 12, ~4KB, UNVERIFIED markers on every hypothesis and every unverified
  number — the model must see which figures are guesses); a configured
  corpus that fails to load refuses the tick rather than planning without
  it. Three docs are *generated* from the corpus — `BEHAVIOR_REFERENCE.md`,
  `BUILD42_MECHANICS_SOURCES.md` (the provenance ledger), and the Russian
  `GAMEPLAY_AGENT_GUIDE_RU.md` — with a byte-drift gate in `check.sh`
  (`generate_knowledge_docs.py --check`), so code and prose cannot part ways
  quietly.
- **A need can interrupt the current goal — and give it back**
  (`epic/p2-goal-controller`, wave 3). The queue learned suspension without a
  new state: `suspend()` parks the ACTIVE goal at the front of the backlog
  with a marker, its wall budget stops burning while parked, and ordinary
  activation resumes it with exactly the remaining budget; a fourth
  suspension of the same goal is a typed refusal, so preemption cannot
  ping-pong a goal forever. On top rides the NeedsArbiter — AUTONOMOUS mode
  only, edge-triggered (a crossing, never a level): bleeding appearing
  outranks danger reaching HIGH outranks thirst outranks hunger past the
  policies' own critical lines. It suspends, injects the satisfy/care/avoid
  goal at the front, and on that goal's *any* terminal — success or failure —
  the original resumes mid-mission with its drive intact (a loot mission
  continues its candidate list after lunch). Every decision lands in a
  bounded ledger; a suspended goal shows its `suspended_by` through
  `pz_goal_status`. A restart mid-preemption restores the original as
  pending-with-marker while the in-flight preemptor honestly ends
  `SESSION_TERMINATED`.
- **Retreat is a route, not just a stop.** The local map remembers zombie
  sightings (bounded, decaying — stale threat is a guess either way, so the
  map errs toward caution within the horizon and forgets beyond it), and the
  route search tolls threatened squares so journeys detour around them —
  costs, never walls: a cornered character still finds the least-bad way out.
  `avoid_threat` (13th kind, speakable — «отступай», «беги») retreats to the
  nearest user safe zone or the square that maximises distance from every
  observed threat, and succeeds only on the observed postcondition: the
  nearest zombie at twice the threat ladder's close distance, or a safe zone
  with nothing chasing. Chasing threats at close range stay the reflex
  guard's band — no second driver under the wheel.
- **The mandatory survival chain runs without an LLM** (`epic/p2-goal-controller`,
  wave 2). `satisfy_hunger` and `satisfy_thirst` — speakable since the voice
  epic, LLM-served until now — are deterministic missions: read the stat
  (absent is a typed refusal, never zero), eat carried safe food first (the
  safety gates stay where they live, in the food/drink policies — the mission
  restates nothing), fetch from reachable containers when nothing is carried
  (memory-known category shelves first, nearest first, locked doors recorded
  as skips), transfer to main, consume, and claim success only when the stat
  is observed at the target. The user's reserves outrank hunger at any level:
  the agent fails typed before eating the strategic stock. Only the goal
  channel reroutes — the autonomy initiative path still asks its provider,
  and the asymmetry is documented in the contract test.
- **Three more care kinds** (12 total, nine deterministic): `treat_wounds`
  (speakable — «перевяжись»; bandages every observed bleeding wound
  worst-first, verifies each stopped from observation, honest partial failure
  when dressings run out), `rest_until` (target endurance, the rest adapter's
  own bounds), `sleep_until_rested` (the sleep adapter's danger>NONE refusal
  surfaces unchanged and is never retried into danger). Care missions carry
  phase tokens and sealed reports like the rest.
- **Goals survive a sidecar restart, honestly** (`epic/p2-goal-controller`,
  wave 1). One versioned `goals.json` beside the memory dir, written
  atomically at most once per tick and only when a goal actually changed
  state. On the next start the previous ACTIVE goal answers terminal
  `FAILED`/`SESSION_TERMINATED` — "the sidecar restarted while this goal was
  active" — never silence; PENDING goals come back under their original ids
  and idempotency digests (a resubmitted key resolves to the same goal, and a
  TTL that ran out during the downtime expires on the first tick). A corrupt
  file is set aside as `goals.json.corrupt` with a typed diagnostic, and the
  channel starts empty rather than guessing.
- **«Домой» is a word the agent obeys.** `return_home` (parameterless, the
  first new speakable voice goal since the voice epic) walks the character to
  the remembered home point through the deterministic navigation executor; no
  home set answers the exact remedy — "stand at home and run: pz-agent
  remember home". `explore_area` sweeps the unknown frontier of the local map
  within a scope (radius by default — exploring one's own room is a no-op),
  approaches each waypoint through a Journey, records locked doors as named
  skips, and claims `complete` only when no frontier cell remains. Nine goal
  kinds; four of them deterministic missions the LLM never touches.
- **`pz_goal_status` finally says what phase the work is in.** Additive
  `progress` (closed phase tokens + counters: approach/open/inspect/transfer,
  legs walked, waypoints visited), `paused` (the manual-takeover marker,
  invisible since the arm epic, now projected with its reason quarantined as
  untrusted text), and `report` (the loot/explore mission ledger, scrubbed,
  live or sealed). An LLM-served goal answers `progress: null` honestly — a
  deterministic phase is a claim only a deterministic server may make.
- **«Облутай квартиру» is now a typed goal** (`epic/p1-loot-area`, wave 2).
  `loot_area` (7 goal kinds) takes a scope — `room` (default), `building`, or
  `radius` 1..30 — plus `take_all` and an optional closed category list, and
  runs as a deterministic mission with no LLM call anywhere: pin the scope
  from the activation observation (a build that reports no rooms answers a
  typed refusal naming `scope=radius`, never a guess), discover world
  containers in scope, skip the ones the memory proves unchanged since their
  last inspection — recorded as a skip, not silently —, approach each through
  the navigation executor (a locked or barricaded door on the way becomes a
  skip reason naming the door), open, inspect, select by the deterministic
  loot policy (closed category vocabulary, user reserves outrank even
  `take_all`, greedy capacity fitting where an exact fit is a fit), and move
  items with `inventory.transfer_batch`. The mission ends `complete` only on
  the provable criterion — every candidate inspected or carrying a recorded
  skip reason — or `encumbered` the moment the main inventory provably cannot
  take the smallest wanted item. The terminal report survives in a bounded
  ledger: containers inspected, skipped with reasons, items taken per
  category, left per reason.
- **Observation grew rooms, buildings and corpses.** The player and every
  nearby object now carry tri-state `room`/`building` tokens (outdoors and
  "no reader on this build" are deliberately the same absence — a scope
  decision must not read the second as the first; names are normalised once,
  space to underscore, or dropped whole rather than mangled). Corpses with
  loot appear as `kind=corpse` container objects — observation-only for now:
  a dead body is not in the square's object list, so the world-container ref
  scheme cannot honestly address its inventory; the gap is recorded in
  `GAME_API_VERIFICATION.md` as a future protocol change, and the loot
  mission records such containers as skipped rather than pretending.
- **One command now moves a batch, and stopping honestly is part of the
  contract** (`epic/p1-loot-area`, wave 1). The dispatcher grew its first
  structured argument type: a declared, bounded LIST (dense array, 1..8
  elements, refs or plain strings, element-wise session checks, duplicates
  refused, a fresh sanitised copy handed to the adapter — and the
  type-dispatch fall-through that would have silently treated an unknown
  declared type as a ref is now a load-time refusal). On it rides
  `inventory.transfer_batch` (26 actions): up to eight items, possibly from
  different source containers, moved one at a time by the game's own transfer
  action with capacity re-checked before every enqueue. `succeeded` only when
  every requested item is observed in the destination; a capacity stop
  partway is a FAILED `CONTAINER_FULL` whose evidence carries the honest
  partial record — what landed, what stopped and why, what the batch never
  attempted. The Python half pre-checks the summed weights and, when only a
  prefix fits, refuses naming "the first k of n". MCP tool:
  `pz_action_transfer_batch` (41 tools).
- **The sidecar finally remembers what it saw in containers.** The
  long-orphaned `KnownContainer` store is now fed: every world container in
  an observation lands as a sighting, every enumeration (`container.inspect`,
  transfer evidence) lands as an inspection carrying `item_count` and a
  `content_revision` — a 16-hex change detector over the observed contents,
  documented as a detector, not an inventory. New queries for the loot
  planner: `container_unchanged(tail, revision)` (three-state honest: an
  empty revision is never "unchanged") and `uninspected_tails()`. Writes are
  batched (one flush per 10 s, plus shutdown), and a memory file another
  process wrote in between is re-read, never clobbered — the user's
  reservations outrank re-derivable sightings.
  square** (`epic/p1-doors-navigation`, wave 2). New `pz_agent_core.navigation`
  package: a bounded `LocalMap` (4096 cells, oldest-seen evicted first) that
  remembers only what observations proved — visited squares, obstacles, stairs
  as floor links, and doors as tri-state knowledge with their refs — and a
  `Journey` executor that plans A* legs bounded by the mod's 30-square walk
  limit, lets `allow_doors` handle closed unlocked doors in-walk, issues an
  explicit `door.open` only on the retry path after a door-shaped failure,
  folds `DOOR_LOCKED`/`DOOR_BARRICADED` answers back into the map and replans
  around them, and declares arrival only from an observed position — a
  `succeeded` move ack alone is never arrival. Every budget (search nodes,
  legs, replans, consecutive failures) is a typed refusal. `navigate_to` is
  now a first-class goal served entirely by this executor: the wrapped LLM
  planner is never asked (pinned by a spy in the contract test), and a loop
  with no planner configured at all still navigates. Voice deliberately
  refuses to carry it — coordinates are not dictated over a microphone, and a
  misheard digit walks the character somewhere else; the carve-out is
  import-checked. The remote RPC wire schema intentionally does not yet carry
  the kind (a `navigate_to` submission over RPC is a loud validation refusal,
  pinned both directions); local MCP and CLI serving is complete.

- **Doors are observable, addressable and operable**
  (`epic/p1-doors-navigation`). The 2026-08-08 live run found real doors and
  could decide nothing about them: the snapshot said only `kind=door`, the
  door shared a `square:` ref with everything else on its tile, and opening
  one took a human at the keyboard. Now: nearby doors carry `open`, `locked`,
  `barricaded` and `orientation` — each tri-state, absent when the build
  exposes no reader, because "the lock could not be read" and "unlocked"
  authorise different plans — plus their own stable `object:` reference. Three
  new protocol actions, `door.open`, `door.close`, `door.unlock` (25 total),
  ride a new `door_toggle` capability end-to-end: Lua adapter (walks into
  reach, toggles via the game's own `IsoDoor:ToggleDoor`, verifies by
  re-reading the door — a toggle the engine swallowed is `POSTCONDITION_FAILED`,
  never a claimed success), Python adapter (postcondition demanded from the
  *after* observation), MCP tools `pz_action_open_door` / `pz_action_close_door`
  / `pz_action_unlock_door`. A locked door answers the new `DOOR_LOCKED`
  reason code and a barricaded one `DOOR_BARRICADED` — distinct codes because
  they demand different replanning (a key hunt versus a detour). `door.unlock`
  demands an observably usable key and unlocks only through the game's own
  interaction, never by writing lock state.
- **`allow_doors` is now true, not just documented.** The move tools promised
  "may open doors on the way" while the flag was parsed sidecar-side and
  dropped. Both movement actions now declare and ship it (default true), and
  the mod honours it: a walk that stalls against a closed, unlocked,
  unbarricaded door toggles the door (verified by re-read), re-enqueues the
  walk, and records each opening in evidence — bounded at three doors per
  command. A locked or barricaded door on the route fails the walk with the
  door's own reason code naming the square. `allow_doors=false` behaves
  byte-identically to before, and a door whose state cannot be read is never
  touched.
- **`pz-agent latency` measures the P0 targets instead of estimating them.**
  A bounded reader joins the command and ack journals by `command_id` and
  reports exact nearest-rank p50/p95 for submit→accepted, accepted→started,
  started→terminal and end-to-end, plus observation cadence and heartbeat
  facts — labelling every cross-clock delta as such (the two processes' clocks
  are never corrected). `--targets` marks each P0 target MET/MISSED/UNMEASURED
  and never invents a number: a gameless machine reports UNMEASURED and exits
  0, and terminal-ack *visibility* is honestly UNMEASURED offline because no
  on-disk record carries the moment the sidecar read an ack. Live p95 numbers
  are the game machine's to produce.

### Fixed

- **The arm a client is told about is now the arm the game confirmed**
  (`epic/p0-windows-ipc-arm-recovery`). Live, `pz_session_arm` answered
  `armed=true` having armed only the sidecar; the game kept publishing
  `armed=false, mode=OFF` because no `session.arm` command was ever enqueued.
  Arming is now two-phase: the sidecar submits a real `session.arm` command
  through the queue and reports success only after observing *both* the mod's
  terminal `succeeded` ack *and* a fresh game heartbeat of the same session
  reporting `armed=true` in the requested mode — within a bounded window
  (default 5 s), after which the refusal names which half never arrived and a
  countermanding `session.disarm` closes the late-ack hole. Disarm stays
  locally ungated (user input wins) and notifies the game, surfacing an
  unconfirmed notification honestly.
- **A restarted sidecar is no longer a second producer.** The command queue
  seeded its outbound sequence at 0 on every start while the mod's journal
  still held records 0..N. The queue now recovers `highest+1` from the durable
  journal tail (bounded read, newest rotated generation when the live file is
  fresh, session-scoped), refuses with a typed error when bytes exist but
  nothing parses, and logs — rather than crashes on — a terminal ack for the
  previous process's in-flight command. In-process, a second `attach()` on an
  attached loop is now a typed refusal instead of a silent second
  `JournalWriter` over the same file.
- **The mod stopped colliding with everyone — including itself — on the
  snapshot pointer.** Live, the game repeatedly failed to open
  `observation.snapshot.pointer` for writing. The Lua IPC layer now retries a
  refused open boundedly (3 attempts, in-call), remembers the slot it last
  committed instead of re-reading the pointer from disk before every publish,
  carries a refused pointer commit over to the next publish (committed first,
  bounded at 10 publishes, and the pointer can never name a slot whose write
  failed), and reports a reader close that failed instead of discarding it.
  On the Python side, `read_json_document` gained a small read-side patience
  (4 × 10 ms — sized for a 125 ms tick, documented against the 0.5 s write
  budget) raising `SharingViolationError` so a locked file is distinguishable
  from a corrupt one, and `SnapshotReader` treats a locked pointer or slot as
  an honest per-poll miss that never regresses `_last_seq`. A two-process
  contention soak — a child process writing truncate-in-place exactly like the
  mod, torn slots included, against the real reader at 20 Hz — pins the whole
  protocol.
- **An action can no longer sit in `accepted` forever, invisibly.** New public
  tools: `pz_action_status` (a typed answer even for an id this sidecar no
  longer knows, naming the likely causes), `pz_action_await` (bounded wait for
  a terminal result; the name `pz_action_wait` was already taken by the
  in-game clock wait), and `pz_action_cancel_all` (mass cancel of mod-owned
  work only, idempotent, honest `null` for counts it cannot yet observe).
  `pz_session_status` now reports the game's own word beside the sidecar's —
  `desired_mode`, `effective_mode`, `game_armed`, and a tri-state
  `armed_mismatch` where "the game has said nothing" is not agreement. Every
  submission now carries a wall deadline (lease + grace) swept into a terminal
  `ACTION_TIMEOUT`, re-attach turns the previous attachment's records terminal
  instead of leaving clients polling `accepted`, and a manual takeover parks
  the active goal as paused-by-user rather than losing it (a new arm does not
  silently resume it).

- **The thirteen defects the first live session proved, fixed at their roots**
  (`epic/p0-build42-live-compat`; live run 2026-08-08, Project Zomboid Build
  42.20.2, Windows — the findings themselves are recorded in
  `docs/GAME_API_VERIFICATION.md`):
  - *The mod now appears in the Build 42.20 mod list.* `mod.info` declares
    `pzversion=42` (the real installer refuses `42.20`) and the empty
    `require=` line is gone. `TARGET_BUILD` stays `42.20` for the heartbeat;
    the new `MOD_INFO_PZVERSION` constant records the split, and the contract
    test pins both files to it.
  - *Adapters no longer depend on lucky load order.* Eight adapters
    (`Consumption`, `Containers`, `Equipment`, `Inventory`, `Literature`,
    `Medical`, `Rest`, `Sleep`) open with a statement-form
    `require "PZAgent/adapters/Toolkit"`; the dynamic-loading ban still holds
    (`require(` stays forbidden, and a new contract asserts every require in
    the mod names a `PZAgent/` module and nothing aliases the token).
  - *Kahlua has no global `next`, so the mod no longer calls it.* Every
    `next(t) == nil` emptiness check (`CommandDispatcher`, `ActionRuntime`,
    `CapabilityRuntime`, `adapters/Medical`) now goes through the new shared
    `PZAgent.Compat.hasEntries`, built on `pairs`, which the live game does
    provide. A contract test bans the global across the whole mod tree with no
    allowlist, and the ActionRuntime tests re-run the full command path with
    `_G.next` removed.
  - *An exception in the adapter lifecycle is now a terminal answer, not a
    wedge.* Live, `ActionRuntime.verify` crashed (on the missing `next`) after
    `session.arm`, the terminal ack never appeared, and the runtime hung on its
    current work forever. Every raise escaping `start`/`poll`/`verify`/
    `finalize` — and the runtime's own ack writes — now becomes a bounded
    terminal `failed`/`INTERNAL_ERROR` naming the phase, clears the in-flight
    slot, and leaves the runtime able to take `safety.stop` and the next valid
    command. An exception has no route to `succeeded`.
  - *The game now reads the session offer it was always supposed to read.*
    `pz_session_arm` armed only the sidecar; the game kept publishing
    `armed=false, mode=OFF` because nothing ever read `session.json`. The new
    `Runtime.readSession` reads the offer once per tick through the same `Ipc`
    primitive the heartbeat reader uses and feeds it to the session manager
    the mod always had (`Session.evaluate`: freshness, nonce replay, sidecar
    liveness). A nonce is only remembered once decidable — an offer rejected
    solely for a missing sidecar heartbeat is retried and accepted when
    liveness appears, which is exactly the ordering race the live run hit.
  - *A Russian item name no longer costs the whole observation.* Kahlua's
    `string.byte` on a Java string returns UTF-16 code units (Cyrillic "п" is
    `0x043F`, not two UTF-8 bytes), which the byte-oriented encoder refused.
    `PZAgent.Json` now classifies each string once — any unit above `0xFF`
    commits the string to the UTF-16 model, surrogate pairs combine, lone
    surrogates are refused by offset — while valid UTF-8 byte strings encode
    byte-identically to before and a lone high byte falls back to Latin-1
    deterministically. The overlong-encoding hole stays closed.
  - *The sidecar's atomic writer waits out Windows sharing violations.*
    `write_json_atomic` retries `os.replace` on `PermissionError` with the
    same bounded budget journal rotation uses (10 × 0.05 s), raises a typed
    `SharingViolationError` naming the target when a reader never lets go, and
    takes its scratch file with it on every failure path — including naming
    the leaked path honestly when even removal is refused.

- **Journal rotation no longer crashes on Windows when a reader is mid-poll.**
  `JournalWriter.rotate` moved and deleted files with `os.replace`/`unlink`,
  which on POSIX succeed under any open handle but on Windows raise
  `PermissionError` (WinError 32) when another handle holds the file — and the
  reader on the far side of every journal opens it for each poll. A rotation
  racing a poll took the CI soak down (`test_loop_soak`, and the second-process
  sidecar test through the same path). Rotation now retries each move on
  `PermissionError` with a small bounded budget (half a second worst case, zero
  cost on POSIX where the first attempt always wins), and a reader that never
  lets go becomes the writer's own `JournalError` naming the file rather than a
  bare crash. This is a real Windows defect, not a test-timing flake: the same
  identical tree was green on an earlier run only because no rotation happened
  to race a poll that time.

- **Three more input-boundary crashes closed, on the heartbeat/session, the
  observation diff, and the descriptor.** A third fuzz round found the same
  depth/number gap in every remaining place that reads JSON another program
  wrote: `ipc/atomic.py`'s `read_json_document` (the single boundary behind
  `HeartbeatMonitor` and `SessionManager`) and `observation/diff.py`'s
  `MappingDelta`/`ListDelta.from_dict` both recursed without bound and let a
  bare `ValueError` through on an absurd integer, and the descriptor loader
  could still overflow the parser on a file nested deep inside its byte cap.
  All three now measure nesting depth before parsing (`MAX_DOCUMENT_DEPTH` /
  `MAX_DESCRIPTOR_DEPTH`, both via the shared `pz_agent_core.jsonbytes`
  primitive) and refuse with their own typed error. Two seeded fuzzers
  (`test_session_handshake_fuzz.py`, `test_observation_model_fuzz.py`) join the
  suite. Two deeper issues the fuzz surfaced in `protocol/messages.py::_as_float`
  — an unguarded `float()` overflow and `allow_nan=True` letting `Infinity`/
  `NaN` through — are recorded for the protocol owner, not silently patched.

- **Five more crashes on hostile or corrupt input, across three boundaries,
  found by seeded fuzzers and all now typed refusals.** The same seeded-fuzz
  approach that hardened the RPC decoder was pointed at the other places that
  read bytes another program produced. (1) The **journal reader** — fed by the
  mod writing to disk — crashed with a `RecursionError` on a deeply nested line
  and a bare `ValueError` on an absurd integer literal, both under its line
  cap, where §3.5 promises a skipped "corrupt record"; it now bounds nesting
  depth before parsing and catches the broader `ValueError`, skipping the line
  with a diagnostic. (2) The **descriptor loader** — read at startup from the
  state directory — raised a bare `OverflowError` when a corrupt descriptor
  carried a pid past the platform's `pid_t` (`os.kill` overflowed), and a bare
  `TypeError` when `family` arrived as a JSON array or object (`x in {…}` on an
  unhashable value); both are now the loader's own `DescriptorError`. (3)
  `AgentConfig.to_toml` did not escape control characters, so a config with a
  newline in a free-form string field validated but could not re-load from the
  support bundle's rendered copy; it now escapes every control character. The
  depth-scan primitive is shared between the RPC decoder and the journal reader
  in the new `pz_agent_core.jsonbytes` module — one source of truth for the
  "measure depth before parsing, because catching `RecursionError` after the
  fact is not a recovery" rule.

- **Two decoder crashes on hostile RPC frames, both under the byte cap, both
  now typed refusals.** A seeded fuzz over `decode_request`/`decode_response`
  found that a frame of a thousand nested brackets (thirty-two times under the
  64 KiB request cap) overflowed the interpreter with a bare `RecursionError`,
  and an integer literal of five thousand digits raised a plain `ValueError`
  from CPython's integer-string-conversion ceiling — neither caught by the
  decoder, both reaching the serving loop raw. `_loaded` now measures nesting
  depth on the raw bytes *before* parsing and refuses past
  `MAX_NESTING_DEPTH` (so the parser is never handed a document that could
  recurse past the bound — catching `RecursionError` after the fact is not a
  safe recovery), and widens its catch to the `ValueError` the absurd number
  raises, naming both as `MALFORMED` without echoing the payload. The two
  reproducers are promoted from the fuzz suite's `finds_` markers to real
  regression tests.

### Added

- **A seeded, deterministic wire fuzzer and a bounded loop soak.**
  `tests/unit/test_wire_fuzz.py` drives thousands of structured mutations
  (truncation at every boundary, type swaps, absurd lengths, unicode and
  surrogate garbage, bounded deep nesting, duplicate keys) through both
  decoders, asserting every input either round-trips or raises a typed
  `RpcError` and nothing else — the property that surfaced the two crashes
  above. `tests/unit/test_loop_soak.py` runs a real `SidecarLoop` for
  thousands of ticks under a deterministic interleaving of observations, goal
  and action submissions, disarm/re-arm and panic, and asserts the design's
  boundedness invariants from the outside: the action record store never
  exceeds its cap and never evicts an in-flight record, the goal queue's
  pending stays within its cap, and the thread set returns to its start after
  a clean shutdown.


- **Remote actions are served over the link.** `action.submit` no longer
  refuses: a bounded `ActionChannel` (explicit caps, idempotent resubmission,
  frozen whole-replaced records) is drained one submission per tick on the
  loop's own thread through the real `ActionEngine` and its full safety
  machinery — a disarmed submission terminates as the engine's own refusal,
  a panic clears pending work naming the lever, and `action.status` answers
  the real record over the socket. Plans remain a *reasoned* refusal,
  recorded on the port: a synchronous multi-step plan on the tick thread
  would hold the stop levers hostage for its whole wall budget, and the
  served multi-step shape is the goal channel.
- **The wire speaks one language.** `action.wait` was unusable end to end
  (Python sent `game_seconds`, the mod demanded integer `duration_ms`, the
  units disagreed); the mod now measures the same world clock observations
  carry, counting a date change as exactly one midnight so a wait can finish
  late but never early. `plan.cancel` with a `command_id` cancels exactly
  the named command — in flight or queued — and refuses by name when nothing
  matches. The agreement contract that let both live now builds its registry
  the way `app.py` does, dumps the control adapters from the runtime's own
  table, carries zero exempt actions, and runs a two-way family census.
- **Movement survives reality.** Every walk that had ground to cover died
  `INTERNAL_ERROR` on its first poll (the bypassed declare-wrapper's
  `"running"` vocabulary), and an already-satisfied move failed its own
  postcondition (all-unchanged evidence with no way to say arrival is
  success). Both fixed at the adapter, both pinned by a runtime-level Lua
  suite that drives the real adapters through the real `ActionRuntime`.
- **Adversarial coverage across the stack.** Hostile RPC frames, replays,
  stale and wrong-server descriptors, mid-call death, restarts with key
  rotation, partial frames and dead-pid descriptors (15 new transport
  tests); six MCP adversarial paths driven against a live `pz-agent-mcp`
  child, each ending with a healthy follow-up call on the same process; and
  eleven executable pins over the Windows workflow, both PyInstaller specs
  and the evidence index, mutation-verified in both directions.
- **The map for the local agent matches the mod.** `GAME_API_VERIFICATION.md`
  rebuilt against the code: 195 swept symbols, zero missing rows, the five
  wrong rows (queue reader, `onSleep` argument order, `ISReadABook` arity,
  `getBodyParts`, `PlayerStats` spellings) now match their call sites, every
  row still `requires_live`.

- **The link is now proven across a real process boundary, three ways.**
  (1) `tests/contract/test_sidecar_serves_the_core.py` hands a genuine child
  interpreter nothing but the state directory; the child dials the descriptor
  cold and prints back the session id the loop's own attach minted in that run
  and the observation sequence number that travelled journal → store → link —
  facts an in-process client could have read from shared memory, and a second
  process cannot invent. Its negative companion runs the identical child with
  nothing serving and watches it refuse by name. (2) The Windows workflow
  gained `packaging/windows/prove_packaged_link.py`: the packaged
  `pz-agent.exe` serves the Core RPC link for real and the packaged
  `pz-agent-mcp.exe` answers a JSON-RPC `initialize` through it, driven with
  shipped flags only, every wait bounded, success only on the observed
  `serverInfo` result; the driver is exercised on Linux against the module
  entry points, both directions. (3) `scripts/check_release.py` now requires
  the MCP end-to-end suite by name in the JUnit report it certifies from —
  a report where `tests/contract/test_mcp_subprocess_e2e.py` never ran, or
  ran as skips, is refused, because aggregate counters cannot tell a full run
  from one where the suite that crosses the seam was deselected.

### Fixed

- **The Windows drop guard learned the platform's spelling of a hang-up.**
  Run 31247921064 showed the server-survival fix below was half right: a
  short idle budget does end the wait on an abandoned named pipe, but the
  hang-up surfaces from `connection.poll` itself as `BrokenPipeError` — and
  the poll sat outside `_exchange`'s recv-drop guard, so one vanished client
  unwound `serve_forever` and took the sidecar with it. On Unix the same
  fact arrives as `EOFError` from the recv, inside the guard. The poll now
  sits inside the guard, with a seam-pinning test driving the Windows
  spelling directly. The two remaining account-name tests that planted a
  `Users/<name>` segment under pytest's temp directory (mid-path past the
  stripped profile lead on Windows — out of the floor redactor's documented
  scope) now pin the collection report's spelling to the floor redactor's
  exactly, plus direct profile-rooted probes.

- **Three Windows-only regressions from the goal-channel/transport iteration,
  none reproducible on Linux, diagnosed from the CI log.** (1) The transport
  rework hand-dialled its socket and referenced `socket.AF_UNIX`
  unconditionally, so a Unix-family descriptor reaching the client on Windows
  crashed with `AttributeError` instead of reporting the sidecar unreachable;
  `_dial` now guards on `unix_socket_supported()` and raises the transport's
  own not-answering error, which the entry point maps to `EXIT_NOT_WIRED`.
  (2) The two new server-survival tests left the server's idle budget at 60 s,
  but a Windows named-pipe read begun before the peer vanished has no hard
  deadline — the documented asymmetry — so an abandoned handshake held the
  single serving thread past the follow-up client's patience; both tests now
  inject a short idle budget so recovery is observable on both platforms
  without weakening the survival assertion. (3) The new account-name evidence
  test buried a synthetic profile segment under pytest's temp directory, which
  the floor redactor deliberately does not reach (it targets profile *leads*,
  not mid-path `Users/` segments); the test now asserts the manifest carries
  the floor-redacted spelling and that a genuinely profile-rooted path has its
  account segment stripped.

- **The remaining criterion-coverage gaps are closed — and four criteria were
  false in code, now true.** Fifteen audit entries across four fronts: secret
  hygiene now scans REAL writers (a real token issued into a real state dir,
  the real support bundle built and every member's bytes swept; a full
  SidecarRpc lifecycle with all loggers captured; the three untested key
  shapes each removed by its own rule; `verify_bundle` over a
  credential-carrying archive); recovery now observed (the game dying
  mid-action ends GAME_DISCONNECTED; an established link dropped mid-exchange
  is survived on both ends; an unwritable state directory is *reported* — it
  used to raise and lose the session; a truncated journal now *refuses to
  arm* — it used to arm right over the tear); the release gate gained its
  missing teeth (an all-NOT_RUN manifest certifies nothing; a same-size
  member edit is caught by its digest; a member the index never recorded is
  refused — the gate used to certify an archive carrying an extra file); and
  the MCP subprocess E2E gained its two missing journeys (a real client
  submits, polls and cancels a goal against the real queue — which also
  found the fixture never passed its goal channel to the router; a sidecar
  lost mid-session is an error payload and the child survives). The plan
  gate itself gained two rules: archive tasks must transitively require the
  per-member verification, and no build task may PASS without a run to
  witness the build — the second caught a real instance the moment it ran.

- **The typed goal channel is served by the real sidecar.** `SidecarLoop` owns
  a `GoalQueue` ticked every loop tick (budgets and TTLs expire for real);
  armed and AUTONOMOUS, the loop activates the oldest admissible goal and asks
  the planner for a plan for *that* goal; a succeeded action with observed
  evidence ends it, anything else charges a step; a guard-forced disarm, an
  RPC disarm and shutdown all end the active goal through the queue's own
  vocabulary, and the panic sentinel empties the whole channel as a level.
  The goals port serves the queue's real admissions, statuses and
  cancellations under one documented lock seam — no IO, planner or engine
  call ever under the lock. Proven over the link: a client submits
  `satisfy_to=0.73` through `from_state_dir` and the loop's planner is asked
  for exactly that goal, lifecycle, duplicate detection, cancel and
  disarm-leaves-nothing all asserted with bounded waits.
- **Every wait on the peer process in the RPC transport is bounded — for
  real, per family.** The audit's finding that the criterion was partly false
  is fixed: a poll-guarded handshake on both families; on the Unix socket a
  deadline watchdog that severs the link mid-read, so a header-then-trickle
  peer ends within the call's budget; connect under `settimeout`; the
  server's accept-side handshake — an unbounded wait the audit had not even
  flagged, where a silent peer wedged the accept loop forever — now runs
  under the injectable idle budget; request read, reply write and idle wait
  each bounded; both `maxlength` caps now observed by tests that claim 1 GiB
  and send four bytes. On Windows named pipes a started read has no hard
  deadline — documented in the module docstring and pinned by a test, never
  claimed. An adversarial verifier mutation-tested every bound, found three
  claimed-but-unobserved ones and a per-family docstring overstatement, and
  fixed all four. The forbidden-pattern gate's eval/exec/pickle rules are now
  exercised over planted snippets and the shipped tree is swept unfiltered.

- **R-008 closed: the shipped sidecar now serves the Core RPC router over its
  real subsystems.** `pz_agent_cli/core_services.py` adapts the running
  `SidecarLoop` onto the CoreServices ports — session status, observations,
  capabilities, memory and diagnostics served from the loop's own immutable
  state (each read a single reference to whole-replaced frozen objects); arm,
  disarm and stop travelling the shipped one-slot control channel and panic
  sentinel with doubly bounded waits on the loop's own published decision.
  `pz-agent start` now serves the link between attach and the tick loop and
  withdraws the descriptor in the same finally as shutdown. Ports that cannot
  be served honestly yet refuse by name — `REMOTE_ACTIONS_UNSERVED`,
  `REMOTE_PLANS_UNSERVED`, the router's own no-goal-channel refusal — because
  a queue nothing drains would fabricate acceptance. Proven by
  `test_sidecar_serves_the_core.py`: the MCP client's exact
  `from_state_dir` path reaches the real loop over a real socket and reads
  the observation the fake mod wrote (fixture-chosen seq 47, no default
  produces it), and — after an adversarial verifier's mutation check found
  the shipped call site unpinned — by a test driving the real
  `start --foreground` and asserting the `rpc.serving` record.

### Changed

- **A criterion-coverage audit of the 75 heaviest claims moved the honest
  figure from 74.26% to 59.66%.** Twelve read-only auditors asked one question
  per weight-8+ PASS task: does the named test observe the stated criterion,
  such that the criterion becoming false fails the suite? 53 confirmed, with
  the observing assertion named. 22 refused — and among them the audit found
  the project's recurring defect live in the product: **the shipped sidecar
  never serves the Core RPC router** (R-008). `CoreRouter` is constructed
  only by tests; `pz-agent start` publishes no link, so a real `pz-agent-mcp`
  against a real sidecar finds nothing to connect to, while every E2E test
  hosts the router itself over fakes. The 22 tasks and their 34 ordered
  dependents are back to IN_PROGRESS (R-009), each with its precise gap
  recorded in `docs/control/evidence/criterion-audit-094cb8a.md`; each
  returns to PASS when an assertion observes its criterion.


### Fixed

- **The R-002 boundary tests hung windows-latest, and the suite gained the
  bound it preached.** The two new tests raised their exceptions through
  `asyncio.run` over a monkeypatched coroutine — Linux tolerated it, the
  Windows Tests step ran half an hour and never finished. They are rewritten
  loop-free: what they test is one `except` clause, which needs no event loop.
  And because a suite with no per-test bound violates the project's own
  "everything bounded" rule in the one place nobody had applied it,
  `pytest-timeout` (300 s per test, thread method) now turns any future hang
  into a named failure instead of a runner held to the platform's six-hour
  ceiling.

- **R-002, the last open remote blocker: a server crash is now a diagnosis,
  not a traceback.** An exception escaping the MCP server's build or serve
  loop — a catalogue defect, or an SDK that keeps its constructor signature
  and changes behind it — used to kill the child with Python's generic exit 1
  under a stack trace. `main` now reports it as `EXIT_SERVER_FAILED` (10) with
  one bounded stderr line naming the exception, stdout untouched because it
  belongs to the protocol even in death. `KeyboardInterrupt` deliberately
  passes through — the user's own hand is not a server failure. Every remote
  blocker in `docs/control/BLOCKERS.md` is now CLOSED.

### Changed

- **FINAL_IMPLEMENTATION_REPORT.md re-pinned to a green tree.** The report now
  states what `094af1e` actually measures: `scripts/check.sh` exits 0 (6559 of
  6564 tests passing, five named skips — one of them the plan's own "every
  remote task is closed"), both workflows green against the exact commit, and
  the artefact of record is CI's PATH-stripped, gate-certified archive
  (run 31227188006, sha256 `2d3d9e4b...`) rather than the incomplete
  Linux-built ZIP, which stays documented as what this container can and
  cannot produce. The release gate's refusal is now down to exactly the two
  missing executables — the boundary §9 exists to name.

- **The remote stage is complete: every remote-owned task and 48 of 54
  integration checks PASS; thirteen of fifteen epics close.** Each runnable
  check's command was executed and its outcome recorded with the observation
  it rests on; the CI-observed checks cite the green runs (31223693322,
  31225901032 — the latter answering both executables with PATH reduced to
  the system directories, so "the bundle needs no Python" is an observation).
  The six open checks and the two open epics are E14 and E15 — the live-game
  claims only a machine with the game can establish, and the plan's own skip
  message now reads "every remote task is closed; there is no next one to
  check". RB-003 superseded accordingly.

### Fixed

- **A plan regeneration wiped every check's evidence.** The generator carried
  a check's status but not its evidence, so the first rebuild after the
  checks were established left 48 PASS checks evidence-free and the gate
  refused the plan. Status and evidence now travel together, pinned by a
  regression test over the real plan.

- **R-007: the voice intent resolver existed twice, and the tested copy was
  not the shipped one.** `pz_agent_voice/intents.py` — 600+ lines production
  never imported — is deleted; `intent.py`, the module `session.py` actually
  runs, survives and now carries what the dead copy alone had: the percent
  sign as a spoken unit («поешь 80%»), the closed bare-number table that gives
  «прокачай механику до 7» its one honest reading, a defensive
  `IntentRefusal.INTERNAL` so a range-table drift becomes a spoken sentence
  instead of a `ValueError` quoting the spoken number, and import-time checks
  that every vocabulary word survives normalisation, no word is claimed by two
  tables or shadows a stop word, and every trainable skill has a spoken form.
  `test_voice_intents.py` is rewritten against the survivor; every behavioural
  claim that applied was kept or replaced by the survivor's equivalent, each
  fold recorded. An adversarial verifier mutation-tested the stop-first
  ordering (the reordered scan fails the test) and restored one dropped pin —
  a digit run past the length bound is refused before `int()` even when its
  value would fit.
- **The answer check now also proves the bundle needs no Python.** The
  `windows package` step that runs both executables does so with PATH reduced
  to the system directories, so an import that escaped the PyInstaller bundle
  fails on the runner instead of on a user's machine that never had Python.

### Added

- **The bridge protocol has a published schema.**
  `schemas/teamon_bridge.schema.json` states the wire contract a TeamON bridge
  implementer builds against: eight message types with their directions, the
  closed goal-token set, the outcome statuses, the handle shape and the
  utterance cap. `tests/contract/test_teamon_schema_conformance.py` holds the
  schema and `pz_agent_voice.teamon` together in both directions — every line
  the code can emit validates, every line the schema permits decodes — and
  compares the closed sets set-for-set so neither place can drift. The error
  branch deliberately leaves the fault-code set open: a reader must survive a
  code from a newer bridge, and refusing the report of a failure loses the
  failure.
- **A CI verdict survives its own recording.** The plan gate refused a
  STATUS.json claiming `GREEN` for any commit other than HEAD — an
  unsatisfiable rule, because recording a verdict requires a commit and the
  commit moves HEAD. It shipped and promptly refused its own recording commit.
  `GREEN` and RC `CURRENT` are now judged by the same predicate as the
  staleness rule always used: the verdict's commit must be an ancestor of HEAD
  with nothing outside `docs/control/` changed since. A code change still
  demotes the verdict to `STALE:GREEN` everywhere, generator and gate agreeing.

### Changed

- **The master plan reflects what a green `main` verified.** With both
  workflows green at `276b9d9` and the release candidate built from it,
  `scripts/verify_carryover.py` confirmed 133 tasks by running each one's named
  regression test here and now — the typed goal channel, the voice companion,
  the TeamON bridge, the RPC codecs and client, the MCP subprocess E2E surface,
  and the failure-recovery suite among them. Eleven CI-observed facts (the two
  executables building and answering on `windows-latest`, the archive being
  assembled, the Windows suite reproduced through Actions) are recorded with
  the run that observed them. Evidence pointers that predicted modules the
  architecture never grew (`session/holder.py`, `safety/stop.py`,
  `companion.py`) now name the modules the behaviours actually live in.
  Weighted progress moves from 24.87% to 53.25%; nothing was claimed whose
  test did not run, and the live-game fifth of the plan remains untouched at
  zero from this environment.

- **Local Core RPC: the channel that was missing between the two processes.**
  `pz-agent-mcp` is launched as a subprocess by an MCP client, so it never
  shares a process with the sidecar that owns the session, the observation store
  and the action engine. Until now it said so and refused to serve — the message
  `NO_SERVICES_MESSAGE` stated in its own words that this build had no channel
  handing the core to a second process. `pz_agent_core.rpc` is that channel: a
  Windows named pipe or a Unix socket, never a TCP address, authenticated per
  run by a 32-byte token in its own mode-0600 file, with a descriptor at
  `<state-dir>/runtime/core-rpc.json` that a client checks before using — format,
  protocol major, the recorded process still alive, and the token still beside
  it. A stale descriptor is refused rather than dialled: a pid can be reused,
  and then a client reads a *different* process's silence as the core's state.
  Documented in `docs/CORE_RPC.md` and pinned by two JSON schemas.
- **JSON on that link, never pickle.** `multiprocessing.connection` puts `send`
  and `recv`, which pickle, one letter from `send_bytes` and `recv_bytes`, which
  do not — and a pickle stream is arbitrary code execution in the process that
  reads it. Only the byte calls are used. The suite feeds a pickle whose
  `__reduce__` would raise on load, and poisons `Connection.recv`/`send` for the
  duration of a real call, so reaching for the convenient one fails in CI rather
  than in a user's process.
- **A weighted plan of record.** `docs/control/MASTER_PLAN.yaml`: 480 tasks in 15
  epics, five levels (EPIC → MILESTONE → TASK → CHECK → EVIDENCE), progress
  derived on every read as the summed weight of passing tasks over the summed
  weight of all of them. It replaces a model that counted steps, which said a
  paragraph of documentation and a live Project Zomboid scenario were the same
  size. Weight bands are validated, not advisory. Seven metrics are reported
  separately because a single figure hides a subsystem at zero — MCP and voice
  operability are both at 0.0% while Windows compatibility is at 89.3%.

### Fixed

- **The two CI workflows installed different projects.** `windows.yml` installed
  `.[dev,mcp]`; `ci.yml` installed `.[dev]`. `pz-agent-mcp` checks one thing
  before anything else — is the MCP SDK importable — and answers `EXIT_NO_SDK`
  (3) if it is not, so every assertion in `test_mcp_entry.py` and
  `test_mcp_subprocess_e2e.py` that runs the entry point and compares an exit
  code was comparing 3 against the code it meant to check. The same commit was
  green on windows-latest and red on ubuntu-latest with 34 failures, none of
  which were about the code they named; the local gate was green too, because a
  developer venv has the SDK in it. The fix is the extra. The guard is
  `tests/contract/test_ci_installs_what_the_tests_need.py`, which reads the
  extras out of both workflow files and requires them to be *equal* — the
  divergence, not the missing package, is what made the failure invisible — and
  proves its own premise by running the real entry point with the SDK shadowed
  by a package that raises `ImportError` and observing `EXIT_NO_SDK`.
- **The RPC token was written in text mode on Windows.** `os.open` defaults to
  text mode there, so `os.write` translated every `0x0A` in the payload into
  `0x0D 0x0A`. A token is 32 random bytes; the chance one of them is a newline
  is about one in eight. On those runs the file was 33 bytes, did not match the
  token the server was authenticating with, and the client was refused — on
  Windows only, one run in eight, with a message about authentication rather
  than about encoding. `os.O_BINARY` where the platform has it.
- **CI cloned shallow, so the recorded baseline SHAs could not resolve.**
  `actions/checkout@v4` defaults to `fetch-depth: 1`, and
  `tests/unit/test_control_baseline_evidence.py` resolves every recorded SHA
  with `git cat-file` — which is what makes that file evidence rather than
  prose. The objects were simply absent. Both workflows fetch the full history
  now, and the test says "this is a shallow clone" instead of "these SHAs are
  wrong".
- **`safety.disabled_capabilities`: switch a capability off by name.** The
  state `disabled_by_policy` existed, the mod guarded on it, `PermissionEngine`
  refused on it with a message written for a user — *"X is switched off by
  configuration"* — and `docs/COMPATIBILITY.md`, which ships inside the Windows
  archive, listed it as "available, but configuration forbids it". No
  configuration could produce it: the only constructor was called from three
  tests, there was no key to write, and unknown keys are hard errors here, so
  anything an operator invented was rejected. The page's own warning three rows
  below — that a panic stop cannot reach a sleeping character — gave a cautious
  reader a concrete reason to want the switch the same page described.
  Implemented rather than documented away, the way the multiplayer refusal was.
  Applied by the ledger rather than by editing the capability report, because
  the report is evidence about the install and a user's decision is not a
  finding about it; `status` reports a switched-off capability with that reason
  instead of dropping the name; an unknown name is a configuration error.

### Fixed

- **A path that mixed separators matched no redaction rule.** Spellings of a
  literal were enumerated whole — all-`/`, all-`\`, all-doubled,
  all-percent-encoded — so `C:\Users\Иван/Zomboid`, which is what
  `f"{path}/name"` produces, matched none of them and fell through to the
  shorter `home_dir` literal. Not a leak; the path was still struck out, but
  under `<USER_HOME>` instead of `<ZOMBOID>`, so the same file produced a
  different line on each platform — which is the one guarantee a placeholder
  exists to provide. Mixtures are exponential in the number of separators, so
  each position is matched independently now.
- **`portable_relative_path` was untestable, and that hid a defect.** It coerced
  both arguments with `PurePath`, which builds the *running* platform's flavour,
  so on Linux a `PureWindowsPath` was normalised before `as_posix()` ever ran and
  removing the call changed nothing any Linux test could see. It also made
  `portable_posix` return `C:\/Users/...`. The flavour is preserved now.
- **The credential tests asserted the placeholder appeared, never that the secret
  had left.** Those are different claims: shortening the value pattern to a
  single character inserts `<REDACTED>` in front of an intact key and satisfies
  every containment check in the file, while `findings()` returns nothing and
  `verify_bundle` calls the archive safe to share.
- **`ActionEngine`'s pre-flight manual-takeover guard had no failing mutation.**
  Deleting it left the whole suite green while the engine dispatched a command
  into a character the player had taken control of and then cancelled it — which
  is not the same as never sending it.
- **`pz-agent-mcp.exe` could not be built.** PyInstaller's `collect_submodules`
  discovers modules by importing them, and `mcp.cli` calls `sys.exit` at import
  time without its optional `typer` extra. `SystemExit` is not an `Exception`, so
  `on_error` could not skip it and the build died packaging a program that never
  runs a command line of the SDK's at all. `packaging/windows/specutil.py` reads
  the package directory instead.
- **The documented-command guard now covers `pz-agent-mcp` too.** It has its own
  parser — `--version` and `--describe` and nothing else — so a document naming
  a flag for it fails exactly the way `logs --redact` did, and the guard written
  for the first executable deliberately skipped the second. `configs/mcp/README.md`
  is a first-contact document for anyone wiring a client and prints several of
  these; they are parsed now.
- **A handoff document stated a count that this branch's own work changed.**
  `docs/LOCAL_GAME_HANDOFF.md` illustrated `live-test collect` with "copied 0,
  skipped 15"; wiring the trace made it 16. The sentence now states the
  behaviour — every missing file named, one line each, with counts — rather than
  a number that drifts whenever a file is added to the evidence.
- **`pz-agent start` no longer prints an MCP configuration naming a variable
  nothing reads.** The block it prints for pasting into a client set
  `PZ_AGENT_STATE_DIR`, a name that occurred exactly once in the repository — in
  the literal that printed it. `pz_agent_mcp` reads no environment variable at
  all, discovery reads `USERPROFILE`/`OneDrive`/`HOME`/`USERNAME`, and the
  server's parser takes neither a path nor a variable, so there was never a
  route for it. Meanwhile `configs/mcp/README.md` carries a section titled "Why
  `env` is empty" arguing that naming an unread variable "would look like
  configuration and be decoration", all three shipped client configurations
  carry `"env": {}`, and a test pinned exactly that — over the checked-in files
  only. The pin now covers the configuration the CLI hands a user, which is the
  one anybody actually pastes.
- **`docs/QUICKSTART.md` stopped telling a new user to command the agent by
  voice.** Section 7 named two routes for a first command and one of them is
  refused: this build carries `arm`, `disarm` and `stop` from a second process
  and has no channel that carries a *goal*, so a spoken "eat something" is
  refused and the companion answers «Не получилось.» The quickstart now says so
  and points at `VOICE.md`, which the archive now ships.
- **`voice run` writes the log the debug map sends an operator to.** Defect 18's
  shape, one package over: `docs/LOCAL_DEBUG_MAP.md` names `logs/` for both
  voice symptoms it lists — a phrase not recognised, and «стоп» heard while the
  character kept going — and the companion had never written a byte there. Its
  turn history and synthesiser failures sat in two bounded rings inside a
  process that then exited, while `VoiceCompanion.speech_failures` says in its
  own docstring that they are kept because "the companion went quiet" with
  nothing recorded is what a support bundle cannot explain. Written at the run's
  edges into the same rotating file the sidecar uses, so both halves of "did the
  stop I said reach the sidecar" end up in one place in order. **Intents and
  outcomes, never transcripts** — a bundle is designed to be attached to a
  public issue and a microphone's contents do not belong in one.
- **`installer/` says what it is.** A complete, tested, 927-line standalone
  installer with a guide titled "Installing pz-agent on Windows", reachable from
  nothing, in no shipped artefact, and read as *the* install instructions by
  anyone who opens the directory. The shipped path is `install.bat` →
  `pz-agent install-mod`; a checkout follows `docs/QUICKSTART.md`. It is kept —
  it is the only path that works before anything is installed — and the guide
  and the module now open by naming which of the three cases each is for.
- **AGENTS.md and CONTRIBUTING.md claimed an enforcement that did not exist.**
  Both said `scripts/check_forbidden.py` fails the build on an empty exception
  handler. It had no such check, for exactly the handler style this codebase
  writes, so a rule two governing documents declare binding was unenforced and
  unreviewed. It cannot be scanned honestly either: the tree contains an
  `except (OSError, UnicodeDecodeError): pass` that falls through to a second
  lookup, and several `except OSError: return` that deliberately trade a
  diagnostic for a session in flight. So the part that *can* be scanned now is —
  an untyped `except:`, which also catches `KeyboardInterrupt` and `SystemExit`
  and of which the tree has none — and the swallow is stated as a review rule
  with the reason it is one. `check_forbidden.py` had no tests at all; it has
  them now, including both directions of every rule the documents promise.
- **Every link a shipped document makes now lands inside the archive.** Defect
  13 was one instance of this — two documents the archive omitted while its own
  shipped documents told an operator to open them — and the fix was two names in
  a tuple, which left the general case untouched. Seven of the archive README's
  links resolved to nothing: `CONTRIBUTING.md`, `AGENTS.md`,
  `docs/ARCHITECTURE.md`, `docs/PROTOCOL.md`, `docs/TESTING.md`,
  `docs/DEVELOPMENT.md` and the blueprint directory, plus `PROGRESS.md`'s link
  to the task graph and `PROTOCOL.md`'s to `schemas/` and `tests/contract/`. An
  operator on Windows has no repository, so a relative link is either a file
  beside the one they are reading or nothing at all. `ARCHITECTURE.md` and
  `PROTOCOL.md` now ship — the second is what `LOCAL_DEBUG_MAP.md` and
  `LIVE_TEST_PLAYBOOK.md` assume when they discuss journals, refs and recovery —
  and the links about *building* the project became absolute, so a GitHub reader
  follows them and an operator gets a URL rather than a dead path.
- **`game.install_dir` and `game.user_dir` now do something.** Both were parsed,
  validated, typed and read by nothing, while `doctor`'s own remediation for
  `PZD001`, `docs/TROUBLESHOOTING.md` for `PZD001` and `PZD003`, and
  `configs/mcp/README.md` all told a blocked user to set them. Those two
  failures brick every other command — a GOG or manual copy Steam does not
  list, a profile moved by OneDrive or `-cachedir` — so the one documented
  escape hatch produced "configuration is valid" and then the identical failure
  telling the user to do what they had just done. Discovery now runs a second
  pass with the configured paths. Precedence is command line, then
  configuration, then discovery. A configured path that does not exist is
  reported *at that path* rather than falling back to a search, so a typo is
  visible instead of hidden behind the original error.
- **`safety.panic_hotkey` no longer accepts a value it cannot bind.**
  `PZAgent_Main.lua` binds DirectInput scancode 88 directly and reads no
  configuration, so every value other than `F12` bound nothing — and this is the
  stop button. A user rebinding away from F12 (Steam's default screenshot key,
  so there is a real reason to) was told "configuration is valid" and had bound
  nothing at all. Any other value is now a hard error naming the two routes that
  do work: `pz-agent stop`, and the `panic.stop` sentinel. Rebinding for real
  needs the mod to read a published keycode *and* a live run to prove the new
  key reaches the stop; until both exist, saying so is the honest answer.
- **The mod can publish `experimental`.** `CapabilityRuntime` reads
  `adapter.experimental` and `Toolkit.declare` never carried the field, so it
  was read in one place and written in none — the same shape as the very first
  defect on this branch. Two adapters carried comments saying "the probe caps
  this at experimental" and both published as ordinary `available_unverified`,
  while `docs/PROTOCOL.md` documents `capabilities.json` with an example showing
  a state its own writer could not emit. `survival_sleep` and
  `drink_world_source` declare it now.
- **Three documents told users to run commands that do not exist.**
  `SECURITY.md` — the page a vulnerability reporter lands on — said to check a
  support bundle with `pz-agent logs --redact --verify` before attaching it to a
  public issue; there is no `--redact`, so the single gate between a reporter
  and an unredacted archive was an instruction that exits 2. `PRIVACY.md` said
  `pz-agent memory --forget` clears the memory store; there is no `memory`
  command, and the real one — `remember forget` — appeared in no document at
  all. `docs/TROUBLESHOOTING.md` sent a user to `pz-agent status --explain` for
  the food policy's rejection list, and additionally said the thresholds are
  "in configuration" when `[safety]` holds four keys and none of them is one.
  All three corrected, and `tests/contract/test_documented_commands_parse.py`
  now puts every `pz-agent` command line any shipped document prints through
  the real parser — it is what found the third one.
- **The reflex guard's comment described the opposite of the running system.**
  `ReflexConfig.block_at` said the engine's threat threshold and its own compare
  against two inputs of which "only one is filled in by anything". Both are
  filled in and both are live: `Observe.lua` sets the danger floor from the
  squares around the player, and the guard takes the higher of that and its own
  assessment. A maintainer trusting the comment would have concluded
  `ActionEngine.threat_threshold` was dead configuration — and it is the only
  thing that interrupts a two-minute `literature.read` when a zombie closes,
  because the guard cannot run while the engine holds the tick.

- **The sidecar now writes the log nineteen live scenarios tell an operator to
  collect.** `DiagnosticLog` was complete — rotating, redacting, level-filtered,
  well tested — and constructed nowhere outside the test suite, so
  `logs/pz-agent.log` and `logs/pz-agent.jsonl` did not exist and could not.
  Nineteen of the twenty scenarios name the first among the files to collect and
  three name the second; `docs/LOCAL_DEBUG_MAP.md` sends an operator to it by
  name; `pz-agent logs` reads it; `logs --bundle` packs its directory into the
  archive `docs/TROUBLESHOOTING.md` asks a user to attach to a report. Four
  documents and twenty scenarios rested on a file the product never produced,
  and `live-test collect` had been reporting "copied 0 file(s), skipped 15" the
  whole time. `pz-agent start --foreground` now records the attach, the run's
  end, every retained safety event and the shutdown. Writing is at the run's
  edges rather than in the tick, and every write is optional and guarded: a log
  directory that will not take a file costs the log, never the session.
- **`pz-agent replay` has something to replay.** `TraceWriter` had the same
  defect and one more document on top of it: `docs/QUICKSTART.md` printed
  `pz-agent replay <trace>` under "When something goes wrong", `logs --bundle`
  packed `traces/*.jsonl`, and nothing had ever written a trace. The sidecar now
  records each observation — a full snapshot first, then diffs against it — and
  each action next to the terminal result that closed it, at
  `<state>/traces/session.jsonl`. Closing it needed a seam rather than a call:
  `ActionEngine` returns a result and never let go of the command it sent, so it
  gained an optional `on_dispatch` observer and the loop pairs the two. An
  action refused before dispatch is recorded with its reason and no command,
  because that is the case an operator is most likely to be reading a trace for.
- **`live-test collect` takes the trace, which no scenario knows to ask for.**
  `collect` builds its file list from each scenario's declared `logs`, and all
  twenty of those lists were written when nothing in the product produced a
  trace — so the newest piece of evidence would have stayed in the workspace
  while `docs/LOCAL_GAME_HANDOFF.md` told an operator to replay it from the
  evidence. The current file and every rotated generation are now copied into
  the scenario's `logs/` unconditionally, alongside the journals and snapshots
  that are collected the same way. The current file is named rather than
  globbed, so its absence is *reported*; the rotated generations are globbed,
  because a scenario short enough not to rotate is not missing anything.
- **A rotated trace stays replayable from its first line.** Found by writing the
  first one: `replay_observations` refuses an observation diff it has no
  baseline for, and a rotation that fell on a diff put one at the top of the new
  file — so every run long enough to rotate would have produced a trace that
  read back as a refusal. `TraceWriter.record_world` now asks whether a diff
  would rotate the file and writes the snapshot instead, letting the *snapshot*
  trigger the rotation and open the new file with what a replay needs.

### Changed

- **`docs/RELEASE.md` asks for the evidence the executable gate checks.** Its
  evidence checklist required "Game smoke evidence — S01–S15" from
  `tests/game-smoke/` and never mentioned `release/evidence-manifest.json`,
  which is the only thing `scripts/check_release.py --release` actually looks
  for. A human working the checklist and a machine working the gate were
  checking different things. The checklist now names the manifest, and states
  plainly that two scenario catalogues exist with colliding numbers —
  `S06_drink.yaml` in one is `S06_MANUAL_TAKEOVER` in the other — so a
  scenario id is ambiguous unless the catalogue is named with it.
- **Protocol 1.0 → 1.1.** The action whitelist grew from fifteen names to
  twenty-two, seventeen of them owned by the mod's adapter files. Added:
  `container.inspect`,
  `container.open_nearby`, `inventory.search`, `medical.bandage`,
  `survival.rest`, `survival.sleep`, `consume.drink_source`.
  `container.open_nearby` is deliberately not
  read-only — opening a container is a timed action the character performs, so
  placing it beside `world.inspect` would let an unarmed session move the
  character.
- **`inventory.equip` and `inventory.unequip` are now `equipment.equip` and
  `equipment.unequip`.** A rename, not an alias: the dispatcher's whitelist
  decides what may reach an adapter at all, and two spellings for one action is
  a second door. `SCHEMA_VERSION` stays at 1.0 — the document shapes did not
  change, only an enum inside them gained members.

### Added

- **`consume.drink_source`: fill a vessel at a sink, well or rain collector and
  drink from it.** The mod could already do this, behind an optional
  `refill_from` argument on `consume.drink`; the sidecar had no argument for it
  at all, so the path was unreachable from Python. Worse, it ran under
  `drink_carried` — a capability a static scan verifies — while §12.4 caps
  `drink_world_source` at `experimental`. Splitting it into its own action makes
  the gate structural: the engine reads `required_capability` from the adapter
  that owns the action, before that adapter is entered. `consume.drink` now
  refuses a world-source argument rather than honouring it, and the world-source
  postcondition accepts only thirst — a refill raises the vessel's volume and
  the drink lowers it again, so the vessel witnesses nothing in either
  direction. Published as `pz_action_drink_source`.
- **The support bundle's verifier no longer flags its own redaction.**
  `docs/TROUBLESHOOTING.md` tells a stuck user to run
  `pz-agent logs --bundle --verify` before attaching an archive to a public
  issue, and the whole point of `--verify` is to answer whether anything
  private survived. The `credential_assignment` rule matched
  `api_key=<REDACTED>` — its value group accepts the placeholder the rule
  itself writes — so the command printed "REVIEW BEFORE SHARING" and exited 1
  over a line whose secret had been correctly struck out. `text` was
  unaffected; `findings` is what the verifier asks. Nothing leaked, and that is
  not the harm: a verifier that flags its own success teaches an operator to
  ignore the next flag, and the next flag is the real one. Every rule is now
  checked against every placeholder this module writes, not only the one that
  bit, and redaction is asserted stable under a second pass.
- **`configs/mcp/README.md` names both refusals a client can meet, not one.**
  It said `pz-agent-mcp` "starts, finds no core services attached to its
  process ... and exits with status 1". On a plain install you get **3**,
  because the SDK gate fires first and its message is about a missing optional
  extra rather than a missing sidecar. The exit codes are deliberately distinct
  — `EXIT_NO_SDK` exists precisely "because the remedy is a single install
  command" — and documenting only the second sent a client author after the
  wrong cause on their very first launch. Both are described now, in the order
  they fire. `tests/contract/test_mcp_exit_codes_documented.py` pins the stated
  codes to the constants and to a real subprocess launch, and exercises
  `--describe`, which is the one thing that document promises works with no
  game, no sidecar and no SDK.
- **`pz-agent start` confirms the sidecar is still there before reporting one.**
  It returned success as soon as `Popen` returned, which reports that a *fork*
  succeeded and nothing about whether the program ran. A sidecar that died on
  its first import left `start` printing "sidecar started as pid N" and exiting
  0; `arm` then failed for reasons that named nothing, and `stop` said "the
  signal could not be delivered (No such process)" and exited 0 as well. The
  spawner now watches the child for `SPAWN_GRACE_S` and, if it is already gone,
  raises with the exit code and the tail of the spawn log — the child's own
  words, which are the whole diagnosis. No pid is claimed, so `status` still
  says NEVER_STARTED rather than STOPPED, because "it crashed" and "it never
  ran" are different things to tell someone. Every other test of the supervisor
  injects a fake spawner, which is exactly why nothing caught this; the new
  ones use a real subprocess.
- **A first-run remedy that pointed at the wrong document.** `start` without a
  configuration said to "copy the sample in docs/QUICKSTART.md". That page
  shows a TOML fragment and never names `config.toml` or
  `config.example.toml`, so an operator whose first command failed was sent
  somewhere that did not contain the thing they were told to copy. It names
  `configs/agent/config.example.toml` now, and the test asserts the file it
  names exists rather than asserting the wording.
- **The operator's loop is driven end to end.** `backup-save` → `prepare` →
  `run`, through the real CLI over a synthetic Zomboid directory. Every step had
  a unit test; the sequence did not, and the sequence is what a person performs.
  It is here for a specific reason: gating `run` on `prepare` creates the
  opposite risk to the one it closes, because a gate whose precondition can
  never be satisfied is a bricked release, and nothing could previously tell
  "refuses correctly" from "refuses always". The test asserts both directions.
  It also pins the three refusals an operator can actually hit — a save whose
  name does not say "test", a test save with no backup, and an evidence
  directory with no schemas.
- **A refusal that named no remedy now names one.** `prepare` reported
  "evidence schema missing" and stopped. The schemas ship in the archive's
  `evidence/schema/` and are in git in a checkout, so this is met only by
  pointing `--evidence-dir` somewhere new — or by running the bundled
  executable directly, where "the directory I came from" is a temporary unpack
  folder. Every other refusal in this project names its way out; this one did
  not. (The tempting fix — a second copy of the schemas inside the package —
  was started and reverted: it would have created a second source of truth for
  the documents that validate all release evidence, to improve a message.)
- **`live-test run` and `resume` refuse until `prepare` has completed.** They
  did not. `prepare` is the subcommand that proves the world is safe to
  experiment on — a save whose name marks it a test world, and a backup that
  *reads back* rather than merely existing — and it wrote `prepare.json` only
  when both held. Nothing read that file. So twenty scenarios that deliberately
  hurt the character and end in restores would start against any save at all,
  and the only thing between them and somebody's main world was a check whose
  answer went nowhere. `status` and `collect` stay ungated: reading the table
  and gathering logs change nothing, and gating them would leave an operator
  unable to see why they are stuck. The runner's own test fixture had never
  written a prepare record and every test passed, which is how this survived;
  the fixture writes one now and a second fixture exercises the refusal.
- **The eleven `.bat` wrappers are checked against the real parser.** They are
  the entire interface of the release — an operator installing from the ZIP
  never types `pz-agent` — and not one had ever been executed, here or on
  Windows. `tests/contract/test_bat_wrappers_invoke_the_real_cli.py` extracts
  every command line they build, expands the batch variables, and parses it.
  The risk is concrete: `--evidence-dir` belongs on the `live-test` group and
  not on its subcommands, so one transposed token would fail an operator's
  first command with an argparse usage message they could not act on.
- **The release archive carries the documents it tells you to read.** Fixing
  the "grep lists every guess" claim pointed five documents at
  `docs/GAME_API_VERIFICATION.md`, and `DOC_NAMES` did not ship it — so two
  shipped documents instructed an operator with no checkout to open a file that
  was not there. `docs/LOCAL_AGENT_PROMPT.md` was absent for the same reason,
  and it in turn told the agent to read `docs/PROGRESS.md`, also absent, as
  `docs/LIMITATIONS.md` did for `docs/RELEASE.md`. All four ship now.
  `tests/contract/test_release_docs_are_self_contained.py` follows every
  `docs/*.md` reference out of every shipped document and fails on a dangling
  one; contributor-only documents are exempt as a pinned literal set, so a new
  dangle fails rather than being waved through. One defect's fix created
  another within the hour, and only opening the archive showed it.
- **The blueprint's command names are accounted for.** `docs/blueprint/` is the
  requirement baseline and read-only, and it asks for two commands this build
  does not have under those names: `setup` (§14.2) and `support-bundle`
  (§14.7). Both were invisible to every test, because
  `tests/contract/test_cli_docs_agreement.py` globbed `docs/*.md` and never
  descended into the blueprint. That check now covers it, against a declared
  alias map, so a *third* unaccounted name fails rather than sitting there.
  Neither is a missing feature: the diagnostics bundle is `logs --bundle`, and
  the install flow is `install-mod` plus the separate steps QUICKSTART
  sequences. One part of §14.2 is a deliberate refusal rather than a
  simplification — the blueprint asks to back up an existing same-id mod before
  overwriting it, and `install-mod` audits first and **refuses**, naming the
  file, on anything it did not write or anything modified since it did. Backing
  up and overwriting would still have overwritten. Recorded with its reasoning
  in `docs/PROGRESS.md`.
- **The doctor's codes are documented.** `pz-agent doctor` stamps every check
  `PZD001`…`PZD010` and `README.md` bills `docs/TROUBLESHOOTING.md` as "Doctor
  codes and remedies"; `grep -rn 'PZD0' docs/` returned nothing, so the one
  instruction the tool gives a stuck user pointed at a page where their code did
  not appear. There is a table now, ordered as `doctor` runs the checks and
  saying which failures are consequences of an earlier one — and noting that
  `unknown` is not a pass. `tests/contract/test_doctor_codes_documented.py`
  pins it in both directions and checks each code against the check it belongs
  to, because a row naming the wrong check misdirects while passing a presence
  test.
- **`grep -rn "Build 42:" pz-mod/` is no longer described as the list of every
  guess.** It returns six lines in two files;
  `docs/GAME_API_VERIFICATION.md` marks 52 symbols `requires_live`. The claim
  appeared in five documents including `docs/LOCAL_AGENT_PROMPT.md`, where it
  read "Это исчерпывающий список" — so an agent working from that prompt would
  have enumerated six places and believed the unconfirmed surface covered. All
  five now point at the table and say what the grep is.
  `tests/contract/test_game_api_inventory.py` checks the table is complete
  against every engine class the mod constructs or probes for; its first
  version used a substring match and a mutation caught that, so it matches on a
  word boundary.
- **The mod names the capability that gates each action.** Five adapters —
  `equipment.equip`, `equipment.unequip`, `medical.bandage`, `survival.rest`
  and `survival.sleep` — declared `capability = nil`, each with a comment
  asserting that no probe existed for it. Probes exist for all five.
  `Toolkit.CAPABILITY` held six of the twelve names while its own comment
  claimed to spell them "exactly as `pz_agent_core.capabilities.probes` spells
  them", and the omission is what the five comments had read as absence. No
  command was ever ungated — the mod enforces by required symbols and the
  sidecar by the ledger — but the mod's published capability document named six
  capabilities where the system knows twelve, so five were missing from the
  report a person reads to find out why something was refused.
  `survival_sleep` is the one that matters most: its `experimental` ceiling
  exists because a sleeping character cannot be reached by a panic stop, and
  that ceiling was reaching nobody. `tests/contract/test_capability_declaration_agreement.py`
  compares both sides of the wire and was mutation-checked against a missing
  name and a wrong one.
- **Multiplayer is actually refused now.** It was documented as "refused in
  configuration and again at the session handshake", and neither refusal
  existed: a grep for "multiplayer" across `packages/` and `pz-mod/` found the
  warning's own text and two unrelated comments. `safety.allow_multiplayer`
  lived in `_advisories`, whose contract is "Never errors", carrying the
  sentence "multiplayer is refused at the handshake regardless of this setting"
  — so the flag loaded, the agent ran, and the only thing between it and a
  server was a line of advice describing a gate nobody had written. Now:
  the config key is a hard error; `observation.game.multiplayer` carries three
  states; and `ActionEngine._multiplayer_abort` refuses every mutating command
  unless the mod positively reported single player, with an **absent reading
  refused exactly as `true` is**. Stopping, disarming, cancelling and the three
  read-only actions stay exempt, because an agent that cannot be stopped in the
  one session it should not be running in is worse than no gate. Both halves
  mutation-checked. `isClient`/`isServer` are unconfirmed against Build 42.20
  like every other engine symbol, and are now the first row in
  `docs/GAME_API_VERIFICATION.md` for a reason: if they cannot be read, the
  agent refuses everything, which is correct and looks exactly like being
  broken.
- **`pz-agent smoke` is in `COMMANDS`.** It always had a parser, a dispatch
  branch and a working subsystem; it was missing from the tuple that declares
  what this build wires, so the CLI accepted a command its own list denied
  having. `tests/contract/test_cli_docs_agreement.py` treats `COMMANDS` as the
  truth about the surface, which made both of its directions wrong: a document
  naming `pz-agent smoke` failed for naming something "absent from the CLI",
  and the check that every real command is documented could never see it. The
  new `test_the_command_list_is_the_parser_and_the_parser_is_the_command_list`
  derives the set from the parser instead of restating it.
- **`scripts/generate_playbook.py`**, and a gate step that runs it with
  `--check`. `docs/LIVE_TEST_PLAYBOOK.md` said it was generated from
  `pz_agent_cli.livetest.scenarios` and had no generator and no check, so it
  could drift from the runner in silence. The generator reproduces the twenty
  existing scenarios byte for byte, which is what validates the template.
- **A real command executor in the mod.** `CommandReader` →
  `CommandDispatcher` → `ActionRuntime` → adapter, with an acknowledgement at
  every transition. One command in flight and one waiting, the lease re-checked
  before each step, TTL, idempotent replay, session validation, panic stop,
  manual takeover and heartbeat-loss stop. A success acknowledgement has one
  constructor and it requires observed evidence.
- **Seventeen Lua game adapters** covering movement, world and container
  inspection, inventory search/transfer/ensure-main, eating, drinking, reading,
  equipping, bandaging, resting and sleeping.
- **`tests/lua/test_adapter_registry.lua`**, which asks whether the adapters
  actually reach the dispatcher. They did not: thirteen of sixteen game actions
  were unreachable while every individual adapter test passed.
- **Python adapters** for the new actions, a deterministic medical triage
  policy, and capability probes for each.
- **`openai_compatible` and `teamon` plan providers**, over a standard-library
  HTTP transport with bounded retries, a response byte ceiling and separate
  connect and read timeouts. Credentials come from an environment variable named
  in config, never from the config file.
- **Handoff documentation** for a machine with the game installed:
  `docs/LOCAL_GAME_HANDOFF.md`, `docs/LIVE_TEST_PLAYBOOK.md`,
  `docs/LOCAL_DEBUG_MAP.md`, `docs/GAME_API_VERIFICATION.md` and
  `docs/LOCAL_AGENT_PROMPT.md`.

- **The whole MCP action surface.** Thirty-one tools, nineteen of them actions, so
  every action with a registered adapter can be asked for. A fourth tool kind,
  `QUERY`, covers the three that only read: they submit an action and return an
  action id like any other, and need no arming. `container.open_nearby` is
  refused entry to that kind by construction — its name reads like a query, but
  opening a container is a timed action the character performs.
- **Seam tests, as a category.** Every defect below was found by a test that
  crosses a boundary rather than covering a unit, because every subsystem
  involved was already written, tested and green on its own side:
  `tests/lua/test_adapter_registry.lua` (do the adapters reach the dispatcher),
  `tests/contract/test_adapter_args_agreement.py` (does the sidecar send what
  the mod declared), `tests/contract/test_capability_evidence_agreement.py`
  (can a capability ever be proven), `tests/contract/test_mcp_action_coverage.py`
  (is every action reachable, and does its tool publish arguments its adapter
  accepts) and `tests/contract/test_sidecar_capability_wiring.py` (does the
  assembled sidecar refuse everything).

### Fixed

- **The assembled sidecar refused every action.** `build_loop` never passed a
  capability check, so `SidecarLoop` kept its `deny_capability` default — which
  returns `False` for everything, by design, so that "nobody wired a probe"
  fails closed. All seventeen game adapters name a required capability, so a
  real session refused every one of them, always. No test saw it: each adapter
  and engine test injects its own check, and the production assembly path was
  the one thing none of them exercised.
- **A capability could never become `verified`.** `confirm()` is the only thing
  that promotes one, and nothing outside tests called it — in a build whose
  stated design is that only a live run promotes anything. Now wired, with the
  ack restated flat before `confirm()` sees it: the engine's `ActionResult`
  nests its evidence one level down and `missing_keys` matches at the top, so
  feeding it the engine result verbatim reported every key missing, silently.
- **`movement.move_near` could not be called at all.** It required a
  `RefKind.OBJECT` reference, and `PZAgent.ObserveModel` never mints one — a
  nearby thing that holds a container gets a `container:` reference and
  everything else gets a `square:` one. It refused every reference the mod is
  capable of producing.
- **Every movement command would have been refused.** The sidecar sent `target`
  as a nested object plus `square_ref` and four policy flags; the mod declares
  `x`, `y`, `z` and `radius`, because its dispatcher accepts only scalars. Six
  undeclared keys, and an undeclared key is a refusal. `inventory.transfer` and
  `inventory.ensure_main` had the same defect with an `origin` object, which no
  scalar declaration could ever have accepted; the origin is now read from the
  before-observation, which is a better source anyway — one is an assertion
  about the world, the other a reading of it.
- **The client-facing tool list went stale unnoticed.** `configs/mcp/README.md`
  advertised nineteen tools and seven actions. The test meant to catch that
  unions every document before comparing, so `docs/MCP_TOOLS.md` naming all of
  them satisfied it while the README fell arbitrarily far behind. It now asks
  per-document, and in both directions.
- **Adapters registered nowhere.** `Toolkit.declare` produced tables naming
  themselves under `name`, while `ActionRuntime` looks an adapter up by
  `adapter.action`. The mod would have loaded cleanly, reported healthy and
  answered `CAPABILITY_UNAVAILABLE` to every game action.
- **Adapter arguments were silently dropped.** `CommandDispatcher` builds the
  argument table from the adapter's declaration, so an adapter that declared no
  arguments ran with all of them gone rather than being refused. Declarations
  are now mandatory and asserted at load time.
- **`RUNTIME_OWNED` was referenced and never defined**, so `ActionRuntime.install`
  raised on any build where the adapters directory had published anything.
- **A lease expiring mid-flight was reported as `ACTION_TIMEOUT`**, which tells
  the sidecar its adapter is slow when in fact its own grant lapsed. It is now
  `LEASE_EXPIRED`; whether anything reached the character's queue is carried by
  the phase, which already distinguished `interrupted` from `rejected`.
- **`pz-agent restore-save` passed `game_running=False` unconditionally**, so it
  would have overwritten a save with the game open — the exact failure the
  keyword-only argument exists to prevent.

### Added

- Game-smoke harness (`pz-agent smoke`). A scenario that did not run is
  reported as not run — never as passing, never omitted — and a dry run cannot
  produce a pass, because it touched no game.
- `FINAL_IMPLEMENTATION_REPORT.md`, naming exactly what still requires a person
  with Project Zomboid installed.

### Added

- Repository foundation: package layout, `pyproject.toml`, ruff/mypy/pytest
  configuration, `.luacheckrc`, editor and git attributes.
- `pz_agent_core.version` as the single source of truth for the five versions,
  with a release gate that checks every place they are restated.
- Wire protocol package: closed enums, stable session-scoped references with
  generation tracking, and strict total parsers for commands, action results
  and observations.
- Safety invariant enforced in the type system: an `ActionResult` with status
  `succeeded` cannot be constructed without `POSTCONDITION_MET` and non-empty
  postcondition evidence.
- CI gate against forbidden shortcuts — stub bodies, `TODO` markers in shipped
  code, `eval`/`exec`/`shell=True`/`loadstring`, and committed secrets.
- GitHub Actions workflow covering Python 3.11/3.12, luacheck, Lua unit tests
  and a build artifact.
- Installation discovery across every Steam library, with an injectable
  filesystem root and environment so the Windows path is testable on Linux CI.
  Build detection reports an honest unknown rather than guessing.
- Save backup and restore with a hashed manifest. Restore refuses while the
  game is running and verifies every hash before writing; prune never removes
  the newest backup.
- File IPC: fixed layout, byte-offset journal reader that ignores a partial
  trailing line and skips a corrupt one, alternating-slot snapshots with the
  pointer written last, sequence gap detection, bounded idempotency cache and
  lease enforcement at both check points.
- Session handshake requiring a nonce different from the previous session, so a
  file left by a crashed sidecar cannot read as a fresh connection request.
- Lua mod for Build 42: pure shared modules (JSON with deterministic key order
  and no `loadstring`, references, protocol constants, sequences, queue
  ownership) and the engine-coupled client half, with a test harness that runs
  under a plain interpreter.
- Sixteen game-smoke scenario definitions, each naming the evidence that closes
  it.
- Documentation: protocol, architecture, safety, testing, compatibility,
  limitations, MCP boundary, quick start, troubleshooting, development and
  release.

- Capability model and read-only symbol scanner. A static scan yields
  `available_unverified` at best; only a live runtime confirmation produces
  `verified`, and a report from a different build downgrades every verified
  entry. The scan records symbol names, paths, signature lines and file hashes
  but never file contents.
- Action lifecycle engine. Preconditions are checked against an observation
  newer than anything already seen, and the mod's ack never overrides
  observation: without evidence from the adapter's verify, the result is
  `POSTCONDITION_FAILED` regardless of what the mod claimed.
- Deterministic selection policy for food, drink and literature, returning the
  score breakdown and the reason each rejected candidate lost.
- Observation diff, bounded store and the compact planner view, which is the
  only observation an LLM ever sees.
- Deterministic reflex guard, threat assessment and priority arbitration with
  anti-loop rate limiting. No LLM in the path, so it runs whether or not a
  planner is configured.
- Cross-language contract tests asserting the Lua and Python halves agree on
  versions, the action whitelist, reason codes, enums and IPC filenames.

- Typed planner, critic and executor. A plan structurally cannot carry code:
  `StepArgs` is a closed Protocol over a fixed parser table, so there is no
  field a Lua snippet, a shell string or a path could occupy. `NullProvider`
  plans deterministically from the policy modules, making `provider = "none"`
  a tested configuration rather than a claim.
- Sidecar attach/observe/act loop behind `start`, `stop`, `arm` and `disarm`.
  It attaches in OBSERVE, runs the reflex guard before anything else whether or
  not a planner is configured, and never re-arms itself after a restart.
- Windows installer and uninstaller that record a manifest of what they wrote
  and remove exactly that, so a file the user placed in the mod directory
  survives an uninstall.
- Doctor CLI, diagnostics with redaction applied as records are written, MCP
  boundary, permissions and autonomy engines, bounded save-scoped memory, and
  the voice companion.
- Lua observation producer, with a cross-language contract test that runs the
  builder under lua5.4, validates its output against the schema, parses it with
  the Python dataclasses and re-parses every reference.

### Fixed

- Every zombie in a horde shared one reference: the observer read `getOnlineID`
  first, which answers `-1` outside multiplayer, and `-1` was a legal reference
  segment. Threat assessment counts distinct references, so a horde read as one
  zombie.
- The inventory walk was unbounded on the game thread — nested bags multiplied
  to thousands of engine calls to produce a document that keeps 64 containers.
- Mutual exclusion did not hold: `O_EXCL` makes the lock file's *creation*
  exclusive, not the claim, so two sidecars could both report `acquired`.
- Backups were returned as complete without reading back what landed on disk,
  and restore hashed every file it copied and discarded the result.
- The support-bundle verifier reported the forbidden literal it found — in a
  report printed to a terminal and emitted as JSON, which would have been the
  leak it was reporting.

- `scripts/check.sh` ran luacheck but never executed the Lua tests, so failing
  assertions would not have been caught locally. It now runs them over the same
  glob CI uses.

[Unreleased]: https://github.com/natural0101/poject-zombigpt/compare/main...dev
