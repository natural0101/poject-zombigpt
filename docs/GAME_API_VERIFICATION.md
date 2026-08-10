# Build 42.20 API verification

Every Project Zomboid API this mod touches, what it is assumed to look like,
where it is used, and a place to record what it actually turned out to be.

**None of these is confirmed.** The mod was written in an environment with no
Project Zomboid installed, so every signature below is a reading of the game's
published Lua rather than something that was called. That is why each one is
declared in the adapter's `requires` list and resolved through
`PZAgent.Adapters.Toolkit`, which probes the symbol and `pcall`s the call: a
build that spells a class differently costs one `CAPABILITY_UNAVAILABLE`
*naming the symbol* rather than an error unwinding the dispatcher mid-command.

Some guesses carry a `-- Build 42:` comment, findable with:

```
grep -rn "Build 42:" pz-mod/
```

`grep -rn "Build 42:" pz-mod/` returns six lines, in two files. It is a
*shortcut*, not an inventory: the table below marks 129 symbol rows
`requires_live` (several rows carry two or three slash-separated spellings,
probed in the order written), so the grep covers roughly a twentieth of what is
unconfirmed. **This document is the list.** Use the grep to jump to a comment;
use the table to know what has not been checked.

An earlier revision of this file made the same claim while carrying about
fifty rows and missing roughly sixty symbols the mod actually touches —
including the boot-path globals without which the mod does nothing at all —
and five of the rows it did carry disagreed with the call sites. This revision
was rebuilt from a sweep of the sources themselves: every `requires` list,
every class name handed to `Toolkit.construct` / `Toolkit.constructFirst`,
every accessor string handed to a probing reader, every direct global lookup,
and every `Events.*` registration in `pz-mod/42/media/lua`. If a symbol is in
the code and not here, that is a bug in this file.

## Live run 2026-08-08 — Build 42.20.2, Windows (first confirmed contact)

One live session has now touched this surface. It does not retire the table —
most rows below are still `requires_live` because the run exercised the boot
path, observation, movement and one door, not the full playbook — but it turned
several assumptions from guesses into findings, and every finding produced a
code change in the `P0-build42-live-compat` epic:

- **The mod list refused `pzversion=42.20`.** On the real install the mod only
  appears with `pzversion=42`, and the empty `require=` line has to go.
  `mod.info` now says so; `TARGET_BUILD` (`42.20`) remains what the heartbeat
  reports — the two are different facts.
- **Adapter load order is not alphabetical-safe.** Eight adapters executed
  before `PZAgent.Adapters.Toolkit` existed and now carry an explicit
  `require "PZAgent/adapters/Toolkit"`.
- **Kahlua provides `pairs` but not the global `next`.** Every `next(t) == nil`
  emptiness check crashed its handler — `CommandDispatcher`, `ActionRuntime`,
  `CapabilityRuntime`, `adapters/Medical` were all hit. The shared shim is
  `PZAgent.Compat.hasEntries`, and a contract test now bans the global.
- **`string.byte` on a Java string returns UTF-16 code units, not bytes.** A
  Cyrillic display name yields units like `0x043F`, which the byte-oriented
  UTF-8 encoder refused, losing the whole observation. `Json.lua` now
  classifies each string once and escapes UTF-16 units, surrogate pairs
  included.
- **The game never read `session.json`.** `pz_session_arm` armed only the
  sidecar; the game kept publishing `armed=false, mode=OFF`. `Runtime` now
  reads the offer and feeds it to the session manager it always had.
- **Windows really does hold files open.** `os.replace` on the sidecar side
  took intermittent `PermissionError`s, and the game repeatedly failed to open
  the observation pointer for writing while the sidecar polled it.

Confirmed working as designed, first time live: the mod loaded and published
structured observation; real coordinates, health, moodles, inventory, nearby
objects, containers and doors were read; the character moved and a door was
opened. Part of the movement was driven by the system-input fallback because of
the command/ack defects above — that fallback is a diagnostic tool, not the
product, and the fixes exist so it never has to be.

## How to read a row

- **Symbol** — the engine name the mod looks up. Slash-separated names are a
  closed list probed in order; the first that answers wins.
- **Assumed signature** — what the call site passes or expects back.
- **Used by** — the file (under `pz-mod/42/media/lua/client/PZAgent/` unless
  spelled otherwise) and, for a timed action, the capability it publishes.
- **Fallback** — what the mod does when the symbol is missing or answers
  wrongly. "Field absent" means the observation simply does not carry the
  reading — never a default value.
- **Test** — the `docs/LIVE_TEST_PLAYBOOK.md` scenario that exercises the
  symbol against the real game, and/or the `tests/lua/` file that exercises the
  code path around it with fakes. A Lua test proves the mod's logic, never the
  symbol; only the scenario can move a row past `requires_live`.
- **Failure signature** — the string the mod emits, spelled exactly as the code
  spells it. Most probed symbols share one shape, written here as *default*:
  `<symbol> is not available in this build`, carried on a
  `CAPABILITY_UNAVAILABLE` ack or in `last_error`, with `<symbol>` spelled as
  the *row* spells it. That spelling is the mod's own naming and does not
  always match the class the call lands on — the sleep adapter's refusal says
  `PlayerStats.getFatigue` even though the call is
  `player:getStats():getFatigue()`. Grep for the row's spelling, not for the
  wiki's.

## Status vocabulary

| Status | Means |
|---|---|
| `static_verified` | The symbol was found by a scan of the local install's Lua. It exists; nothing more is claimed. |
| `requires_live` | Neither confirmed nor refuted. Everything below starts here. |
| `live_verified` | Called in a real session and the postcondition was observed. This is the only status that means the adapter works. |
| `incompatible` | Confirmed to differ. Record what it actually is in the last column. |

The gap between `static_verified` and `live_verified` is deliberate and is the
same rule the capability system enforces: a static scan can prove a name exists,
it cannot prove a call does what the adapter needs. Only an observed
postcondition can.

## How to verify one row

1. Run the scenario in `docs/LIVE_TEST_PLAYBOOK.md` that exercises it.
2. If the ack comes back `CAPABILITY_UNAVAILABLE` naming the symbol, the name is
   wrong — find the real one in the game's `media/lua` and fix `requires` plus
   the call site.
3. If the action queues but never runs, the constructor arity or argument order
   differs. This is the most common Build 42 break.
4. If it runs and the result is `failed` with a postcondition that did not move,
   the *reader* is wrong, not the action. Fix the accessor. Do not relax the
   postcondition.
5. Record the outcome in the last column, with the signature you actually found.

## The boot path — when a wrong name means the mod does nothing at all

