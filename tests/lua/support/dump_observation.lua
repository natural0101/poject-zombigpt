--[[
Print one observation document, built by the mod's own code, as JSON.

This is the observation-direction twin of `dump_adapter_args.lua`, and it exists
because that direction had no such check. Every dead gate this project has found
at the observation seam -- eight of them, over four sweeps -- was found by
reading both sides by hand, because the sidecar's tests build the document they
expect and the mod's tests build the document they emit, and nothing ever put
one side's output into the other side's reader. One of those findings was later
retracted: the producer existed and the checker's regex could not see how it was
written. A checker that runs the producer cannot make that mistake.

The fakes here stand in for the *engine*, never for the document. They answer
exactly the accessor names the mod's own readers ask for -- taken from the
reader's `readNumber(item, { "getX" })` lists -- so what comes out is whatever
`Observe.playerFields` and `ObserveModel.build` actually make of an item that
answers those calls. Nothing about the document's shape is written here.

Run from the repository root:

    lua5.4 tests/lua/support/dump_observation.lua
]]

local ROOT = "tests/lua/"
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/observe_support.lua")

local PZ = Harness.loadModules()
Support.loadModules(Harness.root)

local Model = PZ.ObserveModel
local Json = PZ.Json
local Observe = PZ.Observe

local SESSION = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33"
local NOW = 1700000000000

--- An engine item that answers the food reader's calls, and answers them the
--- way a spoiled, poisonous, burnt meal would. The point is the *unhappy*
--- reading: a document where every hazard is false proves nothing about a
--- sidecar that defaults every missing key to false.
local function rottenMeal()
  local item = Support.item({
    id = 4001,
    full_type = "Base.Sandwich",
    name = "Sandwich",
    category = "Food",
    hunger_change = -20,
    cooked = true,
  })
  item.isRotten = function()
    return true
  end
  item.isBurnt = function()
    return true
  end
  item.isPoison = function()
    return true
  end
  item.isTaintedWater = function()
    return false
  end
  item.getCalories = function()
    return 350
  end
  item.getThirstChange = function()
    return -5
  end
  item.getDrainableUsesInt = function()
    return 3
  end
  return item
end

--- An engine item that answers the literature reader's calls. `getNumberOfPages`
--- is what makes the reader emit a literature block at all.
local function skillBook()
  local item = Support.item({
    id = 4002,
    full_type = "Base.BookCarpentry1",
    name = "Carpentry for Beginners",
    category = "Literature",
    pages = 220,
  })
  item.getAlreadyReadPages = function()
    return 40
  end
  item.getSkillTrained = function()
    return "Woodwork"
  end
  item.getLvlSkillTrained = function()
    return 0
  end
  item.getMaxLevelTrained = function()
    return 2
  end
  return item
end

--- A bottle with water in it, answering the fluid reader's calls.
local function taintedBottle()
  local item = Support.item({
    id = 4003,
    full_type = "Base.WaterBottleFull",
    name = "Water Bottle",
    category = "Container",
  })
  local fluid = {
    getAmount = function()
      return 0.6
    end,
    getCapacity = function()
      return 1.0
    end,
    isTaintedWater = function()
      return true
    end,
  }
  item.getFluidContainer = function()
    return fluid
  end
  return item
end

--- A grid square that answers the square reader's calls. `passable` and `free`
--- are written out rather than defaulted because each is tri-state on the mod's
--- side, and a document where every square answers the same way would not tell
--- a reading apart from a substitution.
local function gridSquare(fields)
  local square = Support.square(fields.objects or {})
  square.isSolid = function()
    return fields.solid == true
  end
  square.isSolidTrans = function()
    return fields.solid_trans == true
  end
  square.isFree = function()
    return fields.free ~= false
  end
  square.getFloor = function()
    return fields.floor ~= false and {} or nil
  end
  return square
end

--- The window `Observe.describeSquares` walks, keyed the way `getGridSquare`
--- is asked for it. One square is deliberately solid: a document in which
--- nothing blocks proves nothing about a reader that must refuse a blocked
--- destination.
local function squareWindow()
  local squares = {}
  for x = 100 - Observe.RADIUS, 100 + Observe.RADIUS do
    for y = 200 - Observe.RADIUS, 200 + Observe.RADIUS do
      squares[string.format("%d,%d,%d", x, y, 0)] = gridSquare({
        solid = (x == 102 and y == 200),
        free = not (x == 101 and y == 200),
      })
    end
  end
  return squares
end

local player = Support.player({
  x = 100.5,
  y = 200.5,
  z = 0,
  angle = 90,
  inventory = Support.container({ rottenMeal(), skillBook(), taintedBottle() }, 30),
})

local removeCell = Support.installCell(squareWindow(), {})

local playerFields = Observe.playerFields(player)
local inventoryRoots = Observe.inventoryRoots(player)
local nearbyFields = Observe.nearbyFields(player, playerFields.position)

local document = Model.build({
  session_id = SESSION,
  seq = 7,
  timestamp_ms = NOW,
  full = true,
  capability_revision = 1,
  game = { build = "42.20", save_key = "Muldraugh, KY/survivor", paused = false, speed = 1 },
  player = playerFields,
  inventory = inventoryRoots,
  nearby = nearbyFields,
  safety = {
    armed = false,
    mode = "OBSERVE",
    danger_level = "none",
    manual_takeover = false,
    sidecar_stale = false,
  },
  action = { ownership = "none", busy = false },
})

removeCell()

print((Json.encode(document)))
