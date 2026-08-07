# Compatibility

## The rule

**An API is not available because a wiki said so, because an old mod used it, or
because it existed in Build 41.** It is available because a probe confirmed it
against the build actually installed on this machine.

This document describes the capability model in
`packages/pz_agent_core/src/pz_agent_core/capabilities/` — four modules:
`model.py` (the states, the evidence, the report), `probes.py` (the twelve
probes), `scanner.py` (the read-only symbol scan) and `report_io.py` (loading and
saving). The generated result lives in `compat/generated_api_report.json`, which
is gitignored — it describes *your* installation, not the project's.

---

## Capability states

`CapabilityState` has exactly five members, and `CapabilityState.usable` is the
property that decides whether an MCP write tool may be published.

| State | What it means | `usable` |
| --- | --- | --- |
| `verified` | A probe ran against the live game and confirmed the behaviour | **yes** |
| `available_unverified` | The symbols exist in the local files; nothing has exercised them | **yes** |
| `experimental` | Symbols present, but driving them is not proven safe | no |
| `unsupported` | No verified API. The reason is recorded | no |
| `disabled_by_policy` | Available, and you switched it off in `config.toml` | no |

To switch one off, list it under `safety.disabled_capabilities` in
`config.toml`. The accepted names are `KNOWN_CAPABILITIES`, which is
`sorted(PROBES_BY_NAME)` — read from the probe table rather than restated, so a
probe added tomorrow is switchable off the same day and a name invented in the
config file could never validate. An unknown name there is a configuration
error, not an ignored line.

The state ladder is one-directional in an important way: **a static scan can
never produce `verified`.** `ProbeDefinition.__post_init__` raises
`CapabilityError` for a probe whose `static_state` is `verified`, so the
ordering cannot be skipped by a new probe definition. Reading the game's Lua
files tells you a symbol exists, not that calling it does what you expect.

`verified` requires evidence, and only `Evidence.from_ack` can mint it, and only
from a `succeeded` `ActionResult` carrying the postcondition keys the probe's
`RuntimeConfirmation` names. `RuntimeConfirmation.__post_init__` refuses a
confirmation that names no evidence keys at all, and `missing_keys()` is what
checks the ack against them: an ack that says `succeeded` while carrying an
empty or unrelated evidence bag does not upgrade anything.

Three further guards in `model.py` worth knowing:

- A `CapabilityReport` may hold at most 64 capabilities, may not list the same
  name twice, and refuses a `Capability` whose `build` differs from the
  report's. A claim proven on another build, sitting inside a report that says
  it describes this one, would pass `usable()` on the strength of evidence about
  a different game.
- Evidence per capability is bounded at 8 entries; reasons at 200 characters,
  details at 300, paths at 400.
- `revision` is the counter the mod echoes back on every observation as
  `capability_revision`, so a consumer that cached a tool list can tell its
  cache is stale without diffing.

---

## How the scan works

`pz-agent doctor` runs a **read-only** scanner over the game's local Lua
directory (`<install>/media/lua`) and builds a `SymbolIndex`.

Hard constraints, in code:

- It never writes into the game directory. `refuse_write()` raises
  `WriteRefused` for a destination inside a protected root.
- The report records symbol names, relative paths, a signature line and a
  sha256 of each file — **never the file's contents**. Vendoring game source is
  forbidden by licence and by this project's rules.
- It is bounded, and every bound is a named constant in `scanner.py`:

| Limit | Value | Why that number |
| --- | --- | --- |
| `DEFAULT_MAX_FILES` | 8000 | Vanilla ships a few thousand; far more is a modded install or a symlink loop |
| `DEFAULT_MAX_FILE_BYTES` | 1 MiB | A larger file is refused whole, not read in part — a partial read yields a sha256 that does not describe the file |
| `DEFAULT_MAX_TOTAL_BYTES` | 96 MiB | |
| `DEFAULT_MAX_SYMBOLS` | 40 000 | |
| `DEFAULT_MAX_DEPTH` | 12 | The vanilla tree is about six deep |
| `MAX_SIGNATURE_LEN` | 300 chars | |
| `MAX_PARAMS` | 16 | |
| `MAX_PROBLEMS` | 50 | One problem per file, capped, and the cap is reported |
| `MAX_TRUNCATION_REASONS` | 20 | One reason per directory, same rule |

