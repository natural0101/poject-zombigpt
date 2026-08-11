--[[
PZAgent.Adapters.Toolkit -- the one place in adapters/ that touches the game.

Invariant: no adapter resolves an engine symbol, constructs a timed action or
reads a Java object except through this file. Every lookup is probed and every
call crosses into the engine inside a pcall, so a build that spells a class or a
method differently costs one CAPABILITY_UNAVAILABLE *naming the symbol* rather
than an error unwinding the dispatcher mid-command.

The second invariant is narrower and just as load-bearing: the only mutating
call this file makes is ISTimedActionQueue.add, and it makes it only after
PZAgent.Ownership has stamped the action with this session's tag. An untagged
action of ours is indistinguishable from work the player queued by hand, and
PZAgent.Safety would then -- correctly -- refuse to cancel it on a panic stop.
Nothing here writes a stat, a coordinate or a body-part flag: the game's own
timed actions are the only way an adapter changes the world.

Snapshots are taken here rather than in each adapter because "what changed" is
the only thing that may promote a command to succeeded, and a snapshot an
adapter built for itself could quietly grow a field the adapter also wrote.
]]

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Toolkit = {}
PZAgent.Adapters.Toolkit = Toolkit

--- Registry the dispatcher reads. Each adapter file appends to it as it loads,
--- which is the same "every file populates PZAgent.*" contract the engine loads
--- the rest of the mod under.
PZAgent.Adapters.ALL = PZAgent.Adapters.ALL or {}
PZAgent.Adapters.BY_NAME = PZAgent.Adapters.BY_NAME or {}

--- Adapters whose action name is implemented here but is not yet in
--- PZAgent.Protocol.ACTION_NAMES. They are registered so the dispatcher can see
--- them, and PZAgent.Safety.mayStart still refuses them -- an action outside the
--- protocol whitelist is rejected before dispatch, which is the correct
--- direction to be wrong in while the wire contract catches up.
PZAgent.Adapters.PENDING_PROTOCOL = PZAgent.Adapters.PENDING_PROTOCOL or {}

-- Bounds. Every walk below is over something the player controls the size of --
-- a hoarder's base, a warehouse shelf, a crate of nails -- so each one stops.
Toolkit.MAX_SNAPSHOT_ITEMS = 256
Toolkit.MAX_SNAPSHOT_DEPTH = 3
Toolkit.MAX_WORN = 32
Toolkit.MAX_CONTAINER_ITEMS = 256
Toolkit.MAX_BODY_PARTS = 24
Toolkit.MAX_SQUARE_OBJECTS = 32

--- A measure that has not improved for this long is stalled. Chosen well above
--- the longest single pathfinding hitch a walk action takes to re-plan, so a
--- character rounding a corner is not called stuck.
Toolkit.DEFAULT_STALL_MS = 6000

--- Capability keys, spelled exactly as pz_agent_core.capabilities.probes spells
--- them. The sidecar gates its write tools on these strings, so a typo here
--- would open an ungated path rather than fail loudly.
--- Every probe in that module, not a subset. This table held six of the twelve
--- for a while and five adapters read the omission as "no probe exists" -- their
--- comments said so -- so those actions published no capability at all and the
--- mod's capability document named six where the system knows twelve.
--- `autonomous_attack` is the one deliberate absence: no adapter implements it,
--- because §12.4 makes it permanently unsupported. `combat_assist` is not that
--- capability and must never be mistaken for it: it is the ASSISTED rung --
--- four bounded, user-commanded actions in adapters/Combat.lua -- and adding it
--- here neither touches the autonomous_attack probe nor raises its ceiling.
Toolkit.CAPABILITY = {
  MOVE_TO_SQUARE = "move_to_square",
  INVENTORY_TRANSFER = "inventory_transfer",
  EAT_PERCENTAGE = "eat_percentage",
  DRINK_CARRIED = "drink_carried",
  READ_LITERATURE = "read_literature",
  DRINK_WORLD_SOURCE = "drink_world_source",
  EQUIPMENT_EQUIP = "equipment_equip",
  EQUIPMENT_UNEQUIP = "equipment_unequip",
  MEDICAL_BANDAGE = "medical_bandage",
  SURVIVAL_REST = "survival_rest",
  SURVIVAL_SLEEP = "survival_sleep",
  DOOR_TOGGLE = "door_toggle",
  COMBAT_ASSIST = "combat_assist",
}

--- Health scale the game reports body parts on.
local PERCENT = 100

local function reasons()
  return PZAgent.Protocol.REASON
end

Toolkit.reasons = reasons

-- ---------------------------------------------------------------------------
-- probing
-- ---------------------------------------------------------------------------

--- Refusal for a symbol this build does not expose, naming the symbol.
function Toolkit.unavailable(symbol)
  return nil, reasons().CAPABILITY_UNAVAILABLE, string.format("%s is not available in this build", tostring(symbol))
end

--- The value of a global, or nil. Indexing the environment is itself guarded
--- because Kahlua's global table is backed by the engine.
local function globalNamed(name)
  local ok, value = pcall(function()
    return _G[name]
  end)
  if not ok then
    return nil
  end
  return value
end

Toolkit.globalNamed = globalNamed

--- `owner[name]` when it is a function, else nil. Some Java-backed objects
--- raise on an unknown member, so the read is guarded rather than compared.
local function method(owner, name)
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

Toolkit.method = method

local function pcallWith(fn, owner, ...)
  return pcall(fn, owner, ...)
end

--- Call `owner:name(...)`. Returns ok, value -- and on a false ok, `value` is
--- the reason. Two returns rather than one because a method that legitimately
--- answers nil and a method that is missing are different facts.
function Toolkit.call(owner, name, ...)
  local fn = method(owner, name)
  if fn == nil then
    return false, string.format("%s is not available in this build", tostring(name))
  end
  local ok, result = pcallWith(fn, owner, ...)
  if not ok then
    return false, string.format("%s failed: %s", tostring(name), tostring(result))
  end
  return true, result
