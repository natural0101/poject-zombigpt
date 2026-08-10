--[[
PZAgent.Adapters.Inventory -- inventory.transfer, inventory.ensure_main and the
bounded search the other adapters pick their items with.

Invariant: an item is moved by ISInventoryTransferAction and by nothing else --
no container list is edited, no item is added or removed by hand. The
postcondition is symmetric and both halves are required: the item resolves *in*
the destination and is *gone from* the origin, with the count matching. Checking
only the destination would call a copied item a moved one; checking only the
origin would call a vanished item a delivered one.

An unreadable container is never read as an empty one. Toolkit.countIdentity
answers nil for "I could not look", and every branch below treats that as a
capability gap rather than as proof the item is not there -- a transfer whose
destination stopped answering must not report the item as delivered, and one
whose origin stopped answering must not report it as gone.

Search is here rather than in each consumer because the choice of *which*
sandwich is deterministic policy the sidecar owns (AGENTS.md). This function
enumerates candidates against explicit filters and sorts them stably; it never
decides that one of them is the right one.
]]

-- Load-order guard, live-proven 2026-08-08 on Build 42.20.2: the engine walked
-- adapters/ in an order that ran this file before Toolkit.lua. The statement
-- form is deliberate -- the paren form is banned as dynamic loading -- and the
-- test harness pre-resolves this module, so there the require is a no-op.
require "PZAgent/adapters/Toolkit"

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Inventory = {}
PZAgent.Adapters.Inventory = Inventory

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

--- An item reference names one item, so a transfer moves one item. Moving five
--- is five commands, each with its own evidence.
Inventory.QUANTITY = 1

--- Results one search may return. The caller gets `truncated` when the walk hit
--- either this or the container budget, so "there is no bandage" is never said
--- of a bag that was not finished.
Inventory.MAX_RESULTS = 32

--- Ceiling on a `min_uses` filter. Far above any drainable the game ships, and
--- named once because the declaration below and the reader in searchSpec both
--- state it -- a bound stated twice is a bound that drifts.
Inventory.MAX_USES = 1000

Inventory.TIMEOUT_MS = 20000
Inventory.POLL_MS = 250

--- Argument kinds this file declares, spelled the way
--- PZAgent.CommandDispatcher.ARG spells them. Held here rather than read off the
--- dispatcher because the engine chooses the order it walks media/lua in, and a
--- declaration that indexed another client module while this file was loading
--- would break on a build that reaches adapters/ first.
local ARG = { STRING = "string", NUMBER = "number", BOOLEAN = "boolean", REF = "ref" }

--- The reference kinds the declarations below accept, named once because four
--- of the five arguments that carry a reference are container references.
local CONTAINER_KIND = { [PZAgent.Refs.KIND.CONTAINER] = true }
local ITEM_KIND = { [PZAgent.Refs.KIND.ITEM] = true }

local REQUIRES = {
  "ISInventoryTransferAction",
  "ISInventoryTransferAction.new",
  "ISTimedActionQueue.add",
}

-- ---------------------------------------------------------------------------
-- search
-- ---------------------------------------------------------------------------

--- Which container kinds a search walks, in the order it walks them.
Inventory.SCOPES = { "main", "worn", "carried" }

