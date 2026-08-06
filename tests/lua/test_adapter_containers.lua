-- PZAgent.Adapters.Containers: reading a container, and getting to one.
--
-- The group that carries the file is "a walk that ended without arriving". The
-- postcondition of open_nearby is a distance, and the only thing between "a
-- walk was queued" and an ack claiming the container is open is that verify
-- measures that distance instead of trusting the queue. The rest is argument
-- hygiene and the rule that a container which would not answer is never read as
-- an empty one.

local ROOT = arg[0]:match("^(.*)test_adapter_containers%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
Support.loadModules(Harness.root, { "Containers" })
-- The declaration an adapter carries is worth exactly what the dispatcher makes
-- of it, so the gate is loaded and asked rather than restated here.
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")

local equal, ok, isNil = Harness.equal, Harness.ok, Harness.isNil
local contains, same = Harness.contains, Harness.same

local Containers = PZ.Adapters.Containers
local Inspect = Containers.Inspect
local Open = Containers.Open
local REASON = PZ.Protocol.REASON
local ARG = PZ.CommandDispatcher.ARG

--- Walking puts the character on the square the action was built with -- and
--- only when the test performs it, so "the walk was queued" and "the character
--- arrived" stay two different facts.
local function installWalk()
  return Support.installAction("ISWalkToTimedAction", function(_, args)
    local player, destination = args[1], args[2]
    player.state.x = destination.getX()
    player.state.y = destination.getY()
    player.state.z = destination.getZ()
  end)
end

--- A crate five squares east of the character, with a bench beside it that
--- holds no container at all.
local function scene(options)
  options = options or {}
  local book = Support.item({ id = 7, full_type = "Base.Book", name = "Book", pages = 120 })
  local nails = Support.item({ id = 8, full_type = "Base.Nails", name = "Box of Nails", uses = 30 })
  local crateContainer = Support.container({ book, nails })
  local crate = Support.worldObject({ name = "crate", container = crateContainer })
  local bench = Support.worldObject({ name = "bench" })
  local bottle = Support.item({ id = 5, full_type = "Base.WaterBottle", name = "Water Bottle" })
  local satchel = Support.item({ id = 99, full_type = "Base.Bag_Satchel", name = "Satchel", contents = { bottle } })
  local apple = Support.item({ id = 1, full_type = "Base.Apple", name = "Apple", hunger_change = -15 })
  local main = Support.container({ apple, satchel })
  local player = Support.player({ x = options.x or 100, y = 200, z = 0, inventory = main })
  Support.installCell({ [Support.squareKey(105, 200, 0)] = Support.square(105, 200, 0, { crate, bench }) })
  local walk = installWalk()
  local queue = Support.installQueue()
  local ctx = Support.context({ player = player, safety = options.safety })
  return {
    player = player,
    ctx = ctx,
    walk = walk,
    queue = queue,
    crate = crateContainer,
    main = main,
    book = book,
  }
end

local CRATE = Support.worldContainerRef(105, 200, 0, 0)
local BENCH = Support.worldContainerRef(105, 200, 0, 1)
local UNLOADED = Support.worldContainerRef(300, 300, 0, 0)
local MAIN = Support.mainRef()
local SATCHEL = "container:" .. Support.SESSION .. ":carried:99"

Harness.group("inspect refuses a command it cannot act on")
do
  local s = scene()
  local function refuse(args)
    local accepted, code, detail = Inspect:validate(args, s.ctx)
    isNil(accepted, "the inspection is refused")
    return code, detail
  end

  local code = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "inspecting nothing in particular is not a command")

  code = refuse({ container_ref = CRATE, hurry = true })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  code = refuse({ container_ref = CRATE, limit = 0 })
  equal(code, REASON.INVALID_ARGUMENT, "a listing of nothing reports nothing")

  code = refuse({ container_ref = CRATE, limit = 1000 })
  equal(code, REASON.INVALID_ARGUMENT, "and a thousand descriptors is not an ack")

  code = refuse({ container_ref = Support.itemRef("player-main", 1) })
  equal(code, REASON.INVALID_REF, "an item reference does not name a container")

  code = refuse({ container_ref = Support.worldContainerRef(105, 200, 0, 0, "11111111-2222-3333-4444-555555555555") })
  equal(code, REASON.INVALID_REF, "a reference from another session names an object nobody here minted")

  local detail
  code, detail = refuse({ container_ref = UNLOADED })
  equal(code, REASON.TARGET_NOT_LOADED, "a square the game has not streamed in is not a missing container")
  contains(detail, "not loaded", "and the detail says so")

  code = refuse({ container_ref = BENCH })
  equal(code, REASON.INVALID_REF, "an object on the square that holds no container is a stale reference")
