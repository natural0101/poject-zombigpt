--[[
PZAgent.Observe -- the engine-coupled half of the observation.

Invariant: this file reads the game and converts what it read into the plain
Lua values PZAgent.ObserveModel shapes. It decides nothing. Every accessor is
probed before it is called and every call crosses into the engine inside a
pcall, so a build that names a method differently costs one field rather than
the whole snapshot -- and the field it costs is *absent*, never defaulted.
That asymmetry is the point: `PlayerState.hunger` on the sidecar reads a
missing hunger as "not hungry", so a stat this file could not read must not
arrive as a number.

Everything walked here is something the player controls the size of -- the
inventory of a hoarder's base, a horde, a warehouse full of shelves -- so every
loop has a bound, and reaching one is reported to ObserveModel rather than
absorbed.

Nothing here writes to the game. Nothing here emits chat, radio or book text:
display names are carried because the planner and the player both need to know
what an item is called, and they are inert data at every layer above.
]]

PZAgent = PZAgent or {}

local Observe = {}
PZAgent.Observe = Observe

--- Squares scanned around the player, in each direction. The scan is
--- (2R+1)^2 grid lookups on the game thread, so the radius is small and the
--- square budget below caps it again.
Observe.RADIUS = 6

Observe.MAX_SQUARES = 256
Observe.MAX_OBJECTS_PER_SQUARE = 16
Observe.MAX_ZOMBIE_SCAN = 256

--- Objects one nearby scan may read out of the engine, shared across every
--- square it visits.
---
--- The per-square cap does not bound the walk, for the same reason the
--- per-container cap does not bound the inventory: the number of squares is the
--- other factor, and (2R+1)^2 of them at MAX_OBJECTS_PER_SQUARE is thousands of
--- descriptors for a document that keeps ObserveModel.MAX_OBJECTS of them. The
--- rest is engine work on the game thread that the model's sort throws away a
--- moment later, four times a second.
---
--- Four times the document's cap rather than exactly it: the model discards an
--- object whose kind is not a reference-safe token, so a budget of exactly
--- MAX_OBJECTS could leave the snapshot short of what it had room for. Spending
--- it nearest-square-first is what makes the bound safe -- see nearbyObjects.
Observe.MAX_OBJECTS_SCANNED = 256

--- Worn slots inspected. Build 42 gives a fully dressed character rather more
--- than a dozen body locations, so this sits above what a player can wear
--- rather than in the middle of it -- a backpack dropped for being the
--- seventeenth worn item is a bag the agent would simply never see.
Observe.MAX_WORN = 32
Observe.MAX_BODY_PARTS = 24

--- Health scales the game reports out of 100.
local PERCENT = 100

local function model()
  return PZAgent.ObserveModel
end

-- ---------------------------------------------------------------------------
-- probing
--
-- Kahlua exposes Java objects whose members may be absent, and indexing some of
-- them can itself raise. Every lookup and every call therefore goes through
-- these two helpers.
-- ---------------------------------------------------------------------------

local function member(owner, name)
  if owner == nil then
    return nil
  end
  local ok, value = pcall(function()
    return owner[name]
  end)
  if not ok or type(value) ~= "function" then
    return nil
  end
  return value
end

--- Call `owner:name(...)`. Returns the value, or nil plus a reason.
local function invoke(owner, name, ...)
  local method = member(owner, name)
  if method == nil then
    return nil, string.format("%s is not available in this build", tostring(name))
  end
  local ok, result = pcall(method, owner, ...)
  if not ok then
    return nil, string.format("%s failed: %s", tostring(name), tostring(result))
  end
  return result
end

Observe.invoke = invoke

--- The first of `names` that exists and returns a value of `wanted` type.
---
--- Several of the accessors below are spelled differently across builds and
--- across the classes that implement them. Trying a short, closed list is what
--- keeps a rename costing one field instead of the section it belongs to.
local function firstOf(owner, names, wanted)
  for index = 1, #names do
    local value = invoke(owner, names[index])
    if type(value) == wanted then
      return value, names[index]
    end
  end
  return nil
end

local function readNumber(owner, names)
  return firstOf(owner, names, "number")
end

local function readBoolean(owner, names)
  return firstOf(owner, names, "boolean")
end

