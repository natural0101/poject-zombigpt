-- PZAgent.Adapters.Doors: opening, closing and unlocking one named door.
--
-- The group that carries the file is the anti-lie one: ToggleDoor answers
-- nothing useful, so the only thing between "the toggle was called" and an ack
-- claiming the door opened is that verify re-reads the door. A double whose
-- toggle returns while nothing moves is kept here for exactly that reason. The
-- rest is the reference discipline -- the index-ordering caveat made real --
-- and the two typed refusals a door can earn: DOOR_LOCKED needs a key hunt,
-- DOOR_BARRICADED needs a detour, and a merely closed door is no error at all.

local ROOT = arg[0]:match("^(.*)test_adapter_doors%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
Support.loadModules(Harness.root, { "Doors" })
-- The declaration an adapter carries is worth exactly what the dispatcher makes
-- of it, so the gate is loaded and asked rather than restated here.
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")

local equal, ok, isNil = Harness.equal, Harness.ok, Harness.isNil
local contains, same = Harness.contains, Harness.same

local Doors = PZ.Adapters.Doors
local Open = Doors.Open
local Close = Doors.Close
local Unlock = Doors.Unlock
local REASON = PZ.Protocol.REASON
local ARG = PZ.CommandDispatcher.ARG

--- Walking puts the character on the square the action was built with -- and
--- only when the test performs it, so "the walk was queued" and "the character
--- arrived" stay two different facts.
local function installWalk()
  return Support.installAction("ISWalkToTimedAction", function(_, args)
    local player, destination = args[1], args[2]
    player.state.x = destination.getX()
    player.state.y = destination.getY()
    player.state.z = destination.getZ()
  end)
end

--- A door five squares east of the character, with a bench beside it that is
--- no door at all.
local function scene(options)
  options = options or {}
  local door = Support.door(options.door or {})
  local bench = Support.worldObject({ name = "bench" })
  local main = Support.container(options.items or {})
  local player = Support.player({ x = options.x or 100, y = 200, z = 0, inventory = main })
  Support.installCell({ [Support.squareKey(105, 200, 0)] = Support.square(105, 200, 0, { door, bench }) })
  local walk = installWalk()
  local queue = Support.installQueue()
  local ctx = Support.context({ player = player, safety = options.safety })
  return { player = player, ctx = ctx, walk = walk, queue = queue, door = door, main = main }
end

local DOOR = Support.objectRef(105, 200, 0, 0)
local BENCH = Support.objectRef(105, 200, 0, 1)
local MISSING = Support.objectRef(105, 200, 0, 9)
local UNLOADED = Support.objectRef(300, 300, 0, 0)

Harness.group("open refuses a command it cannot act on")
do
  local s = scene()
  local function refuse(args)
    local accepted, code, detail = Open:validate(args, s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "opening nothing in particular is not a command")

  code = refuse({ door_ref = DOOR, hurry = true })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  code = refuse({ door_ref = DOOR, radius = 0 })
  equal(code, REASON.INVALID_ARGUMENT, "a reach no position satisfies is not a reach")

  code = refuse({ door_ref = DOOR, radius = 50 })
  equal(code, REASON.INVALID_ARGUMENT, "and a reach of fifty squares is not standing at the door")

  code = refuse({ door_ref = Support.squareRef(105, 200, 0) })
  equal(code, REASON.INVALID_REF, "a square reference does not name a door")

  code = refuse({ door_ref = Support.objectRef(105, 200, 0, 0, "11111111-2222-3333-4444-555555555555") })
  equal(code, REASON.INVALID_REF, "a reference from another session names an object nobody here minted")

  local detail
  code, detail = refuse({ door_ref = UNLOADED })
  equal(code, REASON.TARGET_NOT_LOADED, "a square the game has not streamed in holds no door yet")
  contains(detail, "not loaded", "and the detail says so")

  code = refuse({ door_ref = MISSING })
  equal(code, REASON.INVALID_REF, "an index past the end of the square's list names nothing")

  code, detail = refuse({ door_ref = BENCH })
  equal(code, REASON.INVALID_REF, "an index that resolves to a non-door is a stale reference")
  contains(detail, "not a door", "and the refusal says what stood there instead of guessing a nearby door")

  -- A window answers an open reader too; the smash state is what marks it, and
  -- toggling one as a door would smash through the classification.
  local windowScene = scene()
  windowScene.door.isSmashed = function()
    return false
  end
  local accepted, windowCode = Open:validate({ door_ref = DOOR }, windowScene.ctx)
  isNil(accepted, "an object with a smash state is a window, not a door")
  equal(windowCode, REASON.INVALID_REF, "and is refused as a stale reference")
end

Harness.group("a missing engine symbol costs one capability, naming it")
do
  local s = scene()
  Support.removeAction("ISWalkToTimedAction")
  local accepted, code, detail = Open:validate({ door_ref = DOOR }, s.ctx)
  isNil(accepted, "with no walk action there is nothing to validate")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "ISWalkToTimedAction", "naming the symbol this build did not have")

  local noQueue = scene()
  Support.removeQueue()
  local queued, queueCode, queueDetail = Open:validate({ door_ref = DOOR }, noQueue.ctx)
  isNil(queued, "and with no action queue there is nowhere to put the walk")
  equal(queueCode, REASON.CAPABILITY_UNAVAILABLE, "which is the same kind of gap")
  contains(queueDetail, "ISTimedActionQueue.add", "naming the other symbol the walk needs")

  local noToggle = scene({ door = { no_toggle = true } })
  local toggled, toggleCode, toggleDetail = Open:validate({ door_ref = DOOR }, noToggle.ctx)
  isNil(toggled, "a door with no toggle cannot be operated")
  equal(toggleCode, REASON.CAPABILITY_UNAVAILABLE, "which is a capability gap, not a stuck door")
  contains(toggleDetail, "IsoDoor.ToggleDoor", "naming the toggle that was looked for")

  local noReader = scene({ door = { no_open_reader = true } })
  -- With the engine's class test present the object is still known to be a
  -- door; what is missing is the reader the postcondition stands on.
  instanceof = function(object, className)
    return object.state ~= nil and className == "IsoDoor"
  end
  local blind, blindCode, blindDetail = Open:validate({ door_ref = DOOR }, noReader.ctx)
  isNil(blind, "a door whose open state cannot be read cannot prove an open")
  equal(blindCode, REASON.CAPABILITY_UNAVAILABLE, "so it refuses up front rather than toggling blind")
  contains(blindDetail, "IsoDoor.IsOpen", "naming the reader")
  instanceof = nil