end

--- The first accessor in `names` that answers with a `wanted`-typed value.
function Toolkit.read(owner, names, wanted)
  for index = 1, #names do
    local ok, value = Toolkit.call(owner, names[index])
    if ok and type(value) == wanted then
      return value, names[index]
    end
  end
  return nil
end

local function readNumberOf(owner, names)
  return (Toolkit.read(owner, names, "number"))
end

local function readBooleanOf(owner, names)
  return (Toolkit.read(owner, names, "boolean"))
end

local function readStringOf(owner, names)
  return (Toolkit.read(owner, names, "string"))
end

Toolkit.readNumberOf = readNumberOf
Toolkit.readBooleanOf = readBooleanOf
Toolkit.readStringOf = readStringOf

--- A non-negative whole identity, skipping the -1 Java answers for "no id".
function Toolkit.readIdentity(owner, names)
  for index = 1, #names do
    local ok, value = Toolkit.call(owner, names[index])
    if ok and type(value) == "number" and value == value and value >= 0 and value < math.huge then
      return math.floor(value)
    end
  end
  return nil
end

local function listSize(list)
  local size = readNumberOf(list, { "size" })
  if size == nil then
    return nil
  end
  return math.floor(size)
end

local function listGet(list, index)
  local ok, value = Toolkit.call(list, "get", index)
  if not ok then
    return nil
  end
  return value
end

Toolkit.listSize = listSize
Toolkit.listGet = listGet

--- True when the dotted `path` names something usable.
---
--- "ISWalkToTimedAction" asks only that the global exists; "ISWalkToTimedAction.new"
--- also asks that the member is callable, so a forward declaration with no
--- constructor does not count as a working API.
function Toolkit.symbolPresent(path)
  local head, tail = path:match("^([^%.]+)%.([^%.]+)$")
  if head == nil then
    return globalNamed(path) ~= nil
  end
  local owner = globalNamed(head)
  if type(owner) ~= "table" then
    return false
  end
  local ok, value = pcall(function()
    return owner[tail]
  end)
  return ok and type(value) == "function"
end

--- Check an adapter's whole `requires` list, naming the first symbol missing.
function Toolkit.requireSymbols(required)
  if type(required) ~= "table" then
    return true
  end
  for index = 1, #required do
    if not Toolkit.symbolPresent(required[index]) then
      return Toolkit.unavailable(required[index])
    end
  end
  return true
end

--- Construct `ClassName:new(...)`, guarded end to end.
function Toolkit.construct(className, ...)
  local class = globalNamed(className)
  if type(class) ~= "table" then
    return Toolkit.unavailable(className)
  end
  local new = method(class, "new")
  if new == nil then
    return Toolkit.unavailable(className .. ".new")
  end
  local ok, instance = pcallWith(new, class, ...)
  if not ok then
    return nil, reasons().INTERNAL_ERROR, string.format("%s:new failed: %s", className, tostring(instance))
  end
  if instance == nil then
    return nil, reasons().QUEUE_REJECTED, string.format("%s:new produced no action", className)
  end
  return instance
end

--- Construct the first of `names` this build actually has.
---
--- The same idea as probing a closed list of accessor spellings: a couple of
--- these classes are spelled differently across builds and across the mods that
--- reimplement them, and trying a short closed list is what keeps a rename
--- costing nothing. When none of them exists the refusal names every candidate,
--- so the report says what was looked for rather than only the first guess.
function Toolkit.constructFirst(names, ...)
  for index = 1, #names do
    if Toolkit.symbolPresent(names[index] .. ".new") then
      return Toolkit.construct(names[index], ...)
    end
  end
  return Toolkit.unavailable(table.concat(names, " / "))
end

-- ---------------------------------------------------------------------------
-- the action queue
-- ---------------------------------------------------------------------------

--- Stamp `action` as this session's and hand it to the game's queue.
---
--- The tag goes on before the add, never after: an action that reached the
--- queue untagged is one a panic stop would have to leave alone.
function Toolkit.enqueue(ctx, action)
  if type(action) ~= "table" then
    return nil, reasons().INTERNAL_ERROR, "there is no timed action to enqueue"
  end
  local tag, tagError = PZAgent.Ownership.tagFor(ctx.session_id, ctx.command_id)
  if tag == nil then
    return nil, reasons().INTERNAL_ERROR, tagError
  end
  local tagged = pcall(function()
    action.pzAgentTag = tag
    action.pzAgentSession = ctx.session_id
  end)
  if not tagged then
    return nil, reasons().INTERNAL_ERROR, "the timed action refused the ownership tag"
  end
  if type(ISTimedActionQueue) ~= "table" or type(ISTimedActionQueue.add) ~= "function" then
    return Toolkit.unavailable("ISTimedActionQueue.add")
  end
  local ok, err = pcall(ISTimedActionQueue.add, action)
  if not ok then
    return nil, reasons().QUEUE_REJECTED, "the action queue refused the action: " .. tostring(err)
  end
  return true
end

--- Why work must stop right now, or nil when it need not.
---
--- Consulted at the top of every prepare, start and progress. Takeover outranks
--- threat because the two can be true at once and the player's decision is the
--- one that has to be reported.
function Toolkit.interruption(ctx)
  local safety = ctx.safety
  if type(safety) ~= "table" then
    return nil
  end
  if safety.manual_takeover == true then
    return reasons().USER_TAKEOVER, "the player has taken over"
  end
  local rank = PZAgent.Protocol.dangerRank(safety.danger_level) or 0
  local block = safety.danger_block_rank or PZAgent.Safety.DANGER_BLOCK_RANK
  if rank >= block then
    return reasons().THREAT_INTERRUPTED, string.format("danger level is %s", tostring(safety.danger_level))
  end
  return nil