local function readString(owner, names)
  return firstOf(owner, names, "string")
end

--- The first accessor that answers with a non-negative whole number.
---
--- Identity is read through this rather than through readNumber because Java
--- answers -1 for "there is no id here" -- `getOnlineID` does exactly that
--- outside multiplayer. Taking that -1 would give every zombie in the horde the
--- same reference, so the probe falls through to the next accessor instead.
local function readIdentity(owner, names)
  for index = 1, #names do
    local value = invoke(owner, names[index])
    if type(value) == "number" and value == value and value >= 0 and value < math.huge then
      return math.floor(value)
    end
  end
  return nil
end

--- The size of a Java collection, or nil when it is not one.
local function listSize(list)
  local size = invoke(list, "size")
  if type(size) ~= "number" then
    return nil
  end
  return math.floor(size)
end

--- Entry `index` of a Java collection, which is zero-based.
local function listGet(list, index)
  return (invoke(list, "get", index))
end

--- The name of an enum-like Java object, as a token.
local function enumName(value)
  if type(value) == "string" then
    return value
  end
  return (readString(value, { "name", "toString", "getName" }))
end

-- ---------------------------------------------------------------------------
-- game
-- ---------------------------------------------------------------------------

--- The string that identifies this save, before hashing.
---
--- Never emitted: ObserveModel hashes it, because the raw value is a directory
--- fragment that can carry the profile name (§3.13).
function Observe.saveKey()
  if type(getWorld) ~= "function" then
    return nil
  end
  local ok, world = pcall(getWorld)
  if not ok or world == nil then
    return nil
  end
  local parts = {}
  local count = 0
  for _, name in ipairs({ "getWorld", "getMap", "getSaveFolderName" }) do
    local value = invoke(world, name)
    if type(value) == "string" and #value > 0 then
      count = count + 1
      parts[count] = value
    end
  end
  if count == 0 then
    return nil
  end
  return table.concat(parts, "/")
end

--- Everything the game block needs, as plain values.
---
--- Returns the fields plus the reason the build could not be read, if it could
--- not: ObserveModel refuses an observation with no build, and "the game build
--- is unknown" is a far worse thing to put on the HUD than the probe's own
--- account of which accessor was missing.
function Observe.gameFields()
  local build, buildError = PZAgent.Heartbeat.detectBuild()
  local fields = {
    build = build,
    save_key = Observe.saveKey(),
  }
  if type(getGameTime) == "function" then
    local ok, gameTime = pcall(getGameTime)
    if ok and gameTime ~= nil then
      fields.speed = readNumber(gameTime, { "getTrueMultiplier", "getMultiplier" })
      fields.world_time = model().worldTime({
        year = readNumber(gameTime, { "getYear" }),
        month = readNumber(gameTime, { "getMonth" }),
        day = readNumber(gameTime, { "getDay" }),
        hour = readNumber(gameTime, { "getHour" }),
        minute = readNumber(gameTime, { "getMinutes" }),
      })
      local paused = readBoolean(gameTime, { "isPaused" })
      if paused ~= nil then
        fields.paused = paused
      end
    end
  end
  if fields.paused == nil and fields.speed ~= nil then
    -- The speed control is the only reading of "is time running" this build is
    -- known to expose; zero multiplier is the game's own definition of paused.
    fields.paused = fields.speed <= 0
  end
  return fields, buildError
end

-- ---------------------------------------------------------------------------
-- player
-- ---------------------------------------------------------------------------

local function positionOf(object)
  local x = readNumber(object, { "getX" })
  local y = readNumber(object, { "getY" })
  local z = readNumber(object, { "getZ" })
  if x == nil or y == nil or z == nil then
    return nil
  end
  return { x = x, y = y, z = math.floor(z) }
end

Observe.positionOf = positionOf

--- Character stats. A name whose accessor this build does not expose is left
--- out of the table entirely, never set to a plausible number.
function Observe.playerStats(player)
  local stats = {}
  local body = invoke(player, "getBodyDamage")
  local health = readNumber(body, { "getOverallBodyHealth" })
  if health ~= nil then
    stats.health = health / PERCENT
  end
  local raw = invoke(player, "getStats")
  if raw ~= nil then
    stats.endurance = readNumber(raw, { "getEndurance" })
    stats.hunger = readNumber(raw, { "getHunger" })
    stats.thirst = readNumber(raw, { "getThirst" })
    stats.fatigue = readNumber(raw, { "getFatigue" })
    stats.stress = readNumber(raw, { "getStress" })
    stats.panic = readNumber(raw, { "getPanic" })
  end
  return stats