Truncation is reported rather than silently applied.

The extractor is line-based and tolerant. It recognises `function X:y(...)`,
`function X.y(...)`, `X.y = function(...)`, `X = X or {}` declarations and
`ISBaseTimedAction:derive("...")`, and it strips `--` line comments and
`--[[ ]]` / `--[==[ ]==]` block comments. It is not a Lua parser and does not try
to be — a tolerant extractor that reports what it found beats a strict one that
fails on the first unusual file.

---

## The twelve probes

`PROBES` in `probes.py` holds exactly twelve `ProbeDefinition`s. Each names the
symbols a static scan must find (an AND over all of them), the runtime
confirmation that could raise it to `verified`, and a ceiling.

| Capability | Required symbols | Static ceiling | Confirmed by evidence keys |
| --- | --- | --- | --- |
| `move_to_square` | `ISWalkToTimedAction`, `.new`, `ISTimedActionQueue.add` | `available_unverified` | `position` |
| `inventory_transfer` | `ISInventoryTransferAction`, `.new`, `ISTimedActionQueue.add` | `available_unverified` | `item_ref`, `container_ref` |
| `eat_percentage` | `ISEatFoodAction`, `.new` | `available_unverified` | `hunger_before`, `hunger_after` |
| `drink_carried` | `ISDrinkFromBottle`, `.new` | `available_unverified` | `thirst_before`, `thirst_after` |
| `read_literature` | `ISReadABook`, `.new` | `available_unverified` | `item_ref`, `reading_started` |
| `equipment_equip` | `ISEquipWeaponAction`, `.new`, `ISTimedActionQueue.add` | `available_unverified` | `item_ref`, `slot` |
| `equipment_unequip` | `ISUnequipAction`, `.new`, `ISTimedActionQueue.add` | `available_unverified` | `item_ref`, `container_ref` |
| `medical_bandage` | `ISApplyBandage`, `.new`, `ISTimedActionQueue.add` | `available_unverified` | `body_part`, `bleeding_after` |
| `survival_rest` | `ISTimedActionQueue.add` | `available_unverified` | `endurance_before`, `endurance_after` |
| `survival_sleep` | `ISWorldObjectContextMenu`, `.onSleep` | **`experimental`** (`EXPERIMENTAL_API`) | `fatigue_before`, `fatigue_after`, `elapsed_game_seconds` |
| `drink_world_source` | `ISTakeWaterAction`, `.new` | **`experimental`** (`EXPERIMENTAL_API`) | `thirst_before`, `thirst_after`, `source_ref` |
| `autonomous_attack` | none | **`unsupported`**, hard ceiling (`NO_VERIFIED_API`) | none accepted |

Three design decisions that are visible in that table and are worth reading as
decisions rather than as omissions:

**`equipment_equip` requires only the weapon class.** Wearing a garment goes
through `ISWearClothing`, which the mod resolves at construction time and
reports as `CAPABILITY_UNAVAILABLE` naming the class. A probe is an AND over its
symbols, so requiring both here would refuse to draw a weapon on a build that
merely spells the clothing action differently.

**`survival_rest` requires only the queue.** Resting is mostly the *absence* of
a queued action, and the two sitting classes differ between builds; a build with
only one must degrade to that one rather than refuse to rest. Which posture was
available is reported by the mod, per attempt.

**`autonomous_attack` has a ceiling, not a missing probe.** `ceiling` is
`unsupported`, so `confirm()` cannot raise it even with a live ack. The decision
not to drive autonomous combat is a design decision.

Three actions have **no probe at all**: `world.inspect`, `container.inspect` and
`inventory.search`. Everything they read is reached through Java accessors that
never appear in the game's Lua, so a probe over those names would report
`unsupported` on a perfectly healthy install. They gate on the observation tier
they need instead. The consequence is honest and worth stating: those three are
the actions whose availability rests on no runtime evidence.

## Which tools each capability gates

`pz_agent_mcp.catalog` names the capability on the tool. Eleven of the twelve
probes gate a tool; `autonomous_attack` gates nothing, because nothing was built
on it.

