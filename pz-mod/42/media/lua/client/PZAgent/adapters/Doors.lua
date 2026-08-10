--[[
PZAgent.Adapters.Doors -- door.open, door.close and door.unlock.

Invariant: the only way this file changes a door is the game's own interaction.
Build 42 has no constructible open-door timed action -- ISOpenCloseDoor does not
exist as a class -- so the vanilla path is the one the E key lands on:
IsoDoor:ToggleDoor(character), reached here through Toolkit.call after a walk
into reach. Nothing writes a state: setLockedByKey and its siblings are exactly
the stat-writing this project forbids, because a door "unlocked" that way was
never unlocked by the character, only edited under them.

The postcondition is a re-read, never the call returning. ToggleDoor answers
nothing useful -- a locked door swallows the toggle and plays a sound -- so the
only honest promotion to succeeded is reading the door again and seeing the
state it was asked for. The test suite keeps a case where the toggle "works"
and the door does not move, because that is the lie this verify exists to catch.

A door already in the asked-for state is not an error and not a fabricated
change: the command succeeds with `unchanged_is_success` and identical
before/after readings, which is the protocol's way of saying "nothing needed
doing and that was observed".

The reference is object:<session>:<x>:<y>:<z>:<index>, and the index is the
engine's own ordering of the square's object list -- the same stability caveat
world container references carry. Resolution therefore re-checks doorness at
the index, and a mismatch is INVALID_REF, never "the nearest door instead".
]]

-- Load-order guard, live-proven 2026-08-08 on Build 42.20.2: the engine walked
-- adapters/ in an order that ran sibling files before Toolkit.lua. The
-- statement form is deliberate -- the paren form is banned as dynamic loading --
-- and the test harness pre-resolves this module, so there the require is a
-- no-op.
require "PZAgent/adapters/Toolkit"

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Doors = {}
PZAgent.Adapters.Doors = Doors

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

Doors.TIMEOUT_MS = 30000
Doors.POLL_MS = 250

--- Reach for the toggle: the same interaction range every other adapter uses.
Doors.DEFAULT_RADIUS = 1.6

--- Reaches a command may ask for, bounded for the same reasons as containers:
--- the floor keeps a caller from naming a radius no position ever satisfies,
--- the ceiling keeps the character next to the door it is about to touch.
Doors.MIN_RADIUS = 0.1
Doors.MAX_RADIUS = 3.0

--- Argument kinds this file declares, spelled the way
--- PZAgent.CommandDispatcher.ARG spells them. Held here rather than read off the
--- dispatcher because the engine chooses the order it walks media/lua in.
local ARG = { NUMBER = "number", REF = "ref" }

--- All three commands take an object reference and nothing else that is a
--- reference, so the accepted kinds are one table rather than three copies.
local OBJECT_KIND = { [PZAgent.Refs.KIND.OBJECT] = true }

local DOOR_ARGS = { "door_ref", "radius" }

--- The engine spellings this file probes, each a short closed list so a Build
--- 42 rename costs one refusal naming the symbol rather than a silent miss.
local OPEN_READERS = { "IsOpen", "isOpen" }
local LOCK_READERS = { "isLockedByKey", "isLocked" }
local BARRICADE_READERS = { "isBarricaded" }
local TOGGLE_NAMES = { "ToggleDoor", "toggleDoor" }

--- How the refusal names the toggle. One spelling on the wire, whichever of
--- the probed member names was looked for.
Doors.TOGGLE_SYMBOL = "IsoDoor.ToggleDoor"
Doors.OPEN_SYMBOL = "IsoDoor.IsOpen"
Doors.KEY_SYMBOL = "ItemContainer.haveThisKeyForDoor / IsoDoor.getKeyId"

local function readOpen(door)
  return toolkit().readBooleanOf(door, OPEN_READERS)
end

local function readLocked(door)
  return toolkit().readBooleanOf(door, LOCK_READERS)
end

local function readBarricaded(door)
  return toolkit().readBooleanOf(door, BARRICADE_READERS)
end

-- ---------------------------------------------------------------------------
-- resolving the door
-- ---------------------------------------------------------------------------

