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
Observe.MAX_WORN = 16
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
function Observe.gameFields()
  local build = PZAgent.Heartbeat.detectBuild()
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
  return fields
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
    runtime_id = readNumber(item, { "getID" }),
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

--- Read one ItemContainer into a container node, recursing into carried bags.
---
--- `depth` is bounded here as well as in ObserveModel: the model refuses a node
--- that is too deep, but the walk that produced it would already have run.
walkItemContainer = function(container, node, hands, depth)
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
  for index = 0, scanned - 1 do
    local item = listGet(items, index)
    if item ~= nil then
      local descriptor = itemFields(item, hands)
      count = count + 1
      node.items[count] = descriptor
      local nested = invoke(item, "getInventory")
      if nested ~= nil and depth < model().MAX_DEPTH then
        descriptor.container = walkItemContainer(nested, {
          kind = model().CONTAINER_KIND.CARRIED,
          runtime_id = descriptor.runtime_id,
          name = descriptor.display_name,
          accessible = true,
        }, hands, depth + 1)
      end
    end
  end
  if size > scanned then
    node.truncated = true
    node.dropped = size - scanned
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
  local roots = {}
  local hands = {
    primary = invoke(player, "getPrimaryHandItem"),
    secondary = invoke(player, "getSecondaryHandItem"),
  }
  local main = invoke(player, "getInventory")
  if main == nil then
    return nil, "the character exposes no main inventory"
  end
  roots[1] = walkItemContainer(main, {
    kind = model().CONTAINER_KIND.PLAYER_MAIN,
    name = "Inventory",
    accessible = true,
  }, hands, 1)

  local worn = invoke(player, "getWornItems")
  local size = listSize(worn)
  if size ~= nil then
    local scanned = math.min(size, Observe.MAX_WORN)
    for index = 0, scanned - 1 do
      local entry = listGet(worn, index)
      local item = invoke(entry, "getItem") or entry
      local slot = readString(entry, { "getLocation", "getBodyLocation" })
      local container = invoke(item, "getInventory")
      if container ~= nil and slot ~= nil then
        roots[#roots + 1] = walkItemContainer(container, {
          kind = model().CONTAINER_KIND.WORN,
          slot = slot,
          runtime_id = readNumber(item, { "getID" }),
          name = readString(item, { "getName", "getDisplayName" }),
          accessible = true,
        }, hands, 1)
      end
    end
  end
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

--- Objects on the squares around the player.
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
  local budget = Observe.MAX_SQUARES
  local count = 0
  for dx = -radius, radius do
    for dy = -radius, radius do
      if budget <= 0 then
        -- One per square nobody looked at. A lower bound on what was missed,
        -- which is the only honest number available without looking.
        result.truncated = true
        result.dropped = result.dropped + 1
      else
        budget = budget - 1
        local x = math.floor(playerPosition.x) + dx
        local y = math.floor(playerPosition.y) + dy
        local square = invoke(cell, "getGridSquare", x, y, playerPosition.z)
        local objects = invoke(square, "getObjects")
        local size = listSize(objects)
        if size ~= nil then
          local scanned = math.min(size, Observe.MAX_OBJECTS_PER_SQUARE)
          if size > scanned then
            result.truncated = true
            result.dropped = result.dropped + (size - scanned)
          end
          local position = { x = x, y = y, z = playerPosition.z }
          local distance = PZAgent.Refs.chebyshevDistance(playerPosition, position)
          for index = 0, scanned - 1 do
            local fields = objectFields(listGet(objects, index), index, position, distance)
            if fields ~= nil then
              count = count + 1
              result.objects[count] = fields
            end
          end
        end
      end
    end
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
          runtime_id = readNumber(zombie, { "getOnlineID", "getID" }),
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
  -- An unreadable inventory costs the section, not the snapshot: the sidecar
  -- reads an absent `inventory` as "not walked". The reason still surfaces,
  -- because an agent that silently stops seeing its own bag looks healthy.
  local roots, rootsError = Observe.inventoryRoots(player)
  if roots == nil and rootsError ~= nil then
    agent.safety.last_error = rootsError
  end
  return {
    session_id = sessionId,
    seq = seq,
    timestamp_ms = nowMs,
    full = true,
    capability_revision = agent.capability_revision,
    active_goal_id = agent.active_goal_id,
    game = Observe.gameFields(),
    player = playerFields,
    inventory = roots,
    nearby = Observe.nearbyFields(player, playerFields.position),
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
    player = PZAgent.Runtime.currentPlayer()
    agent.player = player
  end
  if player == nil then
    return nil, "no player character"
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