These are resolved before any command exists. If `getSpecificPlayer`,
`getFileWriter` or `getFileReader` is wrong on this build, there is no
heartbeat, no observation and no ack: the exchange directory stays quiet and
the sidecar reads it as "the game never started". Nothing downstream can name
the missing symbol for you — the naming happens in a file that never gets
written — so confirm these in S01 before concluding anything else in this file
is wrong.

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `getSpecificPlayer` | `(0) -> IsoPlayer`; index 0, split screen is out of scope | `Runtime.lua` (`Runtime.currentPlayer`) — the sole source of the player object | no player: heartbeat reports `player_present false`, observation stops, every mutating command is refused | S01, S02; `tests/lua/test_runtime.lua` | `getSpecificPlayer is not available` / `getSpecificPlayer(0) failed: <err>` in `last_error` | `requires_live` | |
| `getFileWriter` | `(path, createIfNull, append) -> writer` | `Ipc.lua` (`Ipc.defaultFileApi`) | every IPC write returns the reason instead of writing; the mod is invisible to the sidecar | S01; `tests/lua/test_ipc.lua` | `the game's file API (getFileWriter/getFileReader) is not available` | `requires_live` | |
| `getFileReader` | `(path, createIfNull) -> reader or nil` | `Ipc.lua` (`Ipc.defaultFileApi`) | same string, same probe: reads never happen, commands never arrive | S01; `tests/lua/test_ipc.lua` | `the game's file API (getFileWriter/getFileReader) is not available` | `requires_live` | |
| writer `:write(text)` / `:close()` | methods on whatever `getFileWriter` answers | `Ipc.lua` (`Handle:writeRaw`) | the write is reported failed with the engine's own error | S01, S02; `tests/lua/test_ipc.lua` | `opening <path> failed: <err>` / `the game refused to open <path> for writing` / `writing <path> failed: <err>` / `closing <path> failed: <err>` | `requires_live` | |
| reader `:readLine()` / `:close()` | methods on whatever `getFileReader` answers; `readLine` answers nil at end of file | `Ipc.lua` (`Handle:readLines`) | the read is reported failed; a nil *reader* is treated as an empty file that may appear later | S02, S16; `tests/lua/test_ipc.lua` | `opening <path> failed: <err>` / `reading <path> failed: <err>` | `requires_live` | |
| `getTimestampMs` | `() -> number`, wall-clock milliseconds | `PZAgent_Main.lua` (`now`), `Ipc.lua` (`Handle:nowMs`) — every timestamp the mod writes | falls back to `0`: heartbeats carry timestamp 0, the sidecar's staleness window never sees a fresh one, and the agent will not arm | S02; `tests/lua/test_heartbeat.lua` | none — no string is emitted; the signature is every `timestamp_ms` reading 0 | `requires_live` | |
| `getCore` | `() -> Core` | `Heartbeat.lua` (`Heartbeat.detectBuild`) | `build` falls back to the release's target build with `build_verified false` | S01 (`build_string`), S02; `tests/lua/test_heartbeat.lua` | `getCore is not available` / `getCore() did not return the core object` | `requires_live` | |
| `Core.getVersionNumber` | `() -> string`, non-empty | `Heartbeat.lua` (`Heartbeat.detectBuild`) | same fallback as `getCore` | S01, S02; `tests/lua/test_heartbeat.lua` | `getCore():getVersionNumber is not available in this build` / `the game did not report a version string` | `requires_live` | |
| `Events` (global table), `Events.<name>.Add` | each event is a table with an `Add(handler)` function | `PZAgent_Main.lua` (`register`) | the event name is appended to `PZAgent.unavailableEvents` and its feature is lost; the file keeps loading | S01; every event row below | none on the wire — the name sits in `PZAgent.unavailableEvents` | `requires_live` | |

## The event surface

Registration is guarded: an event this build does not expose is recorded in
`PZAgent.unavailableEvents` and skipped, so a missing event costs one feature
rather than aborting the file and leaving the mod half-loaded. The cost is
*silent* beyond that table, which is why each row below names what the lost
feature is.

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `Events.OnGameStart` | event, fires once the save is loaded | `PZAgent_Main.lua` — agent construction | the agent is never built; the mod is inert | S01 | entry in `PZAgent.unavailableEvents` | `requires_live` | |
| `Events.OnPlayerDeath` | event, `(player)` | `PZAgent_Main.lua` — shutdown: stop with `SESSION_TERMINATED`, tear down the HUD | **safety-relevant.** The agent outlives its character: heartbeats keep flowing and a stale session keeps looking live over a corpse | no playbook scenario kills the character; dying on the test save is the only exercise. Note the gap per the closing paragraph | entry in `PZAgent.unavailableEvents` | `requires_live` | |
| `Events.OnTick` | event, per rendered tick | `PZAgent_Main.lua` — the mod's whole tick: safety, actions, observation | the mod is constructed and then does nothing, silently | S02 | entry in `PZAgent.unavailableEvents` | `requires_live` | |
| `Events.OnKeyPressed` | event, `(keyCode)`; F12 is DirectInput scancode 88 | `PZAgent_Main.lua` — the panic hotkey | the hotkey is dead; panic stop remains reachable through the sidecar's `panic.stop` file only | S18 | entry in `PZAgent.unavailableEvents` | `requires_live` | |
| `Events.OnPlayerMove` | event, `(player)` | `PZAgent_Main.lua` — manual-takeover detection, via `Runtime.noteInput` | **the one place a wrong symbol is dangerous rather than merely broken** — see the note below | S06; `tests/lua/test_safety.lua` | entry in `PZAgent.unavailableEvents` | `requires_live` | |
| `Events.OnPlayerAttackFinished` | event, `(player)` | `PZAgent_Main.lua` — manual-takeover detection for combat input | **safety-relevant.** A player fighting for their life is not noticed as having taken over; automation keeps queueing work over them | S06 | entry in `PZAgent.unavailableEvents` | `requires_live` | |

**Manual-takeover detection is the one place a wrong symbol is dangerous rather
than merely broken.** If `OnPlayerMove` is not the right hook — or
`OnPlayerAttackFinished` is not, for combat — the mod will not notice the
player taking control and will keep acting over them. Verify both in S06 before
running anything autonomous.

## The multiplayer reading — check this right after the boot path

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `isClient` | `() -> boolean`; true on a multiplayer client | `Observe.lua` (`Observe.multiplayer`), the no-multiplayer gate | absent reading is `nil`, refused exactly as `true` is | S02; `tests/lua/test_observe.lua` | none from the mod — the sidecar refuses with `POLICY_DENIED` and "the mod did not report whether this session is multiplayer" | `requires_live` | |
| `isServer` | `() -> boolean`; true on a host | `Observe.lua` (`Observe.multiplayer`), the no-multiplayer gate | same | S02; `tests/lua/test_observe.lua` | same | `requires_live` | |

After the boot path these are next, because their failure mode is the agent
*refusing everything while looking healthy*, rather than one action failing.

The blueprint forbids multiplayer, and `ActionEngine._multiplayer_abort` enforces
it by refusing every mutating command unless the mod positively reported single
player. `Observe.multiplayer` returns `true`, `false`, or `nil` when neither
accessor could be read — and `nil` is refused exactly as `true` is, because an
absent reading is not a negative reading.

So if these two names are wrong on Build 42.20, a perfectly ordinary
single-player session refuses every command with `POLICY_DENIED` and the message
"the mod did not report whether this session is multiplayer". That is the
conservative failure and it is the right one, but it is indistinguishable at a
glance from the agent being broken. **Confirm these in S02 before concluding
anything else is wrong.**

Neither has ever been exercised against a real multiplayer session either. The
gate is written and tested against fakes; nobody has watched it refuse a server.

## Timed actions