end

--- Active moodles, keyed by their type name.
---
--- A moodle whose type name cannot be read is skipped: an unnamed level is not
--- something the sidecar can key a decision on, and inventing a name would put
--- a made-up moodle into the planner's view.
function Observe.playerMoodles(player)
  local moodles = {}
  local holder = invoke(player, "getMoodles")
  if holder == nil then
    return moodles
  end
  local count = readNumber(holder, { "getNumMoodles" })
  if count == nil then
    return moodles
  end
  local scanned = math.min(math.floor(count), model().MAX_MOODLES)
  for index = 0, scanned - 1 do
    local level = invoke(holder, "getMoodleLevel", index)
    local name = enumName(invoke(holder, "getMoodleType", index))
    if type(level) == "number" and name ~= nil then
      moodles[name] = level
    end
  end
  return moodles
end

--- One body part, as the flags ObserveModel turns into a wound.
local function bodyPartFields(part, index)
  local name = enumName(invoke(part, "getType")) or string.format("part-%d", index)
  local health = readNumber(part, { "getHealth" })
  local fracture = readNumber(part, { "getFractureTime" })
  local burn = readNumber(part, { "getBurnTime" })
  return {
    part = name,
    bleeding = readBoolean(part, { "bleeding", "isBleeding" }) == true,
    bitten = readBoolean(part, { "bitten", "isBitten" }) == true,
    scratched = readBoolean(part, { "scratched", "isScratched" }) == true,
    cut = readBoolean(part, { "isCut", "haveGlass" }) == true,
    deep_wounded = readBoolean(part, { "isDeepWounded", "deepWounded" }) == true,
    fractured = (fracture ~= nil and fracture > 0) or readBoolean(part, { "isFractured" }) == true,
    burnt = (burn ~= nil and burn > 0) or readBoolean(part, { "isBurnt" }) == true,
    severity = health ~= nil and (PERCENT - health) / PERCENT or nil,
  }
end

--- Every injured body part, bounded.
function Observe.playerWounds(player)
  local wounds = {}
  local count = 0
  local body = invoke(player, "getBodyDamage")
  local parts = invoke(body, "getBodyParts")
  local size = listSize(parts)
  if size == nil then
    return wounds
  end
  local scanned = math.min(size, Observe.MAX_BODY_PARTS)
  for index = 0, scanned - 1 do
    local part = listGet(parts, index)
    if part ~= nil then
      count = count + 1
      wounds[count] = bodyPartFields(part, index)
    end
  end
  return wounds
end

--- The player block's plain values. `alive` is read, not assumed.
function Observe.playerFields(player)
  local position = positionOf(player)
  if position == nil then
    return nil, "the player reports no position"
  end
  local angle = readNumber(player, { "getDirectionAngle", "getForwardDirection" })
  position.direction = model().direction(angle)
  local dead = readBoolean(player, { "isDead" })
  return {
    present = true,
    -- Unknown liveness reads as dead, which suspends mutating work rather than
    -- letting the agent act on a character it cannot confirm is alive.
    alive = dead == false,
    position = position,
    stats = Observe.playerStats(player),
    moodles = Observe.playerMoodles(player),
    wounds = Observe.playerWounds(player),
  }
end

-- ---------------------------------------------------------------------------
-- inventory
-- ---------------------------------------------------------------------------

--- The domain payloads a policy needs, or nil when the item carries none.
local function itemFood(item)
  local hunger = readNumber(item, { "getHungerChange" })
  if hunger == nil then
    return nil
  end
  return {
    hunger_change = hunger,
    thirst_change = readNumber(item, { "getThirstChange" }),
    calories = readNumber(item, { "getCalories" }),
    uses_remaining = readNumber(item, { "getDrainableUsesInt" }),
    cooked = readBoolean(item, { "isCooked" }),
    rotten = readBoolean(item, { "isRotten" }),
    burnt = readBoolean(item, { "isBurnt" }),
    poisonous = readBoolean(item, { "isPoison", "isTaintedWater" }),
  }
