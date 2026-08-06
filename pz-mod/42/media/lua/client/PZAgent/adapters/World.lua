--[[
PZAgent.Adapters.World -- world.inspect.

Invariant: this file only reads. It constructs no timed action, queues nothing,
equips nothing and moves nobody. That is not a stylistic preference -- it is the
reason world.inspect is in PZAgent.Protocol.READ_ONLY_ACTIONS, which lets it run
in OBSERVE mode and without arming. An inspect that walked the character to see
round a corner would be a mutating command wearing a read-only command's
permissions, so the absence of any call to Toolkit.construct or Toolkit.enqueue
below is load-bearing.

Because it only reads, its postcondition is unusual and worth stating plainly:
what a look proves is that a reading was taken, so the evidence *is* the
findings. There is no before/after pair to record, since nothing changed. Every
number in the document comes from an accessor that answered; a field the build
does not expose is absent rather than defaulted, because "light level 0" and
"this build does not report light" are different facts and only one of them
means the room is dark.

Everything is bounded twice: at most Toolkit.MAX_SQUARE_OBJECTS objects are read
from any one square, and at most World.MAX_OBJECTS across the whole reading. A
warehouse floor is exactly the input that would otherwise produce a document too
large to write, and truncation is declared in the evidence rather than hidden.
]]

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

