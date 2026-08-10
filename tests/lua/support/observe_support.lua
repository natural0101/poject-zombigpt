--[[
Support for the observation tests.

Two jobs. First, load the two modules the observation is built from: the shared
harness deliberately lists only the modules that existed when it was written,
and adding to that list from here keeps this task's files self-contained.

Second, stand in for the parts of the engine PZAgent.Observe reads. The doubles
are plain Lua tables whose methods are the accessors the reader probes for, so a
test can remove one method and watch the reader report the field as absent
rather than invent it -- which is the behaviour worth testing, and the one that
cannot be observed from a double that always answers.
]]

local Support = {}

local MOD_LUA = "pz-mod/42/media/lua/"

local MODULES = {
  -- Compat first, mirroring the game's shared-before-client order, so an
  -- observation module that reaches for PZAgent.Compat at call time finds it.
  "shared/PZAgent/Compat.lua",
  "shared/PZAgent/ObserveModel.lua",
  "client/PZAgent/Observe.lua",
}

--- Load the observation modules into the global PZAgent namespace.
function Support.loadModules(root)
  for index = 1, #MODULES do
    local path = (root or "") .. MOD_LUA .. MODULES[index]
    local chunk, err = loadfile(path)
    if chunk == nil then
      error("could not load " .. path .. ": " .. tostring(err), 0)
    end
    chunk()
  end
  return PZAgent
end

-- ---------------------------------------------------------------------------
-- java-ish collections
-- ---------------------------------------------------------------------------

--- A zero-based, size()/get() collection, as Kahlua sees a Java list.
function Support.list(entries)
  local items = entries or {}
  return {
    size = function()
      return #items
    end,
    get = function(_, index)
      return items[index + 1]
    end,
  }
end

--- An object whose `name()` returns `value`, standing in for a Java enum.
function Support.enum(value)
  return {
    name = function()
      return value
    end,
  }
end

-- ---------------------------------------------------------------------------
-- inventory doubles
-- ---------------------------------------------------------------------------

--- An inventory item. `fields` may carry any of the accessors the reader probes.
function Support.item(fields)
  local item = {
    getID = function()
      return fields.id
    end,
    getFullType = function()
      return fields.full_type or "Base.Thing"
    end,
    getName = function()
      return fields.name or "Thing"
    end,
    getDisplayCategory = function()
      return fields.category or "Item"
    end,
    getUnequippedWeight = function()
      return fields.weight or 1
    end,
    isFavorite = function()
      return fields.favorite == true
    end,
    isEquipped = function()
      return fields.equipped == true
    end,
  }
  if fields.hunger_change ~= nil then
    item.getHungerChange = function()
      return fields.hunger_change
    end
    item.isCooked = function()
      return fields.cooked == true
    end
  end
  if fields.pages ~= nil then
    item.getNumberOfPages = function()
      return fields.pages
    end
  end
  if fields.contents ~= nil then
    local container = Support.container(fields.contents, fields.capacity)
    item.getInventory = function()
      return container
    end
  end
  return item
end

--- An ItemContainer holding `items`.
function Support.container(items, capacity)
  local list = Support.list(items)
  return {
    getItems = function()
      return list
    end,
    getCapacity = function()
      return capacity or 10
    end,
    getContentsWeight = function()
      return 1.5
    end,
  }
end

--- A WornItem entry: a slot name plus the item worn in it.
function Support.worn(slot, item)
  return {
    getLocation = function()
      return slot
    end,
    getItem = function()
      return item
    end,
  }
end

-- ---------------------------------------------------------------------------
-- player double
-- ---------------------------------------------------------------------------

--- A character. Every section is optional so a test can remove one and assert
--- the reader omits it instead of filling it in.
function Support.player(fields)
  fields = fields or {}
  local player = {
    getX = function()
      return fields.x or 100.5
    end,
    getY = function()
      return fields.y or 200.5
    end,
    getZ = function()
      return fields.z or 0
    end,
    isDead = function()
      return fields.dead == true
    end,
  }
  if fields.angle ~= nil then
    player.getDirectionAngle = function()
      return fields.angle
    end
  end
  if fields.stats ~= nil then
    player.getStats = function()
      return fields.stats
    end
  end
  if fields.body ~= nil then
    player.getBodyDamage = function()
      return fields.body
    end
  end
  if fields.moodles ~= nil then
    player.getMoodles = function()
      return fields.moodles
    end
  end
  if fields.inventory ~= nil then
    player.getInventory = function()
      return fields.inventory
    end
  end
  if fields.worn ~= nil then
    local list = Support.list(fields.worn)
    player.getWornItems = function()
      return list
    end
  end
  if fields.primary ~= nil then
    player.getPrimaryHandItem = function()
      return fields.primary
    end
  end
  if fields.secondary ~= nil then
    player.getSecondaryHandItem = function()
      return fields.secondary
    end
  end
  if fields.can_see ~= nil then
    player.CanSee = function()
      return fields.can_see
    end
  end
  return player