end

Harness.group("a container that will not answer is a capability gap, not an empty crate")
do
  local s = scene()
  Support.removeCell()
  local accepted, code, detail = Inspect:validate({ container_ref = CRATE }, s.ctx)
  isNil(accepted, "with no cell there is nothing to resolve the reference against")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is reported as a capability")
  contains(detail, "getCell", "naming the symbol this build did not have")

  local readable = scene()
  local args = { container_ref = CRATE }
  ok(Inspect:validate(args, readable.ctx), "with the cell back the crate resolves")
  readable.crate.getItems = nil
  local evidence, itemsCode, itemsDetail = Inspect:verify(nil, nil, args, readable.ctx)
  isNil(evidence, "a crate that stopped answering proves nothing about its contents")
  equal(itemsCode, REASON.CAPABILITY_UNAVAILABLE, "which is a capability gap, not an empty crate")
  contains(itemsDetail, "getItems", "naming what could not be read")
end

Harness.group("inspect reports what the read saw and queues nothing")
do
  local s = scene()
  local args = { container_ref = CRATE }
  ok(Inspect:validate(args, s.ctx), "a crate on a loaded square validates")
  ok(Inspect:begin(args, s.ctx), "and begins")
  equal(Inspect:progress(args, s.ctx), "done", "a read is finished the moment it starts")
  equal(#s.queue.added, 0, "with nothing queued in the world at all")

  local proof = Inspect:verify(nil, nil, args, s.ctx)
  ok(proof ~= nil, "the read itself is the evidence")
  equal(proof.kind, "container_contents", "named for what was observed")
  equal(proof.container_ref, CRATE, "carrying the container that was read")
  equal(proof.item_count, 2, "with the two items the crate holds")
  equal(proof.total_items, 2, "and the total it holds")
  equal(proof.truncated, false, "nothing was cut short")
  equal(proof.items[1].runtime_id, 7, "the first descriptor is the book")
  equal(proof.items[1].full_type, "Base.Book", "by full type")

  local capped = Inspect:verify(nil, nil, { container_ref = CRATE, limit = 1 }, s.ctx)
  equal(capped.item_count, 1, "a limit is honoured")
  equal(capped.total_items, 2, "the total still reports what is really in there")
  equal(capped.truncated, true, "and the caller is told the listing was cut short")

  local carried = Inspect:verify(nil, nil, { container_ref = MAIN }, s.ctx)
  equal(carried.item_count, 2, "the main inventory reads the same way as a crate")
end

Harness.group("open_nearby refuses a container there is no walking to")
do
  local s = scene()
  local function refuse(args)
    local accepted, code, detail = Open:validate(args, s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "opening nothing in particular is not a command")

  code = refuse({ container_ref = MAIN })
  equal(code, REASON.INVALID_ARGUMENT, "the main inventory travels with the character and is already open")

  code = refuse({ container_ref = SATCHEL })
  equal(code, REASON.INVALID_ARGUMENT, "and so does a bag being carried")

  code = refuse({ container_ref = CRATE, radius = 0 })
  equal(code, REASON.INVALID_ARGUMENT, "a reach no position satisfies is not a reach")

  code = refuse({ container_ref = CRATE, radius = 50 })
  equal(code, REASON.INVALID_ARGUMENT, "and a reach of fifty squares is not nearby")

  code = refuse({ container_ref = UNLOADED })
  equal(code, REASON.TARGET_NOT_LOADED, "a crate on an unstreamed square cannot be walked to yet")
end

Harness.group("a missing walk costs one capability, naming the symbol")
do
  local s = scene()
  Support.removeAction("ISWalkToTimedAction")
  local accepted, code, detail = Open:validate({ container_ref = CRATE }, s.ctx)
  isNil(accepted, "with no walk action there is nothing to validate")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "ISWalkToTimedAction", "naming the symbol this build did not have")

  local noQueue = scene()
  Support.removeQueue()
  local queued, queueCode, queueDetail = Open:validate({ container_ref = CRATE }, noQueue.ctx)
  isNil(queued, "and with no action queue there is nowhere to put the walk")
  equal(queueCode, REASON.CAPABILITY_UNAVAILABLE, "which is the same kind of gap")
  contains(queueDetail, "ISTimedActionQueue.add", "naming the other symbol the walk needs")
