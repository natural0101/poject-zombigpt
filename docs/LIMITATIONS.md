# Known limitations

An honest list. Everything here is either a limitation by design or a gap this
build actually has. Where a claim could not be checked from the code, it says
so.

For implementation status — what is built, what is tested, what still needs a
running game — see [`PROGRESS.md`](PROGRESS.md).

---

## The three that matter most

**1. Live game validation has never run.** Not once. No probe in
`pz_agent_core.capabilities` has ever been confirmed against a running Project
Zomboid; every row in `docs/GAME_API_VERIFICATION.md` is `requires_live` with an
empty "Actual" column, 52 symbols in total; all twenty scenarios in
`pz_agent_cli.livetest` are `NOT_RUN`; the sixteen files under
`tests/game-smoke/` have never been executed against a game. That includes
`ISTakeWaterAction`, whose argument order the document flags as unconfirmed and
silently wrong-filling if the build differs, and `isClient`/`isServer`, without
which the agent refuses every mutating command. **Every behavioural claim in
this repository is a claim about code, not about the game.**

**2. The TeamON voice bridge is exercised only against a fake subprocess.**
`tests/contract/test_teamon_bridge_e2e.py` launches a real child process over a
real pipe and speaks the real JSONL framing to it — but the child is a script
the test writes to a temporary directory at run time — so nothing a build or an
import walk can reach speaks this protocol — and it is built to be impossible to
mistake for the real thing: it refuses to start without `--acknowledge-fake`, so
a configuration pointed at it by accident meets a program that exits non-zero
with nothing on stdout instead of a convincing handshake. Nothing in that file imports the vendor
SDK, opens a microphone or produces a sound. What is proven is the *wire
contract* — framing, bounds, refusals, process lifetime, kill behaviour. What is
not proven is that a bridge written against the real SDK behaves the way that
fake does.

**3. No voice adapter has been tested against real TeamON.** The SDK is not
installed in this environment. `require_teamon_sdk()` and
`teamon_sdk_available()` are exercised in their **absent branch only**.
`TeamONVoiceAdapter` is complete and fully tested against the `TeamONClient`
protocol this project defines, and that protocol is a statement of what the
adapter needs — not a transcription of a vendor surface anyone has seen. No call
into TeamON has ever been made from this repository.

---

## Scope

**Single player only.** Two gates enforce it — `pz_agent_cli.config._forbidden`
refuses `safety.allow_multiplayer = true` as a configuration error, and
`ActionEngine._multiplayer_abort` refuses every mutating command unless the mod
positively reported the session as single player.
`observation.game.multiplayer` has three states and **an absent reading is
refused exactly as `true` is**, because silence is not permission.

Stopping, disarming, cancelling and the read-only actions are deliberately
exempt: an agent that cannot be stopped in the one session it should not be
running in is worse than one that never had the gate.

**This has never been exercised against a real multiplayer session.** The mod
reads `isClient` and `isServer`, both unconfirmed against Build 42.20 like every
other engine symbol. If neither can be read, the agent refuses to act at all
rather than assuming single player — a conservative failure, but a failure.

**One character.** The protocol is session-scoped to a single active character.
Split-screen and multiple simultaneous characters are out of scope.

**Windows 10/11 x64, Steam.** Path discovery targets the Steam layout. The core
is platform-neutral and its tests run on Linux, but nobody has verified the
end-to-end path against a GOG or a Linux install of the game.

**Build 42.20 Stable.** `SUPPORTED_BUILDS` is `("42.20",)`. On a different build
the doctor warns and every previously `verified` capability downgrades.

---

## What the agent will not do

**Autonomous combat.** `autonomous_attack`'s probe has a hard ceiling of
`unsupported` with `NO_VERIFIED_API`, so even a live ack cannot raise it. Faking
it by writing stats would be a lie shaped like a feature. What the assisted-combat
epic shipped rides a *separate* capability, `combat_assist`, and narrows this
limitation without touching that ceiling: single-target ASSISTED combat exists —
four P4 actions (`combat.equip_best`, `combat.shove`, `combat.engage`,
`combat.retreat`) and the `engage_single_zombie` goal — reachable only by an
explicit user submission or tool call, one bounded attack window per command,
policy-refused for groups over the limit, exhaustion, injury or a broken weapon,
and verified only by the re-observed zombie. `combat_assist` itself resolves to
`experimental` until a live session confirms the attack entry points, so on an
unverified install the combat surface is withheld, not offered. Nothing on the
agent's own initiative — no arbiter, no planner, no initiative table — can mint
a combat goal or propose a combat action; each of those absences is pinned by
its own test.

