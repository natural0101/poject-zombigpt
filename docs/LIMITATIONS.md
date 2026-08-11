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
empty "Actual" column, 159 symbol rows in total; all twenty scenarios in
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

**Build anything.** No action places a wall, a floor, a stair or a barricade,
and no goal kind asks for one. The crafting rung this build ships makes *items*
from materials on the character and nothing else; placing structures is not
started, not stubbed and not published.

**Craft on the agent's own initiative.** `crafting.craft` is `P3` because it
destroys what it spends, and `P4` whenever the recipe may need a surface or
world-container materials — and `_p4_gate` has no autonomous path at all. No
needs arbiter, no initiative table and no plan provider mints a craft: the
`craft_item` goal is reached by an explicit user submission and by nothing else,
because a provider planning a craft would be a model deciding which of the
character's possessions to destroy. Nor does the mission go shopping for what is
missing — a recipe short of materials ends the goal naming the shortfall, and
whether to loot for it is the user's next sentence.

**Write game statistics directly.** Setting `hunger = 0` always "succeeds",
which is precisely why it is forbidden.

---

## Gaps in the wiring

These are not design trades. They are places where a component exists, is
tested, and is not connected to the thing that would use it.

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

**`pz_action_craft` is absent from `list_tools` on every install this project
can ship to.** Its `crafting` capability is `experimental` for two reasons at
once. Build 42 rewrote crafting, so every recipe accessor the mod names — the
known-recipe collection, the script-manager lookup, the ingredient and output
readers, the craft action class itself — is an unconfirmed guess probed through
a closed candidate list; the crafting rows in `docs/GAME_API_VERIFICATION.md`
are jointly the least certain in that document. And a wrong guess is paid for
differently here than anywhere else: a craft that goes wrong has already spent
the materials by the time anyone finds out, and no observation returns them.
Only a live run — the recipe's product observed in the inventory afterwards —
promotes the capability. `pz_action_inspect_recipe` is deliberately *not*
withheld with it, because reading a recipe spends nothing.

**`world.inspect`, `container.inspect`, `inventory.search` and
`crafting.inspect` carry no capability evidence at all.** They gate on the
observation tier they read rather than on a probe, because everything they read
is reached through Java accessors that never appear in the game's Lua — a probe
over those names would report `unsupported` on a perfectly healthy install. So
"the scan says nothing about these four" is by design, and it does mean they are
the four actions whose availability rests on no runtime evidence.

**`allow_windows` is not published.** The movement adapter refuses it with
`POLICY_DENIED`, so offering it would advertise something policy forbids.

---

## Things mocks do not prove

`tests/lua/` runs the mod's real modules under a plain Lua interpreter with
mocked engine globals — suites covering the command dispatcher, the action
runtime, the safety layer, the observation model, ownership, sequence handling
and every adapter file. The cross-language reference agreement is asserted
from the Python side, in `tests/unit/test_lua_observation_contract.py`, which
runs the mod's own observation builder and puts its bytes through the schema and
the dataclasses.

It does **not** prove that `ISInventoryTransferAction`, `ISEatFoodAction`,
`ISReadABook` or `ISCraftRecipeAction` behave as expected in Build 42.20. Only a
live session does that. The crafting readers are the sharpest case: a mocked
`getKnownRecipes` answers because the mock was written to answer, and whether
Build 42 spells it that way at all is exactly the open question.

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