--- Is this object a door?
---
--- The engine's own class test first. The accessor fallback deliberately does
--- not require a lock reader the way World.classify does: door.unlock refuses
--- on an unreadable lock with its own precise reason, and a doorness check that
--- demanded the lock reader would turn that case into INVALID_REF -- blaming
--- the reference for a gap in the build. What the fallback does demand is an
--- open reader and no smash state, because a smash state is the window's
--- marker and a window must never be toggled as a door.
local function isDoor(object)
  local Toolkit = toolkit()
  if type(instanceof) == "function" then
    local ok, result = pcall(instanceof, object, "IsoDoor")
    if ok and result == true then
      return true
    end
    local okWindow, window = pcall(instanceof, object, "IsoWindow")
    if okWindow and window == true then
      return false
    end
  end
  if Toolkit.method(object, "isSmashed") ~= nil then
    return false
  end
  for index = 1, #OPEN_READERS do
    if Toolkit.method(object, OPEN_READERS[index]) ~= nil then
      return true
    end
  end
  return false
end

Doors.isDoor = isDoor

--- The engine object a parsed door reference names, or nil plus a reason.
---
--- The index is checked against the square's list as it is right now, and the
--- object found there is checked for doorness: the index-ordering caveat on
--- the reference format made real. A non-door at the index means the list has
--- shifted since the reference was minted, and acting on whatever stands there
--- now would touch an object nobody named.
local function resolveDoor(parsed)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local square, missing = Toolkit.gridSquare(parsed.x, parsed.y, parsed.z)
  if square == nil then
    if missing == "IsoCell.getGridSquare" then
      return nil, reasons.TARGET_NOT_LOADED, "the square the door stands on is not loaded"
    end
    return Toolkit.unavailable(missing)
  end
  local ok, objects = Toolkit.call(square, "getObjects")
  if not ok or objects == nil then
    return Toolkit.unavailable("IsoGridSquare.getObjects")
  end
  local object = Toolkit.listGet(objects, parsed.object_index)
  if object == nil then
    return nil, reasons.INVALID_REF, "no object stands at that index on the square"
  end
  if not isDoor(object) then
    return nil,
      reasons.INVALID_REF,
      "the object at that index is not a door -- the square's object list has shifted since the reference was minted"
  end
  return object
end

Doors.resolveDoor = resolveDoor

--- Parse the reference and radius all three commands take.
local function doorSpec(args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, DOOR_ARGS, { "door_ref" })
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local ref, refCode, refDetail = Toolkit.readRef(args, "door_ref", PZAgent.Refs.KIND.OBJECT, ctx)
  if ref == nil then
    return nil, refCode, refDetail
  end
  local parsed, parseError = PZAgent.Refs.parseObject(ref)
  if parsed == nil then
    return nil, reasons.INVALID_REF, parseError
  end
  local radius, radiusCode, radiusDetail = Toolkit.readNumber(args, "radius", {
    default = Doors.DEFAULT_RADIUS,
    minimum = Doors.MIN_RADIUS,
    maximum = Doors.MAX_RADIUS,
  })
  if radius == nil then
    return nil, radiusCode, radiusDetail
  end
  return {
    door_ref = ref,
    parsed = parsed,
    point = { x = parsed.x, y = parsed.y, z = parsed.z },
    radius = radius,
  }
end

Doors.doorSpec = doorSpec

-- ---------------------------------------------------------------------------
-- the toggle
-- ---------------------------------------------------------------------------

--- Drive the game's own door interaction, guarded end to end.
---
--- The call returning is deliberately not treated as the door moving: verify
--- re-reads the state, and a toggle the door swallowed fails there.
local function toggleDoor(ctx, door)
  local Toolkit = toolkit()
  local name = nil
  for index = 1, #TOGGLE_NAMES do
    if Toolkit.method(door, TOGGLE_NAMES[index]) ~= nil then
      name = TOGGLE_NAMES[index]
      break
    end
  end
  if name == nil then
    return Toolkit.unavailable(Doors.TOGGLE_SYMBOL)
  end
  local ok, err = Toolkit.call(door, name, ctx.player)
  if not ok then
    return nil, Toolkit.reasons().QUEUE_REJECTED, "the door refused the toggle: " .. tostring(err)
  end
  return true
end

--- True when the resolved door exposes a toggle at all, checked in validate so
--- a build without one refuses before a walk is queued, not after it.
local function toggleAvailable(door)
  local Toolkit = toolkit()
  for index = 1, #TOGGLE_NAMES do
    if Toolkit.method(door, TOGGLE_NAMES[index]) ~= nil then
      return true
    end
  end
  return false
end

-- ---------------------------------------------------------------------------
-- door.open and door.close
--
-- One machine, parameterised by the state the command asks for. The lifecycle
-- is the container.open_nearby ceremony plus one touch: prepare walks into
-- reach, progress toggles once the character is there, verify re-reads.
-- ---------------------------------------------------------------------------

