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
*shortcut*, not an inventory: the table below marks 50 symbol rows `requires_live`
(several rows carry two or three slash-separated names),
so the grep covers roughly an eighth of what is unconfirmed. **This document is
the list.** Use the grep to jump to a comment; use the table to know what has
not been checked.

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

## Timed actions

These are the calls that change the world. Every one is constructed through
`Toolkit.construct` and queued through `Toolkit.enqueue`, which stamps the
session's ownership tag before `ISTimedActionQueue.add` sees it.

| Symbol | Assumed signature | Used by | Capability | Status | Actual |
|---|---|---|---|---|---|
| `ISWalkToTimedAction` | `:new(character, IsoGridSquare)` | `adapters/Movement.lua`, and as a prerequisite by anything that must reach a container first | `move_to_square` | `requires_live` | |
| `ISInventoryTransferAction` | `:new(character, item, source, destination)` | `adapters/Inventory.lua` | `inventory_transfer` | `requires_live` | |
| `ISEatFoodAction` | `:new(character, item, percentage)` | `adapters/Consumption.lua` | `eat_percentage` | `requires_live` | |
| `ISDrinkFromBottle` | `:new(character, item, percentage)` | `adapters/Consumption.lua` | `drink_carried` | `requires_live` | |
| `ISTakeWaterAction` | **the mod calls `:new(character, waterObject, amount, item)`** | `adapters/Consumption.lua`, the `consume.drink_source` adapter | `drink_world_source` | `requires_live` | **Check this first.** Three places in this repository stated three different argument orders; the column now records the call the mod actually makes, and nothing here can confirm it. A build that orders them differently fills the wrong thing rather than erroring, which is why the capability is capped at `experimental` and why the postcondition refuses to accept the vessel's own volume as proof |
| `ISReadABook` | `:new(character, item, pageCount)` | `adapters/Literature.lua` | `read_literature` | `requires_live` | |
| `ISApplyBandage` | `:new(character, patient, item, bodyPart)` | `adapters/Medical.lua` | `medical_bandage` | `requires_live` | |
| `ISEquipWeaponAction` | `:new(character, item, time, primaryHand, twoHands)` | `adapters/Equipment.lua` | `equipment_equip` | `requires_live` | |
| `ISWearClothing` | `:new(character, item, time)` | `adapters/Equipment.lua`, for a clothing slot rather than a hand | `equipment_equip` | `requires_live` | |
| `ISUnequipAction` | `:new(character, item, time)` | `adapters/Equipment.lua` | `equipment_unequip` | `requires_live` | |
| `ISSitOnGroundAction` / `ISSitOnGround` | `:new(character)` | `adapters/Rest.lua` | `survival_rest` | `requires_live` | |
| `ISSitOnChairAction` / `ISSitOnChair` | `:new(character, chairObject)` | `adapters/Rest.lua` | `survival_rest` | `requires_live` | |
| `ISWorldObjectContextMenu.onSleep` | `(worldObjects, player, bedObject)` | `adapters/Sleep.lua` | `survival_sleep` | `requires_live` | |

Two of these carry a note worth reading before the first run.

**Sleep has no plain timed action.** Sleeping in Project Zomboid goes through the
bed's context menu rather than a constructible action, which is why
`adapters/Sleep.lua` reaches for `ISWorldObjectContextMenu.onSleep`. That is the
least certain entry in this table and the most consequential one, because a
sleeping character cannot react. If it turns out to be wrong, the correct fix is
to find the real entry point — not to fake sleep by advancing a stat.

**Rest has two shapes.** Sitting on the ground and sitting on a chair are
different actions in the game's Lua, and the adapter declares both. A build that
has only one should degrade to that one, not refuse to rest.

## The action queue

