--[[
Support for the in-game adapter tests.

What these doubles prove and what they do not. They prove the *shape* of the
adapters' behaviour: which branch runs, which reason code comes back, whether a
postcondition is checked against an observation or assumed. They prove nothing
about Build 42.20's real API surface -- every class here is a plain Lua table
this file invented, and a method the real engine spells differently would pass
every test below and still be missing in the game. That asymmetry is why each
adapter routes its engine calls through PZAgent.Adapters.Toolkit and declares a
`requires` list: the tests can then remove a symbol and assert the adapter
reports CAPABILITY_UNAVAILABLE naming it, which is the behaviour that survives
being wrong about the API.

Doubles expose exactly the accessors a test asks for, so deleting a field is how
"this build does not have that method" is expressed.

Timed actions are constructed but never run by themselves. A test calls
`record.actions[n]:perform()` to make the world change, which is what keeps the
"the action was queued but nothing happened" case reachable -- and that case is
the one every verify in adapters/ exists to catch.
]]

local Support = {}

local MOD_LUA = "pz-mod/42/media/lua/"

--- Load the adapter modules named by `names`, Compat then Toolkit first.
---
--- Compat comes first because the adapters reference PZAgent.Compat at call
--- time, and loading it here rather than leaning on the shared harness keeps
--- this file self-contained the way the other support files are.
---
--- After Toolkit runs, its require name is marked already loaded: the shipped
--- adapters open with the statement-form `require "PZAgent/adapters/Toolkit"`
--- as a load-order guard for the engine, and under lua5.4 that line must stay
--- a no-op safety net rather than become the load mechanism -- the harness's
--- contract is that every file is executed into the shared global namespace
--- and must not depend on being required.
function Support.loadModules(root, names)
  local prefix = (root or "") .. MOD_LUA
  local paths = {
    prefix .. "shared/PZAgent/Compat.lua",
    prefix .. "client/PZAgent/adapters/Toolkit.lua",
  }
  for index = 1, #(names or {}) do
    paths[#paths + 1] = prefix .. "client/PZAgent/adapters/" .. names[index] .. ".lua"
  end
  for index = 1, #paths do
    local chunk, err = loadfile(paths[index])
    if chunk == nil then
      error("could not load " .. paths[index] .. ": " .. tostring(err), 0)
    end
    chunk()
    if paths[index]:find("Toolkit.lua", 1, true) ~= nil then
      package.loaded["PZAgent/adapters/Toolkit"] = true
    end
  end
  return PZAgent
end

Support.SESSION = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33"
Support.NOW = 1700000000000

-- ---------------------------------------------------------------------------
-- java-ish collections
-- ---------------------------------------------------------------------------

--- A zero-based size()/get() collection, as Kahlua sees a Java list.
function Support.list(entries)
  local items = entries or {}
  return {
    size = function()
      return #items
    end,
    get = function(_, index)
      return items[index + 1]
    end,
    entries = items,
  }
end

function Support.enum(value)
  return {
    name = function()
      return value
    end,
  }
end

-- ---------------------------------------------------------------------------
-- items and containers
-- ---------------------------------------------------------------------------

--- An inventory item. Only the accessors implied by `fields` are defined.
function Support.item(fields)
  fields = fields or {}
  local item = { fields = fields }
  item.getID = function()
    return fields.id
  end
  item.getFullType = function()
    return fields.full_type or "Base.Thing"
  end
  item.getName = function()
    return fields.name or "Thing"
  end
  item.getUnequippedWeight = function()
    return fields.weight or 1
  end
  item.isEquipped = function()
    return fields.equipped == true
  end
  if fields.hunger_change ~= nil then
    item.getHungerChange = function()
      return fields.hunger_change
    end
    item.getBaseHunger = function()
      return fields.base_hunger or fields.hunger_change
    end
    item.isCooked = function()
      return fields.cooked == true
    end
    item.isRotten = function()
      return fields.rotten == true
    end
    item.isBurnt = function()
      return fields.burnt == true
    end
  end
  if fields.thirst_change ~= nil then
    item.getThirstChange = function()
      return fields.thirst_change
    end
  end
  -- Poison and taint read off the *item*, which is a different surface from the
  -- fluid container's `isTainted` below. Granted only when the case names them,
  -- for the usual reason: a build that does not report poison at all has to
  -- stay distinguishable from one that reports false. Their absence here is
  -- what made the mod's poison refusals structurally unreachable from this
  -- harness -- branches no test could enter, so their deletion was invisible.
  if fields.poison ~= nil then
    item.isPoison = function()
      return fields.poison == true
    end
  end
  if fields.tainted_water ~= nil then
    item.isTaintedWater = function()
      return fields.tainted_water == true
    end
  end
  if fields.uses ~= nil then
    item.getDrainableUsesInt = function()
      return fields.uses
    end
  end
  if fields.fluid ~= nil then
    local fluid = {
      getAmount = function()
        return fields.fluid
      end,
      getCapacity = function()
        return fields.fluid_capacity or 1
      end,
      isTainted = function()
        return fields.tainted == true
      end,
    }
    item.getFluidContainer = function()
      return fluid
    end
  end
  if fields.pages ~= nil then
    item.getNumberOfPages = function()
      return fields.pages
    end
    item.getAlreadyReadPages = function()
      return fields.pages_read or 0
    end
  end
  if fields.body_location ~= nil then
    item.getBodyLocation = function()
      return fields.body_location
    end
  end
  if fields.two_handed ~= nil then
    item.isTwoHandWeapon = function()
      return fields.two_handed == true
    end
  end
  -- Weapon-shaped readers, granted only when the case names them: withholding
  -- `condition` is how "this build cannot read the wear" is expressed, and the
  -- combat ranking must exclude such an item rather than rank it as pristine.
  if fields.category ~= nil then
    item.getDisplayCategory = function()
      return fields.category
    end
  end
  if fields.condition ~= nil then
    item.getCondition = function()
      return fields.condition
    end
  end
  if fields.condition_max ~= nil then
    item.getConditionMax = function()
      return fields.condition_max
    end
  end
  if fields.contents ~= nil then
    local container = Support.container(fields.contents, fields.capacity)
    item.getInventory = function()
      return container
    end
    item.container = container
  end
  return item
end

--- An ItemContainer holding `items`. `container.entries` is the live list, so a
--- mock timed action can move an item between two containers.
function Support.container(items, capacity)
  local entries = items or {}
  local container = { entries = entries }
  container.getItems = function()
    return Support.list(entries)
  end
  container.getCapacity = function()
    return capacity or 50
  end
  container.getContentsWeight = function()
    local total = 0
    for index = 1, #entries do
      total = total + (entries[index].fields.weight or 1)
    end
    return total
  end
  container.getType = function()
    return "bag"
  end
  return container
end

--- Move `item` out of `from` and into `into`, as a transfer action would.
function Support.moveItem(item, from, into)
  if from ~= nil then
    for index = 1, #from.entries do
      if from.entries[index] == item then
        table.remove(from.entries, index)
        break
      end
    end
  end
  if into ~= nil then
    into.entries[#into.entries + 1] = item
  end
end

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
-- the character
-- ---------------------------------------------------------------------------

--- Character stats over a mutable table, so a test can move one and watch an
--- adapter's verify notice -- or leave it still and watch verify refuse.
function Support.stats(values)
  local state = values or {}
  return {
    state = state,
    getHunger = function()
      return state.hunger or 0
    end,
    getThirst = function()
      return state.thirst or 0
    end,
    getFatigue = function()
      return state.fatigue or 0
    end,
    getEndurance = function()
      return state.endurance or 1
    end,
    getStress = function()
      return state.stress or 0
    end,
    getPanic = function()
      return state.panic or 0
    end,
  }
end

--- One body part. `state` is mutable so a bandage action can change it.
function Support.bodyPart(fields)
  local state = {
    bleeding = fields.bleeding == true,
    bandaged = fields.bandaged == true,
    deep_wounded = fields.deep_wounded == true,
    health = fields.health or 100,
  }
  local typeEnum = Support.enum(fields.part or "Torso")
  return {
    state = state,
    getType = function()
      return typeEnum
    end,
    isBleeding = function()
      return state.bleeding
    end,
    isBandaged = function()
      return state.bandaged
    end,
    isDeepWounded = function()
      return state.deep_wounded
    end,
    getHealth = function()
      return state.health
    end,
  }
end

function Support.bodyDamage(parts)
  local list = Support.list(parts or {})
  return {
    parts = parts or {},
    getBodyParts = function()
      return list
    end,
    getOverallBodyHealth = function()
      return 100
    end,
  }
end

--- A character. Every section is optional so a test can remove one and assert
--- the adapter reports the gap instead of filling it in.
function Support.player(fields)
  fields = fields or {}
  local state = {
    x = fields.x or 100,
    y = fields.y or 200,
    z = fields.z or 0,
    running = false,
    primary = fields.primary,
    secondary = fields.secondary,
    asleep = fields.asleep == true,
    reading = fields.reading == true,
    traits = fields.traits or {},
  }
  local player = { state = state, fields = fields }
  player.getX = function()
    return state.x
  end
  player.getY = function()
    return state.y
  end
  player.getZ = function()
    return state.z
  end
  player.isDead = function()
    return fields.dead == true
  end
  player.setRunning = function(_, value)
    state.running = value
    return value
  end
  player.isAsleep = function()
    return state.asleep
  end
  player.isReading = function()
    return state.reading
  end
  player.HasTrait = function(_, name)
    return state.traits[name] == true
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
  if fields.inventory ~= nil then
    player.getInventory = function()
      return fields.inventory
    end
  end
  if fields.worn ~= nil then
    local worn = Support.list(fields.worn)
    player.getWornItems = function()
      return worn
    end
  end
  player.getPrimaryHandItem = function()
    return state.primary
  end
  player.getSecondaryHandItem = function()
    return state.secondary
  end
  if fields.vehicle ~= nil then
    player.getVehicle = function()
      return fields.vehicle
    end
  end
  if fields.square ~= nil then
    player.getCurrentSquare = function()
      return fields.square
    end
  end
  return player
end

-- ---------------------------------------------------------------------------
-- the world
-- ---------------------------------------------------------------------------

--- A grid square holding `objects`.
function Support.square(x, y, z, objects)
  local entries = objects or {}
  return {
    objects = entries,
    getX = function()
      return x
    end,
    getY = function()
      return y
    end,
    getZ = function()
      return z
    end,
    getObjects = function()
      return Support.list(entries)
    end,
  }
end

--- A world object, optionally holding a container.
function Support.worldObject(fields)
  fields = fields or {}
  local object = { fields = fields }
  object.getObjectName = function()
    return fields.name or "Object"
  end
  if fields.container ~= nil then
    object.getContainer = function()
      return fields.container
    end
  end
  if fields.sprite ~= nil then
    object.getSprite = function()
      return { getName = function()
        return fields.sprite
      end }
    end
  end
  if fields.water ~= nil then
    object.getWaterAmount = function()
      return fields.water
    end
    object.isTaintedWater = function()
      return fields.tainted == true
    end
  end
  if fields.can_sleep ~= nil then
    object.isBed = function()
      return fields.can_sleep == true
    end
  end
  return object
end

--- An IsoDoor double. State is mutable so the game's own toggle can move it,
--- and each reader exists only when the case grants it, so withholding one is
--- how "this build cannot read that" is expressed.
---
--- ToggleDoor is the vanilla behaviour in miniature: a barricade holds the
--- door, a lock holds it only while it is closed (with the key carried the
--- game unlocks it as part of the interaction, which `toggle_unlocks` stands
--- in for), and an unlocked door swings. `toggle_noop` makes the call return
--- while nothing moves -- the case every door verify exists to catch.
function Support.door(fields)
  fields = fields or {}
  local state = {
    open = fields.open == true,
    locked = fields.locked == true,
    barricaded = fields.barricaded == true,
  }
  local door = { state = state, fields = fields }
  door.getObjectName = function()
    return fields.name or "Door"
  end
  if fields.no_open_reader ~= true then
    door.IsOpen = function()
      return state.open
    end
  end
  if fields.no_lock_reader ~= true then
    door.isLockedByKey = function()
      return state.locked
    end
  end
  if fields.no_barricade_reader ~= true then
    door.isBarricaded = function()
      return state.barricaded
    end
  end
  if fields.north ~= nil then
    door.getNorth = function()
      return fields.north
    end
  end
  if fields.key_id ~= nil then
    door.getKeyId = function()
      return fields.key_id
    end
  end
  if fields.no_toggle ~= true then
    door.ToggleDoor = function()
      if fields.toggle_noop == true then
        return
      end
      if state.barricaded then
        return
      end
      if not state.open and state.locked then
        if fields.toggle_unlocks == true then
          state.locked = false
        else
          return
        end
      end
      state.open = not state.open
    end
  end
  return door
end

--- An IsoZombie double over a mutable `state`, so a mock press can knock it
--- down, start it crawling, kill it, or move it -- and a verify can observe
--- the change. Each body-state reader exists only when the case grants it
--- (`on_floor`, `crawling`, `dead` keys, even as false), so withholding all of
--- them is how "this build reports no body state" is expressed.
function Support.zombie(fields)
  fields = fields or {}
  local state = {
    x = fields.x or 0,
    y = fields.y or 0,
    z = fields.z or 0,
    on_floor = fields.on_floor == true,
    crawling = fields.crawling == true,
    dead = fields.dead == true,
    present = true,
  }
  local zombie = { state = state, fields = fields }
  zombie.getX = function()
    return state.x
  end
  zombie.getY = function()
    return state.y
  end
  zombie.getZ = function()
    return state.z
  end
  zombie.getID = function()
    return fields.id
  end
  if fields.online_id ~= nil then
    zombie.getOnlineID = function()
      return fields.online_id
    end
  end
  if fields.on_floor ~= nil then
    zombie.isOnFloor = function()
      return state.on_floor
    end
  end
  if fields.crawling ~= nil then
    zombie.isCrawling = function()
      return state.crawling
    end
  end
  if fields.dead ~= nil then
    zombie.isDead = function()
      return state.dead
    end
  end
  return zombie
end

local function squareKey(x, y, z)
  return string.format("%d:%d:%d", x, y, z)
end

Support.squareKey = squareKey

--- Install a getCell global backed by `squares`, a map of "x:y:z" to square.
---
--- `zombies` is optional and additive: when given, the cell also answers
--- getZombieList over the doubles whose `state.present` is true, so a mock
--- attack can remove one from the world by flipping the flag. When omitted
--- the cell has no zombie accessor at all, which is how "this build cannot
--- list zombies" is expressed to the combat adapters.
function Support.installCell(squares, zombies)
  local cell = {
    getGridSquare = function(_, x, y, z)
      return squares[squareKey(math.floor(x), math.floor(y), math.floor(z))]
    end,
  }
  if zombies ~= nil then
    cell.getZombieList = function()
      local present = {}
      for index = 1, #zombies do
        local zombie = zombies[index]
        if zombie.state == nil or zombie.state.present ~= false then
          present[#present + 1] = zombie
        end
      end
      return Support.list(present)
    end
  end
  getCell = function()
    return cell
  end
  return cell
end

function Support.removeCell()
  getCell = nil
end

-- ---------------------------------------------------------------------------
-- the action queue and the timed actions
-- ---------------------------------------------------------------------------

--- Install an ISTimedActionQueue that records what was added.
---
--- `options.addFails` makes the add raise, which is the only way to reach the
--- QUEUE_REJECTED branch without a game.
function Support.installQueue(options)
  options = options or {}
  local queue = { queue = {} }
  local record = { added = {}, queue = queue }
  ISTimedActionQueue = {
    getTimedActionQueue = function()
      return queue
    end,
    add = function(action)
      if options.addFails then
        error("mock add failure", 0)
      end
      record.added[#record.added + 1] = action
      queue.queue[#queue.queue + 1] = action
      return action
    end,
    clear = function()
      queue.queue = {}
    end,
  }
  return record
end

function Support.removeQueue()
  ISTimedActionQueue = nil
end

--- Empty the queue, standing in for the game finishing every queued action.
function Support.drainQueue(record)
  record.queue.queue = {}
end

--- Put one entry the mod does not own into the queue.
function Support.queueForeignAction(record)
  record.queue.queue[#record.queue.queue + 1] = { Type = "ISPlayerQueuedAction" }
end

--- Install a timed-action class under `name`.
---
--- The constructed action carries the arguments it was built with and a
--- `perform` the test calls to make the world change. Nothing performs itself:
--- an adapter that verified against an action it merely constructed would
--- report success it never observed.
function Support.installAction(name, onPerform, options)
  options = options or {}
  local record = { actions = {}, name = name }
  local class = {}
  class.new = function(_, ...)
    if options.constructFails then
      error("mock " .. name .. " construction failure", 0)
    end
    if options.constructNil then
      return nil
    end
    local args = { ... }
    local action = { Type = name, args = args }
    action.perform = function(_)
      if onPerform ~= nil then
        onPerform(action, args)
      end
      action.performed = true
      return true
    end
    record.actions[#record.actions + 1] = action
    return action
  end
  _G[name] = class
  return record
end

function Support.removeAction(name)
  _G[name] = nil
end

-- ---------------------------------------------------------------------------
-- the adapter context
-- ---------------------------------------------------------------------------

--- The ctx an adapter is called with. `safety` defaults to a calm, armed state.
function Support.context(options)
  options = options or {}
  local safety = options.safety
  if safety == nil then
    safety = PZAgent.Safety.newState()
    safety.armed = true
    safety.mode = PZAgent.Protocol.MODE.ASSISTED
  end
  return {
    player = options.player,
    session_id = options.session_id or Support.SESSION,
    command_id = options.command_id or "9b2e4f1a",
    now_ms = options.now_ms or Support.NOW,
    safety = safety,
    state = {},
  }
end

--- A safety state that reports the reflex guard has seen a horde.
function Support.threatened()
  local safety = PZAgent.Safety.newState()
  safety.armed = true
  safety.mode = PZAgent.Protocol.MODE.ASSISTED
  PZAgent.Safety.setDanger(safety, PZAgent.Protocol.DANGER.HIGH)
  return safety
end

--- A safety state that reports the player has taken the controls.
function Support.takenOver()
  local safety = PZAgent.Safety.newState()
  safety.armed = true
  safety.mode = PZAgent.Protocol.MODE.ASSISTED
  safety.manual_takeover = true
  safety.takeover_reason = "movement"
  return safety
end

-- ---------------------------------------------------------------------------
-- references
-- ---------------------------------------------------------------------------

function Support.squareRef(x, y, z, session)
  return (PZAgent.Refs.buildSquare(session or Support.SESSION, x, y, z))
end

function Support.mainRef(session)
  return (PZAgent.Refs.playerMainContainer(session or Support.SESSION))
end

function Support.itemRef(containerTail, runtimeId, session)
  return (PZAgent.Refs.buildItem(session or Support.SESSION, containerTail, tostring(runtimeId), 0))
end

function Support.worldContainerRef(x, y, z, objectIndex, session)
  return (PZAgent.Refs.worldContainer(session or Support.SESSION, x, y, z, objectIndex, 0))
end

function Support.objectRef(x, y, z, objectIndex, session)
  return (PZAgent.Refs.buildObject(session or Support.SESSION, x, y, z, objectIndex))
end

function Support.zombieRef(runtimeId, session)
  return (PZAgent.Refs.buildZombie(session or Support.SESSION, tostring(runtimeId), 0))
end

return Support