--- The keys under which each adapter parks its scratch, so open and close
--- polled in one session never read each other's marks.
local function marks(ctx, key)
  local state = toolkit().state(ctx)
  local mine = state[key]
  if type(mine) ~= "table" then
    mine = {}
    state[key] = mine
  end
  return mine
end

local function makeToggleValidate(desired, key, adapterRequires)
  return function(_, args, ctx)
    local Toolkit = toolkit()
    local reasons = Toolkit.reasons()
    local required, requiredCode, requiredDetail = Toolkit.requireSymbols(adapterRequires)
    if required == nil then
      return nil, requiredCode, requiredDetail
    end
    local spec, code, detail = doorSpec(args, ctx)
    if spec == nil then
      return nil, code, detail
    end
    local door, doorCode, doorDetail = resolveDoor(spec.parsed)
    if door == nil then
      return nil, doorCode, doorDetail
    end
    -- Barricades block both directions: the planks hold the door where it is,
    -- and the honest answer is the detour code, not a toggle that bounces.
    if readBarricaded(door) == true then
      return nil, reasons.DOOR_BARRICADED, "the door is barricaded; it needs the barricade removed, or a detour"
    end
    local open = readOpen(door)
    if open == nil then
      -- With no open reader there is no postcondition this command could ever
      -- observe, so it refuses up front rather than toggling blind.
      return Toolkit.unavailable(Doors.OPEN_SYMBOL)
    end
    local state = marks(ctx, key)
    state.open_before = open
    state.unchanged = open == desired
    if not state.unchanged then
      if desired == true and readLocked(door) == true then
        return nil, reasons.DOOR_LOCKED, "the door is locked; it needs its key (door.unlock) before it will open"
      end
      if not toggleAvailable(door) then
        return Toolkit.unavailable(Doors.TOGGLE_SYMBOL)
      end
    end
    return true
  end
end

local function makeTogglePrepare(key)
  return function(_, args, ctx)
    local Toolkit = toolkit()
    local code, detail = Toolkit.interruption(ctx)
    if code ~= nil then
      return nil, code, detail
    end
    local spec, specCode, specDetail = doorSpec(args, ctx)
    if spec == nil then
      return nil, specCode, specDetail
    end
    local state = marks(ctx, key)
    if state.unchanged == true then
      -- Already in the asked-for state: there is nothing to walk to and
      -- nothing to touch, only a re-read for verify to make.
      return true
    end
    local outcome, reachCode, reachDetail = Toolkit.approach(ctx, spec.point, spec.radius)
    if outcome == nil then
      return nil, reachCode, reachDetail
    end
    state.walked = outcome == "walking"
    return true
  end
end

--- Toggle when the character is within reach; report how far off it still is
--- otherwise. Shared by begin and progress so the near case and the walked
--- case run the same touch exactly once.
local function toggleWhenClose(ctx, spec, state)
  local Toolkit = toolkit()
  local close, distanceOrCode, reachDetail = Toolkit.inReach(ctx, spec.point, spec.radius)
  if close == nil then
    return nil, distanceOrCode, reachDetail
  end
  if not close then
    return false, distanceOrCode
  end
  local door, doorCode, doorDetail = resolveDoor(spec.parsed)
  if door == nil then
    return nil, doorCode, doorDetail
  end
  local toggled, toggleCode, toggleDetail = toggleDoor(ctx, door)
  if toggled == nil then
    return nil, toggleCode, toggleDetail
  end
  state.toggled = true
  return true
end

local function makeToggleBegin(key)
  return function(_, args, ctx)
    local Toolkit = toolkit()
    local code, detail = Toolkit.interruption(ctx)
    if code ~= nil then
      return nil, code, detail
    end
    local spec, specCode, specDetail = doorSpec(args, ctx)
    if spec == nil then
      return nil, specCode, specDetail
    end
    local state = marks(ctx, key)
    if state.unchanged == true then
      return "done"
    end
    local touched, touchCode, touchDetail = toggleWhenClose(ctx, spec, state)
    if touched == nil then
      return nil, touchCode, touchDetail
    end
    if touched == true then
      return "done"
    end
    -- Out of reach: the walk queued by prepare runs, and progress takes over.
    return true
  end
end