| Symbol | Assumed signature | Used by | Status | Actual |
|---|---|---|---|---|
| `ISTimedActionQueue.add` | `(action)` | `adapters/Toolkit.lua` — the *only* mutating call in the whole adapter layer | `requires_live` | |
| `ISTimedActionQueue.queues` | a table keyed by character, each holding a list of queued actions | `Safety.lua`, to read what is queued and who owns it | `requires_live` | |
| `ISTimedActionQueue.clear` | `(character)` | `Safety.lua`, panic stop | `requires_live` | |

The queue reader is load-bearing for safety, not just for progress. Panic stop
cancels only actions carrying this session's ownership tag; if the queue cannot
be read, the honest answer is `CAPABILITY_UNAVAILABLE`, and the mod must not
report "nothing to cancel". Verify this one before trusting any stop path.

## The multiplayer reading — check this before anything else

| Symbol | Assumed signature | Used by | Status | Actual |
|---|---|---|---|---|
| `isClient` | `() -> boolean` | `Observe.multiplayer`, the no-multiplayer gate | `requires_live` | |
| `isServer` | `() -> boolean` | `Observe.multiplayer`, the no-multiplayer gate | `requires_live` | |

These two are first because they are the only engine symbols whose failure mode
is **the agent doing nothing at all**, rather than one action failing.

The blueprint forbids multiplayer, and `ActionEngine._multiplayer_abort` enforces
it by refusing every mutating command unless the mod positively reported single
player. `Observe.multiplayer` returns `true`, `false`, or `nil` when neither
accessor could be read — and `nil` is refused exactly as `true` is, because an
absent reading is not a negative reading.

So if these two names are wrong on Build 42.20, a perfectly ordinary
single-player session refuses every command with `POLICY_DENIED` and the message
"the mod did not report whether this session is multiplayer". That is the
conservative failure and it is the right one, but it is indistinguishable at a
glance from the agent being broken. **Confirm these first, in S02, before
concluding anything else is wrong.**

Neither has ever been exercised against a real multiplayer session either. The
gate is written and tested against fakes; nobody has watched it refuse a server.

## World and squares

| Symbol | Assumed signature | Used by | Status | Actual |
|---|---|---|---|---|
| `getCell` | `() -> IsoCell` | `Toolkit.gridSquare` | `requires_live` | |
| `IsoCell.getGridSquare` | `(x, y, z) -> IsoGridSquare or nil` | `Toolkit.gridSquare`, every adapter that resolves a square | `requires_live` | |
| `IsoGridSquare.getObjects` | `() -> a zero-based list` | `adapters/World.lua`, `adapters/Containers.lua`, water sources | `requires_live` | |
| `AdjacentFreeTileFinder.Find` | `(square, character) -> IsoGridSquare` | `adapters/Movement.lua`, choosing where to stand next to a thing | `requires_live` | |
| `IsoDoor` / `IsoWindow` | type probes for what a square holds | `adapters/World.lua` | `requires_live` | |

`IsoGridSquare.getObjects` returns a Java list as Kahlua sees it — zero-based,
with `size()` and `get(i)`. `Toolkit.listSize` and `Toolkit.listGet` wrap that
so an adapter never indexes it directly. If the collection shape differs, fix it
in those two functions and every adapter follows.

## Character state readers

These decide whether an action *succeeded*. A wrong name here does not break the
action — it breaks the proof, and the result comes back `failed` for something
that worked. That failure mode is why they are listed separately.

| Symbol | Assumed signature | Reads | Status | Actual |
|---|---|---|---|---|
| `IsoPlayer.getStats` | `() -> Stats` | the object the four below hang on | `requires_live` | |
| `Stats.getHunger` | `() -> number, 0..1` | postcondition for `consume.eat` | `requires_live` | |
| `Stats.getThirst` | `() -> number, 0..1` | postcondition for `consume.drink` and the *only* postcondition for `consume.drink_source` | `requires_live` | |
| `Stats.getFatigue` | `() -> number, 0..1` | postcondition for `survival.sleep` | `requires_live` | |
| `Stats.getEndurance` | `() -> number, 0..1` | postcondition for `survival.rest` | `requires_live` | |
| `IsoPlayer.getBodyDamage` | `() -> BodyDamage` | the wound and bandage state | `requires_live` | |
| `BodyDamage.getBodyPart` | `(BodyPartType) -> BodyPart` | postcondition for `medical.bandage` | `requires_live` | |
| `BodyPart.bandaged` / `isBleeding` | boolean readers | postcondition for `medical.bandage` | `requires_live` | |
| `IsoPlayer.isReading` | `() -> boolean` | progress for `literature.read` | `requires_live` | |
| `IsoPlayer.isAsleep` | `() -> boolean` | progress for `survival.sleep` | `requires_live` | |
| `IsoPlayer.getX` / `getY` / `getZ` | `() -> number` | postcondition for both movement actions | `requires_live` | |
| `IsoPlayer.HasTrait` | `(name) -> boolean` | policy input | `requires_live` | |