-- Guarded the way Toolkit.lua guards them: the engine loads
-- media/lua/client/**.lua in an order this file does not control, so whichever
-- adapter file the game reads first is the one that creates the registry.
PZAgent.Adapters.ALL = PZAgent.Adapters.ALL or {}
PZAgent.Adapters.BY_NAME = PZAgent.Adapters.BY_NAME or {}

local World = {}
PZAgent.Adapters.World = World

--- Squares out from the centre. Two is a five-by-five block: enough to answer
--- "what is in this room with me", small enough that the reading stays a
--- glance rather than a survey.
World.MAX_RADIUS = 2

--- Objects across the whole reading, however many squares it covers. Roughly
--- three squares' worth of Toolkit.MAX_SQUARE_OBJECTS, which is what a document
--- the sidecar has to parse every tick can carry.
World.MAX_OBJECTS = 96

--- Engine symbol the reading stands on. The per-square accessors are not
--- declared here because they are methods on a Java object rather than globals:
--- PZAgent.CapabilityRuntime can only probe a global, and a method is checked
--- where it is called, by Toolkit.call.
local REQUIRED_SYMBOLS = { "getCell" }

--- Argument kinds, spelled out rather than read from
--- PZAgent.CommandDispatcher.ARG, because the dispatcher is a sibling file and
--- nothing here may dereference a module at load time. A typo costs a refused
--- registration at load rather than a loosened check at dispatch.
local ARG = {
  NUMBER = "number",
  REF = "ref",
}

-- ---------------------------------------------------------------------------
-- reading one object
-- ---------------------------------------------------------------------------

--- True when the engine's own class test says `object` is a `className`.
---
--- `instanceof` is the game's helper and is the only authority worth trusting
--- about a Java class. When the build does not expose it, the caller falls back
--- to classifying by the accessors an object answers to -- weaker, but wrong in
--- the safe direction: a door misread as a plain object is reported as a plain
--- object, never as a door whose state is known.
local function isA(object, className)
  if type(instanceof) ~= "function" then
    return nil
  end
  local ok, result = pcall(instanceof, object, className)
  if not ok then
    return nil
  end
  return result == true
end

World.isA = isA

--- The sprite name of an object, which is what identifies a tile to a human.
local function spriteName(object)
  local Toolkit = PZAgent.Adapters.Toolkit
  local ok, sprite = Toolkit.call(object, "getSprite")
  if not ok or sprite == nil then
    return nil
  end
  return Toolkit.readStringOf(sprite, { "getName" })
end

--- What kind of thing an object is, as far as this build will say.
---
--- Class test first, accessor shape second. `isBarricaded` alone is not enough
--- to call something a door, because a window answers it too; the pair that
--- separates them is a lock (doors) from a smash state (windows).
local function classify(object)
  local Toolkit = PZAgent.Adapters.Toolkit
  -- Build 42: IsoDoor, IsoWindow, IsoThumpable and IsoObject:getContainer().
  if isA(object, "IsoDoor") then
    return "door"
  end
  if isA(object, "IsoWindow") then
    return "window"
  end
  if Toolkit.method(object, "isLocked") ~= nil and Toolkit.method(object, "IsOpen") ~= nil then
    return "door"
  end
  if Toolkit.method(object, "isSmashed") ~= nil then
    return "window"
  end
  if Toolkit.method(object, "getContainer") ~= nil then
    return "container"
  end
  return "object"
end

World.classify = classify

--- The open/locked/barricaded state of a door or window.
---
--- Read for every object rather than only for the ones classify named, so a
--- build that spells IsoDoor differently still reports the state it does
--- expose. A field the object does not answer to is left out.
local function openingState(object, record)
  local Toolkit = PZAgent.Adapters.Toolkit
  -- Build 42: IsoDoor:IsOpen(), :isLocked(), :isBarricaded();
  -- IsoWindow:IsOpen(), :isSmashed(), :isBarricaded().
  record.open = Toolkit.readBooleanOf(object, { "IsOpen", "isOpen" })
  record.locked = Toolkit.readBooleanOf(object, { "isLocked", "isLockedByKey" })
  record.barricaded = Toolkit.readBooleanOf(object, { "isBarricaded" })
  record.smashed = Toolkit.readBooleanOf(object, { "isSmashed" })
  return record
end

--- Describe the container an object holds, without listing what is in it.
---
--- The count and the capacity are what a planner needs to decide whether to
--- look inside; the contents are container.inspect's job and would blow this
--- document's bound on the first shelf. The reference is minted here so the
--- follow-up command has something to name, and it is built by PZAgent.Refs so
--- the two sides agree on where the delimiters fall.
local function containerRecord(ctx, object, x, y, z, objectIndex, record)
  local Toolkit = PZAgent.Adapters.Toolkit
  local ok, container = Toolkit.call(object, "getContainer")
  if not ok or container == nil then
    return record
  end
  record.container_type = Toolkit.readStringOf(container, { "getType" })
  record.container_capacity = Toolkit.readNumberOf(container, { "getCapacity" })
  local items = Toolkit.containerItems(container)
  if items ~= nil then
    record.container_items = items.total
    record.container_truncated = items.truncated == true
  end
  local ref = PZAgent.Refs.worldContainer(ctx.session_id, x, y, z, objectIndex, 0)
  if ref ~= nil then
    record.container_ref = ref
  end
  return record
end

--- One object standing on a square.
local function objectRecord(ctx, object, x, y, z, objectIndex)
  local Toolkit = PZAgent.Adapters.Toolkit
  local record = {
    index = objectIndex,
    name = Toolkit.readStringOf(object, { "getObjectName", "getName" }),
    sprite = spriteName(object),
    kind = classify(object),
  }
  openingState(object, record)
  return containerRecord(ctx, object, x, y, z, objectIndex, record)
end

-- ---------------------------------------------------------------------------
-- reading one square
-- ---------------------------------------------------------------------------

--- The floor tile under a square, or nil when this build does not report one.
local function floorOf(square)
  local Toolkit = PZAgent.Adapters.Toolkit
  -- Build 42: IsoGridSquare:getFloor() -> IsoObject.
  local ok, floor = Toolkit.call(square, "getFloor")
  if not ok or floor == nil then
    return nil
  end
  return spriteName(floor) or Toolkit.readStringOf(floor, { "getObjectName" })
end

--- How lit a square is, on the engine's own scale.
---
--- Absent rather than zero when the accessor is missing: a caller deciding
--- whether the character needs a torch must be able to tell "it is dark" from
--- "nobody measured".
local function lightOf(square, playerIndex)
  local Toolkit = PZAgent.Adapters.Toolkit
  -- Build 42: IsoGridSquare:getLightLevel(playerIndex) -> 0..1.
  local ok, level = Toolkit.call(square, "getLightLevel", playerIndex or 0)
  if not ok or type(level) ~= "number" or level ~= level then
    return nil
  end
  return level
end

--- Everything on one square, bounded by both budgets.
---
--- `budget.objects` is spent across the whole reading, so a single crowded
--- square cannot starve the rest of the block. What was left out is counted, not
--- dropped in silence: a planner that acts on a partial reading has to know it
--- was partial.
local function squareRecord(ctx, x, y, z, budget)
  local Toolkit = PZAgent.Adapters.Toolkit
  local square, missing = Toolkit.gridSquare(x, y, z)
  if square == nil then
    return nil, missing
  end
  local record = {
    ref = PZAgent.Refs.buildSquare(ctx.session_id, x, y, z),
    x = x,
    y = y,
    z = z,
    floor = floorOf(square),
    light = lightOf(square, ctx.player_index),
    objects = {},
  }
  local ok, objects = Toolkit.call(square, "getObjects")
  if not ok or objects == nil then
    record.objects_readable = false
    return record
  end
  record.objects_readable = true
  local size = Toolkit.listSize(objects)
  if size == nil then
    record.objects_readable = false
    return record
  end
  record.object_count = size
  local scanned = math.min(size, Toolkit.MAX_SQUARE_OBJECTS, budget.objects)
  for index = 0, scanned - 1 do
    local object = Toolkit.listGet(objects, index)
    if object ~= nil then
      record.objects[#record.objects + 1] = objectRecord(ctx, object, x, y, z, index)
      budget.objects = budget.objects - 1
    end
  end
  record.truncated = size > scanned
  record.dropped = size - scanned
  return record
end

World.squareRecord = squareRecord

-- ---------------------------------------------------------------------------
-- world.inspect
-- ---------------------------------------------------------------------------

--- The square the reading is centred on.
---
--- With no reference it is the square the character is standing on, which is
--- the only centre an inspect can pick without being told. The floor comes from
--- the character's own z, so "look around me" never reads the storey below.
local function centreOf(ctx, ref)
  local Toolkit = PZAgent.Adapters.Toolkit
  local reasons = Toolkit.reasons()
  if ref ~= nil then
    local parsed, parseError = PZAgent.Refs.parseSquare(ref)
    if parsed == nil then
      return nil, reasons.INVALID_REF, parseError
    end
    return { x = parsed.x, y = parsed.y, z = parsed.z }
  end
  local snapshot = Toolkit.snapshot(ctx)
  if type(snapshot) ~= "table" or type(snapshot.position) ~= "table" then
    return nil, reasons.PRECONDITION_FAILED, "the character reports no position to look around from"
  end
  local position = snapshot.position
  return { x = math.floor(position.x), y = math.floor(position.y), z = position.z }
end

World.centreOf = centreOf

--- Look at a square and its neighbours.
---
--- Read-only and synchronous: there is nothing to wait for, so `start` finishes
--- the command itself and declares no poll.
local Inspect = {
  action = "world.inspect",
  required_symbols = REQUIRED_SYMBOLS,
  args = {
    -- Optional: a reading with no centre is a reading around the character.
    ref = { type = ARG.REF, kinds = { square = true } },
    radius = { type = ARG.NUMBER, integer = true, min = 0, max = World.MAX_RADIUS },
  },
}

function Inspect.start(ctx, args)
  local Toolkit = PZAgent.Adapters.Toolkit
  local reasons = Toolkit.reasons()
  local ready, symbolCode, symbolDetail = Toolkit.requireSymbols(REQUIRED_SYMBOLS)
  if ready == nil then
    return nil, symbolCode, symbolDetail
  end
  local centre, centreCode, centreDetail = centreOf(ctx, args.ref)
  if centre == nil then
    return nil, centreCode, centreDetail
  end
  local radius = args.radius or 1
  local budget = { objects = World.MAX_OBJECTS }
  local squares = {}
  local scanned, loaded, unavailable = 0, 0, nil
  for dx = -radius, radius do
    for dy = -radius, radius do
      scanned = scanned + 1
      local record, missing = squareRecord(ctx, centre.x + dx, centre.y + dy, centre.z, budget)
      if record ~= nil then
        loaded = loaded + 1
        squares[#squares + 1] = record
      elseif missing ~= "IsoCell.getGridSquare" then
        -- The cell itself could not be reached. That is a capability gap rather
        -- than an unloaded chunk, and it makes the whole reading meaningless.
        unavailable = missing
      end
    end
  end
  if unavailable ~= nil then
    return Toolkit.unavailable(unavailable)
  end
  if loaded == 0 then
    -- Not one square answered, so nothing was observed. Reporting an empty
    -- document as a successful look is precisely the fabricated success this
    -- project forbids.
    return nil, reasons.TARGET_NOT_LOADED, "no square around the requested centre is loaded"
  end
  local state = Toolkit.state(ctx)
  state.evidence = {
    centre = centre,
    radius = radius,
    squares = squares,
    squares_scanned = scanned,
    squares_loaded = loaded,
    objects_seen = World.MAX_OBJECTS - budget.objects,
    objects_truncated = budget.objects <= 0,
    observed_at_ms = ctx.now_ms,
  }
  return "done"
end

-- ---------------------------------------------------------------------------
-- registration
-- ---------------------------------------------------------------------------

--- Publish an adapter where the loader that feeds PZAgent.CommandDispatcher
--- looks for it.
---
--- Replacing by action name rather than always appending: the engine reads this
--- file once, but a reload during development would otherwise leave two copies
--- of the same adapter in ALL, and the dispatcher refuses a second registration
--- for an action it already serves.
local function publish(adapter)
  local all = PZAgent.Adapters.ALL
  for index = 1, #all do
    if all[index].action == adapter.action then
      all[index] = adapter
      PZAgent.Adapters.BY_NAME[adapter.action] = adapter
      return adapter
    end
  end
  all[#all + 1] = adapter
  PZAgent.Adapters.BY_NAME[adapter.action] = adapter
  return adapter
end

World.Inspect = publish(Inspect)

return World