end

Harness.group("prepare walks to the crate, tagged as ours")
do
  local s = scene()
  local args = { container_ref = CRATE }
  ok(Open:validate(args, s.ctx), "a crate five squares off validates")
  ok(Open:prepare(args, s.ctx), "and prepares")
  equal(#s.queue.added, 1, "one action was queued because the crate is out of reach")
  equal(s.queue.added[1].Type, "ISWalkToTimedAction", "and it is a walk")
  local entries = PZ.Safety.describeQueue(s.player)
  equal(PZ.Ownership.describe(entries, s.ctx.session_id).ownership, PZ.Protocol.OWNERSHIP.MOD, "tagged as ours")

  ok(Open:begin(args, s.ctx), "the command begins behind the walk")
  equal(Open:progress(args, s.ctx), "running", "and waits while the walk is still ours to wait for")

  local near = scene({ x = 105 })
  local nearArgs = { container_ref = CRATE }
  ok(Open:validate(nearArgs, near.ctx), "the same command validates from beside the crate")
  ok(Open:prepare(nearArgs, near.ctx), "and prepares")
  equal(#near.queue.added, 0, "with no walk queued, because none was needed")
  equal(Open:progress(nearArgs, near.ctx), "done", "and it is done as soon as it is asked")
end

Harness.group("a walk that ended without arriving is not an open container")
do
  local s = scene()
  local args = { container_ref = CRATE }
  ok(Open:validate(args, s.ctx), "the command validates")
  ok(Open:prepare(args, s.ctx), "and queues its walk")
  ok(Open:begin(args, s.ctx), "and begins")

  -- The queue drains and the character never moved: pathfinding gave up against
  -- a wall, the game dropped the action, or a door was in the way. The mod would
  -- ack the container as open if progress and verify believed the queue.
  Support.drainQueue(s.queue)
  local status, code, detail = Open:progress(args, s.ctx)
  isNil(status, "an empty queue with the crate still five squares away is not arrival")
  equal(code, REASON.PATH_NOT_FOUND, "it is a walk that did not get there")
  contains(detail, "squares from the container", "and the detail carries the distance that was left")

  local evidence, verifyCode, verifyDetail = Open:verify(nil, nil, args, s.ctx)
  isNil(evidence, "and there is no evidence to promote the command with")
  equal(verifyCode, REASON.POSTCONDITION_FAILED, "so it fails its postcondition")
  contains(verifyDetail, "outside", "naming the reach the character is still outside of")
end

Harness.group("arriving is what produces the evidence")
do
  local s = scene()
  local args = { container_ref = CRATE }
  ok(Open:validate(args, s.ctx), "the command validates")
  ok(Open:prepare(args, s.ctx), "and queues its walk")
  ok(Open:begin(args, s.ctx), "and begins")

  s.walk.actions[1]:perform()
  Support.drainQueue(s.queue)
  equal(Open:progress(args, s.ctx), "done", "once the character is there the command is done")

  local proof = Open:verify(nil, nil, args, s.ctx)
  ok(proof ~= nil, "standing within reach with the contents readable is the postcondition")
  equal(proof.kind, "container_within_reach", "named for what was observed")
  equal(proof.container_ref, CRATE, "carrying the container that was reached")
  equal(proof.distance, 0, "with the distance that was measured")
  equal(proof.radius, Containers.DEFAULT_RADIUS, "against the reach that was asked for")
  equal(proof.item_count, 2, "and the contents that could be read from there")
  equal(proof.walked, true, "recording that this one took a walk")

  -- The crate stops answering after the walk: arrival alone is not the
  -- postcondition, because "open" means the contents can be read from here.
  s.crate.getItems = nil
  local evidence, code, detail = Open:verify(nil, nil, args, s.ctx)
  isNil(evidence, "a crate within reach that will not answer proves nothing")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "which is a capability gap")
  contains(detail, "getItems", "naming what could not be read")
end

Harness.group("open_nearby stops for the player and for a horde")
do
  local taken = scene({ safety = Support.takenOver() })
  local args = { container_ref = CRATE }
  local prepared, prepareCode = Open:prepare(args, taken.ctx)
  isNil(prepared, "a taken-over character prepares nothing")
  equal(prepareCode, REASON.USER_TAKEOVER, "the player is driving")
  equal(#taken.queue.added, 0, "and nothing was queued")

  local threatened = scene({ safety = Support.threatened() })
  local started, startCode = Open:begin(args, threatened.ctx)
  isNil(started, "and nothing begins into a horde")
  equal(startCode, REASON.THREAT_INTERRUPTED, "with the reflex guard's reason")
end

Harness.group("the dispatcher builds the argument table from the declaration")
do
  local Dispatcher = PZ.CommandDispatcher
  local registry = Dispatcher.new()
  ok(registry:register(Inspect), "the inspect declaration is one the dispatcher accepts")
  ok(registry:register(Open), "and so is open_nearby's")

  equal(Inspect.args.container_ref.type, ARG.REF, "the container is declared as a reference")
  equal(Inspect.args.limit.max, Containers.MAX_LISTED, "and the listing bound is the file's own")
  equal(Open.args.radius.max, Containers.MAX_RADIUS, "the reach ceiling is declared, not only read")

  same(
    Dispatcher.checkArgs(Inspect, { container_ref = CRATE, limit = 4 }, Support.SESSION),
    { container_ref = CRATE, limit = 4 },
    "a declared command reaches the adapter with the keys it declared"
  )

  local refused, code, detail = Dispatcher.checkArgs(Inspect, { container_ref = CRATE, hurry = true }, Support.SESSION)
  isNil(refused, "an undeclared key never reaches the adapter")
  equal(code, REASON.INVALID_ARGUMENT, "it is an argument error")
  contains(detail, "hurry", "naming the key that was not declared")

  local wide, wideCode = Dispatcher.checkArgs(Open, { container_ref = CRATE, radius = 50 }, Support.SESSION)
  isNil(wide, "a reach of fifty squares is a walk across the map")
  equal(wideCode, REASON.INVALID_ARGUMENT, "so the ceiling refuses it before an adapter sees it")

  local square, squareCode =
    Dispatcher.checkArgs(Open, { container_ref = Support.squareRef(105, 200, 0) }, Support.SESSION)
  isNil(square, "a square reference is not a container reference")
  equal(squareCode, REASON.INVALID_REF, "and the declared kinds are what say so")

  local empty, emptyCode = Dispatcher.checkArgs(Inspect, {}, Support.SESSION)
  isNil(empty, "a command with no container names nothing to inspect")
  equal(emptyCode, REASON.INVALID_ARGUMENT, "which the declaration's `required` catches")
end

Harness.group("the adapters are registered where the dispatcher looks for them")
do
  equal(PZ.Adapters.BY_NAME["container.inspect"], Inspect, "inspect is registered under its action name")
  equal(PZ.Adapters.BY_NAME["container.open_nearby"], Open, "and so is open_nearby")
  equal(Inspect.action, "container.inspect", "the dispatcher's own key carries the action name")
  ok(PZ.Protocol.isKnownAction(Open.name), "both names are in the protocol whitelist")
  equal(Open.capability, "move_to_square", "and walking to a container rides on the movement capability")
  isNil(Inspect.capability, "while reading one claims no capability of its own")
  equal(type(Inspect.start), "function", "each adapter exposes the start the runtime calls")
  equal(type(Inspect.poll), "function", "and the poll it calls after it")
end

Harness.finish("adapter_containers")
