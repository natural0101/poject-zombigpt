--[[
PZAgent.Adapters.Containers -- container.inspect and container.open_nearby.

Invariant: inspecting a container reads the engine's own item list. It does not
open a UI panel, does not construct an ISInventoryPage and does not call
anything that simulates a click. Driving the interface would move the player's
own windows around and would make the read depend on a panel existing; walking
the list is the same information with none of that.

`inspect` is read-only in the protocol's sense -- it queues nothing and needs no
arming -- so its "postcondition" is the read itself, and its evidence is what
the read saw. A container that could not be read produces no evidence and a
CAPABILITY_UNAVAILABLE naming the accessor, never an empty list: "the crate is
empty" and "the crate would not answer" are opposite facts.

`open_nearby` is not read-only, and deliberately so: it moves the character.
What it means here is "get within reach of this container and confirm its
contents can be read from there" -- there is no separate open call in the engine
that a mod can drive without the UI. A container behind a closed door is *not*
opened by this: the door is a different object and opening one is a different
action, so reach fails and says so rather than a door being opened nobody asked
about.
]]

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Containers = {}
PZAgent.Adapters.Containers = Containers

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

--- Items one inspection reports. Smaller than the walk bound in Toolkit,
--- because this list goes on the wire in an ack and a warehouse shelf would
--- otherwise put a thousand descriptors in one message.
Containers.MAX_LISTED = 64

Containers.TIMEOUT_MS = 30000
Containers.POLL_MS = 250

--- Reach for opening: the same interaction range every other adapter uses.
Containers.DEFAULT_RADIUS = 1.6

--- Reaches a command may ask for. The floor keeps a caller from naming a radius
--- no position ever satisfies; the ceiling keeps "nearby" meaning nearby, since
--- a reach of ten squares would call a container on the far side of the room
--- open. Named once because the declaration below and the reader in openSpec
--- both state them, and a bound stated twice is a bound that drifts.
Containers.MIN_RADIUS = 0.1
Containers.MAX_RADIUS = 3.0

--- Argument kinds this file declares, spelled the way
--- PZAgent.CommandDispatcher.ARG spells them. Held here rather than read off the
--- dispatcher because the engine chooses the order it walks media/lua in, and a
--- declaration that indexed another client module while this file was loading
--- would break on a build that reaches adapters/ first.
local ARG = { NUMBER = "number", REF = "ref" }

--- Both commands take a container reference and nothing else that is a
--- reference, so the accepted kinds are one table rather than two copies.
local CONTAINER_KIND = { [PZAgent.Refs.KIND.CONTAINER] = true }

local INSPECT_ARGS = { "container_ref", "limit" }
local OPEN_ARGS = { "container_ref", "radius" }

--- Where the container stands, or nil when it travels with the character.
local function containerPoint(tail)
  if type(tail) ~= "string" then
    return nil
  end
  local x, y, z = tail:match("^world:(-?%d+):(-?%d+):(-?%d+):%d+:%d+$")
  if x == nil then
    return nil
  end
  return { x = tonumber(x), y = tonumber(y), z = tonumber(z) }
end

Containers.containerPoint = containerPoint

--- Parse the container reference both commands take.
local function containerSpec(args, ctx, allowed)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, allowed, { "container_ref" })
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local ref, refCode, refDetail = Toolkit.readRef(args, "container_ref", PZAgent.Refs.KIND.CONTAINER, ctx)
  if ref == nil then
    return nil, refCode, refDetail
  end
  local parsed, parseError = PZAgent.Refs.parseContainer(ref)
  if parsed == nil then
    return nil, reasons.INVALID_REF, parseError
  end
  return { container_ref = ref, tail = parsed.tail, point = containerPoint(parsed.tail) }
end

Containers.containerSpec = containerSpec