**Drive.** Driving is not modelled and no action steers, starts or moves a
vehicle. Vehicles are not entirely absent: `ContainerKind.VEHICLE` exists, so a
vehicle's storage can be named as a container, and `survival.sleep` takes an
`allow_vehicle_seat` flag (off by default).

**Anything requiring an unverified capability.** `_capability_gate` refuses, and
on the agent's own initiative even `available_unverified` is not enough.

**Write game statistics directly.** Setting `hunger = 0` always "succeeds",
which is precisely why it is forbidden.

---

## Gaps in the wiring

These are not design trades. They are places where a component exists, is
tested, and is not connected to the thing that would use it.

**The agent cannot walk: the mod emits no square tier.** This is the largest gap
in the build and it blocks every scenario involving movement.

`movement.move_to`, `movement.move_near`, `world.inspect` and the navigation
local map all locate the destination square by scanning `nearby.objects` for an
entry whose `kind` is the literal `square`, reading `loaded` / `blocked` /
`closed_window` / `drop` from its `semantics`. The mod has no code path that
emits one: `Observe.nearbyObjects` sets each entry's `kind` from the container
type, from `getObjectName` lowercased, or to the literal `corpse`;
`Refs.KIND.SQUARE` occurs only where reference *strings* are minted and parsed;
`Observe.nearbyFields` exports `objects` and `zombies` and no square tier; and
the strings `"loaded"`, `"blocked"` and `"closed_window"` appear nowhere in the
mod's Lua. Driven against a document shaped the way `Observe.nearbyObjects`
shapes it, a one-square walk east refuses `TARGET_NOT_LOADED` — "no loaded
square was reported at (1201, 3400, 0)".

It survived a fully green suite because the sidecar's own fixtures mint the
square objects the mod never sends (`tests/fixtures/adapter_worlds.py:a_square`),
so each side was only ever tested against its own idea of the document.

**A world container can be named but never resolved, so nothing loots.** The
third gap of the square tier's shape, and the one that takes a whole goal kind
with it.

`InventoryView.container` searches `inventory.containers` and nothing else, and
`resolve_container` refuses `INVALID_REF` for any reference not in that list.
`container.inspect` needs it twice — once as a precondition and again to verify
against the observation after — and `inventory.transfer` resolves its source the
same way. The mod's inventory has exactly two roots: the main inventory, and each
worn container, with `CARRIED` containers nested inside items. There is no third
root, and no path anywhere adds a nearby crate to the tree.

The crate is not invisible. `ObserveModel.buildObject` mints it a proper
container reference whenever the descriptor carries both an `object_index` and a
`container_index`, so a planner can see that a crate is there and can name it.
What it cannot do is resolve it: the reference points into a list the crate was
never added to. Every container action against it refuses `INVALID_REF` —
"is not in the observed container tree" — and `loot_area` therefore cannot take
anything out of anything.

With the missing square tier above it, the loot mission is doubly blocked: it
cannot walk to the crate, and it could not open it if it were standing there.

Recorded rather than repaired, for the same reason as the other two: the missing
half is a mod-side inventory tier for an open world container — deciding when a
crate enters the tree, when it leaves, and what its contents cost to read every
tick — and that is a contract addition whose only honest test is a live game.

**The item-detail tier speaks two vocabularies, and neither side knows.** The
same shape as the missing square tier, one layer down, and this time the data is
present under other names. Measured field by field:

| block | sidecar reads | mod sends | overlap |
| --- | --- | --- | --- |
| `food` | 22 | 8 | 6 |
| `literature` | 11 | 5 | 2 |
| `fluid` | 16 | 3 | 1 |