Hunger and thirst are reported on a 0..1 scale where **higher is worse**. The
postconditions check that the value *fell*. If a build reports them inverted or
on 0..100, every consume scenario will fail with the food visibly eaten — that
is the signature of this row being wrong, not of the eat action failing.

## Containers and inventory

| Symbol | Assumed signature | Used by | Status | Actual |
|---|---|---|---|---|
| `IsoPlayer.getInventory` | `() -> ItemContainer` | `Toolkit.playerInventory` | `requires_live` | |
| `ItemContainer.getItems` | `() -> a zero-based list` | `Toolkit.containerItems` | `requires_live` | |
| `ItemContainer.getCapacity` | `() -> number` | `container.inspect` | `requires_live` | |
| `ItemContainer.getContentsWeight` | `() -> number` | `container.inspect` | `requires_live` | |
| `InventoryItem.getID` | `() -> number` | reference minting — see the note below | `requires_live` | |
| `InventoryItem.getFullType` | `() -> string` | item identity | `requires_live` | |
| `IsoPlayer.getWornItems` | `() -> a worn-items list` | `Toolkit.findWorn`, `equipment.*` | `requires_live` | |
| `IsoPlayer.getPrimaryHandItem` / `getSecondaryHandItem` | `() -> InventoryItem or nil` | `equipment.*` postconditions | `requires_live` | |
| `IsoPlayer.getVehicle` | `() -> BaseVehicle or nil` | vehicle containers | `requires_live` | |
| `IsoObject.getContainer` | `() -> ItemContainer or nil` | world containers | `requires_live` | |

**`getID` deserves attention before the first run.** References are minted from
it, and the mod refuses a negative id on purpose: Java answers `-1` for "no id
here", and `-1` is a legal reference segment — two objects both answering `-1`
would produce one reference standing for both, which the action engine would
happily act on. If ids in this build behave differently, that refusal is the
thing to re-examine first.

## Player input

| Symbol | Assumed signature | Used by | Status | Actual |
|---|---|---|---|---|
| `Events.OnPlayerMove` | event, `(player)` | manual-takeover detection in `Safety.lua` | `requires_live` | |
| `Events.OnKeyPressed` | event, `(keyCode)` | the panic hotkey in `PZAgent_Main.lua` | `requires_live` | |
| `Events.OnTick` | event | the mod's whole tick | `requires_live` | |
| `Events.OnGameStart` | event | agent construction | `requires_live` | |
| `getTimestampMs` | `() -> number` | every timestamp the mod writes | `requires_live` | |

Event registration is guarded: an event this build does not expose is recorded
in `PZAgent.unavailableEvents` and skipped, so a missing event costs one feature
rather than aborting the file and leaving the mod half-loaded.

**Manual-takeover detection is the one place a wrong symbol is dangerous rather
than merely broken.** If `OnPlayerMove` is not the right hook, the mod will not
notice the player taking control and will keep acting over them. Verify it in
S06 before running anything autonomous.

## What to do with this file

Fill in the `Status` and `Actual` columns as you go. A row that stays
`requires_live` after a full run means the scenario exercising it did not
actually exercise it — which is itself a finding worth recording, because a
capability nothing proved is not a capability the release can claim.