end

local function itemLiterature(item)
  local pages = readNumber(item, { "getNumberOfPages" })
  if pages == nil then
    return nil
  end
  return {
    pages = pages,
    pages_read = readNumber(item, { "getAlreadyReadPages" }),
    skill = readString(item, { "getSkillTrained" }),
    skill_level_min = readNumber(item, { "getLvlSkillTrained" }),
    skill_level_max = readNumber(item, { "getMaxLevelTrained" }),
  }
end

local function itemFluid(item)
  local container = invoke(item, "getFluidContainer")
  if container == nil then
    return nil
  end
  return {
    amount = readNumber(container, { "getAmount" }),
    capacity = readNumber(container, { "getCapacity" }),
    tainted = readBoolean(container, { "isTainted" }),
  }
end

--- One item, as the descriptor ObserveModel consumes.
local function itemFields(item, hands)
  local descriptor = {
    runtime_id = readIdentity(item, { "getID" }),
    full_type = readString(item, { "getFullType" }),
    display_name = readString(item, { "getName", "getDisplayName" }),
    category = readString(item, { "getDisplayCategory", "getCategory" }),
    weight = readNumber(item, { "getUnequippedWeight", "getActualWeight", "getWeight" }),
    favorite = readBoolean(item, { "isFavorite" }) == true,
    equipped = readBoolean(item, { "isEquipped" }) == true,
    food = itemFood(item),
    literature = itemLiterature(item),
    fluid = itemFluid(item),
  }
  if hands.primary ~= nil and rawequal(hands.primary, item) then
    descriptor.hand = "primary"
    descriptor.equipped = true
  elseif hands.secondary ~= nil and rawequal(hands.secondary, item) then
    descriptor.hand = "secondary"
    descriptor.equipped = true
  end
  return descriptor
end

local walkItemContainer

--- The budget one inventory walk shares across every container it opens.
---
--- The per-container cap alone does not bound the walk: bags nest, so
--- MAX_ITEMS_PER_CONTAINER items each holding a bag is that many containers
--- again at the next depth, and MAX_DEPTH of that is millions of engine calls
--- on the game thread for a document that keeps sixty-four containers. The
--- totals are the model's own caps, because a container or an item the model
--- would refuse anyway is not worth reading out of the engine.
local function newWalkBudget()
  return {
    containers = model().MAX_CONTAINERS,
    items = model().MAX_ITEMS,
    containers_dropped = 0,
  }
end

--- Read one ItemContainer into a container node, recursing into carried bags.
---
--- `depth` is bounded here as well as in ObserveModel: the model refuses a node
--- that is too deep, but the walk that produced it would already have run. The
--- same reasoning is why `budget` exists -- see newWalkBudget.
walkItemContainer = function(container, node, hands, depth, budget)
  local items = invoke(container, "getItems")
  local size = listSize(items)
  node.capacity = readNumber(container, { "getCapacity", "getMaxWeight" })
  node.used_capacity = readNumber(container, { "getContentsWeight", "getCapacityWeight" })
  node.items = {}
  if size == nil then
    return node
  end
  local limit = model().MAX_ITEMS_PER_CONTAINER
  local scanned = math.min(size, limit)
  local count = 0
  -- Where the shared item budget ran out, if it did. Everything from there to
  -- the end of the engine's list was never looked at, and saying so is what
  -- keeps "the bag holds four things" off a bag whose fifth was never read.
  local stopped = nil
  for index = 0, scanned - 1 do
    if budget.items <= 0 then
      stopped = index
      break
    end
    local item = listGet(items, index)
    if item ~= nil then
      budget.items = budget.items - 1
      local descriptor = itemFields(item, hands)
      count = count + 1
      node.items[count] = descriptor
      local nested = invoke(item, "getInventory")
      if nested ~= nil and depth < model().MAX_DEPTH then
        if budget.containers <= 0 then
          -- The bag is reported as an item; its contents are not reported at
          -- all, which is a container the sidecar must be told about.
          budget.containers_dropped = budget.containers_dropped + 1
        else
          budget.containers = budget.containers - 1
          descriptor.container = walkItemContainer(nested, {
            kind = model().CONTAINER_KIND.CARRIED,
            runtime_id = descriptor.runtime_id,
            name = descriptor.display_name,
            accessible = true,
          }, hands, depth + 1, budget)
        end
      end
    end
  end
  local unread = size - scanned
  if stopped ~= nil then
    unread = size - stopped
  end
  if unread > 0 then
    node.truncated = true
    node.dropped = unread
  end
  return node