end

--- A Stats object exposing only the accessors named in `values`.
function Support.stats(values)
  local names = {
    endurance = "getEndurance",
    hunger = "getHunger",
    thirst = "getThirst",
    fatigue = "getFatigue",
    stress = "getStress",
    panic = "getPanic",
  }
  local stats = {}
  for key, accessor in pairs(names) do
    local value = values[key]
    if value ~= nil then
      stats[accessor] = function()
        return value
      end
    end
  end
  return stats
end

--- A BodyDamage object over `parts`.
function Support.bodyDamage(parts, overallHealth)
  local list = Support.list(parts)
  local body = {
    getBodyParts = function()
      return list
    end,
  }
  if overallHealth ~= nil then
    body.getOverallBodyHealth = function()
      return overallHealth
    end
  end
  return body
end

--- One body part. `fields` names the injuries the part reports.
function Support.bodyPart(fields)
  local part = {
    getHealth = function()
      return fields.health or 100
    end,
  }
  if fields.part ~= nil then
    part.getType = function()
      return Support.enum(fields.part)
    end
  end
  local flags = {
    bleeding = "bleeding",
    bitten = "bitten",
    scratched = "scratched",
    deep_wounded = "isDeepWounded",
  }
  for key, accessor in pairs(flags) do
    if fields[key] ~= nil then
      local value = fields[key]
      part[accessor] = function()
        return value
      end
    end
  end
  return part
end

--- A Moodles object over `entries`, an array of {name, level} pairs.
function Support.moodles(entries)
  return {
    getNumMoodles = function()
      return #entries
    end,
    getMoodleLevel = function(_, index)
      return entries[index + 1][2]
    end,
    getMoodleType = function(_, index)
      return Support.enum(entries[index + 1][1])
    end,
  }
end

-- ---------------------------------------------------------------------------
-- world doubles
-- ---------------------------------------------------------------------------

--- A world object on a square.
function Support.worldObject(fields)
  local object = {}
  if fields.name ~= nil then
    object.getObjectName = function()
      return fields.name
    end
  end
  if fields.container_type ~= nil then
    local container = {
      getType = function()
        return fields.container_type
      end,
    }
    object.getContainer = function()
      return container
    end
  end
  if fields.water ~= nil then
    object.getWaterAmount = function()
      return fields.water
    end
  end
  return object
end

--- A zombie at (x, y, z). `target` is what it is chasing, if anything.
function Support.zombie(fields)
  local zombie = {
    getX = function()
      return fields.x
    end,
    getY = function()
      return fields.y
    end,
    getZ = function()
      return fields.z or 0
    end,
    getID = function()
      return fields.id
    end,
  }
  if fields.online_id ~= nil then
    zombie.getOnlineID = function()
      return fields.online_id
    end
  end
  if fields.has_target then
    zombie.getTarget = function()
      return fields.target
    end
  end
  return zombie
end

--- A bag holding `width` bags, `depth` levels deep. Every item is a container,
--- which is the shape that makes an unbounded inventory walk explode.
function Support.bagTree(depth, width, nextId)
  local contents = {}
  if depth > 0 then
    for index = 1, width do
      local child
      child, nextId = Support.bagTree(depth - 1, width, nextId)
      contents[index] = child
    end
  end
  nextId = nextId + 1
  return Support.item({ id = nextId, name = "Bag", contents = contents }), nextId
end

--- Install a `getCell` global returning squares from `squares`, keyed "x,y,z",
--- plus a zombie list. Returns a function that removes it again.
function Support.installCell(squares, zombies)
  local cell = {
    getGridSquare = function(_, x, y, z)
      return squares[string.format("%d,%d,%d", x, y, z)]
    end,
    getZombieList = function()
      return Support.list(zombies or {})
    end,
  }
  getCell = function()
    return cell
  end
  return function()
    getCell = nil
  end
end

--- A grid square holding `objects`.
function Support.square(objects)
  local list = Support.list(objects)
  return {
    getObjects = function()
      return list
    end,
  }
end

--- Install a `getGameTime` global reporting `fields`.
function Support.installGameTime(fields)
  local gameTime = {
    getMultiplier = function()
      return fields.speed or 1
    end,
    getYear = function()
      return fields.year or 1993
    end,
    getMonth = function()
      return fields.month or 6
    end,
    getDay = function()
      return fields.day or 9
    end,
    getHour = function()
      return fields.hour or 13
    end,
    getMinutes = function()
      return fields.minute or 45
    end,
  }
  if fields.paused ~= nil then
    gameTime.isPaused = function()
      return fields.paused
    end
  end
  getGameTime = function()
    return gameTime
  end
  return function()
    getGameTime = nil
  end
end

--- Install a `getWorld` global whose save identity is `name`.
function Support.installWorld(name)
  local world = {
    getWorld = function()
      return name
    end,
  }
  getWorld = function()
    return world
  end
  return function()
    getWorld = nil
  end
end

return Support