end

--- The default progress signal: is the queue still running work that is ours?
---
--- An unreadable queue is not "the action finished". It is reported as the
--- capability gap it is, because the alternative -- calling the postcondition
--- ready to check -- would let verify run against a character mid-action.
function Toolkit.queueProgress(ctx)
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local entries, _, queueError = PZAgent.Safety.describeQueue(ctx.player)
  if entries == nil then
    return nil, reasons().CAPABILITY_UNAVAILABLE, queueError or "the action queue could not be read"
  end
  local description = PZAgent.Ownership.describe(entries, ctx.session_id)
  if description.total == 0 then
    return "done"
  end
  if description.ownership ~= PZAgent.Protocol.OWNERSHIP.MOD then
    return nil, reasons().PLAYER_BUSY_MANUAL_ACTION, "the action queue holds work the mod does not own"
  end
  return "running"
end

-- ---------------------------------------------------------------------------
-- per-command scratch state
-- ---------------------------------------------------------------------------

--- The table an adapter carries resolved handles and progress marks in.
---
--- Created on the context when the runtime did not supply one, so an adapter
--- never has to cope with its own scratch space being absent. It is per-command
--- because a handle resolved for one command must never be reused by the next:
--- the runtime id it came from may denote a different object by then.
function Toolkit.state(ctx)
  local state = ctx.state
  if type(state) ~= "table" then
    state = {}
    ctx.state = state
  end
  return state
end

--- Track a measure that is supposed to keep improving.
---
--- Returns "improving" or "stalled" plus the milliseconds since the last
--- improvement. This is what turns "the walk action is still queued" into a
--- real stuck check: a character wedged against a fence keeps its queue entry
--- alive forever and only the distance stops falling.
function Toolkit.trackProgress(ctx, key, value, options)
  options = options or {}
  local state = Toolkit.state(ctx)
  local marks = state.progress_marks
  if marks == nil then
    marks = {}
    state.progress_marks = marks
  end
  local nowMs = ctx.now_ms or 0
  local epsilon = options.epsilon or 0.05
  local descending = options.direction ~= "up"
  local mark = marks[key]
  if mark == nil then
    marks[key] = { best = value, since_ms = nowMs }
    return "improving", 0
  end
  local improved
  if descending then
    improved = value < (mark.best - epsilon)
  else
    improved = value > (mark.best + epsilon)
  end
  if improved then
    mark.best = value
    mark.since_ms = nowMs
    return "improving", 0
  end
  local elapsed = nowMs - mark.since_ms
  if elapsed >= (options.stall_ms or Toolkit.DEFAULT_STALL_MS) then
    return "stalled", elapsed
  end
  return "improving", elapsed
end

-- ---------------------------------------------------------------------------
-- arguments
-- ---------------------------------------------------------------------------

local function invalid(detail)
  return nil, reasons().INVALID_ARGUMENT, detail
end

Toolkit.invalidArgument = invalid

--- Refuse an argument object with unknown or missing keys.
---
--- Unknown keys are refused rather than ignored: a misspelled `radius` that is
--- silently dropped runs the action with a default nobody asked for.
function Toolkit.checkArgs(args, allowed, required)
  if type(args) ~= "table" then
    return invalid("args must be an object")
  end
  local permitted = {}
  for index = 1, #allowed do
    permitted[allowed[index]] = true
  end
  for key in pairs(args) do
    if type(key) ~= "string" then
      return invalid("argument names must be strings")
    end
    if not permitted[key] then
      return invalid(string.format("unknown argument %q", key))
    end
  end
  for index = 1, #required do
    if args[required[index]] == nil then
      return invalid(string.format("argument %q is required", required[index]))
    end
  end
  return true
end

--- A bounded number. `spec.default` is used only when the key is absent.
function Toolkit.readNumber(args, key, spec)
  spec = spec or {}
  local value = args[key]
  if value == nil then
    if spec.default == nil then
      return invalid(string.format("argument %q is required", key))
    end
    return spec.default
  end
  if type(value) ~= "number" or value ~= value then
    return invalid(string.format("argument %q must be a number", key))
  end
  if spec.minimum ~= nil and value < spec.minimum then
    return invalid(string.format("argument %q must be at least %s", key, tostring(spec.minimum)))
  end
  if spec.maximum ~= nil and value > spec.maximum then
    return invalid(string.format("argument %q must be at most %s", key, tostring(spec.maximum)))
  end
  return value
end

--- A bounded whole number.
function Toolkit.readCount(args, key, spec)
  local value, code, detail = Toolkit.readNumber(args, key, spec)
  if value == nil then
    return nil, code, detail
  end
  if value ~= math.floor(value) then
    return invalid(string.format("argument %q must be a whole number", key))
  end
  return math.floor(value)
end

function Toolkit.readFlag(args, key, default)
  local value = args[key]
  if value == nil then
    return default
  end
  if type(value) ~= "boolean" then
    return invalid(string.format("argument %q must be true or false", key))
  end
  return value
end

function Toolkit.readText(args, key, spec)
  spec = spec or {}
  local value = args[key]
  if value == nil then
    if spec.default == nil then
      return invalid(string.format("argument %q is required", key))
    end
    return spec.default
  end
  if type(value) ~= "string" or #value == 0 then
    return invalid(string.format("argument %q must be a non-empty string", key))
  end
  if spec.maximum ~= nil and #value > spec.maximum then
    return invalid(string.format("argument %q is longer than %d bytes", key, spec.maximum))
  end
  return value
end