| Capability | MCP tools withheld when it is unusable |
| --- | --- |
| `move_to_square` | `pz_action_move_to`, `pz_action_move_near`, `pz_action_open_container` |
| `inventory_transfer` | `pz_action_transfer`, `pz_action_ensure_main` |
| `eat_percentage` | `pz_action_eat` |
| `drink_carried` | `pz_action_drink` |
| `drink_world_source` | `pz_action_drink_source` |
| `read_literature` | `pz_action_read` |
| `equipment_equip` | `pz_action_equip` |
| `equipment_unequip` | `pz_action_unequip` |
| `medical_bandage` | `pz_action_bandage` |
| `survival_rest` | `pz_action_rest` |
| `survival_sleep` | `pz_action_sleep` |

`pz_action_open_container` is gated on `move_to_square` and not on a container
capability, because what it does is walk the character to within reach.

**Two capabilities are experimental, not one.** `experimental` is upgradeable
but not usable, so on a clean scan `pz_action_sleep` and
`pz_action_drink_source` are both withheld — named, with their reason, in
`pz://capabilities` and by `withheld_tools()`. `drink_world_source` is capped
because §12.4 lists the world water action as unconfirmed. `survival_sleep` is
capped for a sharper reason: vanilla drives sleep from a context-menu callback,
so once the character is asleep there is no timed action to interrupt and no
queue entry to cancel, and a panic stop cannot reach them.

`eat_percentage` is a good example of why the state matters. If percentage
eating is usable — `verified` or `available_unverified` from the scan — the food
policy picks a fraction so the character neither overeats nor wastes a large
item on a small need. If it is not, the policy falls back to whole units — and
says so in the rationale, naming the probe's state.

---

## Build changes

A capability report is stamped with the build it was established against.
`CapabilityReport.for_build()` re-states a report against another build and
`Capability.downgraded()` does the per-entry work. It is not a simple demotion:

- Runtime evidence is **discarded**, not kept alongside a weaker state. It was
  gathered against a different build, and leaving it in would let a later reader
  reconstruct a `verified` claim from it.
- A `verified` capability with surviving static evidence becomes
  `available_unverified`.
- A `verified` capability whose only support was the discarded run becomes
  `unsupported` — there is nothing left to stand on.
- A non-`verified` capability with no runtime evidence keeps its state and is
  simply re-stamped with the new build.

A note recording the downgrade is appended to the report, and `revision` is
bumped. 42.19 proves nothing about 42.20.

`pz-agent doctor` warns — rather than hard-failing — when the detected build is
outside `SUPPORTED_BUILDS`, which is `("42.20",)` on this release.
`TARGET_BUILD` is `42.20`: the build the probes were authored against. A point
release usually keeps the API surface we rely on, so refusing to start would be
more annoying than useful; running with every `verified` entry downgraded is the
honest middle.

---

## What `pz-agent doctor` checks

Ten checks, each with a stable code. The codes and their remedies are in
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md); the point here is that `PZD008`,
`timed_actions`, is where the probes above run.

| Check | Code | Why it exists |
| --- | --- | --- |
| `game_installation` | `PZD001` | Across *all* Steam libraries, not just the default one |
| `build_detected` | `PZD002` | Reported explicitly; never guessed |
| `user_directory` | `PZD003` | Honours a custom home directory and non-ASCII paths |
| `directory_permissions` | `PZD004` | The mod cannot write the IPC files without them |
| `mod_installed` | `PZD005` | The most common cause of "nothing happens" |
| `game_heartbeat` | `PZD006` | Distinguishes "mod not loaded" from "no save loaded" |
| `ipc_writable` | `PZD007` | Same directory, the other half of the question |
| `timed_actions` | `PZD008` | The capability probes above |
| `conflicting_files` | `PZD009` | A previous install's IPC files can look like a live session |
| `active_session` | `PZD010` | Whether a save is actually loaded |

`pz-agent doctor --json` emits the report directly.

---

## What has and has not been established

Nothing in this repository claims engine compatibility that a probe has
established, because **no probe has ever run against a live game from this
tree**. Every capability in a report generated here can be at best
`available_unverified`, and `docs/GAME_API_VERIFICATION.md` carries the symbol
inventory with an empty "Actual" column.

`tests/lua/` runs the mod's real modules under a plain Lua interpreter with
mocked engine globals. It proves the mod's *logic*. It proves nothing about the
engine, and [`LIMITATIONS.md`](LIMITATIONS.md) says so in as many words.