`ObserveModel.domain` passes keys through verbatim — it sorts, caps and shapes
values but never renames — and `FoodView.from_payload` and its siblings read
`item.food` / `item.literature` / `item.fluid` straight off the observation. So
the names have to agree, and mostly they do not. The clearest cases are the same
fact under two names: the mod sends `pages`, `skill_level_min`, `skill_level_max`
while the sidecar reads `pages_total`, `min_level`, `max_level`; the mod sends
`amount` and `capacity` while the sidecar reads `remaining_units` and
`capacity_units`; the mod sends `rotten` as a boolean while the sidecar asks
whether `freshness == "rotten"`.

Because every reader defaults a missing key (`read_str` to `""`, the numeric
readers to zero or a supplied default), nothing errors. The decisions simply come
out as though the world were uniformly bland: **`FoodView.is_rotten` is always
false**, a book's length and read-progress are unknown, and a bottle's remaining
volume is not what the mod measured.

What survives the mismatch is worth stating precisely, because it is the
difference between a sharp hazard and a dull one — and the precise version is
narrower than it first looks. Each hazard key crosses in exactly **one** block:
`poisonous` is sent in `food` and not in `fluid`; `tainted` is sent in `fluid`
(`itemFluid` sends `amount`, `capacity`, `tainted`, and nothing else) and not in
`food`. So poisoned food is refused and tainted water is refused — the two cases
that matter most — but the crossed pairs are not: a fluid the engine flags
poisonous rather than tainted, or a food it flags tainted rather than poisonous,
reads as false on that key. Whether the game ever flags them that way is a
live-game question, not one this side can answer.

What is lost outright is the softer judgement — rot, freshness, portions left,
pages left, alcohol.

These counts are no longer prose. `tests/contract/test_item_domain_vocabularies.py`
re-derives them from both sources on every run and pins the exact set of keys the
sidecar decides on without a producer, so a new mismatch fails a test instead of
waiting to be found by hand.

Recorded rather than repaired. Fixing it means choosing which side renames, or
adding a translation layer at the seam, and that is a contract decision across
two languages whose only real test is a live game. Guessing it statically would
be the same move as relaxing the sidecar to accept the square tier: it would turn
the suite green over a question nobody had answered.

**A second gap sits behind it: nothing walks the character up to a door.**
`movement.move_near` accepts `container`, `square` and `item` references and
refuses `object` — on both sides, the sidecar's `_MoveNearSpec.parse` and the
mod's own `Movement` argument declaration. That tuple was chosen when no scan
could produce an object reference, and it has been wrong since `bf92ee2`:
`ObserveModel.buildObject` gives a door its own reference from its
`object_index` (`ObserveModel.lua:981`), falling back to the square only when
the index could not be read, and `doors.py` requires exactly that kind for the
`door_ref` it resolves out of the same `nearby` block. So the single per-object
reference the mod does mint is the one `move_near` rejects: a caller holding a
nearby door — the very reference `navigation/executor.py` hands to `door.open`
— gets `INVALID_REF`.

It is recorded rather than fixed because widening the accepted kinds would
change a contract on both sides of the seam to no observable effect while the
destination square is still missing: a walk that cannot resolve where it is
going does not start caring which reference named the target. The two gaps are
one repair, and neither half can be confirmed without a live game. Both
comments that made this read as impossible ("ObserveModel never mints one",
"nothing ever mints one") were corrected in place; the behaviour was not
touched.

**An implementation was built, adversarially verified, and rejected.** It is
recorded here because the three things that killed it are what the real fix must
solve, and each was proven end to end rather than argued:

1. **Reference collision — it fixed walking by breaking drinking. Since
   closed.**
   `ObserveModel.buildObject` mints a non-container, **non-door** world object's
   ref with `Refs.buildSquare`, the same ref a square entry would carry. (Doors
   are exempt: they get a per-object ref from their `object_index`. The
   adversarial run measured this against an older base and said "every door,
   tree and water source"; on this branch it is every tree and water source, and
   a door whose index the reader could not carry.) Each of those inside the tier
   then shares a reference with the ground under it, and after the merge-and-sort
   the square lands first.
   `common.nearby_object` resolves a ref with `next(o for o in nearby.objects if
   o.ref == ref)`, so it returns the square. On the same bytes, with and without
   the tier, a sink one square away went from `consume.drink_source` **ACCEPTED**
   to **REFUSED `NO_SAFE_DRINK`** — "nothing at square:…:101:201:0 reports
   water". Any fix must give the square tier a reference that cannot collide, or
   change how world objects are addressed, which is a contract change.