local function makeToggleProgress(key)
  return function(_, args, ctx)
    local Toolkit = toolkit()
    local reasons = Toolkit.reasons()
    local code, detail = Toolkit.interruption(ctx)
    if code ~= nil then
      return nil, code, detail
    end
    local spec, specCode, specDetail = doorSpec(args, ctx)
    if spec == nil then
      return nil, specCode, specDetail
    end
    local state = marks(ctx, key)
    if state.unchanged == true or state.toggled == true then
      return "done"
    end
    local touched, distanceOrCode, touchDetail = toggleWhenClose(ctx, spec, state)
    if touched == nil then
      return nil, distanceOrCode, touchDetail
    end
    if touched == true then
      return "done"
    end
    local queue, queueCode, queueDetail = Toolkit.queueProgress(ctx)
    if queue == nil then
      return nil, queueCode, queueDetail
    end
    if queue == "done" then
      return nil,
        reasons.PATH_NOT_FOUND,
        string.format("the walk ended %.2f squares from the door", distanceOrCode)
    end
    return "running"
  end
end

local function makeToggleVerify(desired, key)
  return function(_, _, _, args, ctx)
    local Toolkit = toolkit()
    local reasons = Toolkit.reasons()
    local spec, code, detail = doorSpec(args, ctx)
    if spec == nil then
      return nil, code, detail
    end
    local state = marks(ctx, key)
    local door, doorCode, doorDetail = resolveDoor(spec.parsed)
    if door == nil then
      return nil, doorCode, doorDetail
    end
    local openAfter = readOpen(door)
    if openAfter == nil then
      return Toolkit.unavailable(Doors.OPEN_SYMBOL)
    end
    if openAfter ~= desired then
      -- The toggle call returned and the door did not move. This is the case
      -- the whole verify exists for: a swallowed toggle must fail, not be
      -- reported as a door that opened.
      return nil,
        reasons.POSTCONDITION_FAILED,
        string.format("the door still reads %s", openAfter and "open" or "closed")
    end
    local close, distanceOrCode = Toolkit.inReach(ctx, spec.point, spec.radius)
    local evidence = {
      kind = "door_state_changed",
      door_ref = spec.door_ref,
      open_before = state.open_before,
      open_after = openAfter,
      walked = state.walked == true,
    }
    if close ~= nil and type(distanceOrCode) == "number" then
      evidence.distance = distanceOrCode
    end
    if state.unchanged == true then
      evidence.unchanged_is_success = true
    end
    return evidence
  end
end

--- One declaration for open and one for close, differing only in the state
--- they ask for and refuse-on-lock behaviour: a lock holds a door closed, so
--- it blocks opening and never blocks closing.
local function declareToggle(name, desired)
  local requires = { "getCell", "ISWalkToTimedAction", "ISWalkToTimedAction.new", "ISTimedActionQueue.add" }
  return toolkit().declare({
    name = name,
    capability = toolkit().CAPABILITY.DOOR_TOGGLE,
    requires = requires,
    timeout_ms = Doors.TIMEOUT_MS,
    poll_interval_ms = Doors.POLL_MS,
    args = {
      door_ref = { type = ARG.REF, required = true, kinds = OBJECT_KIND },
      -- A reach the caller picks is a walk the character takes, so the ceiling
      -- is declared here as well as read in doorSpec.
      radius = { type = ARG.NUMBER, min = Doors.MIN_RADIUS, max = Doors.MAX_RADIUS },
    },
    validate = makeToggleValidate(desired, name, requires),
    prepare = makeTogglePrepare(name),
    begin = makeToggleBegin(name),
    progress = makeToggleProgress(name),
    verify = makeToggleVerify(desired, name),
  })
end

Doors.Open = declareToggle("door.open", true)
toolkit().register(Doors.Open)

Doors.Close = declareToggle("door.close", false)
toolkit().register(Doors.Close)

-- ---------------------------------------------------------------------------
-- door.unlock
-- ---------------------------------------------------------------------------

