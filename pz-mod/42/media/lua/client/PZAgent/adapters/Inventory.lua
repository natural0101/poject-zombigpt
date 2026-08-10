--[[
PZAgent.Adapters.Inventory -- inventory.transfer, inventory.transfer_batch,
inventory.ensure_main and the bounded search the other adapters pick their
items with.

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
local ARG = { STRING = "string", NUMBER = "number", BOOLEAN = "boolean", REF = "ref", LIST = "list" }

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
-- transfer_batch
-- ---------------------------------------------------------------------------

--[[
inventory.transfer_batch -- several items into one destination, each moved by
the game's own ISInventoryTransferAction and each observed before the next is
queued. The game's queue order is not a postcondition, so the adapter never
queues item i+1 until item i is seen in the destination: capacity is re-checked
with the single transfer's own checkCapacity before every enqueue, against the
destination as it stands *then*, not as it stood when the command arrived.

The batch stops at the first item that cannot go: that item is recorded with
its own reason (CONTAINER_FULL carrying the weights, or whatever refused it),
every later item is recorded as CANCELLED_BY_REQUEST "the batch stopped before
this item", and the terminal is a FAILED whose evidence carries the honest
partial record -- what landed, what stopped, why. Partial work is never
reported as full success; deciding what to do with a half-moved batch is the
planner's call, made on a record that does not lie.

Chosen semantics for an item already sitting in the destination: it is
transferred-without-work. The postcondition -- the item is observed in the
destination -- already holds, so nothing is queued for it and it counts in
`transferred`, never in `stopped`. The single transfer refuses this case
because a one-item command with nothing to do has no change to prove; a batch
records it and moves on, because refusing the whole list over one satisfied
item would force the planner to re-derive the remainder itself.

succeeded only when *every* requested item is observed in the destination
afterwards -- and, for the items that had to move, gone from their origin, the
same both-halves rule the single transfer enforces.
]]

--- Items one batch may carry. The same number as the dispatcher's
--- MAX_LIST_ITEMS ceiling, restated here for the reason ARG is: the engine
--- chooses the order it walks media/lua in, so this file must not index
--- another client module while it loads.
Inventory.MAX_BATCH_ITEMS = 8

--- Queue budget for each item past the first, on top of the single transfer's
--- TIMEOUT_MS. Linear in the list and nothing else.
Inventory.BATCH_ITEM_MS = 5000

--- Ceiling the proportional budget may never pass, and the timeout_ms the
--- runtime enforces on the whole command.
Inventory.BATCH_TIMEOUT_CAP_MS = 60000

--- The batch's time budget: 20000 ms for the first item plus 5000 ms for each
--- further one, capped at 60000. With eight items the sum is 55000, so today
--- the arithmetic cannot reach the cap; it is enforced anyway because
--- MAX_BATCH_ITEMS may grow and this bound must not drift up silently when it
--- does. Progress enforces this per-command number itself; the declared
--- timeout_ms is the cap, which is the tightest single figure the runtime's
--- static check can hold.
function Inventory.batchTimeoutMs(requested)
  local budget = Inventory.TIMEOUT_MS + Inventory.BATCH_ITEM_MS * (requested - 1)
  if budget > Inventory.BATCH_TIMEOUT_CAP_MS then
    return Inventory.BATCH_TIMEOUT_CAP_MS
  end
  return budget
end

local BATCH_ARGS = { "item_refs", "destination_container_ref" }

--- Parse and resolve a batch, keeping only static facts on the spec.
---
--- Every item reference is checked and -- unless options.resolve == false --
--- resolved up front, so a list with a stale reference at index 4 is refused
--- whole before anything is queued for indices 1..3. The spec holds refs,
--- tails and runtime ids, never engine handles: items move while the batch
--- runs, and a handle kept from validate would name whatever sits where the
--- item used to.
local function batchSpec(args, ctx, options)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  options = options or {}
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, BATCH_ARGS, BATCH_ARGS)
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local destinationRef, destCode, destDetail =
    Toolkit.readRef(args, "destination_container_ref", PZAgent.Refs.KIND.CONTAINER, ctx)
  if destinationRef == nil then
    return nil, destCode, destDetail
  end
  local destinationParsed, destinationError = PZAgent.Refs.parseContainer(destinationRef)
  if destinationParsed == nil then
    return nil, reasons.INVALID_REF, destinationError
  end
  local refs = args.item_refs
  if type(refs) ~= "table" then
    return nil, reasons.INVALID_ARGUMENT, 'argument "item_refs" must be a list of item references'
  end
  -- The dispatcher's LIST check hands adapters a fresh dense array, but this
  -- function is also called directly, so the same bounded walk runs here:
  -- count with pairs and refuse past the bound before trusting the table.
  local count = 0
  for _ in pairs(refs) do
    count = count + 1
    if count > Inventory.MAX_BATCH_ITEMS then
      return nil, reasons.INVALID_ARGUMENT,
        string.format("item_refs carries more than %d items", Inventory.MAX_BATCH_ITEMS)
    end
  end
  if count == 0 then
    return nil, reasons.INVALID_ARGUMENT, "item_refs must carry at least one item reference"
  end
  for index = 1, count do
    if refs[index] == nil then
      return nil, reasons.INVALID_ARGUMENT, "item_refs must be a dense array"
    end
  end
  local items = {}
  local seen = {}
  for index = 1, count do
    local ref = refs[index]
    if type(ref) ~= "string" or #ref == 0 then
      return nil, reasons.INVALID_ARGUMENT, string.format("item_refs[%d] must be a reference string", index)
    end
    if PZAgent.Refs.kindOf(ref) ~= PZAgent.Refs.KIND.ITEM then
      return nil, reasons.INVALID_REF, string.format("item_refs[%d] must be an item reference", index)
    end
    if not PZAgent.Refs.belongsToSession(ref, ctx.session_id) then
      return nil, reasons.INVALID_REF, string.format("item_refs[%d] was minted by another session", index)
    end
    -- A duplicate is one object asked to move twice; the second move could
    -- only succeed by lying about the first. The dispatcher already refuses
    -- this on the wire, and the direct-caller path must not be looser.
    if seen[ref] ~= nil then
      return nil, reasons.INVALID_ARGUMENT,
        string.format("item_refs[%d] repeats item_refs[%d]", index, seen[ref])
    end
    seen[ref] = index
    local parsed, parseError = PZAgent.Refs.parseItem(ref)
    if parsed == nil then
      return nil, reasons.INVALID_REF, string.format("item_refs[%d] (%s): %s", index, ref, tostring(parseError))
    end
    local runtimeId = tonumber(parsed.runtime_id)
    if runtimeId == nil then
      return nil, reasons.INVALID_REF,
        string.format("item_refs[%d] (%s) carries no numeric identity", index, ref)
    end
    items[index] = {
      ref = ref,
      runtime_id = runtimeId,
      source_tail = parsed.container_tail,
      source_ref = PZAgent.Refs.buildContainer(ctx.session_id, parsed.container_tail),
      already_there = parsed.container_tail == destinationParsed.tail,
    }
  end
  local spec = {
    destination_ref = destinationRef,
    destination_tail = destinationParsed.tail,
    items = items,
    requested = count,
  }
  if options.resolve == false then
    return spec
  end
  local destination, containerCode, containerDetail = Toolkit.resolveContainer(ctx, destinationRef)
  if destination == nil then
    return nil, containerCode, containerDetail
  end
  for index = 1, count do
    local item, resolveCode, resolveDetail = Toolkit.resolveItem(ctx, items[index].ref)
    if item == nil then
      return nil, resolveCode,
        string.format("item_refs[%d] (%s): %s", index, items[index].ref, tostring(resolveDetail))
    end
  end
  return spec
end

Inventory.batchSpec = batchSpec

local TransferBatch = nil

--- The destination's item-count change since begin, or nil when it cannot be
--- read right now. Nil rather than zero: an unreadable container is never read
--- as an unchanged one.
local function destinationDelta(ctx, batch)
  local Toolkit = toolkit()
  if batch.destination_count_before == nil then
    return nil
  end
  local destination = (Toolkit.resolveContainer(ctx, batch.spec.destination_ref))
  if destination == nil then
    return nil
  end
  local now = Toolkit.containerItems(destination)
  if now == nil then
    return nil
  end
  return now.total - batch.destination_count_before
end

--- The evidence shape both terminals share. Both lists are marked as JSON
--- arrays because either may legitimately be empty, and an empty Lua table
--- would otherwise encode as an object.
local function batchEvidence(spec, transferred, stopped, delta)
  local evidence = {
    kind = "items_in_destination_container",
    destination_ref = spec.destination_ref,
    requested = spec.requested,
    transferred = PZAgent.Json.array(transferred),
    stopped = PZAgent.Json.array(stopped),
  }
  if delta ~= nil then
    evidence.destination_count_delta = delta
  end
  return evidence
end

--- End the batch at `stopIndex`, leaving the honest partial record for the
--- terminal ack to carry.
---
--- One guarded look at the in-flight item first: an interruption can land in
--- the window between the game moving the item and this adapter observing it,
--- and a record written blind would call landed work stopped. The record is
--- allowed to under-claim only when no single read can settle it; here one
--- can, so it is taken.
local function stopBatch(ctx, batch, stopIndex, code, detail)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec = batch.spec
  local flying = batch.in_flight
  if flying ~= nil and flying == stopIndex then
    local destination = (Toolkit.resolveContainer(ctx, spec.destination_ref))
    if destination ~= nil then
      local arrived = Toolkit.countIdentity(destination, spec.items[flying].runtime_id)
      if arrived ~= nil and arrived >= 1 then
        batch.transferred[#batch.transferred + 1] = spec.items[flying].ref
        batch.in_flight = nil
        batch.next_index = flying + 1
        stopIndex = flying + 1
      end
    end
  end
  local transferred = {}
  for index = 1, #batch.transferred do
    transferred[index] = batch.transferred[index]
  end
  local stopped = {}
  if stopIndex <= spec.requested then
    stopped[1] = { item_ref = spec.items[stopIndex].ref, reason_code = code, detail = detail }
    for index = stopIndex + 1, spec.requested do
      stopped[#stopped + 1] = {
        item_ref = spec.items[index].ref,
        reason_code = reasons.CANCELLED_BY_REQUEST,
        detail = "the batch stopped before this item",
      }
    end
  end
  batch.partial = batchEvidence(spec, transferred, stopped, destinationDelta(ctx, batch))
  return nil, code, detail
end

--- Queue the next item that needs work, or report the batch finished.
---
--- Already-in-destination items are recorded as transferred and skipped; the
--- first item that cannot be queued -- no room, gone from its container, the
--- action refused -- stops the batch through stopBatch. At most one transfer
--- is ever in the game's queue at a time, which is what makes the per-item
--- capacity check mean something: it runs against a destination whose weight
--- already includes every item this batch has landed.
local function advance(ctx, batch)
  local Toolkit = toolkit()
  local spec = batch.spec
  while batch.next_index <= spec.requested do
    local entry = spec.items[batch.next_index]
    if entry.already_there then
      batch.transferred[#batch.transferred + 1] = entry.ref
      batch.next_index = batch.next_index + 1
    else
      local destination, destCode, destDetail = Toolkit.resolveContainer(ctx, spec.destination_ref)
      if destination == nil then
        return stopBatch(ctx, batch, batch.next_index, destCode, destDetail)
      end
      local item, itemCode, itemDetail, source = Toolkit.resolveItem(ctx, entry.ref)
      if item == nil then
        return stopBatch(ctx, batch, batch.next_index, itemCode,
          string.format("item_refs[%d] (%s): %s", batch.next_index, entry.ref, tostring(itemDetail)))
      end
      local room, roomCode, roomDetail = checkCapacity({ destination = destination, item = item })
      if room == nil then
        return stopBatch(ctx, batch, batch.next_index, roomCode, roomDetail)
      end
      local action, actionCode, actionDetail =
        Toolkit.construct("ISInventoryTransferAction", ctx.player, item, source, destination)
      if action == nil then
        return stopBatch(ctx, batch, batch.next_index, actionCode, actionDetail)
      end
      local queued, queueCode, queueDetail = Toolkit.enqueue(ctx, action)
      if queued == nil then
        return stopBatch(ctx, batch, batch.next_index, queueCode, queueDetail)
      end
      batch.in_flight = batch.next_index
      return true
    end
  end
  return "done"
end

local function batchValidate(_, args, ctx)
  local Toolkit = toolkit()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(TransferBatch.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = batchSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  -- No capacity pre-check here, unlike the single transfer: capacity is a fact
  -- about the destination at each enqueue, and a batch whose first item does
  -- not fit fails the same way as one whose fourth does not -- through the
  -- per-item check, with the partial record saying exactly where it stopped.
  Toolkit.state(ctx).batch = { spec = spec, next_index = 1, transferred = {} }
  return true
end

--- Walk to whichever endpoint is out in the world, sources first, the way the
--- single transfer does. One walk per prepare; a batch whose sources sit on
--- two far squares reaches the second when the game's own transfer action
--- closes the remaining distance, exactly as it would for a lone transfer.
local function batchPrepare(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local state = Toolkit.state(ctx)
  local spec = state.batch ~= nil and state.batch.spec or nil
  if spec == nil then
    local built, specCode, specDetail = batchSpec(args, ctx, { resolve = false })
    if built == nil then
      return nil, specCode, specDetail
    end
    spec = built
  end
  local points = {}
  for index = 1, spec.requested do
    local point = containerPoint(spec.items[index].source_tail)
    if point ~= nil then
      points[#points + 1] = point
    end
  end
  local destinationPoint = containerPoint(spec.destination_tail)
  if destinationPoint ~= nil then
    points[#points + 1] = destinationPoint
  end
  for index = 1, #points do
    local outcome, reachCode, reachDetail = Toolkit.approach(ctx, points[index])
    if outcome == nil then
      return nil, reachCode, reachDetail
    end
    if outcome == "walking" then
      state.walking_to = points[index]
      return true
    end
  end
  return true
end

local function batchStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local state = Toolkit.state(ctx)
  local batch = state.batch
  if batch == nil then
    local spec, specCode, specDetail = batchSpec(args, ctx)
    if spec == nil then
      return nil, specCode, specDetail
    end
    batch = { spec = spec, next_index = 1, transferred = {} }
    state.batch = batch
  end
  -- The baseline for destination_count_delta is read before anything is
  -- queued. A destination that cannot be read is a capability gap up front,
  -- with nothing attempted and so nothing to record partially.
  local destination, destCode, destDetail = Toolkit.resolveContainer(ctx, batch.spec.destination_ref)
  if destination == nil then
    return nil, destCode, destDetail
  end
  local before = Toolkit.containerItems(destination)
  if before == nil then
    return Toolkit.unavailable("ItemContainer.getItems")
  end
  batch.destination_count_before = before.total
  batch.deadline_ms = (ctx.now_ms or 0) + Inventory.batchTimeoutMs(batch.spec.requested)
  return advance(ctx, batch)
end

local function batchProgress(_, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local state = Toolkit.state(ctx)
  local batch = state.batch
  if batch == nil then
    return nil, reasons.INTERNAL_ERROR, "the batch was polled before it started"
  end
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return stopBatch(ctx, batch, batch.in_flight or batch.next_index, code, detail)
  end
  if batch.deadline_ms ~= nil and (ctx.now_ms or 0) > batch.deadline_ms then
    return stopBatch(ctx, batch, batch.in_flight or batch.next_index, reasons.ACTION_TIMEOUT,
      string.format("the batch has run past its %d ms budget", Inventory.batchTimeoutMs(batch.spec.requested)))
  end
  local flying = batch.in_flight
  if flying == nil then
    return advance(ctx, batch)
  end
  local entry = batch.spec.items[flying]
  local destination, destCode, destDetail = Toolkit.resolveContainer(ctx, batch.spec.destination_ref)
  if destination == nil then
    return stopBatch(ctx, batch, flying, destCode, destDetail)
  end
  local arrived = Toolkit.countIdentity(destination, entry.runtime_id)
  if arrived == nil then
    return stopBatch(ctx, batch, flying, reasons.CAPABILITY_UNAVAILABLE,
      "ItemContainer.getItems is not available in this build")
  end
  if arrived >= 1 then
    batch.transferred[#batch.transferred + 1] = entry.ref
    batch.in_flight = nil
    batch.next_index = flying + 1
    return advance(ctx, batch)
  end
  local queue, queueCode, queueDetail = Toolkit.queueProgress(ctx)
  if queue == nil then
    return stopBatch(ctx, batch, flying, queueCode, queueDetail)
  end
  if queue == "done" then
    -- The game finished everything it was given and the item is still not in
    -- the destination: the transfer was dropped or undone, and waiting longer
    -- would only turn a clear answer into a timeout.
    return stopBatch(ctx, batch, flying, reasons.POSTCONDITION_FAILED,
      string.format("the queue drained but item_refs[%d] is not in the destination", flying))
  end
  return true
end

--- Every requested item observed in the destination -- and, for the items
--- that had to move, gone from their origin. The before/after snapshots are
--- not consulted for the reason transferVerify does not consult them: the
--- postcondition is a fact about containers, some of which are not on the
--- character.
local function batchVerify(_, _, _, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local batch = Toolkit.state(ctx).batch
  local spec
  if batch ~= nil then
    spec = batch.spec
  else
    local built, code, detail = batchSpec(args, ctx, { resolve = false })
    if built == nil then
      return nil, code, detail
    end
    spec = built
  end
  local destination, destCode, destDetail = Toolkit.resolveContainer(ctx, spec.destination_ref)
  if destination == nil then
    return nil, destCode, destDetail
  end
  local transferred = {}
  for index = 1, spec.requested do
    local entry = spec.items[index]
    local arrived = Toolkit.countIdentity(destination, entry.runtime_id)
    if arrived == nil then
      return Toolkit.unavailable("ItemContainer.getItems")
    end
    if arrived ~= 1 then
      return nil, reasons.POSTCONDITION_FAILED,
        string.format("the destination holds %d of item_refs[%d], not 1", arrived, index)
    end
    if not entry.already_there then
      local source, sourceCode, sourceDetail = Toolkit.resolveContainer(ctx, entry.source_ref)
      if source == nil then
        return nil, sourceCode, sourceDetail
      end
      local left = Toolkit.countIdentity(source, entry.runtime_id)
      if left == nil then
        return Toolkit.unavailable("ItemContainer.getItems")
      end
      if left ~= 0 then
        return nil, reasons.POSTCONDITION_FAILED,
          string.format("item_refs[%d] is in the destination but %d copies remain in the origin", index, left)
      end
    end
    transferred[index] = entry.ref
  end
  local delta = nil
  if batch ~= nil then
    delta = destinationDelta(ctx, batch)
  end
  return batchEvidence(spec, transferred, {}, delta)
end

TransferBatch = toolkit().declare({
  name = "inventory.transfer_batch",
  capability = toolkit().CAPABILITY.INVENTORY_TRANSFER,
  requires = REQUIRES,
  timeout_ms = Inventory.BATCH_TIMEOUT_CAP_MS,
  poll_interval_ms = Inventory.POLL_MS,
  args = {
    item_refs = {
      type = ARG.LIST,
      of = ARG.REF,
      required = true,
      max_items = Inventory.MAX_BATCH_ITEMS,
      kinds = ITEM_KIND,
    },
    destination_container_ref = { type = ARG.REF, required = true, kinds = CONTAINER_KIND },
  },
  validate = batchValidate,
  prepare = batchPrepare,
  begin = batchStart,
  progress = batchProgress,
  verify = batchVerify,
})

-- The declared start/poll speak the three-value convention, which has no slot
-- for evidence on a refusal -- and a batch that stopped partway owes the
-- terminal ack the partial record. These wrappers translate a refusal that
-- left one into the outcome-table shape ActionRuntime already accepts, and
-- give the running poll the fraction the three-value form drops. The
-- lifecycle steps themselves are untouched, so the direct-caller tests see
-- the same behaviour the runtime does.
local declaredBatchStart = TransferBatch.start
local declaredBatchPoll = TransferBatch.poll

local function carryPartial(ctx, first, second, third)
  if first == nil then
    local batch = toolkit().state(ctx).batch
    if batch ~= nil and batch.partial ~= nil then
      return { failed = true, reason_code = second, detail = third, evidence = batch.partial }
    end
  end
  return first, second, third
end

TransferBatch.start = function(ctx, args)
  return carryPartial(ctx, declaredBatchStart(ctx, args))
end

TransferBatch.poll = function(ctx, args)
  local first, second, third = declaredBatchPoll(ctx, args)
  if first == true then
    local batch = toolkit().state(ctx).batch
    if batch ~= nil and batch.spec ~= nil and batch.spec.requested > 0 then
      -- Items landed over items requested: the only measure of a batch that
      -- means anything to a progress bar.
      return true, #batch.transferred / batch.spec.requested
    end
    return true
  end
  return carryPartial(ctx, first, second, third)
end

Inventory.TransferBatch = TransferBatch
toolkit().register(TransferBatch)

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