--- Read `{x, y, z}` in either the nested or the flat spelling.
function Toolkit.readPosition(args, key)
  local value = args[key]
  if type(value) ~= "table" then
    return invalid(string.format("argument %q must be an object with x, y and z", key))
  end
  local coordinates = {}
  local names = { "x", "y", "z" }
  for index = 1, 3 do
    local component = value[names[index]]
    if type(component) ~= "number" or component ~= component then
      return invalid(string.format("argument %q is missing a numeric %s", key, names[index]))
    end
    coordinates[index] = component
  end
  if coordinates[3] ~= math.floor(coordinates[3]) then
    return invalid(string.format("argument %q has a fractional floor", key))
  end
  return { x = coordinates[1], y = coordinates[2], z = math.floor(coordinates[3]) }
end

--- A reference of `kind`, minted by this session.
---
--- The session check is not a formality: a reference from an earlier session
--- carries runtime ids that now denote different objects, so resolving it would
--- act on something nobody named.
function Toolkit.readRef(args, key, kind, ctx)
  local raw = args[key]
  if type(raw) ~= "string" or #raw == 0 then
    return invalid(string.format("argument %q must be a reference string", key))
  end
  local actual = PZAgent.Refs.kindOf(raw)
  if actual ~= kind then
    return nil, reasons().INVALID_REF, string.format("argument %q must be a %s reference", key, kind)
  end
  if ctx ~= nil and not PZAgent.Refs.belongsToSession(raw, ctx.session_id) then
    return nil, reasons().INVALID_REF, string.format("argument %q was minted by another session", key)
  end
  return raw
end

-- ---------------------------------------------------------------------------
-- reference resolution
-- ---------------------------------------------------------------------------

local function playerInventory(player)
  local ok, inventory = Toolkit.call(player, "getInventory")
  if not ok or inventory == nil then
    return Toolkit.unavailable("IsoPlayer.getInventory")
  end
  return inventory
end

Toolkit.playerInventory = playerInventory