These are the calls that change the world. Every one is constructed through
`Toolkit.construct` and queued through `Toolkit.enqueue`, which stamps the
session's ownership tag before `ISTimedActionQueue.add` sees it. The shared
failure signatures of construction are `Toolkit.construct`'s own:
`<class> is not available in this build`, `<class>.new is not available in this
build`, `<class>:new failed: <err>` (an `INTERNAL_ERROR`), and
`<class>:new produced no action` (a `QUEUE_REJECTED`). A `constructFirst` over
a slash list refuses with every candidate joined by ` / `.

Each constructible class is declared in its adapter's `requires` list twice —
the class and its constructor member, because `Toolkit.symbolPresent` on the
dotted form also asks that the member is callable, so a forward declaration
with no constructor does not count as a working API. Those dotted spellings are
part of this inventory and fail to the same row as their class:
`ISWalkToTimedAction.new`, `ISInventoryTransferAction.new`,
`ISEatFoodAction.new`, `ISDrinkFromBottle.new`, `ISTakeWaterAction.new`,
`ISReadABook.new`, `ISApplyBandage.new`, `ISEquipWeaponAction.new`,
`ISWearClothing.new`, `ISUnequipAction.new`.

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `ISWalkToTimedAction` | `:new(character, IsoGridSquare)` | `adapters/Movement.lua` (`move_to_square`); `adapters/Toolkit.lua` (`Toolkit.approach`) as a prerequisite for anything that must reach a place first; `adapters/Containers.lua` (`container.open_nearby`) | command refused before dispatch; capability published `unsupported` | S04, S05; `tests/lua/test_adapters_movement.lua` | default | `requires_live` | |
| `ISInventoryTransferAction` | `:new(character, item, source, destination)` | `adapters/Inventory.lua` (`inventory_transfer`; also `inventory.ensure_main` and `Inventory.bringToMain`, the fetch step of eat, drink, read, bandage and equip) | command refused; every adapter that fetches first fails in prepare | S07 (and S08, S09, S10, S12, S13 via the fetch); `tests/lua/test_adapter_inventory.lua` | default | `requires_live` | |
| `ISEatFoodAction` | `:new(character, item, fraction)`; fraction 0.1..1.0 | `adapters/Consumption.lua` (`eat_percentage`) | command refused | S08; `tests/lua/test_adapter_consumption.lua` | default | `requires_live` | |
| `ISDrinkFromBottle` | `:new(character, item, fraction)` | `adapters/Consumption.lua` (`drink_carried`) | command refused | S09; `tests/lua/test_adapter_consumption.lua` | default | `requires_live` | |
| `ISTakeWaterAction` | **the mod calls `:new(character, waterObject, amount, item)`, amount hardcoded 50** | `adapters/Consumption.lua`, the `consume.drink_source` adapter (`drink_world_source`) | command refused; a build with no water action can still drink from a bottle, because the refill symbols are declared by `consume.drink_source` alone | S09; `tests/lua/test_adapter_consumption.lua` | default | `requires_live` | **Check this first.** Three places in this repository stated three different argument orders; the column now records the call the mod actually makes, and nothing here can confirm it. A build that orders them differently fills the wrong thing rather than erroring, which is why the capability is capped at `experimental` and why the postcondition refuses to accept the vessel's own volume as proof |
| `ISReadABook` | `:new(character, item)` — two arguments; the page budget is the mod's own, enforced by polling `getAlreadyReadPages`, and is never passed to the constructor | `adapters/Literature.lua` (`read_literature`) | command refused | S10; `tests/lua/test_adapter_literature.lua` | default | `requires_live` | |
| `ISApplyBandage` | `:new(doctor, patient, item, bodyPart)`; the mod passes the player twice and a live `BodyPart` object re-read at start | `adapters/Medical.lua` (`medical_bandage`) | command refused | S13; `tests/lua/test_adapter_medical.lua` | default | `requires_live` | |
| `ISEquipWeaponAction` | `:new(character, item, time, primaryHand, twoHands)` | `adapters/Equipment.lua` (`equipment_equip`), the held branch | the held branch is refused; the worn branch is checked separately per item | S12; `tests/lua/test_adapter_equipment.lua` | default | `requires_live` | |
| `ISWearClothing` | `:new(character, item, time)` | `adapters/Equipment.lua` (`equipment_equip`), the worn branch, chosen when the item answers `getBodyLocation`/`getCanBeEquipped` | the worn branch is refused | S12; `tests/lua/test_adapter_equipment.lua` | default | `requires_live` | |
| `ISUnequipAction` | `:new(character, item, time)` | `adapters/Equipment.lua` (`equipment_unequip`) | command refused | S12; `tests/lua/test_adapter_equipment.lua` | default | `requires_live` | |
| `ISSitOnChairAction` / `ISSitOnChair` | `:new(character, chairObject)` | `adapters/Rest.lua` (`survival_rest`), when a seat was named | a named seat that cannot be sat on is a refusal | S14; `tests/lua/test_adapter_rest.lua` | `ISSitOnChairAction / ISSitOnChair is not available in this build` | `requires_live` | |
| `ISSitOnGround` / `ISSitOnGroundAction` | `:new(character)` | `adapters/Rest.lua` (`survival_rest`), when the ground was permitted | rest continues standing; the evidence records `posture = "standing"` and the missing class | S14; `tests/lua/test_adapter_rest.lua` | none — the refusal code is kept in the evidence as `ground_unavailable`, the command itself proceeds | `requires_live` | |
| `ISWorldObjectContextMenu.onSleep` | `(worldObjects, bedObject, player)` — the mod calls `onSleep(nil, bed, player)`, bed being the bed object or the vehicle | `adapters/Sleep.lua` (`survival_sleep`) | command refused; a call that is accepted but does nothing fails on the postcondition instead of being reported as a night's sleep | S14; `tests/lua/test_adapter_sleep.lua` | default; a raising call is `ISWorldObjectContextMenu.onSleep failed: <err>` on a `QUEUE_REJECTED` | `requires_live` | |
| `ISWorldObjectContextMenu.checkCanSleep` | `(player) -> boolean` | `adapters/Sleep.lua` (`Sleep.maySleep`) — asks the game itself whether sleep is permitted before the entry point is touched | a build with no check, or one answering a non-boolean, refuses sleep: "the question cannot be asked" and "sleep is permitted" must not collapse | S14; `tests/lua/test_adapter_sleep.lua` | `ISWorldObjectContextMenu.checkCanSleep is not available in this build` | `requires_live` | |

Two of these carry a note worth reading before the first run.

**Sleep has no plain timed action.** Sleeping in Project Zomboid goes through the
bed's context menu rather than a constructible action, which is why
`adapters/Sleep.lua` reaches for `ISWorldObjectContextMenu.onSleep`. That is the
least certain entry in this table and the most consequential one, because a
sleeping character cannot react. If it turns out to be wrong, the correct fix is
to find the real entry point — not to fake sleep by advancing a stat. The
argument order recorded above is the call in `sleepStart`:
`pcall(onSleep, nil, target.object, ctx.player)` — worldObjects first and nil,
then the bed, then the player. An earlier revision of this row recorded
`(worldObjects, player, bedObject)`, which is not what the code does.

**Rest has two shapes.** Sitting on the ground and sitting on a chair are
different actions in the game's Lua, and the adapter declares both, in the
probe order the rows spell — `ISSitOnGround` before `ISSitOnGroundAction` for
the ground. A build that has only one should degrade to that one, not refuse to
rest.