2. **A partial solidity read publishes a wall as open ground.** The attempt read
   `isSolid` and `isSolidTrans` and published when *at least one* answered. On a
   build exposing `isSolid` and not `isSolidTrans` — the exact case the two-reader
   design was justified by — a glass wall is published `['loaded']` with no
   `blocked`, and `_check_square` accepts the walk into it. "At least one reader
   answered" is not the same as "the question was answered": a fix must require
   every declared reader, or record which ones answered and treat a partial read
   as unread.

3. **The planner's compact view starves one layer below the fix.**
   `compact_for_planner` sorts `nearby.objects` by distance and keeps the nearest
   `MAX_OBJECTS = 24` in one merged list with one cap. A separate square budget in
   the mod's document does not survive that. Measured on a furnished room, a
   nine-square tier cost the planner nine real objects, evicting a real door two
   squares east; in a warehouse aisle at sixteen objects per square, **zero**
   squares reached the planner at all. No test anywhere puts a `kind == "square"`
   entry through `compact_for_planner`.

Two smaller things fall out of the same attempt. `closed_window` and `drop` have
no reader that can be grounded in anything this codebase already trusts, and
`_check_square` reads the absence of each as permission — so those two refusals
are unenforceable until a reader exists. And any solidity accessor the mod comes
to depend on must be added to `GAME_API_VERIFICATION.md` by hand:
`tests/contract/test_game_api_inventory.py` deliberately checks engine *classes*,
so a lowercase method name is invisible to it and would ship unverified.

Until this is closed, `TARGET_NOT_LOADED` on every move is expected, and the
sidecar's precondition must not be relaxed to get past it — an unassessed square
is exactly the thing that refusal exists to catch.

One consequence of that reference scheme has been fixed independently of the
tier, because it bites without one: a reference that names a square denotes the
place *and everything on it*, so a question about the place has to be asked of
every object carrying it. `consume.drink_source` asked only the first, and a tree
scanned before a sink made the sink invisible. It now asks all of them
(`nearby_objects` in `actions/adapters/common.py`). Any future consumer of a
square-scoped reference has the same obligation.

**That fix closes blocker 1 above**, which changes what a square tier now has to
solve. Every other place that resolves a reference against `nearby.objects` was
audited, and each asks a *position* question rather than a property one:
`movement.move_near` in all four of its uses (`validate`, `build_args` and both
sides of `verify`) and the planner critic's `_destination`. Position is shared by
construction — everything answering to a square's reference stands on that square
— so a square entry landing first changes nothing there. The square lookups
themselves (`_square_object`, `world.inspect`'s `_reported_square`, the local
map) match on **kind and position**, never on the reference, so they are
unaffected by definition. A regression test pins the exact case that killed the
attempt: a `kind="square"` entry listed ahead of a sink at the same reference, and
the drink still accepted. It is written against the *shape* a tier would produce
rather than against any tier, since none is shipped.

One residual, noted rather than fixed because it could not be turned red: if the
first entry at a reference carries no position while a later one does,
`move_near` and the critic both answer "unlocatable" and refuse. That is an extra
refusal, never a false success, and no path was found by which the mod emits a
position-less object at a square reference.

**Two blockers remain**, and they are the ones a tier must still answer: the
partial solidity read (2) and the planner's compact view (3).

**Two more sidecar gates have no producer.** The square tier above is the
expensive instance of a shape that has now turned up three times: the sidecar
branches on something, both branches are tested, and the mod has no path to one
of them. Each side is exercised against its own idea of the document, so the
suite is green and the behaviour is dead in the shipped system.