end

--- The container roots: the main inventory first, then everything worn.
---
--- Worn and carried stay distinct kinds. A backpack on the back is reachable
--- without unequipping anything; a backpack lying in the main inventory is a
--- different proposition for a transfer, and collapsing the two would make the
--- reference for one resolve to the other.
function Observe.inventoryRoots(player)
  local roots = { containers_dropped = 0 }
  local budget = newWalkBudget()
  local hands = {
    primary = invoke(player, "getPrimaryHandItem"),
    secondary = invoke(player, "getSecondaryHandItem"),
  }
  local main = invoke(player, "getInventory")
  if main == nil then
    return nil, "the character exposes no main inventory"
  end
  budget.containers = budget.containers - 1
  roots[1] = walkItemContainer(main, {
    kind = model().CONTAINER_KIND.PLAYER_MAIN,
    name = "Inventory",
    accessible = true,
  }, hands, 1, budget)

  local worn = invoke(player, "getWornItems")
  local size = listSize(worn)
  if size ~= nil then
    local scanned = math.min(size, Observe.MAX_WORN)
    for index = 0, scanned - 1 do
      local entry = listGet(worn, index)
      local item = invoke(entry, "getItem") or entry
      local slot = readString(entry, { "getLocation", "getBodyLocation" })
      local container = invoke(item, "getInventory")
      if container ~= nil then
        if slot == nil or budget.containers <= 0 then
          -- A worn bag whose slot this build does not name has no reference the
          -- sidecar could resolve, so it cannot be emitted -- but it is a bag
          -- the player is wearing, and a snapshot that simply omitted it would
          -- read as a character carrying nothing on their back.
          budget.containers_dropped = budget.containers_dropped + 1
        else
          budget.containers = budget.containers - 1
          roots[#roots + 1] = walkItemContainer(container, {
            kind = model().CONTAINER_KIND.WORN,
            slot = slot,
            runtime_id = readIdentity(item, { "getID" }),
            name = readString(item, { "getName", "getDisplayName" }),
            accessible = true,
          }, hands, 1, budget)
        end
      end
    end
    if size > scanned then
      -- Worn slots past the cap were never inspected; any of them could have
      -- been a bag, so the count travels rather than the assumption.
      budget.containers_dropped = budget.containers_dropped + (size - scanned)
    end
  end
  roots.containers_dropped = budget.containers_dropped
  return roots
end

-- ---------------------------------------------------------------------------
-- nearby
-- ---------------------------------------------------------------------------