--- Every item in `container`, bounded, as a list of the raw engine objects.
function Toolkit.containerItems(container, limit)
  local ok, items = Toolkit.call(container, "getItems")
  if not ok or items == nil then
    return Toolkit.unavailable("ItemContainer.getItems")
  end
  local size = listSize(items)
  if size == nil then
    return Toolkit.unavailable("ItemContainer.getItems().size")
  end
  local scanned = math.min(size, limit or Toolkit.MAX_CONTAINER_ITEMS)
  local result = { truncated = size > scanned, dropped = size - scanned, total = size }
  for index = 0, scanned - 1 do
    local item = listGet(items, index)
    if item ~= nil then
      result[#result + 1] = item
    end
  end
  return result
end

--- Depth-first search for an item with `runtimeId` inside `container`.
local function findByIdentity(container, runtimeId, depth, budget)
  local items = Toolkit.containerItems(container)
  if items == nil then
    return nil
  end
  for index = 1, #items do
    if budget.items <= 0 then
      return nil
    end
    budget.items = budget.items - 1
    local item = items[index]
    if Toolkit.readIdentity(item, { "getID" }) == runtimeId then
      return item, container
    end
    if depth < Toolkit.MAX_SNAPSHOT_DEPTH then
      local ok, nested = Toolkit.call(item, "getInventory")
      if ok and nested ~= nil then
        local found, owner = findByIdentity(nested, runtimeId, depth + 1, budget)
        if found ~= nil then
          return found, owner
        end
      end
    end
  end
  return nil
end

--- The worn entry whose slot and item identity match, or nil.
local function findWorn(player, slot, runtimeId)
  local ok, worn = Toolkit.call(player, "getWornItems")
  if not ok or worn == nil then
    return nil, "IsoPlayer.getWornItems"
  end
  local size = listSize(worn)
  if size == nil then
    return nil, "WornItems.size"
  end
  local scanned = math.min(size, Toolkit.MAX_WORN)
  for index = 0, scanned - 1 do
    local entry = listGet(worn, index)
    local okItem, item = Toolkit.call(entry, "getItem")
    if not okItem or item == nil then
      item = entry
    end
    local location = readStringOf(entry, { "getLocation", "getBodyLocation" })
    local identity = Toolkit.readIdentity(item, { "getID" })
    if location == slot and (runtimeId == nil or identity == runtimeId) then
      return item
    end
  end
  return nil
end

Toolkit.findWorn = findWorn

--- The grid square at (x, y, z), or nil plus the symbol that was missing.
function Toolkit.gridSquare(x, y, z)
  if type(getCell) ~= "function" then
    return nil, "getCell"
  end
  local ok, cell = pcall(getCell)
  if not ok or cell == nil then
    return nil, "getCell"
  end
  local okSquare, square = Toolkit.call(cell, "getGridSquare", x, y, z)
  if not okSquare or square == nil then
    return nil, "IsoCell.getGridSquare"
  end
  return square
end

--- The container named by a `world:x:y:z:objectIndex:containerIndex` tail.
local function worldContainer(tail)
  local x, y, z, objectIndex = tail:match("^world:(-?%d+):(-?%d+):(-?%d+):(%d+):%d+$")
  if x == nil then
    return nil, reasons().INVALID_REF, "the world container reference is malformed"
  end
  local square, missing = Toolkit.gridSquare(tonumber(x), tonumber(y), tonumber(z))
  if square == nil then
    if missing == "IsoCell.getGridSquare" then
      return nil, reasons().TARGET_NOT_LOADED, "the square the container sits on is not loaded"
    end
    return Toolkit.unavailable(missing)
  end
  local ok, objects = Toolkit.call(square, "getObjects")
  if not ok or objects == nil then
    return Toolkit.unavailable("IsoGridSquare.getObjects")
  end
  local object = listGet(objects, tonumber(objectIndex))
  if object == nil then
    return nil, reasons().INVALID_REF, "no object stands at that index on the square"
  end
  local okContainer, container = Toolkit.call(object, "getContainer")
  if not okContainer or container == nil then
    return nil, reasons().INVALID_REF, "the object at that index holds no container"
  end
  return container, nil, nil, object, square
end

--- The container named by a `vehicle:<vehicleId>:<partId>` tail.
local function vehicleContainer(player, tail)
  local vehicleId, partId = tail:match("^vehicle:(%d+):([A-Za-z0-9_%.%-]+)$")
  if vehicleId == nil then
    return nil, reasons().INVALID_REF, "the vehicle container reference is malformed"
  end
  local ok, vehicle = Toolkit.call(player, "getVehicle")
  if not ok then
    return Toolkit.unavailable("IsoPlayer.getVehicle")
  end
  if vehicle == nil then
    return nil, reasons().PRECONDITION_FAILED, "the character is not in a vehicle"
  end
  local identity = Toolkit.readIdentity(vehicle, { "getId", "getID" })
  if identity ~= nil and identity ~= tonumber(vehicleId) then
    return nil, reasons().INVALID_REF, "the reference names a different vehicle"
  end
  local okPart, part = Toolkit.call(vehicle, "getPartById", partId)
  if not okPart then
    return Toolkit.unavailable("BaseVehicle.getPartById")
  end
  if part == nil then
    return nil, reasons().INVALID_REF, "the vehicle has no such part"
  end
  local okContainer, container = Toolkit.call(part, "getItemContainer")
  if not okContainer or container == nil then
    return nil, reasons().INVALID_REF, "that vehicle part holds no container"
  end
  return container
end

--- Resolve a container reference to the engine's ItemContainer.
---
--- Returns container, nil, nil, object, square -- the last two only for a world
--- container, because reaching one is a question about where it stands and the
--- caller would otherwise have to resolve the square a second time.
function Toolkit.resolveContainerTail(ctx, tail)
  local player = ctx.player
  if player == nil then
    return nil, reasons().PRECONDITION_FAILED, "no player character"
  end
  if PZAgent.Refs.isPlayerMainTail(tail) then
    local inventory, code, detail = playerInventory(player)
    if inventory == nil then
      return nil, code, detail
    end
    return inventory
  end
  local slot, wornId = tail:match("^worn:([A-Za-z0-9_%.%-]+):(%d+)$")
  if slot ~= nil then
    local item, missing = findWorn(player, slot, tonumber(wornId))
    if item == nil then
      if missing ~= nil then
        return Toolkit.unavailable(missing)
      end
      return nil, reasons().INVALID_REF, "nothing with that identity is worn in that slot"
    end
    local ok, container = Toolkit.call(item, "getInventory")
    if not ok or container == nil then
      return nil, reasons().INVALID_REF, "the worn item holds no container"
    end
    return container
  end
  local carriedId = tail:match("^carried:(%d+)$")
  if carriedId ~= nil then
    local main, code, detail = playerInventory(player)
    if main == nil then
      return nil, code, detail
    end
    local bag = findByIdentity(main, tonumber(carriedId), 1, { items = Toolkit.MAX_SNAPSHOT_ITEMS })
    if bag == nil then
      return nil, reasons().INVALID_REF, "the carried bag is no longer in the inventory"
    end
    local ok, container = Toolkit.call(bag, "getInventory")
    if not ok or container == nil then
      return nil, reasons().INVALID_REF, "the carried item holds no container"
    end
    return container
  end
  if PZAgent.Refs.isWorldTail(tail) then
    return worldContainer(tail)
  end
  if tail:sub(1, 8) == "vehicle:" then
    return vehicleContainer(player, tail)
  end
  return nil, reasons().INVALID_REF, string.format("no container answers to the tail %q", tail)
end

--- Resolve a container reference string.
function Toolkit.resolveContainer(ctx, ref)
  local parsed, parseError = PZAgent.Refs.parseContainer(ref)
  if parsed == nil then
    return nil, reasons().INVALID_REF, parseError
  end
  if parsed.session_id ~= ctx.session_id then
    return nil, reasons().INVALID_REF, "the container reference was minted by another session"
  end
  return Toolkit.resolveContainerTail(ctx, parsed.tail)
end

--- Resolve an item reference to the engine's InventoryItem plus its container.
---
--- The item is looked for in the container the reference names, never anywhere
--- else: an item that moved since the reference was minted is a stale reference,
--- and finding "an item with the same id somewhere" would act on an object the
--- caller did not name.
function Toolkit.resolveItem(ctx, ref)
  local parsed, parseError = PZAgent.Refs.parseItem(ref)
  if parsed == nil then
    return nil, reasons().INVALID_REF, parseError
  end
  if parsed.session_id ~= ctx.session_id then
    return nil, reasons().INVALID_REF, "the item reference was minted by another session"
  end
  local runtimeId = tonumber(parsed.runtime_id)
  if runtimeId == nil then
    return nil, reasons().INVALID_REF, "the item reference carries no numeric identity"
  end
  local container, code, detail = Toolkit.resolveContainerTail(ctx, parsed.container_tail)
  if container == nil then
    return nil, code, detail
  end
  local items = Toolkit.containerItems(container)
  if items == nil then
    return Toolkit.unavailable("ItemContainer.getItems")
  end
  for index = 1, #items do
    if Toolkit.readIdentity(items[index], { "getID" }) == runtimeId then
      return items[index], nil, nil, container
    end
  end
  return nil, reasons().INVALID_REF, "the item is no longer in the container the reference names"
end

--- How many items with `runtimeId` sit directly in `container`.
---
--- Returns nil when the container could not be read at all. That is not the
--- same fact as "the item is not there", and collapsing the two would let a
--- transfer whose destination went unreadable report the item as gone.
function Toolkit.countIdentity(container, runtimeId)
  local items = Toolkit.containerItems(container)
  if items == nil then
    return nil
  end
  local matches = 0
  local found = nil
  for index = 1, #items do
    if Toolkit.readIdentity(items[index], { "getID" }) == runtimeId then
      matches = matches + 1
      found = found or items[index]
    end
  end
  return matches, found, #items, items.truncated == true
end

-- ---------------------------------------------------------------------------
-- reaching things
-- ---------------------------------------------------------------------------

--- World units within which the character can interact with a square. Roughly
--- "standing next to it"; a container two squares off needs walking to.
Toolkit.DEFAULT_REACH = 1.6

--- Is the character close enough to `point` to touch it?
---
--- Returns true, distance or false, distance. A floor mismatch is never close
--- enough, whatever the plane distance says.
function Toolkit.inReach(ctx, point, radius)
  local snapshot = Toolkit.snapshot(ctx)
  if type(snapshot.position) ~= "table" then
    return nil, reasons().PRECONDITION_FAILED, "the character reports no position"
  end
  local distance = Toolkit.planeDistance(snapshot.position, point.x, point.y)
  if snapshot.position.z ~= point.z then
    return false, distance
  end
  return distance <= (radius or Toolkit.DEFAULT_REACH), distance
end

--- Walk to `point` when it is out of reach, using the game's pathfinding.
---
--- Returns "in_reach" when nothing had to be done, "walking" when a tagged walk
--- was queued, or nil plus a reason. Shared by every adapter whose work happens
--- at a place rather than in the character's own hands, so that "the container
--- was too far away" is one code path with one set of failure codes.
---
--- A nil `point` means the thing travels with the character -- a bag on their
--- back, an item in their hands -- and there is nothing to walk to.
function Toolkit.approach(ctx, point, radius)
  if point == nil then
    return "in_reach"
  end
  -- inReach answers `false, distance` when it is simply far away and
  -- `nil, code, detail` when it could not tell, so the second return is only a
  -- distance once the first is known not to be nil.
  local close, distanceOrCode, detail = Toolkit.inReach(ctx, point, radius)
  if close == nil then
    return nil, distanceOrCode, detail
  end
  if close then
    return "in_reach"
  end
  local square, missing = Toolkit.gridSquare(point.x, point.y, point.z)
  if square == nil then
    if missing == "IsoCell.getGridSquare" then
      return nil, reasons().TARGET_NOT_LOADED, "the square the target stands on is not loaded"
    end
    return Toolkit.unavailable(missing)
  end
  local destination = square
  if type(AdjacentFreeTileFinder) == "table" and type(AdjacentFreeTileFinder.Find) == "function" then
    local ok, found = pcall(AdjacentFreeTileFinder.Find, square, ctx.player)
    if ok and found ~= nil then
      destination = found
    end
  end
  local action, code, actionDetail = Toolkit.construct("ISWalkToTimedAction", ctx.player, destination)
  if action == nil then
    return nil, code, actionDetail
  end
  local queued, queueCode, queueDetail = Toolkit.enqueue(ctx, action)
  if queued == nil then
    return nil, queueCode, queueDetail
  end
  return "walking"
end

-- ---------------------------------------------------------------------------
-- snapshots
-- ---------------------------------------------------------------------------

local function itemRecord(item, containerKey)
  local fluid = nil
  local okFluid, fluidContainer = Toolkit.call(item, "getFluidContainer")
  if okFluid and fluidContainer ~= nil then
    fluid = readNumberOf(fluidContainer, { "getAmount" })
  end
  return {
    id = Toolkit.readIdentity(item, { "getID" }),
    full_type = readStringOf(item, { "getFullType" }),
    name = readStringOf(item, { "getName", "getDisplayName" }),
    container = containerKey,
    equipped = readBooleanOf(item, { "isEquipped" }) == true,
    uses = readNumberOf(item, { "getDrainableUsesInt", "getCurrentUses" }),
    hunger_change = readNumberOf(item, { "getHungerChange" }),
    thirst_change = readNumberOf(item, { "getThirstChange" }),
    base_hunger = readNumberOf(item, { "getBaseHunger" }),
    pages = readNumberOf(item, { "getNumberOfPages" }),
    pages_read = readNumberOf(item, { "getAlreadyReadPages" }),
    fluid = fluid,
    weight = readNumberOf(item, { "getUnequippedWeight", "getActualWeight", "getWeight" }),
  }
end

Toolkit.itemRecord = itemRecord

local function walkItems(container, containerKey, into, depth, budget)
  local items = Toolkit.containerItems(container)
  if items == nil then
    return
  end
  for index = 1, #items do
    if budget.items <= 0 then
      return
    end
    budget.items = budget.items - 1
    local item = items[index]
    local record = itemRecord(item, containerKey)
    if record.id ~= nil then
      into[record.id] = record
      if depth < Toolkit.MAX_SNAPSHOT_DEPTH then
        local ok, nested = Toolkit.call(item, "getInventory")
        if ok and nested ~= nil then
          walkItems(nested, "carried:" .. tostring(record.id), into, depth + 1, budget)
        end
      end
    end
  end
end

local function snapshotBody(player)
  local parts = {}
  local ok, body = Toolkit.call(player, "getBodyDamage")
  if not ok or body == nil then
    return parts
  end
  local okParts, list = Toolkit.call(body, "getBodyParts")
  if not okParts or list == nil then
    return parts
  end
  local size = listSize(list)
  if size == nil then
    return parts
  end
  local scanned = math.min(size, Toolkit.MAX_BODY_PARTS)
  for index = 0, scanned - 1 do
    local part = listGet(list, index)
    local name = readStringOf(part, { "getType" })
    if name == nil then
      local okType, typeValue = Toolkit.call(part, "getType")
      if okType then
        name = readStringOf(typeValue, { "name", "toString", "getName" })
      end
    end
    if name ~= nil then
      local health = readNumberOf(part, { "getHealth" })
      parts[name] = {
        index = index,
        bleeding = readBooleanOf(part, { "bleeding", "isBleeding" }) == true,
        bandaged = readBooleanOf(part, { "bandaged", "isBandaged" }) == true,
        deep_wounded = readBooleanOf(part, { "isDeepWounded", "deepWounded" }) == true,
        severity = health ~= nil and (PERCENT - health) / PERCENT or nil,
      }
    end
  end
  return parts
end

--- Everything an adapter is allowed to call a postcondition against.
---
--- Deliberately narrow and deliberately built here: an adapter that assembled
--- its own "after" could compute the field it is about to compare. This walks
--- the character only -- position, stats, body parts, hands, and every item on
--- the person -- because those are the observations that do not depend on a
--- reference still resolving.
function Toolkit.observe(player)
  local snapshot = { items = {}, body = {}, hands = {}, worn = {} }
  if player == nil then
    return snapshot
  end
  local x = readNumberOf(player, { "getX" })
  local y = readNumberOf(player, { "getY" })
  local z = readNumberOf(player, { "getZ" })
  if x ~= nil and y ~= nil and z ~= nil then
    snapshot.position = { x = x, y = y, z = math.floor(z) }
  end
  local okStats, stats = Toolkit.call(player, "getStats")
  if okStats and stats ~= nil then
    snapshot.stats = {
      hunger = readNumberOf(stats, { "getHunger" }),
      thirst = readNumberOf(stats, { "getThirst" }),
      fatigue = readNumberOf(stats, { "getFatigue" }),
      endurance = readNumberOf(stats, { "getEndurance" }),
      stress = readNumberOf(stats, { "getStress" }),
      panic = readNumberOf(stats, { "getPanic" }),
    }
  end
  snapshot.body = snapshotBody(player)
  snapshot.asleep = readBooleanOf(player, { "isAsleep" })
  snapshot.reading = readBooleanOf(player, { "isReading", "isReadingaBook" })
  snapshot.sitting = readBooleanOf(player, { "isSitOnGround", "isSitting" })

  local budget = { items = Toolkit.MAX_SNAPSHOT_ITEMS }
  local okMain, main = Toolkit.call(player, "getInventory")
  if okMain and main ~= nil then
    walkItems(main, "player-main", snapshot.items, 1, budget)
  end
  local okWorn, worn = Toolkit.call(player, "getWornItems")
  if okWorn and worn ~= nil then
    local size = listSize(worn) or 0
    local scanned = math.min(size, Toolkit.MAX_WORN)
    for index = 0, scanned - 1 do
      local entry = listGet(worn, index)
      local okItem, item = Toolkit.call(entry, "getItem")
      if not okItem or item == nil then
        item = entry
      end
      local slot = readStringOf(entry, { "getLocation", "getBodyLocation" })
      local identity = Toolkit.readIdentity(item, { "getID" })
      if identity ~= nil then
        snapshot.worn[identity] = slot or true
        -- A worn garment is recorded as "worn" rather than as a container tail:
        -- there is no container reference that resolves to a body location, and
        -- minting "worn:<slot>" would produce a reference nothing can resolve.
        -- Equipment names these by their slot instead.
        local record = itemRecord(item, "worn")
        record.worn_slot = slot
        snapshot.items[identity] = record
        local okNested, nested = Toolkit.call(item, "getInventory")
        if okNested and nested ~= nil and slot ~= nil then
          walkItems(nested, "worn:" .. slot .. ":" .. tostring(identity), snapshot.items, 2, budget)
        end
      end
    end
  end
  local okPrimary, primary = Toolkit.call(player, "getPrimaryHandItem")
  if okPrimary and primary ~= nil then
    snapshot.hands.primary = Toolkit.readIdentity(primary, { "getID" })
  end
  local okSecondary, secondary = Toolkit.call(player, "getSecondaryHandItem")
  if okSecondary and secondary ~= nil then
    snapshot.hands.secondary = Toolkit.readIdentity(secondary, { "getID" })
  end
  return snapshot
end

--- The snapshot the runtime supplied, or one taken here.
function Toolkit.snapshot(ctx)
  if type(ctx.snapshot) == "function" then
    local ok, taken = pcall(ctx.snapshot, ctx)
    if ok and type(taken) == "table" then
      return taken
    end
  end
  return Toolkit.observe(ctx.player)
end

--- A stat from a snapshot, or nil when this build did not report it. Never a
--- default: a missing hunger reading must not read as "not hungry".
function Toolkit.stat(snapshot, name)
  if type(snapshot) ~= "table" or type(snapshot.stats) ~= "table" then
    return nil
  end
  local value = snapshot.stats[name]
  if type(value) ~= "number" then
    return nil
  end
  return value
end

--- Plane distance, ignoring the floor. Floors are compared separately because
--- being one storey above the target is not being 0.4 squares from it.
function Toolkit.planeDistance(position, x, y)
  local dx = position.x - x
  local dy = position.y - y
  return math.sqrt((dx * dx) + (dy * dy))
end

-- ---------------------------------------------------------------------------
-- declaration
-- ---------------------------------------------------------------------------

local function defaultPrepare()
  -- Most actions need nothing fetched or walked to before they start; the ones
  -- that do -- a transfer, a bandage, an approach -- override this.
  return true
end

--- Where an adapter's own steps hang, so `start` and `poll` can be the two
--- functions PZAgent.ActionRuntime actually calls.
---
--- This split was not a design choice, it was a defect. The adapters here were
--- written against a five-step lifecycle -- validate, prepare, begin, progress,
--- verify -- each taking `(self, args, ctx)`. The runtime calls exactly two
--- functions, `start(ctx, args)` and `poll(ctx, args)`, and looks up an adapter
--- by `adapter.action`. So every adapter built by this function named itself
--- under `name`, exposed no `action`, and registered nowhere:
--- `tests/lua/test_adapter_registry.lua` found thirteen of the sixteen game
--- actions unreachable. The mod loaded cleanly, reported healthy, and answered
--- CAPABILITY_UNAVAILABLE to everything a player would ask it to do.
---
--- The five steps are worth keeping -- `verify` is where the postcondition
--- lives, and an adapter that folded it into `start` would have nowhere to put
--- the proof -- so the fix is this adaptor rather than a rewrite of ten files.
local LIFECYCLE_STEPS = { "validate", "prepare", "begin", "progress", "verify" }

--- Run the postcondition and publish what it observed.
---
--- The evidence goes on `ctx.state.evidence` because that is where
--- ActionRuntime reads it when an adapter answers "done", and a "done" with no
--- evidence there becomes POSTCONDITION_FAILED rather than a success. Verify
--- returning nil therefore has exactly one meaning: the change could not be
--- observed, so it will not be claimed.
local function runVerify(adapter, args, ctx)
  local state = Toolkit.state(ctx)
  local evidence, code, detail = adapter:verify(state.before, Toolkit.observe(ctx.player), args, ctx)
  if evidence == nil then
    return nil,
      code or reasons().POSTCONDITION_FAILED,
      detail or string.format("%s could not observe its postcondition", adapter.action)
  end
  state.evidence = evidence
  return "done"
end

--- Build an adapter table from a spec, filling in the shared lifecycle steps.
---
--- `validate` and `verify` are mandatory and have no default: an adapter with
--- no postcondition of its own cannot be allowed to inherit one that always
--- passes, which is exactly how a command would report success it never saw.
---
--- `args` is mandatory for the same class of reason. PZAgent.CommandDispatcher
--- builds the argument table it hands an adapter *from the declaration*, so an
--- adapter that declares nothing is handed nothing -- it would not be refused,
--- it would run with every argument silently dropped.
function Toolkit.declare(spec)
  assert(type(spec.name) == "string" and #spec.name > 0, "an adapter must name its action")
  assert(type(spec.validate) == "function", spec.name .. " must implement validate")
  assert(type(spec.begin) == "function", spec.name .. " must implement begin")
  assert(type(spec.verify) == "function", spec.name .. " must implement verify")
  assert(type(spec.args) == "table", spec.name .. " must declare its arguments")
  local adapter = {
    -- What PZAgent.CommandDispatcher reads. `name` stays beside it because the
    -- adapters and their tests were written against it, and one spelling on the
    -- wire is worth more than one spelling in the source.
    action = spec.name,
    name = spec.name,
    capability = spec.capability,
    -- Read by PZAgent.CapabilityRuntime, which publishes `experimental` instead
    -- of `available_unverified` when it is set. It was read there and set here
    -- by nothing, so the mod could not report the state at all: two adapters
    -- carried comments saying "the probe caps this at experimental" and both
    -- published as ordinary unverified, while docs/PROTOCOL.md documents the
    -- file with an example showing a state its writer could not emit.
    experimental = spec.experimental or false,
    args = spec.args,
    required_symbols = spec.requires or {},

    requires = spec.requires or {},
    timeout_ms = spec.timeout_ms or 30000,
    poll_interval_ms = spec.poll_interval_ms or 250,
    validate = spec.validate,
    prepare = spec.prepare or defaultPrepare,
    begin = spec.begin,
    progress = spec.progress or function(_, _, ctx)
      return Toolkit.queueProgress(ctx)
    end,
    verify = spec.verify,
    interrupt = spec.interrupt,
  }

  --- validate -> prepare -> snapshot -> begin, as one call the runtime can make.
  ---
  --- The "before" snapshot is taken after prepare and before begin, which is the
  --- only window where it means anything: prepare may fetch an item into the
  --- character's hands, and a snapshot taken before that would show the fetch as
  --- part of the action's effect.
  adapter.start = function(ctx, args)
    local accepted, code, detail = adapter:validate(args, ctx)
    if accepted == nil then
      return nil, code, detail
    end
    local prepared, prepareCode, prepareDetail = adapter:prepare(args, ctx)
    if prepared == nil then
      return nil, prepareCode, prepareDetail
    end
    Toolkit.state(ctx).before = Toolkit.observe(ctx.player)
    local started, startCode, startDetail = adapter:begin(args, ctx)
    if started == nil then
      return nil, startCode, startDetail
    end
    -- A read-only adapter -- inspect, search -- finishes inside begin. It still
    -- goes through verify: "the walk returned a table" is not a postcondition.
    if started == "done" then
      return runVerify(adapter, args, ctx)
    end
    return true
  end

  adapter.poll = function(ctx, args)
    local status, code, detail = adapter:progress(args, ctx)
    if status == nil then
      return nil, code, detail
    end
    if status == "done" then
      return runVerify(adapter, args, ctx)
    end
    return true
  end

  return adapter
end

--- Every lifecycle step an adapter built by :func:`Toolkit.declare` carries.
--- Exported so a test can assert the set rather than restate it.
Toolkit.LIFECYCLE_STEPS = LIFECYCLE_STEPS

--- Publish an adapter to the dispatcher's registry.
function Toolkit.register(adapter)
  assert(PZAgent.Adapters.BY_NAME[adapter.name] == nil, "two adapters claim " .. adapter.name)
  PZAgent.Adapters.BY_NAME[adapter.name] = adapter
  PZAgent.Adapters.ALL[#PZAgent.Adapters.ALL + 1] = adapter
  if not PZAgent.Protocol.isKnownAction(adapter.name) then
    PZAgent.Adapters.PENDING_PROTOCOL[#PZAgent.Adapters.PENDING_PROTOCOL + 1] = adapter.name
  end
  return adapter
end

return Toolkit