- **`container.accessible` is always true.** Five sidecar sites refuse on it —
  `container.py` twice, `inventory.py`, `selection.py`, and the `container_chain`
  rollup — so an unreachable container is meant to be refused rather than
  attempted. `ObserveModel` computes `accessible = node.accessible ~= false`, and
  **nothing anywhere in the mod ever sets that field**: it is `true` at three
  hardcoded roots and `nil` everywhere else. Every container in every document
  the mod can build is therefore accessible, and all five refusals are dead. This
  is the consequential one — a locked or blocked container is presented to the
  agent as reachable. Closing it needs an engine reader for reachability, the
  same unverified-symbol problem that sank the square tier.
- **`observation.full` is always true.** `observation/store.py` branches three
  times on a partial snapshot, merging it onto the last full one; `Observe.context`
  sets `full = true` unconditionally, so no delta has ever been sent and those
  branches have never run. Benign — a full snapshot every tick is the safe
  direction — but the merge is untested against anything real.

All three are now a ledger rather than folklore:
`tests/contract/test_gates_without_producers.py` asserts each producer is *still*
absent, so the day somebody implements one the test fails and asks for the row to
be moved, instead of the dead branch waking up unnoticed beside its new producer.
It carries a positive control, because a pattern language that matched nothing
would make every row pass by construction — which is the failure mode the file
was written about.

**`plan.execute` is not served over the Core RPC link.** An earlier revision of
this entry said the sidecar published no Core RPC endpoint at all; that gap is
closed — `pz-agent start --foreground` builds `CoreServices` over the running
loop and calls `serve_core_rpc(...)` after a successful attach and before the
first tick, so `pz-agent-mcp` finds `runtime/core-rpc.json` and serves. Session,
observations, capabilities, memory, diagnostics, the goal channel and actions
are answered from the live loop
(`tests/contract/test_sidecar_serves_the_core.py`,
`tests/contract/test_remote_actions_served.py`). What remains unserved is
plans: `plan.execute` over the link answers `CORE_REFUSED` with a named reason
(`REMOTE_PLANS_UNSERVED` in `pz_agent_cli/core_services.py`), and the goal
channel is the multi-step shape the link serves.

Check it yourself, in one command:

```
grep -rn "serve_core_rpc" packages/ tests/
```

If `app.py` no longer calls it, the closed half of this paragraph has gone
stale again.

**`voice run` cannot start a TeamON session.** `select_adapter` refuses with the
install step when the SDK is absent, and refuses with a different message when
the SDK is present but no `TeamONClient` was supplied — because no binding from
the vendor surface has been verified from this repository. Whoever has the SDK
either writes the three methods and passes the client to
`select_adapter(..., client=...)`, or runs the bridge program on the far end of
`pz_agent_voice.teamon`. See [`VOICE.md`](VOICE.md) for which of those the CLI
currently wires.

**`config.schema.json` is loaded by nothing and has drifted.** No code and no
test reads it. It disagrees with `pz_agent_cli.config.SCHEMA`, which is what
actually validates `config.toml`, on the `game` key names, on the case of
`session.default_mode`, on the provider enum, on `planner.max_steps` (10 vs 32)
and on the existence of the provider sub-tables. [`PROTOCOL.md`](PROTOCOL.md)
tabulates the differences. Treat the code as the contract.

**`event.schema.json` is loaded by nothing.** Its fourteen event types include
`voice_input` and `voice_output`, which nothing in this tree emits or consumes.

**Three different plan-step ceilings.** The plan schema and the core planner cap
a plan at 5 steps; `pz_plan_execute` publishes — and *defaults to* — 8;
`planner.max_steps` in `config.toml` accepts up to 32. The CLI clamps the
configured value to 5 explicitly. The MCP path's 8 is not clamped anywhere this
document could find, and the core planner's `PlanRequest.__post_init__` raises
`ValueError` above 5. **What a client actually gets back from `pz_plan_execute`
against a running sidecar is the link's named refusal** — `plan.execute` is not
served over Core RPC (see above) — so the 8-versus-5 collision is reachable
only in an embedded run, where `main()` is handed a services bundle in-process.

