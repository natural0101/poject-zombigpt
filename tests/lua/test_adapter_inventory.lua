-- PZAgent.Adapters.Inventory: moving one item, and proving both halves of it.
--
-- The two groups that carry the file are "verify refuses a transfer that only
-- half happened" and "an unreadable container is not an empty one". Everything
-- else is argument hygiene; those two are the difference between an ack that
-- means something and one that does not.

local ROOT = arg[0]:match("^(.*)test_adapter_inventory%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
Support.loadModules(Harness.root, { "Inventory" })
-- The declaration an adapter carries is worth exactly what the dispatcher makes
-- of it, so the gate is loaded and asked rather than restated here.
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains
local same = Harness.same

local Toolkit = PZ.Adapters.Toolkit
local Inventory = PZ.Adapters.Inventory
local Search = Inventory.Search
local Transfer = Inventory.Transfer
local EnsureMain = Inventory.EnsureMain
local REASON = PZ.Protocol.REASON

--- The transfer action moves the item when -- and only when -- it is performed.
local function installTransfer(options)
  return Support.installAction("ISInventoryTransferAction", function(_, args)
    Support.moveItem(args[2], args[3], args[4])
  end, options)
end

--- A character holding a satchel, standing five squares from a crate.
local function scene(options)
  options = options or {}
  local apple = Support.item({ id = 1, full_type = "Base.Apple", name = "Apple", weight = 0.3, hunger_change = -15 })
  local water = Support.item({ id = 5, full_type = "Base.WaterBottleFull", name = "Water Bottle", thirst_change = -30 })
  local satchel = Support.item({ id = 99, full_type = "Base.Bag_Satchel", name = "Satchel", contents = { water } })
  local book = Support.item({ id = 7, full_type = "Base.Book", name = "Book", pages = 120, pages_read = 4 })
  local crateContainer = Support.container({ book }, options.crate_capacity)
  local crate = Support.worldObject({ name = "crate", container = crateContainer })
  local main = Support.container({ apple, satchel }, options.main_capacity)
  local player = Support.player({ x = 100, y = 200, z = 0, inventory = main })
  Support.installCell({ [Support.squareKey(105, 200, 0)] = Support.square(105, 200, 0, { crate }) })
  Support.installAction("ISWalkToTimedAction")
  local transfer = installTransfer(options.transfer)
  local queue = Support.installQueue(options.queue)
  local ctx = Support.context({ player = player, safety = options.safety })
  return {
    player = player,
    ctx = ctx,
    transfer = transfer,
    queue = queue,
    main = main,
    satchel = satchel.container,
    crate = crateContainer,
    apple = apple,
    water = water,
    book = book,
  }
end

local MAIN = Support.mainRef()
local SATCHEL = "container:" .. Support.SESSION .. ":carried:99"
local CRATE = Support.worldContainerRef(105, 200, 0, 0)

Harness.group("transfer refuses a command it cannot act on")
do
  local s = scene()
  local function refuse(args)
    local accepted, code, detail = Transfer:validate(args, s.ctx)
    isNil(accepted, "the transfer is refused")
    return code, detail
  end

  local code = refuse({ item_ref = Support.itemRef("player-main", 1) })
  equal(code, REASON.INVALID_ARGUMENT, "a transfer with no destination is not a transfer")

  code = refuse({ item_ref = Support.itemRef("player-main", 1), destination_container_ref = SATCHEL, hurry = true })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  code = refuse({
    item_ref = Support.itemRef("player-main", 1),
    destination_container_ref = SATCHEL,
    quantity = 2,
  })
  equal(code, REASON.INVALID_ARGUMENT, "one item reference names one item, so two is not a quantity")

  code = refuse({ item_ref = Support.itemRef("player-main", 1), destination_container_ref = MAIN })
  equal(code, REASON.INVALID_ARGUMENT, "moving an item to where it already is has no postcondition to prove")

  code = refuse({
    item_ref = Support.itemRef("player-main", 1, "11111111-2222-3333-4444-555555555555"),
    destination_container_ref = SATCHEL,
  })
  equal(code, REASON.INVALID_REF, "a reference from another session names an object nobody here minted")

  code = refuse({ item_ref = Support.itemRef("player-main", 404), destination_container_ref = SATCHEL })
  equal(code, REASON.INVALID_REF, "an item that is not in the container the reference names is a stale reference")

  code = refuse({
    item_ref = Support.itemRef("player-main", 1),
    source_container_ref = SATCHEL,
    destination_container_ref = CRATE,
  })
  equal(code, REASON.INVALID_ARGUMENT, "a source that disagrees with the item reference describes another object")

  code = refuse({ item_ref = Support.itemRef("player-main", 1), destination_container_ref = "container:nope" })
  equal(code, REASON.INVALID_REF, "a malformed destination is an invalid reference")
end

Harness.group("a missing transfer class costs a capability, not a crash")
do
  local s = scene()
  Support.removeAction("ISInventoryTransferAction")
  local accepted, code, detail = Transfer:validate({
    item_ref = Support.itemRef("player-main", 1),
    destination_container_ref = SATCHEL,
  }, s.ctx)
  isNil(accepted, "with no transfer action there is nothing to validate")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is reported as a capability")
  contains(detail, "ISInventoryTransferAction", "naming the symbol this build did not have")
  installTransfer()
end

Harness.group("a destination with no room is refused before anything is queued")
do
  local s = scene({ main_capacity = 1 })
  local heavy = Support.item({ id = 42, full_type = "Base.Sledgehammer", name = "Sledgehammer", weight = 12 })
  table.insert(s.crate.entries, heavy)
  local accepted, code, detail = Transfer:validate({
    item_ref = Support.itemRef("world:105:200:0:0:0", 42),
    destination_container_ref = MAIN,
  }, s.ctx)
  isNil(accepted, "a bag that cannot hold it is not a destination")
  equal(code, REASON.CONTAINER_FULL, "which is a full container")
  contains(detail, "12", "and the detail carries the weight that did not fit")
  equal(#s.queue.added, 0, "nothing was queued")
end

Harness.group("prepare walks to a container that is out of reach")
do
  local s = scene()
  local args = { item_ref = Support.itemRef("world:105:200:0:0:0", 7), destination_container_ref = MAIN }
  ok(Transfer:validate(args, s.ctx), "a crate on a loaded square is a legitimate source")
  ok(Transfer:prepare(args, s.ctx), "prepare succeeds")
  equal(#s.queue.added, 1, "one walk was queued because the crate is five squares off")
  equal(s.queue.added[1].Type, "ISWalkToTimedAction", "and it is a walk, not a transfer")

  -- Standing on the crate, nothing needs walking to.
  local near = scene()
  near.player.state.x = 105
  local nearArgs = { item_ref = Support.itemRef("world:105:200:0:0:0", 7), destination_container_ref = MAIN }
  ok(Transfer:validate(nearArgs, near.ctx), "the same command validates from beside the crate")
  ok(Transfer:prepare(nearArgs, near.ctx), "and prepares")
  equal(#near.queue.added, 0, "with no walk queued at all")
end

Harness.group("prepare and begin stop for the player and for a horde")
do
  local taken = scene({ safety = Support.takenOver() })
  local args = { item_ref = Support.itemRef("player-main", 1), destination_container_ref = SATCHEL }
  local prepared, prepareCode = Transfer:prepare(args, taken.ctx)
  isNil(prepared, "a taken-over character prepares nothing")
  equal(prepareCode, REASON.USER_TAKEOVER, "the player is driving")

  local threatened = scene({ safety = Support.threatened() })
  local started, startCode = Transfer:begin(args, threatened.ctx)
  isNil(started, "and nothing starts into a horde")
  equal(startCode, REASON.THREAT_INTERRUPTED, "the reflex guard's level is a reason to stop")
  equal(#threatened.queue.added, 0, "with nothing left in the queue")
end

Harness.group("begin queues one tagged transfer built from the resolved handles")
do
  local s = scene()
  local args = { item_ref = Support.itemRef("player-main", 1), destination_container_ref = SATCHEL }
  ok(Transfer:validate(args, s.ctx), "the command validates")
  ok(Transfer:begin(args, s.ctx), "and starts")
  equal(#s.transfer.actions, 1, "one transfer action was constructed")
  local built = s.transfer.actions[1]
  equal(built.args[1], s.player, "for this character")
  equal(built.args[2], s.apple, "carrying the item the reference resolved to")
  equal(built.args[3], s.main, "out of the container it names")
  equal(built.args[4], s.satchel, "and into the destination")

  local entries = PZ.Safety.describeQueue(s.player)
  equal(PZ.Ownership.describe(entries, s.ctx.session_id).ownership, PZ.Protocol.OWNERSHIP.MOD, "tagged as ours")
end

Harness.group("verify refuses a transfer that was queued but never happened")
do
  local s = scene()
  local args = { item_ref = Support.itemRef("player-main", 1), destination_container_ref = SATCHEL }
  ok(Transfer:validate(args, s.ctx), "the command validates")
  ok(Transfer:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)

  -- The queue drains. The mod would ack. The apple never moved.
  Support.drainQueue(s.queue)
  local evidence, code, detail = Transfer:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "an item still in the origin is not an item in the destination")
  equal(code, REASON.POSTCONDITION_FAILED, "so the transfer failed its postcondition")
  contains(detail, "not 1", "and the detail says how many the destination actually holds")

  s.transfer.actions[1]:perform()
  local proof = Transfer:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "once the action runs the item is observed in the destination")
  equal(proof.kind, "item_in_destination", "named for the postcondition")
  equal(proof.item_ref, args.item_ref, "carrying the item that moved")
  equal(proof.container_ref, SATCHEL, "and the container it moved into")
  equal(proof.destination_count, 1, "with the destination count that was counted")
  equal(proof.origin_count, 0, "and the origin count that was counted")
end

Harness.group("an item that arrived without leaving is not a transfer")
do
  local s = scene()
  local args = { item_ref = Support.itemRef("player-main", 1), destination_container_ref = SATCHEL }
  ok(Transfer:validate(args, s.ctx), "the command validates")
  -- A copy in both places: the destination half holds, the origin half does not.
  table.insert(s.satchel.entries, s.apple)
  local evidence, code, detail = Transfer:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "an item in two places at once is not one that moved")
  equal(code, REASON.POSTCONDITION_FAILED, "the postcondition is both halves or neither")
  contains(detail, "remain in the origin", "and the detail names the half that failed")
end

Harness.group("a container that stopped answering is not an empty container")
do
  local s = scene()
  local args = { item_ref = Support.itemRef("player-main", 1), destination_container_ref = SATCHEL }
  ok(Transfer:validate(args, s.ctx), "the command validates")
  s.satchel.getItems = nil
  local evidence, code, detail = Transfer:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "a destination that cannot be read proves nothing")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "which is a capability gap, not a failed postcondition")
  contains(detail, "getItems", "naming what could not be read")
end

Harness.group("ensure_main only ever moves things into the main inventory")
do
  local s = scene()
  local already, alreadyCode = EnsureMain:validate({ item_ref = Support.itemRef("player-main", 1) }, s.ctx)
  isNil(already, "an item already in the main inventory has nothing to ensure")
  equal(alreadyCode, REASON.INVALID_ARGUMENT, "and asking is an argument error")

  local elsewhere, elsewhereCode = EnsureMain:validate({
    item_ref = Support.itemRef("carried:99", 5),
    destination_container_ref = SATCHEL,
  }, s.ctx)
  isNil(elsewhere, "a destination that is not the main inventory is a different command")
  equal(elsewhereCode, REASON.INVALID_ARGUMENT, "so it is refused rather than rewritten")

  local args = { item_ref = Support.itemRef("carried:99", 5) }
  ok(EnsureMain:validate(args, s.ctx), "pulling the bottle out of the satchel validates")
  ok(EnsureMain:prepare(args, s.ctx), "a carried bag needs no walking to")
  equal(#s.queue.added, 0, "so nothing was queued by prepare")
  ok(EnsureMain:begin(args, s.ctx), "and the transfer starts")

  local stillInside, code = EnsureMain:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  isNil(stillInside, "a bottle still in the satchel is not in the main inventory")
  equal(code, REASON.POSTCONDITION_FAILED, "the postcondition has not held yet")

  s.transfer.actions[1]:perform()
  local proof = EnsureMain:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "once it runs the bottle is observed in the main inventory")
  equal(proof.kind, "item_in_main_inventory", "named for that postcondition")
  equal(proof.container_ref, MAIN, "and the destination is the main container reference")
end

Harness.group("bringToMain is the same transfer, tagged the same way")
do
  local s = scene()
  equal(Inventory.bringToMain(s.ctx, Support.itemRef("player-main", 1)), "in_main", "already there means no work")

  local outcome = Inventory.bringToMain(s.ctx, Support.itemRef("carried:99", 5))
  equal(outcome, "moving", "an item in a bag is fetched")
  equal(#s.queue.added, 1, "with one queued action")
  local entries = PZ.Safety.describeQueue(s.player)
  equal(PZ.Ownership.describe(entries, s.ctx.session_id).ownership, PZ.Protocol.OWNERSHIP.MOD, "and it is ours")

  local missing, code = Inventory.bringToMain(s.ctx, Support.itemRef("carried:99", 404))
  isNil(missing, "an item that is not there cannot be fetched")
  equal(code, REASON.INVALID_REF, "which is a stale reference")
end

Harness.group("search enumerates candidates without choosing between them")
do
  local s = scene()
  local everything = Inventory.search(s.ctx, {})
  equal(#everything, 3, "the apple, the satchel and the bottle inside it")

  local food = Inventory.search(s.ctx, { edible = true })
  equal(#food, 1, "exactly one edible item")
  equal(food[1].record.full_type, "Base.Apple", "and it is the apple")
  equal(food[1].ref, Support.itemRef("player-main", 1), "carrying a reference that resolves back to it")

  local resolved = Toolkit.resolveItem(s.ctx, food[1].ref)
  equal(resolved, s.apple, "the reference search minted resolves to the item search found")

  local drink = Inventory.search(s.ctx, { drinkable = true })
  equal(#drink, 1, "one drinkable item")
  equal(drink[1].record.container, "carried:99", "found inside the satchel, not the main inventory")

  local byType = Inventory.search(s.ctx, { type_prefix = "Base.Bag_" })
  equal(#byType, 1, "one bag by type prefix")

  local byName = Inventory.search(s.ctx, { name_contains = "bottle" })
  equal(#byName, 1, "and one by case-insensitive name")

  local capped = Inventory.search(s.ctx, { limit = 1 })
  equal(#capped, 1, "a limit is honoured")
  equal(capped.truncated, true, "and the caller is told the walk was cut short")

  local none = Inventory.search(s.ctx, { full_type = "Base.Katana" })
  equal(#none, 0, "nothing matches a type that is not there")
  local first, code = Inventory.firstMatch(s.ctx, { full_type = "Base.Katana" }, REASON.NO_SAFE_FOOD, "nothing edible")
  isNil(first, "and firstMatch reports it")
  equal(code, REASON.NO_SAFE_FOOD, "with the domain code the caller supplied")
end

Harness.group("the search adapter queues nothing and reports the walk it made")
do
  local s = scene()
  local refused, code = Search:validate({ hurry = true }, s.ctx)
  isNil(refused, "an undeclared filter is refused rather than ignored")
  equal(code, REASON.INVALID_ARGUMENT, "which is an argument error")

  local wide, wideCode = Search:validate({ limit = 99 }, s.ctx)
  isNil(wide, "and a limit above the result bound is refused")
  equal(wideCode, REASON.INVALID_ARGUMENT, "for the same reason")

  local args = { edible = true }
  ok(Search:validate(args, s.ctx), "an edible filter validates")
  ok(Search:begin(args, s.ctx), "and begins, because a search has nothing to queue")
  equal(#s.queue.added, 0, "with nothing queued in the world at all")
  equal(Search:progress(args, s.ctx), "done", "a read is finished the moment it starts")

  local proof = Search:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "the walk over the character is the evidence")
  equal(proof.kind, "inventory_searched", "named for what was observed")
  equal(proof.match_count, 1, "one edible item was on the character")
  equal(proof.matches[1].full_type, "Base.Apple", "and it is the apple")
  equal(proof.matches[1].item_ref, Support.itemRef("player-main", 1), "carrying a reference back to it")
  equal(proof.truncated, false, "with nothing cut short")
end

Harness.group("the dispatcher builds the argument table from the declaration")
do
  local Dispatcher = PZ.CommandDispatcher
  local registry = Dispatcher.new()
  -- inventory.search declares eight filters, which is CommandDispatcher.MAX_ARGS
  -- exactly: this registration is what would fail if a ninth were ever added.
  ok(registry:register(Search), "the search declaration is one the dispatcher accepts")
  ok(registry:register(Transfer), "and so is transfer's")
  ok(registry:register(EnsureMain), "and ensure_main's")

  local APPLE = Support.itemRef("player-main", 1)
  same(
    Dispatcher.checkArgs(Transfer, { item_ref = APPLE, destination_container_ref = SATCHEL }, Support.SESSION),
    { item_ref = APPLE, destination_container_ref = SATCHEL },
    "a declared transfer reaches the adapter with the keys it declared"
  )

  local descriptor, code, detail = Dispatcher.checkArgs(Transfer, {
    item_ref = APPLE,
    destination_container_ref = SATCHEL,
    origin = { kind = "world" },
  }, Support.SESSION)
  isNil(descriptor, "the origin descriptor the sidecar carries is not a declared argument")
  equal(code, REASON.INVALID_ARGUMENT, "so it is refused")
  contains(detail, "origin", "by name, rather than dropped in silence")

  local container, containerCode = Dispatcher.checkArgs(Transfer, {
    item_ref = SATCHEL,
    destination_container_ref = MAIN,
  }, Support.SESSION)
  isNil(container, "a container reference is not an item reference")
  equal(containerCode, REASON.INVALID_REF, "and the declared kinds are what say so")

  local many, manyCode = Dispatcher.checkArgs(Transfer, {
    item_ref = APPLE,
    destination_container_ref = SATCHEL,
    quantity = 2,
  }, Support.SESSION)
  isNil(many, "two of an item one reference names is not a quantity")
  equal(manyCode, REASON.INVALID_ARGUMENT, "so the bound refuses it before an adapter sees it")

  same(
    Dispatcher.checkArgs(Search, { type_prefix = "Base.Bag_", limit = 4 }, Support.SESSION),
    { type_prefix = "Base.Bag_", limit = 4 },
    "a declared search arrives with its filters intact"
  )

  local uses, usesCode = Dispatcher.checkArgs(Search, { min_uses = -1 }, Support.SESSION)
  isNil(uses, "a negative uses filter is not a filter")
  equal(usesCode, REASON.INVALID_ARGUMENT, "which the declared floor catches")

  local flag, flagCode = Dispatcher.checkArgs(Search, { edible = "yes" }, Support.SESSION)
  isNil(flag, "a string where a flag was declared is refused")
  equal(flagCode, REASON.INVALID_ARGUMENT, "rather than read as truthy")

  local named, namedCode = Dispatcher.checkArgs(Search, { name_contains = "bottle" }, Support.SESSION)
  isNil(named, "name_contains is deliberately undeclared, so it never arrives")
  equal(namedCode, REASON.INVALID_ARGUMENT, "and is refused by name")

  same(
    Dispatcher.checkArgs(EnsureMain, { item_ref = Support.itemRef("carried:99", 5) }, Support.SESSION),
    { item_ref = Support.itemRef("carried:99", 5) },
    "ensure_main needs nothing but the item, since it fixes its own destination"
  )
end

Harness.group("the adapters are registered where the dispatcher looks for them")
do
  equal(PZ.Adapters.BY_NAME["inventory.search"], Search, "search is registered under its action name")
  equal(PZ.Adapters.BY_NAME["inventory.transfer"], Transfer, "transfer is registered under its action name")
  equal(PZ.Adapters.BY_NAME["inventory.ensure_main"], EnsureMain, "and so is ensure_main")
  equal(Transfer.action, "inventory.transfer", "the dispatcher's own key carries the action name")
  ok(PZ.Protocol.isKnownAction(Transfer.name), "all three names are in the protocol whitelist")
  equal(Transfer.capability, "inventory_transfer", "and both transfers declare the capability the sidecar gates on")
  equal(EnsureMain.capability, "inventory_transfer", "the same one, because it is the same move")
  isNil(Search.capability, "while a read claims no capability of its own")
  equal(type(Transfer.start), "function", "each adapter exposes the start the runtime calls")
  equal(type(Transfer.poll), "function", "and the poll it calls after it")
end

Harness.finish("adapter_inventory")
