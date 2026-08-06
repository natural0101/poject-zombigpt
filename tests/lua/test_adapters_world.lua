-- PZAgent.Adapters.World: looking, and only looking.
--
-- The first group is the one that matters most. world.inspect is in
-- PZAgent.Protocol.READ_ONLY_ACTIONS, which is what lets it run in OBSERVE mode
-- and without arming, so "it queued nothing and moved nobody" is not a detail of
-- the implementation -- it is the premise the permission rests on. Everything
-- after it checks that the reading is bounded and that a field the build does
-- not expose is absent rather than invented.

local ROOT = arg[0]:match("^(.*)test_adapters_world%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
Support.loadModules(Harness.root, { "World" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local Toolkit = PZ.Adapters.Toolkit
local World = PZ.Adapters.World
local Inspect = World.Inspect
local REASON = PZ.Protocol.REASON

--- The arguments a command would actually reach the adapter with.
local function accept(raw)
  return PZ.CommandDispatcher.checkArgs(Inspect, raw, Support.SESSION)
end

local function refuse(raw)
  local args, code = accept(raw)
  isNil(args, "the arguments are refused")
  return code
end

--- A square that also answers the accessors a reading asks for.
local function squareAt(x, y, z, objects, options)
  options = options or {}
  local square = Support.square(x, y, z, objects)
  if options.floor ~= nil then
    square.getFloor = function()
      return Support.worldObject({ sprite = options.floor })
    end
  end
  if options.light ~= nil then
    square.getLightLevel = function()
      return options.light
    end
  end
  return square
end

--- A door. Locked plus openable is the accessor pair that separates a door from
--- a window on a build where `instanceof` is not available.
local function door(fields)
  fields = fields or {}
  return {
    getObjectName = function()
      return fields.name or "Door"
    end,
    getSprite = function()
      return { getName = function()
        return fields.sprite or "fixtures_doors_01_0"
      end }
    end,
    IsOpen = function()
      return fields.open == true
    end,
    isLocked = function()
      return fields.locked == true
    end,
    isBarricaded = function()
      return fields.barricaded == true
    end,
  }
end

local function window(fields)
  fields = fields or {}
  return {
    getObjectName = function()
      return "Window"
    end,
    IsOpen = function()
      return fields.open == true
    end,
    isSmashed = function()
      return fields.smashed == true
    end,
    isBarricaded = function()
      return fields.barricaded == true
    end,
  }
end

--- `count` plain objects, which is how a warehouse floor is built.
local function crowd(count)
  local objects = {}
  for index = 1, count do
    objects[index] = Support.worldObject({ name = "Shelf" .. tostring(index) })
  end
  return objects
end

--- A character standing at (100.4, 200.6, 0) with `squares` around them.
local function scene(squares, options)
  options = options or {}
  local player = Support.player({ x = options.x or 100.4, y = options.y or 200.6, z = options.z or 0 })
  local map = {}
  for index = 1, #(squares or {}) do
    local square = squares[index]
    map[Support.squareKey(square.getX(), square.getY(), square.getZ())] = square
  end
  Support.installCell(map)
  local ctx = Support.context({ player = player, safety = options.safety })
  return player, ctx
end

--- Every square in a block of `radius` around (x, y, z), each holding `objects`.
local function block(x, y, z, radius, objects)
  local squares = {}
  for dx = -radius, radius do
    for dy = -radius, radius do
      squares[#squares + 1] = squareAt(x + dx, y + dy, z, objects and crowd(objects) or {})
    end
  end
  return squares
end

Harness.group("world.inspect reads and does not touch anything")
do
  local player, ctx = scene({ squareAt(100, 200, 0, { Support.worldObject({ name = "Table" }) }) })
  local walk = Support.installAction("ISWalkToTimedAction")
  local queue = Support.installQueue()
  local outcome = Inspect.start(ctx, accept({}))
  equal(outcome, "done", "a look finishes in start; there is nothing to wait for")
  equal(#queue.added, 0, "nothing was queued")
  equal(#walk.actions, 0, "no timed action was even constructed")
  equal(player.state.x, 100.4, "and the character did not move to see better")
  isNil(Inspect.poll, "a synchronous command declares no poll")
end

Harness.group("world.inspect refuses an argument object it cannot act on")
do
  local code = refuse({ radius = 3 })
  equal(code, REASON.INVALID_ARGUMENT, "a radius past the bound would be a survey, not a glance")

  code = refuse({ radius = 1.5 })
  equal(code, REASON.INVALID_ARGUMENT, "half a square is not a radius")

  code = refuse({ ref = Support.mainRef() })
  equal(code, REASON.INVALID_REF, "an inspect centres on a square, not on a container")

  code = refuse({ ref = Support.squareRef(100, 200, 0, "11111111-2222-3333-4444-555555555555") })
  equal(code, REASON.INVALID_REF, "a reference from another session names a square nobody asked about")

  code = refuse({ ref = Support.squareRef(100, 200, 0), depth = 2 })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  local args = accept({ ref = Support.squareRef(100, 200, 0), radius = 0 })
  equal(args.radius, 0, "a zero radius is a single square, and is accepted")
end

Harness.group("with no reference the reading is centred on the character's own square")
do
  local _, ctx = scene(block(100, 200, 0, 1))
  Inspect.start(ctx, accept({}))
  local evidence = Toolkit.state(ctx).evidence
  equal(evidence.centre.x, 100, "the fractional position is floored to the square it stands on")
  equal(evidence.centre.y, 200, "in both axes")
  equal(evidence.centre.z, 0, "on the character's own floor, never the storey below")
  equal(evidence.radius, 1, "one square out by default")
  equal(evidence.squares_scanned, 9, "which is a three-by-three block")
  equal(evidence.squares_loaded, 9, "all of it loaded here")
  equal(evidence.observed_at_ms, Support.NOW, "and the reading is stamped with when it was taken")
  equal(evidence.squares[1].ref, Support.squareRef(99, 199, 0), "every square carries a reference a plan can name")
end

Harness.group("a named square is read even when the character is elsewhere")
do
  local _, ctx = scene({ squareAt(300, 400, 1, {}, { floor = "floors_01_2", light = 0.25 }) })
  local outcome = Inspect.start(ctx, accept({ ref = Support.squareRef(300, 400, 1), radius = 0 }))
  equal(outcome, "done", "the reading is taken")
  local evidence = Toolkit.state(ctx).evidence
  equal(evidence.squares_scanned, 1, "a zero radius reads exactly one square")
  equal(evidence.squares[1].floor, "floors_01_2", "the floor tile is reported")
  equal(evidence.squares[1].light, 0.25, "and how lit the square is")
  equal(evidence.squares[1].z, 1, "on the floor the reference named")
end

Harness.group("a build that reports no light says nothing rather than dark")
do
  local _, ctx = scene({ squareAt(100, 200, 0) })
  Inspect.start(ctx, accept({ radius = 0 }))
  local square = Toolkit.state(ctx).evidence.squares[1]
  isNil(square.light, "an unmeasured light level is absent, not zero")
  isNil(square.floor, "and so is an unreported floor")
  equal(square.objects_readable, true, "the objects on it were still readable")
end

Harness.group("objects, containers, doors and windows are described by what they answer to")
do
  local crate = Support.worldObject({
    name = "Crate",
    container = Support.container({ Support.item({ id = 1 }), Support.item({ id = 2 }) }),
  })
  local _, ctx = scene({
    squareAt(100, 200, 0, { door({ locked = true }), window({ smashed = true }), crate }),
  })
  Inspect.start(ctx, accept({ radius = 0 }))
  local square = Toolkit.state(ctx).evidence.squares[1]
  equal(square.object_count, 3, "every object on the square was counted")
  equal(#square.objects, 3, "and described")

  local first = square.objects[1]
  equal(first.kind, "door", "a lockable, openable object is a door")
  equal(first.index, 0, "recorded at the zero-based index the engine lists it at")
  equal(first.open, false, "its open state is read")
  equal(first.locked, true, "and its lock")
  equal(first.sprite, "fixtures_doors_01_0", "with the sprite that identifies the tile")
  isNil(first.smashed, "a door answers no smash state, so none is reported")

  local second = square.objects[2]
  equal(second.kind, "window", "an object with a smash state is a window")
  equal(second.smashed, true, "which is read")
  isNil(second.locked, "and it has no lock to report")

  local third = square.objects[3]
  equal(third.kind, "container", "an object holding a container is a container")
  equal(third.container_items, 2, "with the item count a planner needs to decide whether to look inside")
  equal(third.container_capacity, 50, "and its capacity")
  equal(third.container_ref, Support.worldContainerRef(100, 200, 0, 2),
    "plus the reference container.inspect would be called with")
  isNil(third.container_contents, "the contents themselves are container.inspect's job, not this one's")
end

Harness.group("a warehouse floor does not produce an unbounded document")
do
  local _, ctx = scene({ squareAt(100, 200, 0, crowd(40)) })
  Inspect.start(ctx, accept({ radius = 0 }))
  local square = Toolkit.state(ctx).evidence.squares[1]
  equal(#square.objects, Toolkit.MAX_SQUARE_OBJECTS, "one square yields at most the toolkit's bound")
  equal(square.object_count, 40, "the true count is still reported")
  equal(square.truncated, true, "the reading declares that it was cut short")
  equal(square.dropped, 40 - Toolkit.MAX_SQUARE_OBJECTS, "and by how much")

  local _, wide = scene(block(100, 200, 0, 1, 32))
  Inspect.start(wide, accept({ radius = 1 }))
  local evidence = Toolkit.state(wide).evidence
  equal(evidence.objects_seen, World.MAX_OBJECTS, "the whole reading stops at the document bound")
  equal(evidence.objects_truncated, true, "and says that it did")
  equal(evidence.squares_loaded, 9, "every square is still listed, so nothing vanishes silently")
end

Harness.group("a square that cannot be read is reported, not treated as empty")
do
  local bare = {
    getX = function()
      return 100
    end,
    getY = function()
      return 200
    end,
    getZ = function()
      return 0
    end,
  }
  local _, ctx = scene({ bare })
  Inspect.start(ctx, accept({ radius = 0 }))
  local square = Toolkit.state(ctx).evidence.squares[1]
  equal(square.objects_readable, false, "a square whose objects will not list is flagged")
  isNil(square.object_count, "and reports no count it did not take")
  equal(#square.objects, 0, "rather than an empty square")
end

Harness.group("an unloaded chunk is not an empty room")
do
  local _, ctx = scene({})
  local outcome, code, detail = Inspect.start(ctx, accept({ radius = 0 }))
  isNil(outcome, "a reading of nothing is not a successful reading")
  equal(code, REASON.TARGET_NOT_LOADED, "the chunk is simply not there")
  contains(detail, "loaded", "and the detail says so")

  local _, partial = scene({ squareAt(100, 200, 0) })
  Inspect.start(partial, accept({ radius = 1 }))
  local evidence = Toolkit.state(partial).evidence
  equal(evidence.squares_scanned, 9, "the block that was asked for is counted")
  equal(evidence.squares_loaded, 1, "against the squares that actually answered")
end

Harness.group("a missing engine symbol costs one refusal naming the symbol")
do
  local _, ctx = scene({ squareAt(100, 200, 0) })
  Support.removeCell()
  local outcome, code, detail = Inspect.start(ctx, accept({ radius = 0 }))
  isNil(outcome, "without a cell there is nothing to look at")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "which is a capability gap, not an empty world")
  contains(detail, "getCell", "naming the symbol to look for in the game")
end

Harness.group("the adapter declares itself the way the dispatcher demands")
do
  local registry = PZ.CommandDispatcher.new()
  local registered, reason = registry:register(Inspect)
  equal(registered, true, "world.inspect registers: " .. tostring(reason))
  equal(registry:adapterFor("world.inspect"), Inspect, "and the dispatcher resolves the action to it")
  equal(PZ.Adapters.BY_NAME["world.inspect"], Inspect, "it is published under its action name")
  ok(PZ.Protocol.isReadOnly("world.inspect"), "and the protocol agrees that it only reads")

  local classified = World.classify({ getContainer = function()
    return nil
  end })
  equal(classified, "container", "classification goes by the accessors an object answers to")
  equal(World.classify({}), "object", "and an object that answers to none of them stays a plain object")
end

Harness.finish("adapters_world")