**`safety.manual_takeover` is read by nothing.** It is a validated boolean with
a default of `true` and no reader anywhere in `packages/`. Takeover *detection*
is unconditional, so the behaviour is safe and the setting is decorative;
setting it to `false` disables nothing. See [`SAFETY.md`](SAFETY.md).

**`safety.panic_hotkey` cannot be rebound.** The mod hardcodes DirectInput
scancode 88 (F12) because no `Keyboard` global has been probed on 42.20. The
validator refuses any other value outright rather than accepting a setting that
would bind nothing — which is the honest failure, but it does mean the panic key
is not configurable.

**The Windows executables have never been built.** `bin/pz-agent.exe` and
`bin/pz-agent-mcp.exe` are absent from the release archive, whose
`BUILD-MANIFEST.json` records `complete: false`. They need PyInstaller on
Windows, and the builder exits non-zero on Linux for exactly that reason while
writing the rest of the archive anyway.

---

## Consequences of the design

**Latency is a tick or two.** The transport is a file journal, polled. That is
the right trade when a timed action takes seconds, but it means the agent is not
suitable for anything reflex-speed.

**Actions can fail.** Because success requires an observed postcondition, the
agent reports failures that a screen-scraping bot would report as success. This
is intended and is the reason to prefer it.

**Refs die on save/load.** Every item, container and square reference is scoped
to a session and a generation. After a save/load transition, references minted
earlier are `INVALID_REF` — not stale, invalid. A plan built before the
transition cannot be resumed; it is rebuilt from a fresh observation.

**Plans are short by construction.** Five steps, not a campaign. Long-horizon
goals are pursued by repeatedly re-planning, which means the agent can look
indecisive if the world keeps changing under it.

**The vulnerable-action interrupt rung has never fired.** `ReflexGuard`
carries §17.2's "visible zombie near during read/eat → interrupt" as
`DEFAULT_VULNERABLE_ACTIONS`, matched against `ActionState.type`. The shipped
mod never fills that field: the observation's action block is
`Ownership.describe`'s table (`Runtime.lua:183` → `Observe.lua:1125`), which
carries `ownership`, `busy`, `readable`, `total`, `mod_owned`, `foreign`,
`truncated` and `classes` and no `action_type` — the one field
`ObserveModel.action` reads into `type`. So `running_type` is always `""`,
`vulnerable` is always false, and the rung guarded by it is dead code in the
shipped system. Where the mod does record an `action_type` elsewhere
(`Safety.lua:430`) it is `action.Type`, the engine's Java class name kept "for
diagnostics", so wiring that through would still not match `consume.eat` or
`literature.read`.

The consequence is bounded, not absent: the flee rung above it ignores the
action type entirely, so a real emergency still cancels everything. What is
missing is the *earlier* reaction — during a read or a meal the agent responds
only at `flee_at`, never at the lower `interrupt_at` the spec asks for. Fixing
it means teaching the mod to carry the in-flight command's protocol action name
into the queue description, a change to safety-critical code on the side no
test here can exercise, so it is recorded rather than guessed at.

**Going stale does not disarm the mod.** `Safety.sidecarStale` is consulted in
exactly three places — `Safety.arm`, `Safety.mayStart` and the snapshot — and
all three only refuse; none touches `state.armed` or `state.mode`.
`Safety.disarm` is reached only from a `session.disarm` command, a newly
accepted session, or a panic stop. So after an unclean sidecar exit whose
`session.disarm` never lands, the mod keeps reporting the mode it was granted
until one of those three happens. It cannot act on it: `mayStart` refuses every
action but stop, disarm and cancel while the heartbeat is stale. The residue is
a stale reading, not a running agent — but two places in the sidecar used to
promise a "stale-sidecar disarm" that does not exist, one of them in the notice
text an operator reads.

**Recovery never re-arms.** After any crash, restart or save change, the agent
comes back in `OBSERVE`. You re-arm it. Not configurable.

---

## Operational bounds

Everything is capped, and hitting a cap is reported rather than silently
applied.