--- The contents of `container`, as plain descriptors, bounded.
---
--- Returns nil plus the accessor that was missing when the container would not
--- answer. Nothing here defaults to an empty list.
function Containers.contents(container, limit)
  local Toolkit = toolkit()
  local items = Toolkit.containerItems(container, Toolkit.MAX_CONTAINER_ITEMS)
  if items == nil then
    return nil, "ItemContainer.getItems"
  end
  local ceiling = math.min(limit or Containers.MAX_LISTED, Containers.MAX_LISTED)
  local listed = {}
  for index = 1, math.min(#items, ceiling) do
    local record = Toolkit.itemRecord(items[index], nil)
    listed[index] = {
      runtime_id = record.id,
      full_type = record.full_type,
      display_name = record.name,
      weight = record.weight,
      uses = record.uses,
    }
  end
  return {
    items = listed,
    listed = #listed,
    total = items.total,
    truncated = items.truncated == true or #items > ceiling,
    capacity = Toolkit.readNumberOf(container, { "getCapacity", "getMaxWeight" }),
    used_capacity = Toolkit.readNumberOf(container, { "getContentsWeight", "getCapacityWeight" }),
  }
end

-- ---------------------------------------------------------------------------
-- container.inspect
-- ---------------------------------------------------------------------------

local function inspectSpec(args, ctx)
  local Toolkit = toolkit()
  local spec, code, detail = containerSpec(args, ctx, INSPECT_ARGS)
  if spec == nil then
    return nil, code, detail
  end
  local limit, limitCode, limitDetail = Toolkit.readCount(args, "limit", {
    default = Containers.MAX_LISTED,
    minimum = 1,
    maximum = Containers.MAX_LISTED,
  })
  if limit == nil then
    return nil, limitCode, limitDetail
  end
  spec.limit = limit
  return spec
end

local function inspectValidate(_, args, ctx)
  local Toolkit = toolkit()
  local spec, code, detail = inspectSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local container, containerCode, containerDetail = Toolkit.resolveContainer(ctx, spec.container_ref)
  if container == nil then
    return nil, containerCode, containerDetail
  end
  Toolkit.state(ctx).inspect = spec
  return true
end

--- Reading a container queues nothing: there is no timed action to enqueue and
--- the read happens in verify, against the container as it is at that moment.
local function inspectStart(_, args, ctx)
  local spec, code, detail = inspectSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  return true
end

local function inspectProgress(_, args, ctx)
  local spec, code, detail = inspectSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  return "done"
end

local function inspectVerify(_, _, _, args, ctx)
  local Toolkit = toolkit()
  local spec, code, detail = inspectSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local container, containerCode, containerDetail = Toolkit.resolveContainer(ctx, spec.container_ref)
  if container == nil then
    return nil, containerCode, containerDetail
  end
  local contents, missing = Containers.contents(container, spec.limit)
  if contents == nil then
    return Toolkit.unavailable(missing)
  end
  return {
    kind = "container_contents",
    container_ref = spec.container_ref,
    items = contents.items,
    item_count = contents.listed,
    total_items = contents.total,
    truncated = contents.truncated,
    capacity = contents.capacity,
    used_capacity = contents.used_capacity,
  }
end

local Inspect = toolkit().declare({
  name = "container.inspect",
  capability = nil,
  requires = {},
  timeout_ms = 5000,
  poll_interval_ms = 100,
  args = {
    container_ref = { type = ARG.REF, required = true, kinds = CONTAINER_KIND },
    -- The listing goes out in an ack, so the ceiling belongs on the wire and not
    -- only in the reader: a limit nobody bounded is a warehouse shelf in one
    -- message.
    limit = { type = ARG.NUMBER, integer = true, min = 1, max = Containers.MAX_LISTED },
  },
  validate = inspectValidate,
  begin = inspectStart,
  progress = inspectProgress,
  verify = inspectVerify,
})

Containers.Inspect = Inspect
toolkit().register(Inspect)

-- ---------------------------------------------------------------------------
-- container.open_nearby
-- ---------------------------------------------------------------------------

local function openSpec(args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec, code, detail = containerSpec(args, ctx, OPEN_ARGS)
  if spec == nil then
    return nil, code, detail
  end
  if PZAgent.Refs.isOnPersonTail(spec.tail) then
    return nil, reasons.INVALID_ARGUMENT, "a container the character is carrying is already within reach"
  end
  if spec.point == nil then
    return nil, reasons.INVALID_ARGUMENT, "this container is not somewhere the character can walk to"
  end
  local radius, radiusCode, radiusDetail = Toolkit.readNumber(args, "radius", {
    default = Containers.DEFAULT_RADIUS,
    minimum = Containers.MIN_RADIUS,
    maximum = Containers.MAX_RADIUS,
  })
  if radius == nil then
    return nil, radiusCode, radiusDetail
  end
  spec.radius = radius
  return spec
end

Containers.openSpec = openSpec

local Open = nil

local function openValidate(_, args, ctx)
  local Toolkit = toolkit()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Open.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = openSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local container, containerCode, containerDetail = Toolkit.resolveContainer(ctx, spec.container_ref)
  if container == nil then
    return nil, containerCode, containerDetail
  end
  Toolkit.state(ctx).open = spec
  return true
end

--- Check reach; walk there when it is not already within it.
local function openPrepare(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = openSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local outcome, reachCode, reachDetail = Toolkit.approach(ctx, spec.point, spec.radius)
  if outcome == nil then
    return nil, reachCode, reachDetail
  end
  Toolkit.state(ctx).walked = outcome == "walking"
  return true
end

--- The walk queued by prepare is the whole of this command's work in the world.
local function openStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = openSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local container, containerCode, containerDetail = Toolkit.resolveContainer(ctx, spec.container_ref)
  if container == nil then
    return nil, containerCode, containerDetail
  end
  return true
end

local function openProgress(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = openSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local close, distanceOrCode, reachDetail = Toolkit.inReach(ctx, spec.point, spec.radius)
  if close == nil then
    return nil, distanceOrCode, reachDetail
  end
  if close then
    return "done"
  end
  local queue, queueCode, queueDetail = Toolkit.queueProgress(ctx)
  if queue == nil then
    return nil, queueCode, queueDetail
  end
  if queue == "done" then
    return nil,
      reasons.PATH_NOT_FOUND,
      string.format("the walk ended %.2f squares from the container", distanceOrCode)
  end
  return "running"
end

--- Within reach, and its contents actually readable from there.
local function openVerify(_, _, _, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec, code, detail = openSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local close, distanceOrCode, reachDetail = Toolkit.inReach(ctx, spec.point, spec.radius)
  if close == nil then
    return nil, distanceOrCode, reachDetail
  end
  if not close then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format("the character is %.2f squares away, outside the %.2f reach", distanceOrCode, spec.radius)
  end
  local container, containerCode, containerDetail = Toolkit.resolveContainer(ctx, spec.container_ref)
  if container == nil then
    return nil, containerCode, containerDetail
  end
  local contents, missing = Containers.contents(container, Containers.MAX_LISTED)
  if contents == nil then
    return Toolkit.unavailable(missing)
  end
  return {
    kind = "container_within_reach",
    container_ref = spec.container_ref,
    distance = distanceOrCode,
    radius = spec.radius,
    item_count = contents.listed,
    total_items = contents.total,
    truncated = contents.truncated,
    walked = Toolkit.state(ctx).walked == true,
  }
end

Open = toolkit().declare({
  name = "container.open_nearby",
  -- Walking to the container is what this command does in the world, so it
  -- rides on the movement capability rather than claiming one of its own.
  capability = toolkit().CAPABILITY.MOVE_TO_SQUARE,
  requires = { "getCell", "ISWalkToTimedAction", "ISWalkToTimedAction.new", "ISTimedActionQueue.add" },
  timeout_ms = Containers.TIMEOUT_MS,
  poll_interval_ms = Containers.POLL_MS,
  args = {
    container_ref = { type = ARG.REF, required = true, kinds = CONTAINER_KIND },
    -- A reach the caller picks is a walk the character takes, so the ceiling is
    -- declared here as well as read in openSpec.
    radius = { type = ARG.NUMBER, min = Containers.MIN_RADIUS, max = Containers.MAX_RADIUS },
  },
  validate = openValidate,
  prepare = openPrepare,
  begin = openStart,
  progress = openProgress,
  verify = openVerify,
})

Containers.Open = Open
toolkit().register(Open)

return Containers