--- One world object, or nil when it is nothing the agent can name.
local function objectFields(object, objectIndex, position, distance)
  local container = invoke(object, "getContainer")
  local semantics = {}
  local kind = nil
  if container ~= nil then
    kind = readString(container, { "getType", "getContainerType" })
    semantics[#semantics + 1] = "container"
  end
  if kind == nil then
    kind = readString(object, { "getObjectName", "getName" })
  end
  local water = readNumber(object, { "getWaterAmount" })
  if water ~= nil and water > 0 then
    semantics[#semantics + 1] = "water_source"
    kind = kind or "water"
  end
  if type(kind) ~= "string" then
    return nil
  end
  local fields = {
    kind = kind:lower(),
    distance = distance,
    x = position.x,
    y = position.y,
    z = position.z,
    semantics = semantics,
  }
  if container ~= nil then
    -- A container object is referenced as a container, because that is the
    -- reference an inventory transfer can actually name.
    fields.object_index = objectIndex
    fields.container_index = 0
  end
  return fields
end

--- Read one square into `result`, spending the walk's shared object budget.
---
--- `stopped` mirrors walkItemContainer: where the shared budget ran out, if it
--- did, so "the square holds four things" is never said of a square whose fifth
--- was not read.
local function scanSquare(cell, playerPosition, x, y, result, budget)
  local square = invoke(cell, "getGridSquare", x, y, playerPosition.z)
  local objects = invoke(square, "getObjects")
  local size = listSize(objects)
  if size == nil then
    return
  end
  local scanned = math.min(size, Observe.MAX_OBJECTS_PER_SQUARE)
  local position = { x = x, y = y, z = playerPosition.z }
  local distance = PZAgent.Refs.chebyshevDistance(playerPosition, position)
  local stopped = nil
  for index = 0, scanned - 1 do
    if budget.objects <= 0 then
      stopped = index
      break
    end
    budget.objects = budget.objects - 1
    local fields = objectFields(listGet(objects, index), index, position, distance)
    if fields ~= nil then
      budget.count = budget.count + 1
      result.objects[budget.count] = fields
    end
  end
  local unread = size - scanned
  if stopped ~= nil then
    unread = size - stopped
  end
  if unread > 0 then
    result.truncated = true
    result.dropped = result.dropped + unread
  end
end

--- Objects on the squares around the player, nearest ring first.
---
--- The order is load-bearing, not cosmetic. The walk spends one shared object
--- budget (Observe.MAX_OBJECTS_SCANNED) across every square, and a budget is
--- only safe to spend if what it runs out on is what the document was going to
--- discard anyway. Raster order starts in the far corner, so a budget spent
--- that way would drop the square under the player's feet; walking outward in
--- Chebyshev rings means the squares that go unread are the farthest, which is
--- exactly the end ObserveModel's nearest-first sort cuts off.
function Observe.nearbyObjects(playerPosition)
  local result = { objects = {}, truncated = false, dropped = 0 }
  if type(getCell) ~= "function" then
    return result
  end
  local ok, cell = pcall(getCell)
  if not ok or cell == nil then
    return result
  end
  local radius = Observe.RADIUS
  local originX = math.floor(playerPosition.x)
  local originY = math.floor(playerPosition.y)
  local budget = { squares = Observe.MAX_SQUARES, objects = Observe.MAX_OBJECTS_SCANNED, count = 0 }
  local visited = 0
  for ring = 0, radius do
    if budget.squares <= 0 or budget.objects <= 0 then
      break
    end
    for dx = -ring, ring do
      for dy = -ring, ring do
        local onRing = dx == -ring or dx == ring or dy == -ring or dy == ring
        if onRing and budget.squares > 0 and budget.objects > 0 then
          budget.squares = budget.squares - 1
          visited = visited + 1
          scanSquare(cell, playerPosition, originX + dx, originY + dy, result, budget)
        end
      end
    end
  end
  local unvisited = (2 * radius + 1) * (2 * radius + 1) - visited
  if unvisited > 0 then
    -- One per square nobody looked at. A lower bound on what was missed, which
    -- is the only honest number available without looking.
    result.truncated = true
    result.dropped = result.dropped + unvisited
  end
  return result
end

--- Zombies within the observation radius.
---
--- `chasing` comes from the zombie's own target, not from its distance: a
--- distant zombie that has noticed the player is a live interrupt and a close
--- one that has not may be safely read past.
function Observe.nearbyZombies(player, playerPosition)
  local result = { zombies = {}, truncated = false, dropped = 0 }
  if type(getCell) ~= "function" then
    return result
  end
  local ok, cell = pcall(getCell)
  if not ok or cell == nil then
    return result
  end
  local list = invoke(cell, "getZombieList")
  local size = listSize(list)
  if size == nil then
    return result
  end
  local scanned = math.min(size, Observe.MAX_ZOMBIE_SCAN)
  if size > scanned then
    result.truncated = true
    result.dropped = size - scanned
  end
  local count = 0
  for index = 0, scanned - 1 do
    local zombie = listGet(list, index)
    local position = positionOf(zombie)
    if position ~= nil then
      local distance = PZAgent.Refs.chebyshevDistance(playerPosition, position)
      if distance <= Observe.RADIUS then
        local chasing = nil
        if member(zombie, "getTarget") ~= nil then
          -- An accessor that answered with no target is a reading of "not
          -- chasing"; an accessor this build does not have at all is not, and
          -- must stay absent so the sidecar is told it could not be read.
          chasing = rawequal(invoke(zombie, "getTarget"), player)
        end
        local visible = nil
        if member(player, "CanSee") ~= nil then
          local seen = invoke(player, "CanSee", zombie)
          if type(seen) == "boolean" then
            visible = seen
          end
        end
        count = count + 1
        result.zombies[count] = {
          runtime_id = readIdentity(zombie, { "getOnlineID", "getID" }),
          distance = distance,
          visible = visible,
          chasing = chasing,
          x = position.x,
          y = position.y,
          z = position.z,
        }
      end
    end
  end
  return result
end

--- The nearby block's plain values.
function Observe.nearbyFields(player, playerPosition)
  local objects = Observe.nearbyObjects(playerPosition)
  local zombies = Observe.nearbyZombies(player, playerPosition)
  return {
    objects = objects.objects,
    objects_truncated = objects.truncated,
    objects_dropped = objects.dropped,
    zombies = zombies.zombies,
    zombies_truncated = zombies.truncated,
    zombies_dropped = zombies.dropped,
  }
end

-- ---------------------------------------------------------------------------
-- the tick
-- ---------------------------------------------------------------------------

--- Assemble the context ObserveModel.build consumes.
function Observe.context(agent, player, sessionId, seq, nowMs)
  local playerFields, playerError = Observe.playerFields(player)
  if playerFields == nil then
    return nil, playerError
  end
  -- The build is the one field ObserveModel refuses to do without, so its own
  -- probe's reason is caught here rather than replaced downstream by the
  -- builder's much vaguer "the game build is unknown".
  local gameFields, buildError = Observe.gameFields()
  if gameFields.build == nil then
    return nil, buildError or "the game build is unknown"
  end
  -- An unreadable inventory costs the section, not the snapshot: the sidecar
  -- reads an absent `inventory` as "not walked". The reason still surfaces,
  -- because an agent that silently stops seeing its own bag looks healthy.
  local roots, rootsError = Observe.inventoryRoots(player)
  if roots == nil and rootsError ~= nil then
    agent.safety.last_error = rootsError
  end
  local nearby = Observe.nearbyFields(player, playerFields.position)
  -- The safety snapshot is taken *after* this, because until it was set here
  -- `danger_level` was written by nothing and read by three consumers: the
  -- mod's own gate in Safety.mayStart, the action engine's threat threshold,
  -- and the compact view the planner sees. All three were told the situation
  -- was calm during a horde. The floor is deliberately coarser than
  -- `pz_agent_core.safety.threat`, which stays authoritative for policy.
  PZAgent.Safety.setDanger(
    agent.safety,
    PZAgent.ObserveModel.dangerFloor(nearby, playerFields.position)
  )
  return {
    session_id = sessionId,
    seq = seq,
    timestamp_ms = nowMs,
    full = true,
    capability_revision = agent.capability_revision,
    active_goal_id = agent.active_goal_id,
    game = gameFields,
    player = playerFields,
    inventory = roots,
    nearby = nearby,
    action = agent.queue_description,
    safety = PZAgent.Safety.snapshot(agent.safety, nowMs),
  }
end

--- Build and publish one full snapshot.
---
--- Returns the document, or nil plus a reason. The reason is recorded on the
--- safety state so it reaches the HUD and the next heartbeat: a mod that stops
--- observing while the sidecar keeps receiving heartbeats would look healthy
--- and be blind.
function Observe.tick(agent, nowMs)
  local function fail(reason)
    agent.safety.last_error = reason
    return nil, reason
  end

  local sessionId = agent.session:id()
  if sessionId == nil then
    return nil, "no open session"
  end
  local player = agent.player
  if player == nil then
    local found, lookupError = PZAgent.Runtime.currentPlayer()
    player = found
    agent.player = found
    if player == nil then
      -- Not recorded on the safety state: a tick between characters is routine,
      -- and the reason is still returned so a caller that cares can say why.
      return nil, lookupError or "no player character"
    end
  end
  local seq, seqError = agent.sequence:next("observation")
  if seq == nil then
    return fail(seqError)
  end
  local context, contextError = Observe.context(agent, player, sessionId, seq, nowMs)
  if context == nil then
    return fail(contextError)
  end
  local document, buildError = PZAgent.ObserveModel.build(context)
  if document == nil then
    return fail(buildError)
  end
  local slot, publishError = agent.ipc:publishSnapshot(document)
  if slot == nil then
    return fail(publishError)
  end
  return document
end

return Observe