| Bounded thing | Consequence when the cap is hit |
| --- | --- |
| Observation ring buffer | Oldest observation evicted |
| Idempotency cache | Oldest key evicted; a very old duplicate could re-execute |
| Logs and journals | Rotated; oldest rotated file deleted |
| Memory store | Retention policy applies; derived facts kept, raw history dropped |
| Retries per command | Terminal failure with the last reason code |
| Plan length | Planner output rejected if longer |
| Autonomous radius | Movement beyond it refused |
| Backup source size | Refused with a clear error rather than filling the disk |
| Compatibility scan | Truncated, and the truncation is reported |
| Core RPC request | Refused past 64 KiB |
| Bridge line | Dropped past 16 KiB, as it arrives, and the drop is reported |
| Voice utterance queue | Least urgent oldest pending evicted; a stop can never be |

The idempotency cache bound is the one with a real edge: a duplicate command
whose key was evicted long ago would be treated as new. In practice keys are
per-goal-step-attempt and the cache outlives any plausible redelivery window,
but it is a bound, not an impossibility.

---

## Not published, and why

**`pz_action_sleep` is normally absent from `list_tools`.** `survival_sleep`
resolves to `experimental` on a clean scan, and an experimental capability is
upgradeable but not usable. The reason is specific: sleep runs through a
context-menu callback, so once the character is asleep there is no timed action
to interrupt and no queue entry to cancel, and a panic stop cannot reach them.
`pz_action_drink_source` is withheld the same way, because §12.4 lists the world
water action as unconfirmed. A missing tool here is a capability answer, not an
error, and `pz://capabilities` says which ones are withheld and why.

**`world.inspect`, `container.inspect` and `inventory.search` carry no
capability evidence at all.** They gate on the observation tier they read rather
than on a probe, because everything they read is reached through Java accessors
that never appear in the game's Lua — a probe over those names would report
`unsupported` on a perfectly healthy install. So "the scan says nothing about
these three" is by design, and it does mean they are the three actions whose
availability rests on no runtime evidence.

**`allow_windows` is not published.** The movement adapter refuses it with
`POLICY_DENIED`, so offering it would advertise something policy forbids.

---

## Things mocks do not prove

`tests/lua/` runs the mod's real modules under a plain Lua interpreter with
mocked engine globals — 26 suites covering the command dispatcher, the action
runtime, the safety layer, the observation model, ownership, sequence handling
and all ten adapter files. The cross-language reference agreement is asserted
from the Python side, in `tests/unit/test_lua_observation_contract.py`, which
runs the mod's own observation builder and puts its bytes through the schema and
the dataclasses.

It does **not** prove that `ISInventoryTransferAction`, `ISEatFoodAction` or
`ISReadABook` behave as expected in Build 42.20. Only a live session does that.

Two catalogues track those runs — `pz_agent_cli.livetest` (20 scenarios, which
the release gate enforces) and `tests/game-smoke/` (15 plus an endurance run,
judged by a reviewer) — and their numbering collides, so a scenario id is
ambiguous unless the catalogue is named with it. See `docs/RELEASE.md`.

**The reflex guard's thresholds have never been calibrated.** `ThreatConfig`'s
defaults are a reading of the blueprint, not a measurement. Whether `flee_at`
fires early enough, or too often, is unknown.

**Stop latency has never been measured.** The path is countable — one interim
transcript, one `cancel_speech`, one synchronous `handle()`, one file write, then
the mod's next heartbeat tick — but the two ends of it are a real recogniser and
a real game, and neither exists here. No number is claimed.

Any claim of engine compatibility that is not backed by a run against the
installed game is a claim this project does not make.

---

## Privacy and provider caveats

With `provider = "none"` the agent is fully local and this section is empty.

If you configure an external LLM provider, the compact observation leaves your
machine: scalar character stats, capability flags, item references with display
names and categories, and the current goal. Not the full snapshot, not paths,
not chat text — but it does leave. That is your choice to make, and the agent
embeds no key and defaults to none.

See [`PRIVACY.md`](../PRIVACY.md).

---

## Not signed

Windows will show an unsigned-binary warning for the packaged launcher. Code
signing is not part of this project. The warning is expected, documented, and
not something to click past without understanding.