## The action queue

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `ISTimedActionQueue.add` | `(action)` | `adapters/Toolkit.lua` (`Toolkit.enqueue`) — the *only* mutating call in the whole adapter layer | every mutating command refused | every mutating scenario; S03; `tests/lua/test_action_runtime.lua` | default; a refusing queue is `the action queue refused the action: <err>` on a `QUEUE_REJECTED` | `requires_live` | |
| `ISTimedActionQueue.getTimedActionQueue` | `(character) -> queue object` whose `queue` field is a Lua-visible list of the queued actions | `Safety.lua` (`describeQueue`), to read what is queued and who owns it | queue reported unreadable, capability `unsupported`; progress polling reports `CAPABILITY_UNAVAILABLE`, never "nothing queued" | S03, S06, S18; `tests/lua/test_safety.lua` | `ISTimedActionQueue.getTimedActionQueue is not available in this build` / `reading the action queue failed: <err>` / `the action queue has no readable entry list` | `requires_live` | |
| queue entry fields: `action.Type`, writable `pzAgentTag` / `pzAgentSession` | each queued action is a table that reports its type as a string and accepts the mod's ownership stamp | `Safety.lua` (`describeQueue`), `adapters/Toolkit.lua` (`Toolkit.enqueue` stamps before `add`) | an action that refuses the tag is never enqueued; an entry with no readable tag counts as *foreign*, which blocks a wholesale clear | S18; `tests/lua/test_ownership.lua` | `the timed action refused the ownership tag` on an `INTERNAL_ERROR` | `requires_live` | |
| `ISTimedActionQueue.clear` | `(character)` — clears the whole queue; the game is not known to expose a per-entry cancel | `Safety.lua` (`applyStop`), panic stop, only when *every* queued entry is mod-owned and the read was not truncated | stop still disarms; entries are reported `remaining`, never claimed cleared | S18; `tests/lua/test_safety.lua` | `ISTimedActionQueue.clear is not available in this build` / `clearing the action queue failed: <err>` | `requires_live` | |

The queue reader is load-bearing for safety, not just for progress. Panic stop
cancels only actions carrying this session's ownership tag; if the queue cannot
be read, the honest answer is `CAPABILITY_UNAVAILABLE`, and the mod must not
report "nothing to cancel". Verify this one before trusting any stop path.
An earlier revision of this table named the reader `ISTimedActionQueue.queues`,
a table keyed by character. That is not what `Safety.lua` calls: the code asks
`ISTimedActionQueue.getTimedActionQueue(player)` and then reads the returned
object's `queue` list, and the failure strings above are the ones it emits.

## World and squares

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `getCell` | `() -> IsoCell` | `adapters/Toolkit.lua` (`Toolkit.gridSquare`), `Observe.lua` (nearby scans), `adapters/World.lua` | square resolution refused; the nearby scan silently returns empty — see the zombie note | S04; `tests/lua/test_adapters_world.lua` | default | `requires_live` | |
| `IsoCell.getGridSquare` | `(x, y, z) -> IsoGridSquare or nil` | `Toolkit.gridSquare`, every adapter that resolves a square; `Observe.scanSquare` | a nil square is `TARGET_NOT_LOADED` ("… is not loaded"), a missing method is a capability gap | S04; `tests/lua/test_adapters_movement.lua` | default; an unloaded square is `TARGET_NOT_LOADED`, not a symbol failure | `requires_live` | |
| `IsoCell.getZombieList` | `() -> a zero-based list of IsoZombie` | `Observe.lua` (`nearbyZombies`) | **the zombie section comes back empty, the danger floor never rises, and the reflex guard is blind.** No string names the gap | S15; `tests/lua/test_observe.lua` | none — the observation's `zombies` list is simply always empty | `requires_live` | |
| `IsoZombie.getTarget` | `() -> IsoMovingObject or nil` | `Observe.lua` — `chasing` is the target being the player, not the distance | `chasing` absent (never false-by-default) | S15; `tests/lua/test_observe.lua` | none — field absent | `requires_live` | |
| `IsoZombie.getOnlineID` / `getID` | `() -> number >= 0`; `getOnlineID` answers -1 outside multiplayer and is skipped for it | `Observe.lua` — zombie identity | `runtime_id` absent | S15 | none — field absent | `requires_live` | |
| `IsoPlayer.CanSee` | `(zombie) -> boolean` | `Observe.lua` — the `visible` reading | `visible` absent | S15 | none — field absent | `requires_live` | |
| `IsoGridSquare.getObjects` | `() -> a zero-based list` | `adapters/World.lua`, `adapters/Toolkit.lua` (world containers), `adapters/Sleep.lua` (`bedOn`), `adapters/Rest.lua` (`seatOn`), `adapters/Consumption.lua` (`waterSourceOn`), `Observe.lua` | adapters refuse naming the symbol; `world.inspect` records `objects_readable false`; the nearby scan skips the square | S09, S11, S14; `tests/lua/test_adapters_world.lua` | `IsoGridSquare.getObjects is not available in this build`; an unreadable list is `IsoGridSquare.getObjects().size …` | `requires_live` | |
| `IsoGridSquare.getFloor` | `() -> IsoObject` | `adapters/World.lua` (`floorOf`) | `floor` field absent | no scenario issues `world.inspect` — a gap by the closing paragraph's rule; `tests/lua/test_adapters_world.lua` | none — field absent | `requires_live` | |
| `IsoGridSquare.getLightLevel` | `(playerIndex) -> 0..1` | `adapters/World.lua` (`lightOf`) | `light` field absent — "it is dark" and "nobody measured" stay distinguishable | same as `getFloor` | none — field absent | `requires_live` | |
| `AdjacentFreeTileFinder.Find` | `(square, character) -> IsoGridSquare` | `adapters/Movement.lua`, `adapters/Toolkit.lua` (`Toolkit.approach`) — choosing where to stand next to a thing | the target square itself is walked onto, and the evidence says so | S04; `tests/lua/test_adapters_movement.lua` | none — silent, declared in evidence | `requires_live` | |
| `instanceof` | `(object, "IsoDoor" or "IsoWindow") -> boolean` — the engine's own class test | `adapters/World.lua` (`classify`) | classification falls back to accessor shape (`isLocked`+`IsOpen` = door, `isSmashed` = window, `getContainer` = container), wrong in the safe direction | `tests/lua/test_adapters_world.lua` | none — silent fallback | `requires_live` | |
| `IsoDoor` / `IsoWindow` | class names handed to `instanceof`, never indexed as globals | `adapters/World.lua` (`classify`) | as `instanceof` above | `tests/lua/test_adapters_world.lua` | none | `requires_live` | |
| `IsOpen` / `isOpen`, `isLocked` / `isLockedByKey`, `isBarricaded`, `isSmashed` | boolean readers on doors and windows | `adapters/World.lua` (`openingState`) — read for every object, so a build that spells the class differently still reports what it does expose | each absent reader leaves its field out | `tests/lua/test_adapters_world.lua` | none — fields absent | `requires_live` | |
| `IsoObject.getSprite`, `IsoSprite.getName` | the sprite name, which is what identifies a tile to a human | `adapters/World.lua` (`spriteName`), `adapters/Sleep.lua`, `adapters/Rest.lua` — the name-match fallback for beds and seats | objects are unnamed; beds and seats are found only by their boolean probes | S11, S14 | none — fields absent | `requires_live` | |
| `IsoObject.getObjectName` / `getName` | `() -> string` | `adapters/World.lua`, `Observe.lua` — object naming | `name`/`kind` absent; an object with no readable kind is dropped from the nearby list | S11; `tests/lua/test_observe.lua` | none — field absent | `requires_live` | |
| `isBed` | `() -> boolean` | `adapters/Sleep.lua` (`bedOn`) | falls back to a sprite name containing "bed"; nothing found is `PRECONDITION_FAILED` "there is nothing to sleep on at that square" | S14; `tests/lua/test_adapter_sleep.lua` | none from the probe itself | `requires_live` | |
| `isChair` / `isSofa` | boolean readers | `adapters/Rest.lua` (`seatOn`) | falls back to a sprite name containing "chair"/"sofa"; nothing found is `PRECONDITION_FAILED` "there is nothing to sit on at that square" | S14; `tests/lua/test_adapter_rest.lua` | none from the probe itself | `requires_live` | |
| `getWaterAmount` | `() -> number`, positive on a usable source | `adapters/Consumption.lua` (`waterSourceOn`), `Observe.lua` (water-source semantics) | an object without a positive reading is not a source this adapter will drink from | S09; `tests/lua/test_adapter_consumption.lua` | none — `NO_SAFE_DRINK` "nothing on that square reports any water" when no object answers | `requires_live` | |
| `isTaintedWater` / `isTainted` (world source) | boolean readers on the source object | `adapters/Consumption.lua` (`refillSource`) | **the refusal only fires on a positive `true`. An absent accessor passes this gate** — despite the adapter header saying the source must *report* untainted water — so a live run must confirm the accessor exists before trusting the gate | S09 | `NO_SAFE_DRINK` "the source reports tainted water" on a positive reading; nothing on an absent one | `requires_live` | |