local function matchesFilter(record, filter)
  if filter.full_type ~= nil and record.full_type ~= filter.full_type then
    return false
  end
  if filter.type_prefix ~= nil then
    local fullType = record.full_type
    if type(fullType) ~= "string" or fullType:sub(1, #filter.type_prefix) ~= filter.type_prefix then
      return false
    end
  end
  if filter.name_contains ~= nil then
    local name = record.name
    if type(name) ~= "string" or name:lower():find(filter.name_contains:lower(), 1, true) == nil then
      return false
    end
  end
  if filter.edible == true and record.hunger_change == nil then
    return false
  end
  if filter.drinkable == true and record.thirst_change == nil and record.fluid == nil then
    return false
  end
  if filter.readable == true and record.pages == nil then
    return false
  end
  if filter.exclude_equipped == true and record.equipped == true then
    return false
  end
  if filter.min_uses ~= nil and (record.uses == nil or record.uses < filter.min_uses) then
    return false
  end
  return true
end

Inventory.matchesFilter = matchesFilter

--- The container reference tail an item record came from, for minting refs.
local function tailOf(record)
  local container = record.container
  if type(container) ~= "string" then
    return nil
  end
  if container == "player-main" then
    return "player-main"
  end
  return container
end

--- Everything on the character matching `filter`, newest container walk first.
---
--- Returns a list of `{ record, ref }` plus `truncated`. Deterministic: the
--- results come out in the engine's own container order and are then sorted by
--- runtime id, so two searches over an unchanged inventory agree.
function Inventory.search(ctx, filter, snapshot)
  local Toolkit = toolkit()
  filter = filter or {}
  snapshot = snapshot or Toolkit.snapshot(ctx)
  local results = { truncated = false }
  local identities = {}
  for identity in pairs(snapshot.items or {}) do
    identities[#identities + 1] = identity
  end
  table.sort(identities)
  local limit = filter.limit or Inventory.MAX_RESULTS
  for index = 1, #identities do
    local record = snapshot.items[identities[index]]
    if matchesFilter(record, filter) then
      if #results >= limit then
        results.truncated = true
        break
      end
      local tail = tailOf(record)
      local ref = nil
      if tail ~= nil then
        ref = PZAgent.Refs.buildItem(ctx.session_id, tail, tostring(record.id), 0)
      end
      results[#results + 1] = { record = record, ref = ref, id = record.id }
    end
  end
  return results
end

--- The first search hit, or nil plus the reason there was none.
function Inventory.firstMatch(ctx, filter, emptyCode, emptyDetail)
  local results = Inventory.search(ctx, filter)
  if #results == 0 then
    return nil, emptyCode or toolkit().reasons().PRECONDITION_FAILED, emptyDetail or "nothing on the character matches"
  end
  return results[1]
end

-- ---------------------------------------------------------------------------
-- inventory.search
-- ---------------------------------------------------------------------------

local SEARCH_ARGS = {
  "full_type",
  "type_prefix",
  "name_contains",
  "edible",
  "drinkable",
  "readable",
  "exclude_equipped",
  "min_uses",
  "limit",
}

--- Turn a search command's arguments into a filter, bounding the result count.
local function searchSpec(args)
  local Toolkit = toolkit()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, SEARCH_ARGS, {})
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local filter = {}
  for _, key in ipairs({ "full_type", "type_prefix", "name_contains" }) do
    if args[key] ~= nil then
      local value, code, detail = Toolkit.readText(args, key, { maximum = 128 })
      if value == nil then
        return nil, code, detail
      end
      filter[key] = value
    end
  end
  for _, key in ipairs({ "edible", "drinkable", "readable", "exclude_equipped" }) do
    if args[key] ~= nil then
      local value, code, detail = Toolkit.readFlag(args, key, false)
      if value == nil then
        return nil, code, detail
      end
      filter[key] = value
    end
  end
  if args.min_uses ~= nil then
    local value, code, detail =
      Toolkit.readCount(args, "min_uses", { default = 0, minimum = 0, maximum = Inventory.MAX_USES })
    if value == nil then
      return nil, code, detail
    end
    filter.min_uses = value
  end
  local limit, limitCode, limitDetail = Toolkit.readCount(args, "limit", {
    default = Inventory.MAX_RESULTS,
    minimum = 1,
    maximum = Inventory.MAX_RESULTS,
  })
  if limit == nil then
    return nil, limitCode, limitDetail
  end
  filter.limit = limit
  return filter
end

Inventory.searchSpec = searchSpec

--- A search is read-only: it queues nothing, so there is nothing to start and
--- nothing to wait for. Its postcondition is that the walk happened and the
--- results are what the walk saw -- which is why verify runs the search again
--- over the `after` snapshot rather than replaying a list kept from validate.
local function searchNothingToStart(_, args)
  local spec, code, detail = searchSpec(args)
  if spec == nil then
    return nil, code, detail
  end
  return true
end

local function searchDone(_, args)
  local spec, code, detail = searchSpec(args)
  if spec == nil then
    return nil, code, detail
  end
  return "done"
end

local function searchVerify(_, _, after, args, ctx)
  local filter, code, detail = searchSpec(args)
  if filter == nil then
    return nil, code, detail
  end
  local results = Inventory.search(ctx, filter, after)
  local matches = {}
  for index = 1, #results do
    local record = results[index].record
    matches[index] = {
      item_ref = results[index].ref,
      runtime_id = record.id,
      full_type = record.full_type,
      display_name = record.name,
      container = record.container,
      equipped = record.equipped,
      uses = record.uses,
    }
  end
  return {
    kind = "inventory_searched",
    matches = matches,
    match_count = #matches,
    truncated = results.truncated == true,
  }
end

local Search = toolkit().declare({
  name = "inventory.search",
  capability = nil,
  requires = {},
  timeout_ms = 5000,
  poll_interval_ms = 100,
  -- Eight filters is PZAgent.CommandDispatcher.MAX_ARGS exactly, and
  -- SEARCH_ARGS lists nine. `name_contains` is the one left undeclared: the
  -- dispatcher's alphabet for a plain string carries no space, so over the wire
  -- it could only ever match a one-word display name, which the two type
  -- filters already cover. A command that carries it is refused by name; the
  -- filter itself still works for the adapters that call Inventory.search
  -- directly with a filter table.
  args = {
    full_type = { type = ARG.STRING },
    type_prefix = { type = ARG.STRING },
    edible = { type = ARG.BOOLEAN },
    drinkable = { type = ARG.BOOLEAN },
    readable = { type = ARG.BOOLEAN },
    exclude_equipped = { type = ARG.BOOLEAN },
    min_uses = { type = ARG.NUMBER, integer = true, min = 0, max = Inventory.MAX_USES },
    -- The walk is over everything on the character and the results go out in an
    -- ack, so the ceiling is declared rather than left to the reader alone.
    limit = { type = ARG.NUMBER, integer = true, min = 1, max = Inventory.MAX_RESULTS },
  },
  validate = searchNothingToStart,
  begin = searchNothingToStart,
  progress = searchDone,
  verify = searchVerify,
})

Inventory.Search = Search
toolkit().register(Search)

-- ---------------------------------------------------------------------------
-- transfer
-- ---------------------------------------------------------------------------

local TRANSFER_ARGS = {
  "item_ref",
  "source_container_ref",
  "destination_container_ref",
  "quantity",
  "origin",
}

--- Where a container reference sits in the world, or nil when it travels with
--- the character and so needs no approach.
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

Inventory.containerPoint = containerPoint

--- Parse and resolve a transfer, leaving both containers on the spec.
local function transferSpec(args, ctx, options)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  options = options or {}
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, TRANSFER_ARGS, {
    "item_ref",
    "destination_container_ref",
  })
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local itemRef, itemCode, itemDetail = Toolkit.readRef(args, "item_ref", PZAgent.Refs.KIND.ITEM, ctx)
  if itemRef == nil then
    return nil, itemCode, itemDetail
  end
  local destinationRef, destCode, destDetail =
    Toolkit.readRef(args, "destination_container_ref", PZAgent.Refs.KIND.CONTAINER, ctx)
  if destinationRef == nil then
    return nil, destCode, destDetail
  end
  local quantity, quantityCode, quantityDetail = Toolkit.readCount(args, "quantity", {
    default = Inventory.QUANTITY,
    minimum = 1,
    maximum = 1,
  })
  if quantity == nil then
    return nil, quantityCode, quantityDetail
  end
  local parsed, parseError = PZAgent.Refs.parseItem(itemRef)
  if parsed == nil then
    return nil, reasons.INVALID_REF, parseError
  end
  local runtimeId = tonumber(parsed.runtime_id)
  if runtimeId == nil then
    return nil, reasons.INVALID_REF, "the item reference carries no numeric identity"
  end
  local sourceRef = args.source_container_ref
  if sourceRef ~= nil then
    local given, givenCode, givenDetail =
      Toolkit.readRef(args, "source_container_ref", PZAgent.Refs.KIND.CONTAINER, ctx)
    if given == nil then
      return nil, givenCode, givenDetail
    end
    local sourceParsed, sourceError = PZAgent.Refs.parseContainer(given)
    if sourceParsed == nil then
      return nil, reasons.INVALID_REF, sourceError
    end
    if sourceParsed.tail ~= parsed.container_tail then
      -- The item reference already names its container. A source that disagrees
      -- means one of the two describes an object the caller did not intend.
      return nil, reasons.INVALID_ARGUMENT, "source_container_ref names a different container than item_ref"
    end
  end
  local destinationParsed, destinationError = PZAgent.Refs.parseContainer(destinationRef)
  if destinationParsed == nil then
    return nil, reasons.INVALID_REF, destinationError
  end
  if destinationParsed.tail == parsed.container_tail then
    return nil, reasons.INVALID_ARGUMENT, "the item is already in the destination container"
  end
  local spec = {
    item_ref = itemRef,
    destination_ref = destinationRef,
    source_ref = PZAgent.Refs.buildContainer(ctx.session_id, parsed.container_tail),
    source_tail = parsed.container_tail,
    destination_tail = destinationParsed.tail,
    runtime_id = runtimeId,
    quantity = quantity,
  }
  if options.resolve == false then
    return spec
  end
  local item, resolveCode, resolveDetail, source = Toolkit.resolveItem(ctx, itemRef)
  if item == nil then
    return nil, resolveCode, resolveDetail
  end
  local destination, containerCode, containerDetail = Toolkit.resolveContainer(ctx, destinationRef)
  if destination == nil then
    return nil, containerCode, containerDetail
  end
  spec.item = item
  spec.source = source
  spec.destination = destination
  return spec
end

Inventory.transferSpec = transferSpec

--- Refuse a destination that cannot hold the item.
---
--- Only refused when both numbers were actually read: a build that reports
--- neither capacity nor contents weight leaves the engine to reject the
--- transfer, which is a QUEUE_REJECTED the caller can act on rather than a
--- guess made here.
local function checkCapacity(spec)
  local Toolkit = toolkit()
  local capacity = Toolkit.readNumberOf(spec.destination, { "getCapacity", "getMaxWeight" })
  local used = Toolkit.readNumberOf(spec.destination, { "getContentsWeight", "getCapacityWeight" })
  local weight = Toolkit.readNumberOf(spec.item, { "getUnequippedWeight", "getActualWeight", "getWeight" })
  if capacity == nil or used == nil or weight == nil then
    return true
  end
  if (used + weight) > capacity then
    return nil,
      Toolkit.reasons().CONTAINER_FULL,
      string.format("the destination holds %.2f of %.2f and the item weighs %.2f", used, capacity, weight)
  end
  return true
end

local Transfer = nil

local function transferValidate(_, args, ctx)
  local Toolkit = toolkit()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Transfer.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = transferSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local room, roomCode, roomDetail = checkCapacity(spec)
  if room == nil then
    return nil, roomCode, roomDetail
  end
  Toolkit.state(ctx).transfer = spec
  return true
end

--- Walk to whichever endpoint is out in the world, if either is.
---
--- Both endpoints are checked, and the source first: a bag on a shelf across the
--- room and a crate behind you are the same problem, and reaching the wrong one
--- would leave the transfer to fail at the engine.
local function transferPrepare(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = transferSpec(args, ctx, { resolve = false })
  if spec == nil then
    return nil, specCode, specDetail
  end
  local points = {}
  for _, tail in ipairs({ spec.source_tail, spec.destination_tail }) do
    local point = containerPoint(tail)
    if point ~= nil then
      points[#points + 1] = point
    end
  end
  for index = 1, #points do
    local outcome, reachCode, reachDetail = Toolkit.approach(ctx, points[index])
    if outcome == nil then
      return nil, reachCode, reachDetail
    end
    if outcome == "walking" then
      Toolkit.state(ctx).walking_to = points[index]
      return true
    end
  end
  return true
end

local function transferStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = transferSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local action, actionCode, actionDetail =
    Toolkit.construct("ISInventoryTransferAction", ctx.player, spec.item, spec.source, spec.destination)
  if action == nil then
    return nil, actionCode, actionDetail
  end
  return Toolkit.enqueue(ctx, action)
end

--- Both halves of the postcondition, observed.
local function verifyTransfer(spec, ctx, kind, extra)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local destination, destinationCode, destinationDetail = Toolkit.resolveContainer(ctx, spec.destination_ref)
  if destination == nil then
    return nil, destinationCode, destinationDetail
  end
  local arrived = Toolkit.countIdentity(destination, spec.runtime_id)
  if arrived == nil then
    return Toolkit.unavailable("ItemContainer.getItems")
  end
  if arrived ~= spec.quantity then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format("the destination holds %d of the item, not %d", arrived, spec.quantity)
  end
  local source, sourceCode, sourceDetail = Toolkit.resolveContainer(ctx, spec.source_ref)
  if source == nil then
    -- The origin is a world container that is no longer reachable or loaded.
    -- Half an observation is not the postcondition, so it fails rather than
    -- resting on the destination alone.
    return nil, sourceCode, sourceDetail
  end
  local left = Toolkit.countIdentity(source, spec.runtime_id)
  if left == nil then
    return Toolkit.unavailable("ItemContainer.getItems")
  end
  if left ~= 0 then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format("the item is in the destination but %d copies remain in the origin", left)
  end
  local observed = {
    kind = kind,
    item_ref = spec.item_ref,
    container_ref = spec.destination_ref,
    source_container_ref = spec.source_ref,
    destination_count = arrived,
    origin_count = left,
    quantity = spec.quantity,
  }
  if extra ~= nil then
    for key, value in pairs(extra) do
      observed[key] = value
    end
  end
  return observed
end

--- The before/after snapshots are not consulted: both halves of a transfer's
--- postcondition are facts about containers, and a snapshot of the character
--- cannot see a crate on the far side of the room.
local function transferVerify(_, _, _, args, ctx)
  local spec, code, detail = transferSpec(args, ctx, { resolve = false })
  if spec == nil then
    return nil, code, detail
  end
  return verifyTransfer(spec, ctx, "item_in_destination")
end

Transfer = toolkit().declare({
  name = "inventory.transfer",
  capability = toolkit().CAPABILITY.INVENTORY_TRANSFER,
  requires = REQUIRES,
  timeout_ms = Inventory.TIMEOUT_MS,
  poll_interval_ms = Inventory.POLL_MS,
  -- `origin` is in TRANSFER_ARGS but is deliberately not declared here: the
  -- sidecar fills it with a container *descriptor*, every declared kind is a
  -- scalar, and nothing in this file reads it. Declaring it as a string would
  -- accept a value no caller sends and ignore it; leaving it out means a
  -- command that carries one is refused by name.
  args = {
    item_ref = { type = ARG.REF, required = true, kinds = ITEM_KIND },
    -- Optional, and checked against the item reference's own container rather
    -- than trusted: the two disagreeing is the caller naming two objects.
    source_container_ref = { type = ARG.REF, kinds = CONTAINER_KIND },
    destination_container_ref = { type = ARG.REF, required = true, kinds = CONTAINER_KIND },
    -- One item reference names one item, so the only quantity is one.
    quantity = { type = ARG.NUMBER, integer = true, min = Inventory.QUANTITY, max = Inventory.QUANTITY },
  },
  validate = transferValidate,
  prepare = transferPrepare,
  begin = transferStart,
  verify = transferVerify,
})

Inventory.Transfer = Transfer
toolkit().register(Transfer)

-- ---------------------------------------------------------------------------
-- ensure_main
-- ---------------------------------------------------------------------------

local ENSURE_ARGS = { "item_ref", "destination_container_ref", "origin" }

--- ensure_main is a transfer whose destination is fixed to the main inventory.
local function ensureSpec(args, ctx, options)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, ENSURE_ARGS, { "item_ref" })
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local mainRef, mainError = PZAgent.Refs.playerMainContainer(ctx.session_id)
  if mainRef == nil then
    return nil, reasons.INTERNAL_ERROR, mainError
  end
  if args.destination_container_ref ~= nil and args.destination_container_ref ~= mainRef then
    -- build_args fills this in, so it has to be accepted -- but a caller naming
    -- a different container is asking for inventory.transfer, and silently
    -- rewriting it would run an action nobody requested.
    return nil, reasons.INVALID_ARGUMENT, "ensure_main only ever moves an item into the main inventory"
  end
  local itemRef, itemCode, itemDetail = Toolkit.readRef(args, "item_ref", PZAgent.Refs.KIND.ITEM, ctx)
  if itemRef == nil then
    return nil, itemCode, itemDetail
  end
  local parsed, parseError = PZAgent.Refs.parseItem(itemRef)
  if parsed == nil then
    return nil, reasons.INVALID_REF, parseError
  end
  if PZAgent.Refs.isPlayerMainTail(parsed.container_tail) then
    return nil, reasons.INVALID_ARGUMENT, "the item is already in the main inventory"
  end
  return transferSpec({
    item_ref = itemRef,
    destination_container_ref = mainRef,
  }, ctx, options)
end

Inventory.ensureSpec = ensureSpec

local EnsureMain = nil

local function ensureValidate(_, args, ctx)
  local Toolkit = toolkit()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(EnsureMain.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = ensureSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local room, roomCode, roomDetail = checkCapacity(spec)
  if room == nil then
    return nil, roomCode, roomDetail
  end
  Toolkit.state(ctx).transfer = spec
  return true
end

local function ensurePrepare(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = ensureSpec(args, ctx, { resolve = false })
  if spec == nil then
    return nil, specCode, specDetail
  end
  local point = containerPoint(spec.source_tail)
  local outcome, reachCode, reachDetail = Toolkit.approach(ctx, point)
  if outcome == nil then
    return nil, reachCode, reachDetail
  end
  return true
end

local function ensureStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = ensureSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local action, actionCode, actionDetail =
    Toolkit.construct("ISInventoryTransferAction", ctx.player, spec.item, spec.source, spec.destination)
  if action == nil then
    return nil, actionCode, actionDetail
  end
  return Toolkit.enqueue(ctx, action)
end

local function ensureVerify(_, _, _, args, ctx)
  local spec, code, detail = ensureSpec(args, ctx, { resolve = false })
  if spec == nil then
    return nil, code, detail
  end
  return verifyTransfer(spec, ctx, "item_in_main_inventory")
end

EnsureMain = toolkit().declare({
  name = "inventory.ensure_main",
  capability = toolkit().CAPABILITY.INVENTORY_TRANSFER,
  requires = REQUIRES,
  timeout_ms = Inventory.TIMEOUT_MS,
  poll_interval_ms = Inventory.POLL_MS,
  -- `origin` is left undeclared here for the reason it is left undeclared on
  -- inventory.transfer.
  args = {
    item_ref = { type = ARG.REF, required = true, kinds = ITEM_KIND },
    -- The sidecar's build_args fills this in with the main container, so it has
    -- to be accepted; ensureSpec refuses any other value rather than rewriting
    -- it, which is why it is declared but never required.
    destination_container_ref = { type = ARG.REF, kinds = CONTAINER_KIND },
  },
  validate = ensureValidate,
  prepare = ensurePrepare,
  begin = ensureStart,
  verify = ensureVerify,
})

Inventory.EnsureMain = EnsureMain
toolkit().register(EnsureMain)

--- Move `itemRef` into the main inventory as a step inside another action.
---
--- Medical and Consumption both need an item in hand before the game's own
--- action will accept it, and both must do it with the same transfer action and
--- the same ownership tag rather than reaching into a container themselves.
--- Returns "in_main", "moving" or nil plus a reason.
function Inventory.bringToMain(ctx, itemRef)
  local Toolkit = toolkit()
  local parsed, parseError = PZAgent.Refs.parseItem(itemRef)
  if parsed == nil then
    return nil, Toolkit.reasons().INVALID_REF, parseError
  end
  if PZAgent.Refs.isPlayerMainTail(parsed.container_tail) then
    return "in_main"
  end
  local spec, code, detail = ensureSpec({ item_ref = itemRef }, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local action, actionCode, actionDetail =
    Toolkit.construct("ISInventoryTransferAction", ctx.player, spec.item, spec.source, spec.destination)
  if action == nil then
    return nil, actionCode, actionDetail
  end
  local queued, queueCode, queueDetail = Toolkit.enqueue(ctx, action)
  if queued == nil then
    return nil, queueCode, queueDetail
  end
  return "moving"
end

return Inventory