--- Is a key for `door` observably in the character's inventory?
---
--- The game's own answer first -- ItemContainer.haveThisKeyForDoor is what the
--- vanilla context menu asks -- then a bounded scan matching each carried
--- item's key id against the door's, one nesting level deep because keys live
--- on key rings. When neither surface answers, the refusal names both probes:
--- "the key cannot be looked for" and "there is no key" are opposite facts,
--- and unlocking on the strength of the first would be a blind toggle.
function Doors.haveKey(ctx, door)
  local Toolkit = toolkit()
  local ok, inventory = Toolkit.call(ctx.player, "getInventory")
  if not ok or inventory == nil then
    return Toolkit.unavailable("IsoPlayer.getInventory")
  end
  if Toolkit.method(inventory, "haveThisKeyForDoor") ~= nil then
    local okHave, have = Toolkit.call(inventory, "haveThisKeyForDoor", door)
    if okHave and type(have) == "boolean" then
      return have
    end
  end
  local keyId = Toolkit.readNumberOf(door, { "getKeyId" })
  if keyId == nil then
    return Toolkit.unavailable(Doors.KEY_SYMBOL)
  end
  local items = Toolkit.containerItems(inventory)
  if items == nil then
    return Toolkit.unavailable("ItemContainer.getItems")
  end
  for index = 1, #items do
    if Toolkit.readNumberOf(items[index], { "getKeyId" }) == keyId then
      return true
    end
    local okNested, nested = Toolkit.call(items[index], "getInventory")
    if okNested and nested ~= nil then
      local nestedItems = Toolkit.containerItems(nested)
      if nestedItems ~= nil then
        for inner = 1, #nestedItems do
          if Toolkit.readNumberOf(nestedItems[inner], { "getKeyId" }) == keyId then
            return true
          end
        end
      end
    end
  end
  return false
end

local UNLOCK_KEY = "door.unlock"

local Unlock = nil

local function unlockValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Unlock.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = doorSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local door, doorCode, doorDetail = resolveDoor(spec.parsed)
  if door == nil then
    return nil, doorCode, doorDetail
  end
  if readBarricaded(door) == true then
    return nil, reasons.DOOR_BARRICADED, "the door is barricaded; unlocking it would still leave it held shut"
  end
  local locked = readLocked(door)
  if locked == nil then
    -- Not a capability refusal by design: the door resolved and may be
    -- operable, but the one fact this command exists to change cannot be read
    -- on this build, so the unlock cannot ever prove itself.
    return nil, reasons.PRECONDITION_FAILED, "the lock state could not be read on this build"
  end
  local state = marks(ctx, UNLOCK_KEY)
  state.locked_before = locked
  state.unchanged = locked == false
  if state.unchanged then
    return true
  end
  local have, haveCode, haveDetail = Doors.haveKey(ctx, door)
  if have == nil then
    return nil, haveCode, haveDetail
  end
  if have == false then
    return nil, reasons.DOOR_LOCKED, "the door is locked and no key for it is in the inventory"
  end
  if not toggleAvailable(door) then
    return Toolkit.unavailable(Doors.TOGGLE_SYMBOL)
  end
  return true
end

--- Unlocking rides the same interaction as the toggle: with the key carried,
--- the game's ToggleDoor is what turns it, exactly as the E key would. The
--- postcondition is the lock reading false afterwards, not the door moving --
--- though vanilla will also have swung it open, which the next observation
--- reports as the door state it is.
local function unlockVerify(_, _, _, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec, code, detail = doorSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local state = marks(ctx, UNLOCK_KEY)
  local door, doorCode, doorDetail = resolveDoor(spec.parsed)
  if door == nil then
    return nil, doorCode, doorDetail
  end
  local lockedAfter = readLocked(door)
  if lockedAfter == nil then
    return nil, reasons.PRECONDITION_FAILED, "the lock state could not be read on this build"
  end
  if lockedAfter == true then
    return nil, reasons.POSTCONDITION_FAILED, "the door still reads locked"
  end
  local evidence = {
    kind = "door_unlocked",
    door_ref = spec.door_ref,
    locked_before = state.locked_before,
    locked_after = lockedAfter,
  }
  if state.unchanged == true then
    evidence.unchanged_is_success = true
  end
  return evidence
end

Unlock = toolkit().declare({
  name = "door.unlock",
  capability = toolkit().CAPABILITY.DOOR_TOGGLE,
  requires = { "getCell", "ISWalkToTimedAction", "ISWalkToTimedAction.new", "ISTimedActionQueue.add" },
  timeout_ms = Doors.TIMEOUT_MS,
  poll_interval_ms = Doors.POLL_MS,
  args = {
    door_ref = { type = ARG.REF, required = true, kinds = OBJECT_KIND },
    radius = { type = ARG.NUMBER, min = Doors.MIN_RADIUS, max = Doors.MAX_RADIUS },
  },
  validate = unlockValidate,
  prepare = makeTogglePrepare(UNLOCK_KEY),
  begin = makeToggleBegin(UNLOCK_KEY),
  progress = makeToggleProgress(UNLOCK_KEY),
  verify = unlockVerify,
})

Doors.Unlock = Unlock
toolkit().register(Unlock)

return Doors