`IsoGridSquare.getObjects` returns a Java list as Kahlua sees it — zero-based,
with `size()` and `get(i)`. `Toolkit.listSize` and `Toolkit.listGet` wrap that
so an adapter never indexes it directly. If the collection shape differs, fix it
in those two functions and every adapter follows. The same shape assumption
covers every other engine list in this file — worn items, body parts, traits,
zombies, container items — and has its own row under containers.

## Doors

The door actions have no constructible timed action to ride: ISOpenCloseDoor
does not exist as a class in Build 42, so `door.open`, `door.close` and
`door.unlock` all walk into reach with `ISWalkToTimedAction` (declared in their
`requires`, rows above) and then drive the game's own interaction —
`IsoDoor:ToggleDoor(character)`, the call the E key lands on. The toggle is a
method on a Java object, not a global, so it cannot be probed by the capability
runtime; it is checked at the resolved door by `Toolkit.method` and its absence
is a `CAPABILITY_UNAVAILABLE` spelled `IsoDoor.ToggleDoor` whichever member
spelling was looked for. Every state reader below is also read by `Observe.lua`
for the nearby block's door fields, where an absent reader leaves the field
*absent* — the sidecar reads a missing `locked` as "could not be read", and a
defaulted `false` would read as permission.

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `IsoDoor.ToggleDoor` / `toggleDoor` | `(character)` — the E-key interaction; answers nothing useful, so the postcondition is always a re-read | `adapters/Doors.lua` (all three door actions, capability `door_toggle`) | command refused | no playbook scenario issues a door command yet — a gap by the closing paragraph's rule; `tests/lua/test_adapter_doors.lua` | `IsoDoor.ToggleDoor is not available in this build` | `requires_live` | The 2026-08-08 run opened a door, but via the system-input fallback, not this call |
| `IsoDoor.IsOpen` / `isOpen` | `() -> boolean` | `Observe.lua` (the nearby door's `open` field), `adapters/Doors.lua` — the precondition and the *whole postcondition* of open and close | observation field absent; the door commands refuse up front rather than toggling blind | `tests/lua/test_observe.lua`, `tests/lua/test_adapter_doors.lua` | `IsoDoor.IsOpen is not available in this build` from the commands; none from observation — field absent | `requires_live` | |
| `IsoDoor.isLockedByKey` / `isLocked` | `() -> boolean` | `Observe.lua` (`locked`), `adapters/Doors.lua` — the `DOOR_LOCKED` gate of open and the pre/postcondition of unlock | observation field absent; open proceeds (an unreadable lock surfaces as the toggle bouncing, then `POSTCONDITION_FAILED`); unlock refuses | `tests/lua/test_observe.lua`, `tests/lua/test_adapter_doors.lua` | unlock's refusal is `PRECONDITION_FAILED` "the lock state could not be read on this build" | `requires_live` | |
| `IsoDoor.isBarricaded` | `() -> boolean` | `Observe.lua` (`barricaded`), `adapters/Doors.lua` — the `DOOR_BARRICADED` gate of all three commands | observation field absent; the gate fires only on a positive `true`, so an absent reader passes it and the barricade surfaces as a swallowed toggle | `tests/lua/test_observe.lua`, `tests/lua/test_adapter_doors.lua` | none — field absent | `requires_live` | |
| `IsoDoor.getNorth` | `() -> boolean`; true is the north wall of the square, false the west — the classic IsoDoor convention | `Observe.lua` — the nearby door's `orientation` token | field absent | `tests/lua/test_observe.lua` | none — field absent | `requires_live` | |
| `ItemContainer.haveThisKeyForDoor` | `(door) -> boolean` — the game's own key check, the one the vanilla context menu asks | `adapters/Doors.lua` (`Doors.haveKey`), probed first | falls through to the key-id scan below | `tests/lua/test_adapter_doors.lua` | with the whole chain gone: `ItemContainer.haveThisKeyForDoor / IsoDoor.getKeyId is not available in this build` | `requires_live` | |
| `IsoDoor.getKeyId` | `() -> number` | `adapters/Doors.lua` (`Doors.haveKey`) — the fallback key check matches it against carried keys | with neither this nor `haveThisKeyForDoor`, unlock refuses naming both: "the key cannot be looked for" and "there is no key" are opposite facts | `tests/lua/test_adapter_doors.lua` | as the row above | `requires_live` | |
| `InventoryItem.getKeyId` | `() -> number` on a key item | `adapters/Doors.lua` (`Doors.haveKey`) — the carried side of the key-id match, one nesting level deep because keys live on key rings | an item with no reading never matches, so a build hiding it reports `DOOR_LOCKED` with the key in the bag — conservative, and the row to check when that happens | `tests/lua/test_adapter_doors.lua` | none — the item simply never matches | `requires_live` | |

**Unlock rides the toggle, and that is the least certain assumption here.** With
the key carried, vanilla unlocks a locked door as part of the interaction —
that is what `door.unlock` counts on when it calls `ToggleDoor` and then
re-reads `isLockedByKey`. If Build 42 does not unlock on interaction, the
command fails honestly on its postcondition ("the door still reads locked"),
never by writing the lock: `setLockedByKey(false)` is state-writing and stays
forbidden. If a live run shows the game exposes a proper unlock entry point
callable from Lua, that entry point should replace the toggle here.

## The game clock and the save's identity

All observation-only. A missing accessor here costs a field of the observation
document, never a command — but the fields it costs are ones the sidecar keys
real decisions on (`save_key` pins the session to one save; `paused` and
`speed` gate the planner's sense of time).

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `getWorld` | `() -> IsoWorld` | `Observe.lua` (`saveKey`) | `save_key` absent from the observation | S02; `tests/lua/test_observe.lua` | none — field absent | `requires_live` | |
| `IsoWorld.getWorld` / `getMap` / `getSaveFolderName` | string readers; every one that answers joins the save key, which is hashed before it is ever emitted (§3.13) | `Observe.lua` (`saveKey`) | with none answering, `save_key` is absent | S02; `tests/lua/test_observe.lua` | none — field absent | `requires_live` | |
| `getGameTime` | `() -> GameTime` | `Observe.lua` (`gameFields`) | `speed`, `world_time` and `paused` all absent | S02; `tests/lua/test_observe.lua` | none — fields absent | `requires_live` | |
| `GameTime.getTrueMultiplier` / `getMultiplier` | `() -> number`; 0 when paused | `Observe.lua` — the `speed` reading, and the pause fallback | `speed` absent | S02 | none — field absent | `requires_live` | |
| `GameTime.getYear` / `getMonth` / `getDay` / `getHour` / `getMinutes` | calendar readers | `Observe.lua` — `world_time` | `world_time` incomplete or absent | S02 | none — field absent | `requires_live` | |
| `GameTime.isPaused` | `() -> boolean` | `Observe.lua` | falls back to `speed <= 0`, the game's own definition of paused; with neither, `paused` is absent | S02 | none — field absent | `requires_live` | |

## Character state readers

These decide whether an action *succeeded*. A wrong name here does not break the
action — it breaks the proof, and the result comes back `failed` for something
that worked. That failure mode is why they are listed separately.

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `IsoPlayer.getStats` | `() -> Stats` | `adapters/Toolkit.lua` (`Toolkit.observe`), `Observe.lua` — the object the six stat readers hang on | the whole `stats` block absent | S02; `tests/lua/test_observe.lua` | none — block absent | `requires_live` | |
| `Stats.getHunger` | `() -> number, 0..1` | postcondition for `consume.eat` | verify falls through to the item-shrank observation; with neither, `POSTCONDITION_FAILED` | S08; `tests/lua/test_adapter_consumption.lua` | "this build reports no hunger, and the item is unchanged, so nothing was observed to happen" | `requires_live` | |
| `Stats.getThirst` | `() -> number, 0..1` | postcondition for `consume.drink` and the *only* postcondition for `consume.drink_source` | drink falls through to the container-drained observation; drink_source has no fallback at all — "a refilled container proves nothing about drinking" | S09; `tests/lua/test_adapter_consumption.lua` | "this build reports no thirst, and the container is unchanged, so nothing was observed to happen" | `requires_live` | |
| `Stats.getFatigue` | `() -> number, 0..1` | postcondition for `survival.sleep`; also refused up front in validate when unreadable | sleep refuses to start — a night that could not be measured must not be reported as slept | S14; `tests/lua/test_adapter_sleep.lua` | `PlayerStats.getFatigue is not available in this build` — the mod's refusal spells it *PlayerStats*, not `Stats`, and that is the string to grep for | `requires_live` | |
| `Stats.getEndurance` | `() -> number, 0..1` | postcondition for `survival.rest`; refused up front in validate when unreadable | rest refuses to start | S14; `tests/lua/test_adapter_rest.lua` | `PlayerStats.getEndurance is not available in this build` — same *PlayerStats* spelling | `requires_live` | |
| `Stats.getStress` / `getPanic` | `() -> number` | observation only | fields absent | S02, S15 | none — fields absent | `requires_live` | |
| `IsoPlayer.getBodyDamage` | `() -> BodyDamage` | `adapters/Toolkit.lua` (`snapshotBody`), `adapters/Medical.lua` (`bodyPartAt`), `Observe.lua` (wounds, overall health) | body block absent; bandage refuses | S13; `tests/lua/test_adapter_medical.lua` | `IsoPlayer.getBodyDamage is not available in this build` | `requires_live` | |
| `BodyDamage.getBodyParts` | `() -> a zero-based list of BodyPart` | `adapters/Toolkit.lua`, `adapters/Medical.lua`, `Observe.lua` — the only body-part accessor the mod calls; parts are addressed by list index, re-read live at start | bandage refuses; wounds absent from observation | S13; `tests/lua/test_adapter_medical.lua` | `BodyDamage.getBodyParts is not available in this build`; a list that will not answer an index is `BodyDamage.getBodyParts().get …` | `requires_live` | An earlier revision of this row named `BodyDamage.getBodyPart(BodyPartType)`. The mod never calls that: it walks `getBodyParts()` and keeps the index |
| `BodyDamage.getOverallBodyHealth` | `() -> 0..100` | `Observe.lua` — the `health` stat | field absent | S02 | none — field absent | `requires_live` | |
| `BodyPart.getType` | `() -> BodyPartType`; named via `name` / `toString` / `getName` on the enum | part naming in `adapters/Toolkit.lua` and `Observe.lua`; the names key `Medical.BODY_PARTS` | an unnameable part is skipped (snapshot) or named `part-<index>` (observation) | S13; `tests/lua/test_observe.lua` | none | `requires_live` | |
| `BodyPart.getHealth` | `() -> 0..100`; severity is `(100 - health) / 100` | severity for triage ordering | `severity` absent; worst-bleeding tie-breaks on the name alone | S13 | none — field absent | `requires_live` | |
| `BodyPart.bleeding` / `isBleeding` | boolean readers | the *precondition and postcondition* of `medical.bandage` | with no reading, nothing reports bleeding and the adapter refuses: "nothing on this character is bleeding" | S13; `tests/lua/test_adapter_medical.lua` | postcondition failure is "`<part>` is still bleeding and still reports no dressing" | `requires_live` | |
| `BodyPart.bandaged` / `isBandaged` | boolean readers | the other half of the bandage postcondition | as above — either `bleeding false` or `bandaged true` proves the dressing | S13 | as above | `requires_live` | |
| `BodyPart.bitten`/`isBitten`, `scratched`/`isScratched`, `isCut`/`haveGlass`, `isDeepWounded`/`deepWounded` | boolean wound readers | `Observe.lua` (`bodyPartFields`), `adapters/Toolkit.lua` (deep wounds) | flags read false-when-absent in the wound list — a build hiding them under other names underreports injuries | S02, S13 | none | `requires_live` | |
| `BodyPart.getFractureTime` + `isFractured`, `getBurnTime` + `isBurnt` | number-then-boolean probes | `Observe.lua` (`bodyPartFields`) | as above | S02 | none | `requires_live` | |
| `IsoPlayer.getMoodles`, `Moodles.getNumMoodles` / `getMoodleLevel(i)` / `getMoodleType(i)` | the moodle holder and its indexed readers | `Observe.lua` (`playerMoodles`) | `moodles` empty; a moodle whose type name cannot be read is skipped, never invented | S02; `tests/lua/test_observe.lua` | none — block empty | `requires_live` | |
| `IsoPlayer.isReading` / `isReadingaBook` | `() -> boolean` | progress and postcondition gate for `literature.read` | verify refuses — without the reading there is no way to attribute a page counter's movement to this command | S10; `tests/lua/test_adapter_literature.lua` | `IsoPlayer.isReading is not available in this build` | `requires_live` | |
| `IsoPlayer.isAsleep` | `() -> boolean` | progress and half the postcondition of `survival.sleep`; refused up front in validate when unreadable | sleep refuses to start | S14; `tests/lua/test_adapter_sleep.lua` | `IsoPlayer.isAsleep is not available in this build` | `requires_live` | |
| `IsoPlayer.isSitOnGround` / `isSitting` | `() -> boolean` | posture evidence for `survival.rest` | `sitting` absent; the evidence still records what was queued | S14 | none — field absent | `requires_live` | |
| `IsoPlayer.isDead` | `() -> boolean` | `Runtime.lua` (`isAlive`), `Observe.lua` — liveness | **unknown reads as dead**, which suspends mutating work rather than letting the agent act on a character it cannot confirm is alive | S02; `tests/lua/test_runtime.lua` | none — `player_alive false` in the heartbeat | `requires_live` | |
| `IsoPlayer.getX` / `getY` / `getZ` | `() -> number` | postcondition for both movement actions; the centre of every nearby scan; zombie positions read the same way | "the character reports no position" — a move never starts; the nearby scan has no centre | S04; `tests/lua/test_adapters_movement.lua` | `PRECONDITION_FAILED` "the character reports no position" | `requires_live` | |
| `IsoPlayer.getDirectionAngle` / `getForwardDirection` | `() -> number`, degrees | `Observe.lua` — facing | `direction` absent | S02 | none — field absent | `requires_live` | |
| `IsoPlayer.HasTrait` | `("Illiterate") -> boolean` | `adapters/Literature.lua` (`isIlliterate`) — policy input | falls through to `getTraits` below; with the whole chain gone, `literature.read` refuses — "the trait check is unavailable" and "the character is literate" are opposite answers | S10; `tests/lua/test_adapter_literature.lua` | `IsoPlayer.HasTrait is not available in this build` | `requires_live` | |
| `IsoPlayer.getTraits` + `contains("Illiterate")`, else a list scan | the fallback chain for the trait check | `adapters/Literature.lua` (`isIlliterate`) | see above | S10 | as above — the refusal names `IsoPlayer.HasTrait` whichever link broke | `requires_live` | |

Hunger and thirst are reported on a 0..1 scale where **higher is worse**. The
postconditions check that the value *fell*. If a build reports them inverted or
on 0..100, every consume scenario will fail with the food visibly eaten — that
is the signature of this row being wrong, not of the eat action failing.

## Containers and inventory

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `IsoPlayer.getInventory` | `() -> ItemContainer` | `adapters/Toolkit.lua` (`playerInventory`, `Toolkit.observe`), `Observe.lua` (`inventoryRoots`) | every item resolution refuses; the observation's inventory section is absent with the reason in `last_error` | S07; `tests/lua/test_adapter_inventory.lua` | `IsoPlayer.getInventory is not available in this build`; the observation records "the character exposes no main inventory" | `requires_live` | |
| `ItemContainer.getItems` | `() -> a zero-based list` | `Toolkit.containerItems` — every container walk, count and item resolution; the transfer postcondition counts through it twice | resolution and verification refuse; an unreadable container is never reported empty | S07, S11; `tests/lua/test_adapter_inventory.lua` | `ItemContainer.getItems is not available in this build`; an unanswering list is `ItemContainer.getItems().size …` | `requires_live` | |
| Java list shape: `size()` / `get(i)`, zero-based | every engine list — items, worn items, body parts, traits, zombies, square objects | `Toolkit.listSize` / `Toolkit.listGet`, `Observe.lua`'s twins | fix it in those two functions and every adapter follows | every scenario touching a list | the wrapper names the list: `ItemContainer.getItems().size`, `IsoGridSquare.getObjects().size`, `WornItems.size`, `BodyDamage.getBodyParts().get` | `requires_live` | |
| `ItemContainer.getCapacity` / `getMaxWeight` | `() -> number` | `container.inspect`, `world.inspect`, observation | `capacity` absent | S11; `tests/lua/test_adapter_containers.lua` | none — field absent | `requires_live` | |
| `ItemContainer.getContentsWeight` / `getCapacityWeight` | `() -> number` | `container.inspect`, observation | `used_capacity` absent | S11 | none — field absent | `requires_live` | |
| `ItemContainer.getType` / `getContainerType` | `() -> string` | `adapters/World.lua`, `Observe.lua` — what kind of container this is | `container_type`/`kind` absent | S11 | none — field absent | `requires_live` | |
| `InventoryItem.getID` | `() -> number` | reference minting everywhere — see the note below | an item with no non-negative id is invisible to references | S07; `tests/lua/test_refs.lua` | none — the item simply cannot be named | `requires_live` | |
| `InventoryItem.getFullType` | `() -> string` | item identity; the deterministic dressing and food choices key on it | `full_type` absent; deterministic choices cannot match the item | S07, S13 | none — field absent | `requires_live` | |
| `InventoryItem.getInventory` | `() -> ItemContainer or nil` — a bag's own contents | nested-bag walks in `adapters/Toolkit.lua` and `Observe.lua`; `carried:` and `worn:` container tails | nested contents invisible; a carried-bag reference refuses "the carried item holds no container" | S07; `tests/lua/test_adapter_inventory.lua` | `INVALID_REF` strings, not a symbol failure | `requires_live` | |
| `IsoPlayer.getWornItems` | `() -> a worn-items list` | `Toolkit.findWorn`, `Toolkit.observe`, `Observe.inventoryRoots`, `equipment.*` | worn resolution refuses; worn bags invisible to observation | S12; `tests/lua/test_adapter_equipment.lua` | `IsoPlayer.getWornItems is not available in this build` / `WornItems.size …` | `requires_live` | |
| worn entry `getItem`, `getLocation` / `getBodyLocation` | each entry answers its item and its slot name | `Toolkit.findWorn`, `Toolkit.observe`, `Observe.inventoryRoots` | an entry with no `getItem` is treated as the item itself; a bag whose slot has no name cannot be referenced and is counted dropped, never silently omitted | S12 | none | `requires_live` | |
| `IsoPlayer.getPrimaryHandItem` / `getSecondaryHandItem` | `() -> InventoryItem or nil` | `equipment.*` postconditions, hand markers in every snapshot | equip verification cannot see the hand and fails honestly; unequip-by-hand refuses | S12; `tests/lua/test_adapter_equipment.lua` | `IsoPlayer.getPrimaryHandItem is not available in this build` (or `…getSecondaryHandItem…`), from the unequip path; elsewhere fields absent | `requires_live` | |
| `IsoPlayer.getVehicle` | `() -> BaseVehicle or nil` | vehicle containers in `adapters/Toolkit.lua`; the vehicle-seat branch of `survival.sleep` | vehicle references refuse; sleeping in a seat refuses | S14 | `IsoPlayer.getVehicle is not available in this build` | `requires_live` | |
| `BaseVehicle.getPartById` | `(partId) -> part or nil` | vehicle container resolution | vehicle references refuse | none in the playbook — vehicle containers are unexercised; note the gap | `BaseVehicle.getPartById is not available in this build` | `requires_live` | |
| `BaseVehicle.getId` / `getID` | `() -> number` | checking the reference names the vehicle the character is in | with no identity the check is skipped — the reference is trusted one notch further than intended | same as `getPartById` | none | `requires_live` | |
| vehicle part `getItemContainer` | `() -> ItemContainer or nil` | vehicle container resolution | refused as a bad reference | same as `getPartById` | `INVALID_REF` "that vehicle part holds no container" — not a symbol failure | `requires_live` | |
| `IsoObject.getContainer` | `() -> ItemContainer or nil` | world containers in `adapters/Toolkit.lua`, `adapters/World.lua`, `Observe.lua` | an object with none is not a container; a world reference to it refuses | S11; `tests/lua/test_adapter_containers.lua` | `INVALID_REF` "the object at that index holds no container" | `requires_live` | |

**`getID` deserves attention before the first run.** References are minted from
it, and the mod refuses a negative id on purpose: Java answers `-1` for "no id
here", and `-1` is a legal reference segment — two objects both answering `-1`
would produce one reference standing for both, which the action engine would
happily act on. If ids in this build behave differently, that refusal is the
thing to re-examine first.

## What an item answers

Item readers, split from the container table because their failure mode is
different: nothing refuses, the field is absent, and the consequence lands in
whichever policy or postcondition needed it. All observation fields ride the
S02 observation document; the per-action scenarios named below are where an
absent reading changes an outcome.

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `getName` / `getDisplayName` | `() -> string` | display names everywhere; the search filter `name_contains` | `name` absent; name filters cannot match | S02, S07 | none — field absent | `requires_live` | |
| `getDisplayCategory` / `getCategory` | `() -> string` | observation | `category` absent | S02 | none — field absent | `requires_live` | |
| `getUnequippedWeight` / `getActualWeight` / `getWeight` | `() -> number`, probed in that order | observation, container listings | `weight` absent | S02, S11 | none — field absent | `requires_live` | |
| `getHungerChange` | `() -> number`, negative for food | the *is this food* gate of `consume.eat`; the item-shrank fallback; the `edible` search filter | an item with no reading is refused as food | S08; `tests/lua/test_adapter_consumption.lua` | `PRECONDITION_FAILED` "the item reports no hunger effect, so it is not food" | `requires_live` | |
| `getThirstChange` | `() -> number` | the drink gate, the `drinkable` filter, the item fallback | see the drink gate row below | S09 | part of "the item reports neither a thirst effect nor anything to hold liquid" | `requires_live` | |
| `getBaseHunger` | `() -> number` | observation (portion arithmetic on the sidecar) | field absent | S02 | none — field absent | `requires_live` | |
| `getCalories` | `() -> number` | observation | field absent | S02 | none — field absent | `requires_live` | |
| `getDrainableUsesInt` / `getCurrentUses` | `() -> number` | drink gate and postcondition fallback (`uses` fell); the `min_uses` filter | `uses` absent | S09 | none — field absent | `requires_live` | |
| `isCooked`, `isRotten`, `isBurnt` | boolean readers | the spoiled-food refusal in `consume.eat`; observation | **the refusal fires only on a positive `true`; an absent reader passes the gate** | S08; `tests/lua/test_adapter_consumption.lua` | `NO_SAFE_FOOD` "the item is rotten" / "the item is burnt" | `requires_live` | |
| `isPoison` / `isTaintedWater` (on the item) | boolean readers | the poison refusals in eat and drink | same one-sided gate as above | S08, S09 | `NO_SAFE_FOOD` "the item is poisonous" / `NO_SAFE_DRINK` "the container holds tainted water" | `requires_live` | |
| `getFluidContainer`, then `getAmount` / `getCapacity` / `isTainted` on it | the Build 42 fluid API | drink gate, empty-vessel check, taint check, observation | a vessel with no readable fluid falls back to `getThirstChange`/uses; the taint check is one-sided like the rest | S09; `tests/lua/test_adapter_consumption.lua` | `NO_SAFE_DRINK` "the fluid in the container is tainted" / "the container is empty and no source was named" | `requires_live` | |
| `getNumberOfPages` | `() -> number` | the *is this literature* gate of `literature.read`; the `readable` filter | refused as literature | S10; `tests/lua/test_adapter_literature.lua` | `PRECONDITION_FAILED` "the item reports no page count, so it is not literature" | `requires_live` | |
| `getAlreadyReadPages` | `() -> number` | the page-budget progress and the postcondition of `literature.read` | with no counter the postcondition cannot pass | S10 | `POSTCONDITION_FAILED` "this build reports no page counter for the item" | `requires_live` | |
| `getSkillTrained` / `getLvlSkillTrained` / `getMaxLevelTrained` | literature metadata | observation (literature policy on the sidecar) | fields absent | S02, S10 | none — fields absent | `requires_live` | |
| `isEquipped`, `isFavorite` | boolean readers | observation; the `exclude_equipped` filter | read false-when-absent in descriptors | S02 | none | `requires_live` | |
| `isTwoHandWeapon` | `() -> boolean` | the `twoHands` argument of `ISEquipWeaponAction` | absent reads as one-handed unless the caller asked for both | S12 | none | `requires_live` | |
| `getBodyLocation` / `getCanBeEquipped` | `() -> string`, non-empty for a garment | `Equipment.wornKind` — decides worn branch vs held branch | an unreadable location routes the item to the *weapon* branch; a garment equipped that way fails its postcondition rather than lying | S12; `tests/lua/test_adapter_equipment.lua` | none from the probe; the wrong branch surfaces as `POSTCONDITION_FAILED` | `requires_live` | |

## The HUD

| Symbol | Assumed signature | Used by | Fallback | Test | Failure signature | Status | Actual |
|---|---|---|---|---|---|---|---|
| `ISPanel`, `ISPanel.derive` | the UI base class the panel derives from; `derive` read as a field and required callable | `Hud.lua` (`Hud.isSupported`, `Hud.create`) | the HUD stays off and the rest of the mod carries on; the reason lands in `last_error` | S03 (the armed/disarmed indicator); `tests/lua/test_runtime.lua` | `ISPanel is not available in this build; the HUD stays off` | `requires_live` | |
| panel `:new(x, y, w, h)`, `:initialise()`, `:setAlwaysOnTop(true)`, `:addToUIManager()`, `:removeFromUIManager()`, `:drawText(...)` | the derived panel's lifecycle and render surface | `Hud.lua` (`Hud.create`, `Hud.destroy`, the render callback) | creation failure keeps the mod running HUD-less; a failed removal is reported, because a stale panel is a visible lie about the mod's state | S03 | `creating the HUD panel failed: <err>` / `removing the HUD panel failed: <err>` | `requires_live` | |

## What to do with this file

Fill in the `Status` and `Actual` columns as you go. A row that stays
`requires_live` after a full run means the scenario exercising it did not
actually exercise it — which is itself a finding worth recording, because a
capability nothing proved is not a capability the release can claim. Three rows
already record that finding from the desk: nothing in the playbook kills the
character (`Events.OnPlayerDeath`), nothing issues `world.inspect`
(`IsoGridSquare.getFloor`, `getLightLevel`), and nothing opens a vehicle
container (`BaseVehicle.getPartById` and its two companions).