end

Harness.group("a locked door earns the key hunt and a barricaded one the detour")
do
  local locked = scene({ door = { locked = true } })
  local accepted, code, detail = Open:validate({ door_ref = DOOR }, locked.ctx)
  isNil(accepted, "a locked door does not open")
  equal(code, REASON.DOOR_LOCKED, "the typed refusal asks for a key hunt")
  contains(detail, "door.unlock", "and points at the command that turns the key")

  local barricaded = scene({ door = { barricaded = true } })
  local held, heldCode = Open:validate({ door_ref = DOOR }, barricaded.ctx)
  isNil(held, "a barricaded door does not open")
  equal(heldCode, REASON.DOOR_BARRICADED, "the typed refusal asks for a detour")

  local barricadedOpen = scene({ door = { barricaded = true, open = true } })
  local heldShut, heldShutCode = Close:validate({ door_ref = DOOR }, barricadedOpen.ctx)
  isNil(heldShut, "and a barricade blocks closing just the same")
  equal(heldShutCode, REASON.DOOR_BARRICADED, "with the same reason")
end

Harness.group("open walks to the door, toggles it, and proves it by re-reading")
do
  local s = scene()
  local args = { door_ref = DOOR }
  ok(Open:validate(args, s.ctx), "a closed unlocked door five squares off validates")
  ok(Open:prepare(args, s.ctx), "and prepares")
  equal(#s.queue.added, 1, "one action was queued because the door is out of reach")
  equal(s.queue.added[1].Type, "ISWalkToTimedAction", "and it is a walk")
  local entries = PZ.Safety.describeQueue(s.player)
  equal(PZ.Ownership.describe(entries, s.ctx.session_id).ownership, PZ.Protocol.OWNERSHIP.MOD, "tagged as ours")

  ok(Open:begin(args, s.ctx), "the command begins behind the walk")
  equal(Open:progress(args, s.ctx), "running", "and waits while the walk is still ours to wait for")
  equal(s.door.state.open, false, "the door is untouched from five squares away")

  s.walk.actions[1]:perform()
  Support.drainQueue(s.queue)
  equal(Open:progress(args, s.ctx), "done", "arriving is what triggers the toggle")
  equal(s.door.state.open, true, "and the game's own interaction is what moved the door")

  local proof = Open:verify(nil, nil, args, s.ctx)
  ok(proof ~= nil, "the re-read door open is the postcondition")
  equal(proof.kind, "door_state_changed", "named for what was observed")
  equal(proof.door_ref, DOOR, "carrying the door that was operated")
  equal(proof.open_before, false, "the state before")
  equal(proof.open_after, true, "and the state that was read back")
  equal(proof.walked, true, "recording that this one took a walk")
  equal(proof.distance, 0, "with the distance that was measured")
  isNil(proof.unchanged_is_success, "a door that moved claims no unchanged success")

  local near = scene({ x = 105 })
  local nearArgs = { door_ref = DOOR }
  ok(Open:validate(nearArgs, near.ctx), "the same command validates from beside the door")
  ok(Open:prepare(nearArgs, near.ctx), "and prepares")
  equal(#near.queue.added, 0, "with no walk queued, because none was needed")
  equal(Open:begin(nearArgs, near.ctx), "done", "the toggle happens the moment the command begins")
  equal(near.door.state.open, true, "and the door moved")
  local nearProof = Open:verify(nil, nil, nearArgs, near.ctx)
  equal(nearProof.walked, false, "the evidence records that no walk was taken")
end

Harness.group("a door already in the asked-for state is a success, not a change")
do
  local s = scene({ door = { open = true }, x = 100 })
  local args = { door_ref = DOOR }
  ok(Open:validate(args, s.ctx), "an already-open door validates")
  ok(Open:prepare(args, s.ctx), "and prepares")
  equal(#s.queue.added, 0, "with nothing queued: there is nothing to walk to and nothing to touch")
  equal(Open:begin(args, s.ctx), "done", "the command is done as soon as it begins")
  equal(s.door.state.open, true, "the door was never toggled")

  local proof = Open:verify(nil, nil, args, s.ctx)
  equal(proof.kind, "door_state_changed", "the evidence kind is the same")
  equal(proof.unchanged_is_success, true, "declared as an unchanged success")
  equal(proof.open_before, true, "with identical before")
  equal(proof.open_after, true, "and after readings")
end

Harness.group("a walk that ended short of the door is not an open door")
do
  local s = scene()
  local args = { door_ref = DOOR }
  ok(Open:validate(args, s.ctx), "the command validates")
  ok(Open:prepare(args, s.ctx), "and queues its walk")
  ok(Open:begin(args, s.ctx), "and begins")

  -- The queue drains and the character never moved: pathfinding gave up. The
  -- mod would toggle a door across the room if progress trusted the queue.
  Support.drainQueue(s.queue)
  local status, code, detail = Open:progress(args, s.ctx)
  isNil(status, "an empty queue with the door still five squares away is not arrival")
  equal(code, REASON.PATH_NOT_FOUND, "it is a walk that did not get there")
  contains(detail, "squares from the door", "and the detail carries the distance that was left")
  equal(s.door.state.open, false, "the door was never touched")
end

Harness.group("a toggle the door swallowed fails its postcondition")
do
  -- The anti-lie case. The call returns cleanly, nothing moves, and the only
  -- honest outcome is POSTCONDITION_FAILED -- never a door reported open.
  local s = scene({ door = { toggle_noop = true }, x = 105 })
  local args = { door_ref = DOOR }
  ok(Open:validate(args, s.ctx), "the door validates")
  ok(Open:prepare(args, s.ctx), "and prepares")
  equal(Open:begin(args, s.ctx), "done", "the toggle call itself returns")
  equal(s.door.state.open, false, "but the door did not move")

  local evidence, code, detail = Open:verify(nil, nil, args, s.ctx)
  isNil(evidence, "and there is no evidence to promote the command with")
  equal(code, REASON.POSTCONDITION_FAILED, "so it fails its postcondition")
  contains(detail, "closed", "naming the state the door still reads")
end

Harness.group("close mirrors open, and a lock never holds an open door")
do
  local s = scene({ door = { open = true }, x = 105 })
  local args = { door_ref = DOOR }
  ok(Close:validate(args, s.ctx), "an open door validates for closing")
  ok(Close:prepare(args, s.ctx), "and prepares")
  equal(Close:begin(args, s.ctx), "done", "the toggle happens within reach")
  equal(s.door.state.open, false, "and the door closed")
  local proof = Close:verify(nil, nil, args, s.ctx)
  equal(proof.kind, "door_state_changed", "with the same evidence kind")
  equal(proof.open_before, true, "before: open")
  equal(proof.open_after, false, "after: closed, read back")

  -- A lock holds a door shut, not open: closing a locked-open door must work.
  local lockedOpen = scene({ door = { open = true, locked = true }, x = 105 })
  ok(Close:validate(args, lockedOpen.ctx), "a locked but open door still validates for closing")
  ok(Close:prepare(args, lockedOpen.ctx), "prepares")
  equal(Close:begin(args, lockedOpen.ctx), "done", "and closes")
  equal(lockedOpen.door.state.open, false, "the lock never blocked the swing shut")

  local shut = scene({ x = 105 })
  ok(Close:validate(args, shut.ctx), "an already-closed door validates")
  ok(Close:prepare(args, shut.ctx), "prepares")
  equal(Close:begin(args, shut.ctx), "done", "and is done at once")
  local unchanged = Close:verify(nil, nil, args, shut.ctx)
  equal(unchanged.unchanged_is_success, true, "as an unchanged success")
end

Harness.group("unlock needs a readable lock and an observable key")
do
  local unreadable = scene({ door = { no_lock_reader = true } })
  local accepted, code, detail = Unlock:validate({ door_ref = DOOR }, unreadable.ctx)
  isNil(accepted, "a lock that cannot be read cannot be proven unlocked")
  equal(code, REASON.PRECONDITION_FAILED, "which is a precondition, not a stale reference")
  contains(detail, "lock state could not be read", "and the detail says exactly that")

  local keyless = scene({ door = { locked = true, key_id = 42 } })
  local noKey, noKeyCode, noKeyDetail = Unlock:validate({ door_ref = DOOR }, keyless.ctx)
  isNil(noKey, "a locked door with no key in the inventory does not unlock")
  equal(noKeyCode, REASON.DOOR_LOCKED, "the refusal is the key hunt")
  contains(noKeyDetail, "no key", "and says the key is what is missing")

  -- Neither key surface answers: the door has no key id and the inventory no
  -- haveThisKeyForDoor. "The key cannot be looked for" is not "there is no
  -- key", so this is a capability gap naming both probes.
  local blind = scene({ door = { locked = true } })
  local unknown, unknownCode, unknownDetail = Unlock:validate({ door_ref = DOOR }, blind.ctx)
  isNil(unknown, "with no key surface at all the unlock refuses")
  equal(unknownCode, REASON.CAPABILITY_UNAVAILABLE, "as a capability gap")
  contains(unknownDetail, "haveThisKeyForDoor", "naming the game's own answer")
  contains(unknownDetail, "getKeyId", "and the fallback that was probed after it")

  local barricaded = scene({ door = { locked = true, barricaded = true, key_id = 42 } })
  local held, heldCode = Unlock:validate({ door_ref = DOOR }, barricaded.ctx)
  isNil(held, "a barricaded door refuses the unlock too")
  equal(heldCode, REASON.DOOR_BARRICADED, "because turning the key would still leave it held shut")
end

Harness.group("unlock turns the key through the game's own interaction")
do
  -- The game's own answer: the inventory says it holds the key for this door.
  local s = scene({ door = { locked = true, toggle_unlocks = true }, x = 105 })
  s.main.haveThisKeyForDoor = function()
    return true
  end
  local args = { door_ref = DOOR }
  ok(Unlock:validate(args, s.ctx), "a locked door with the key carried validates")
  ok(Unlock:prepare(args, s.ctx), "and prepares")
  equal(Unlock:begin(args, s.ctx), "done", "the interaction runs within reach")
  equal(s.door.state.locked, false, "and the game's own toggle is what turned the lock")

  local proof = Unlock:verify(nil, nil, args, s.ctx)
  ok(proof ~= nil, "the re-read lock is the postcondition")
  equal(proof.kind, "door_unlocked", "named for what was observed")
  equal(proof.door_ref, DOOR, "carrying the door")
  equal(proof.locked_before, true, "locked before")
  equal(proof.locked_after, false, "and read back unlocked after")
  isNil(proof.unchanged_is_success, "a lock that turned claims no unchanged success")

  -- The fallback surface: no haveThisKeyForDoor, so the key id is matched
  -- against what is carried -- including a key ring one level down.
  local key = Support.item({ id = 7, full_type = "Base.Key1", name = "Key" })
  key.getKeyId = function()
    return 42
  end
  local ring = Support.item({ id = 8, full_type = "Base.KeyRing", name = "Key Ring", contents = { key } })
  local ringed = scene({ door = { locked = true, key_id = 42, toggle_unlocks = true }, items = { ring }, x = 105 })
  ok(Unlock:validate(args, ringed.ctx), "a key on a carried key ring is an observable key")
  ok(Unlock:prepare(args, ringed.ctx), "prepares")
  equal(Unlock:begin(args, ringed.ctx), "done", "and unlocks")
  equal(ringed.door.state.locked, false, "with the lock really turned")

  local open = scene({ x = 105 })
  ok(Unlock:validate(args, open.ctx), "an unlocked door validates")
  ok(Unlock:prepare(args, open.ctx), "prepares nothing")
  equal(#open.queue.added, 0, "queues nothing")
  equal(Unlock:begin(args, open.ctx), "done", "and is done at once")
  local unchanged = Unlock:verify(nil, nil, args, open.ctx)
  equal(unchanged.unchanged_is_success, true, "an already-unlocked door is an unchanged success")
  equal(unchanged.locked_before, false, "with locked_before false")
  equal(unchanged.locked_after, false, "and locked_after false")

  -- The unlock's own anti-lie case: the interaction returns and the lock
  -- still reads locked.
  local stuck = scene({ door = { locked = true, key_id = 42, toggle_noop = true }, x = 105 })
  local stuckKey = Support.item({ id = 9, full_type = "Base.Key1", name = "Key" })
  stuckKey.getKeyId = function()
    return 42
  end
  stuck.main.entries[#stuck.main.entries + 1] = stuckKey
  ok(Unlock:validate(args, stuck.ctx), "the key is carried, so the command validates")
  ok(Unlock:prepare(args, stuck.ctx), "prepares")
  equal(Unlock:begin(args, stuck.ctx), "done", "the call returns")
  local evidence, code, detail = Unlock:verify(nil, nil, args, stuck.ctx)
  isNil(evidence, "but a lock that still reads locked proves nothing")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command fails")
  contains(detail, "locked", "naming the state that did not move")
end

Harness.group("a lock that stopped answering between the toggle and the re-read")
do
  -- The index-ordering caveat and the lock reader, together. `Doors.isDoor`
  -- deliberately does not demand a lock reader -- that is what lets an
  -- unreadable lock earn door.unlock's own precise reason instead of
  -- INVALID_REF -- so `resolveDoor` can legitimately hand verify a door-shaped
  -- object whose lock nobody can read. Verify's own re-read is the only thing
  -- in the mod that notices.
  --
  -- The asymmetry with open/close is why this needed its own case: the toggle
  -- verify compares `openAfter ~= desired`, which nil satisfies, so its nil
  -- check is not load-bearing. The unlock compares `lockedAfter == true`, which
  -- nil does *not* satisfy -- delete the check and control falls through to the
  -- evidence table with `locked_after = nil`. `ActionRuntime.observedPairs`
  -- skips a `_before` whose `_after` is nil, so it counts zero pairs and the
  -- all-unchanged refusal never fires: the mod acks SUCCEEDED for a door nobody
  -- observed unlocked.
  --
  -- What this does NOT claim, because an independent re-test refuted the
  -- stronger version: the false ack does not reach the caller. The sidecar's
  -- `DoorUnlockAdapter.verify` requires `door.locked is False` and treats an
  -- absent reading as not-unlocked, so the run ends POSTCONDITION_FAILED after
  -- the grace window instead of succeeding. This guard is the mod's own layer
  -- of that defence -- and the layer nothing else in the mod pins.
  local s = scene({ door = { locked = true, key_id = 42, toggle_unlocks = true }, x = 105 })
  local key = Support.item({ id = 11, full_type = "Base.Key1", name = "Key" })
  key.getKeyId = function()
    return 42
  end
  s.main.entries[#s.main.entries + 1] = key
  local args = { door_ref = DOOR }
  ok(Unlock:validate(args, s.ctx), "the lock reads, the key is carried, so it validates")
  ok(Unlock:prepare(args, s.ctx), "prepares")
  equal(Unlock:begin(args, s.ctx), "done", "and the game's toggle turns the lock")
  equal(s.door.state.locked, false, "which really did happen")

  -- The square's object list shifts under the reference: index 0 is now a
  -- door-shaped object on a build with no `isLockedByKey`. Nothing about the
  -- reference changed; what it names did.
  local blind = Support.door({ locked = false, no_lock_reader = true })
  Support.installCell({
    [Support.squareKey(105, 200, 0)] = Support.square(105, 200, 0, { blind, s.door }),
  })

  local unread, unreadCode, unreadDetail = Unlock:verify(nil, nil, args, s.ctx)
  isNil(unread, "a lock that cannot be read proves nothing about the lock")
  equal(unreadCode, REASON.PRECONDITION_FAILED, "it is a capability gap, not a failed postcondition")
  contains(unreadDetail, "lock state could not be read", "and the detail says which reading is missing")
end

Harness.group("door work stops for the player and for a horde")
do
  local taken = scene({ safety = Support.takenOver() })
  local args = { door_ref = DOOR }
  local prepared, prepareCode = Open:prepare(args, taken.ctx)
  isNil(prepared, "a taken-over character prepares nothing")
  equal(prepareCode, REASON.USER_TAKEOVER, "the player is driving")
  equal(#taken.queue.added, 0, "and nothing was queued")

  local threatened = scene({ safety = Support.threatened() })
  local started, startCode = Open:begin(args, threatened.ctx)
  isNil(started, "and nothing begins into a horde")
  equal(startCode, REASON.THREAT_INTERRUPTED, "with the reflex guard's reason")
end

Harness.group("the dispatcher builds the argument table from the declaration")
do
  local Dispatcher = PZ.CommandDispatcher
  local registry = Dispatcher.new()
  ok(registry:register(Open), "the open declaration is one the dispatcher accepts")
  ok(registry:register(Close), "and so is close's")
  ok(registry:register(Unlock), "and unlock's")

  equal(Open.args.door_ref.type, ARG.REF, "the door is declared as a reference")
  equal(Open.args.radius.max, Doors.MAX_RADIUS, "the reach ceiling is declared, not only read")

  same(
    Dispatcher.checkArgs(Open, { door_ref = DOOR, radius = 1.2 }, Support.SESSION),
    { door_ref = DOOR, radius = 1.2 },
    "a declared command reaches the adapter with the keys it declared"
  )

  local refused, code, detail = Dispatcher.checkArgs(Open, { door_ref = DOOR, hurry = true }, Support.SESSION)
  isNil(refused, "an undeclared key never reaches the adapter")
  equal(code, REASON.INVALID_ARGUMENT, "it is an argument error")
  contains(detail, "hurry", "naming the key that was not declared")

  local wrongKind, wrongCode =
    Dispatcher.checkArgs(Open, { door_ref = Support.squareRef(105, 200, 0) }, Support.SESSION)
  isNil(wrongKind, "a square reference is not an object reference")
  equal(wrongCode, REASON.INVALID_REF, "and the declared kinds are what say so")
end

Harness.group("the adapters are registered where the dispatcher looks for them")
do
  equal(PZ.Adapters.BY_NAME["door.open"], Open, "open is registered under its action name")
  equal(PZ.Adapters.BY_NAME["door.close"], Close, "and so is close")
  equal(PZ.Adapters.BY_NAME["door.unlock"], Unlock, "and unlock")
  ok(PZ.Protocol.isKnownAction(Open.name), "all three names are in the protocol whitelist")
  ok(PZ.Protocol.isKnownAction(Close.name), "close included")
  ok(PZ.Protocol.isKnownAction(Unlock.name), "unlock included")
  equal(Open.capability, "door_toggle", "all three ride the one door capability")
  equal(Close.capability, "door_toggle", "close too")
  equal(Unlock.capability, "door_toggle", "and unlock")
  ok(not PZ.Protocol.isReadOnly(Open.name), "opening a door is not a read")
  ok(not PZ.Protocol.isAlwaysAllowed(Open.name), "and needs an armed session")
  equal(type(Open.start), "function", "each adapter exposes the start the runtime calls")
  equal(type(Open.poll), "function", "and the poll it calls after it")
end

Harness.finish("adapter_doors")
