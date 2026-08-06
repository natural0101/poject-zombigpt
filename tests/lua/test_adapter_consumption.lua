-- PZAgent.Adapters.Consumption: eating and drinking, and what counts as proof.
--
-- Two groups are the point of the file. "a build that reports no hunger" checks
-- that a missing stat falls through to a *different observation* rather than to
-- a shrug, and "a refilled container witnesses nothing" checks that the one case
-- where the item cannot be a witness is refused instead of glossed.

local ROOT = arg[0]:match("^(.*)test_adapter_consumption%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
Support.loadModules(Harness.root, { "Inventory", "Consumption" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local Toolkit = PZ.Adapters.Toolkit
local Consumption = PZ.Adapters.Consumption
local Eat = Consumption.Eat
local Drink = Consumption.Drink
local REASON = PZ.Protocol.REASON

--- Eating moves hunger and leaves less nourishment in what is left.
local function installEat()
  return Support.installAction("ISEatFoodAction", function(_, args)
    local player, item, fraction = args[1], args[2], args[3]
    if player.fields.stats ~= nil then
      local state = player.fields.stats.state
      state.hunger = math.max(0, (state.hunger or 0) - (0.4 * fraction))
    end
    item.fields.hunger_change = item.fields.hunger_change * (1 - fraction)
  end)
end

--- Drinking moves thirst and drains a unit out of the container.
local function installDrink()
  return Support.installAction("ISDrinkFromBottle", function(_, args)
    local player, item = args[1], args[2]
    if player.fields.stats ~= nil then
      local state = player.fields.stats.state
      state.thirst = math.max(0, (state.thirst or 0) - 0.3)
    end
    item.fields.uses = (item.fields.uses or 1) - 1
  end)
end

local function scene(options)
  options = options or {}
  local apple = Support.item({
    id = 1,
    full_type = "Base.Apple",
    name = "Apple",
    hunger_change = -15,
    rotten = options.rotten,
  })
  local bottle = Support.item({
    id = 5,
    full_type = "Base.WaterBottleFull",
    name = "Water Bottle",
    thirst_change = -30,
    uses = 10,
    fluid = 0.8,
    tainted = options.tainted_bottle,
  })
  local rock = Support.item({ id = 9, full_type = "Base.Rock", name = "Rock" })
  local biscuit = Support.item({ id = 3, full_type = "Base.Biscuit", name = "Biscuit", hunger_change = -8 })
  local satchel = Support.item({ id = 99, full_type = "Base.Bag_Satchel", name = "Satchel", contents = { biscuit } })
  local main = Support.container({ apple, bottle, rock, satchel })
  local stats
  if not options.no_stats then
    stats = Support.stats({ hunger = 0.6, thirst = 0.5 })
  end
  local player = Support.player({ x = 100, y = 200, z = 0, inventory = main, stats = stats })

  local sink = Support.worldObject({ name = "sink", water = options.source_water or 40, tainted = options.tainted })
  Support.installCell({ [Support.squareKey(101, 200, 0)] = Support.square(101, 200, 0, { sink }) })
  Support.installAction("ISWalkToTimedAction")
  Support.installAction("ISInventoryTransferAction", function(_, args)
    Support.moveItem(args[2], args[3], args[4])
  end)
  local eat = installEat()
  local drink = installDrink()
  local water = Support.installAction("ISTakeWaterAction", function(_, args)
    args[4].fields.uses = (args[4].fields.uses or 0) + 5
  end)
  local queue = Support.installQueue()
  local ctx = Support.context({ player = player, safety = options.safety })
  return {
    player = player,
    ctx = ctx,
    eat = eat,
    drink = drink,
    water = water,
    queue = queue,
    apple = apple,
    bottle = bottle,
    main = main,
    stats = stats,
  }
end

local APPLE = Support.itemRef("player-main", 1)
local BOTTLE = Support.itemRef("player-main", 5)
local ROCK = Support.itemRef("player-main", 9)
local BISCUIT = Support.itemRef("carried:99", 3)
local SINK = Support.squareRef(101, 200, 0)

Harness.group("eat refuses a command it cannot act on")
do
  local s = scene()
  local function refuse(args)
    local accepted, code, detail = Eat:validate(args, s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "eating nothing in particular is not a command")

  code = refuse({ item_ref = APPLE, portion = 0.5 })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  code = refuse({ item_ref = APPLE, fraction = 0 })
  equal(code, REASON.INVALID_ARGUMENT, "a portion too small to observe is not a portion")

  code = refuse({ item_ref = APPLE, fraction = 1.5 })
  equal(code, REASON.INVALID_ARGUMENT, "and more than the whole item is not either")

  code = refuse({ item_ref = ROCK })
  equal(code, REASON.PRECONDITION_FAILED, "an item with no hunger effect is not food")

  code = refuse({ item_ref = Support.itemRef("player-main", 404) })
  equal(code, REASON.INVALID_REF, "an item that is not there is a stale reference")

  local rotten = scene({ rotten = true })
  local rottenCode, rottenDetail = select(2, Eat:validate({ item_ref = APPLE }, rotten.ctx))
  equal(rottenCode, REASON.NO_SAFE_FOOD, "spoiled food is refused as unsafe, not merely unwanted")
  contains(rottenDetail, "rotten", "and the detail says why")
end

Harness.group("a missing eat action costs a capability, not a crash")
do
  local s = scene()
  Support.removeAction("ISEatFoodAction")
  local accepted, code, detail = Eat:validate({ item_ref = APPLE }, s.ctx)
  isNil(accepted, "with no eat action there is nothing to validate")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "ISEatFoodAction", "naming the symbol this build did not have")
  installEat()
end

Harness.group("eat fetches the portion before it eats it")
do
  local s = scene()
  local args = { item_ref = BISCUIT, fraction = 0.5 }
  ok(Eat:validate(args, s.ctx), "a biscuit in a satchel is edible")
  ok(Eat:prepare(args, s.ctx), "prepare succeeds")
  equal(#s.queue.added, 1, "one action was queued to fetch it")
  equal(s.queue.added[1].Type, "ISInventoryTransferAction", "and it is the same tagged transfer any move uses")

  ok(Eat:begin(args, s.ctx), "the eat starts behind the fetch")
  equal(#s.eat.actions, 1, "one eat action was constructed")
  equal(s.eat.actions[1].args[3], 0.5, "carrying the portion that was asked for")

  local already = scene()
  ok(Eat:validate({ item_ref = APPLE }, already.ctx), "an apple already in hand validates")
  ok(Eat:prepare({ item_ref = APPLE }, already.ctx), "and prepares")
  equal(#already.queue.added, 0, "with nothing queued, because nothing needed fetching")
end

Harness.group("eat verifies against hunger, not against the queue")
do
  local s = scene()
  local args = { item_ref = APPLE, fraction = 1.0 }
  ok(Eat:validate(args, s.ctx), "the command validates")
  ok(Eat:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)

  Support.drainQueue(s.queue)
  local evidence, code, detail = Eat:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "a queue that emptied without the character eating proves nothing")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed its postcondition")
  contains(detail, "hunger stayed at", "and the detail carries the reading that did not move")

  s.eat.actions[1]:perform()
  local proof = Eat:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "hunger falling is the evidence")
  equal(proof.kind, "hunger_fell", "named for what was observed")
  equal(proof.hunger_before, 0.6, "carrying the reading before")
  ok(proof.hunger_after < 0.6, "and the lower reading after")
end

Harness.group("a build that reports no hunger falls back to the item, not to a shrug")
do
  local s = scene({ no_stats = true })
  local args = { item_ref = APPLE, fraction = 0.5 }
  ok(Eat:validate(args, s.ctx), "the command still validates")
  ok(Eat:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)
  isNil(Toolkit.stat(before, "hunger"), "there is no hunger reading to compare")

  local nothing, code, detail = Eat:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(nothing, "and an unchanged item is not evidence either")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command fails")
  contains(detail, "no hunger", "with a detail that says the stat was the thing that was missing")

  s.eat.actions[1]:perform()
  local proof = Eat:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "half an apple left where a whole one was is an observation")
  equal(proof.kind, "portion_consumed", "named for the observation that was actually available")
  equal(proof.hunger_change_before, -15, "carrying what the item was worth")
  ok(proof.hunger_change_after > -15, "and what it is worth now")
  isNil(proof.hunger_before, "with no invented hunger reading")
end

Harness.group("an item eaten to nothing is evidence of being eaten")
do
  local s = scene({ no_stats = true })
  local args = { item_ref = APPLE, fraction = 1.0 }
  local before = Toolkit.observe(s.player)
  Support.moveItem(s.apple, s.main, nil)
  local proof = Eat:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "an item that left the inventory during an eat was eaten")
  equal(proof.item_consumed, true, "and the evidence says so plainly")
end

Harness.group("eat stops for the player and for a horde")
do
  local taken = scene({ safety = Support.takenOver() })
  local prepared, prepareCode = Eat:prepare({ item_ref = APPLE }, taken.ctx)
  isNil(prepared, "a taken-over character prepares nothing")
  equal(prepareCode, REASON.USER_TAKEOVER, "the player is driving")

  local threatened = scene({ safety = Support.threatened() })
  local started, startCode = Eat:begin({ item_ref = APPLE }, threatened.ctx)
  isNil(started, "and nothing starts into a horde")
  equal(startCode, REASON.THREAT_INTERRUPTED, "with the reflex guard's reason")
  equal(#threatened.queue.added, 0, "and nothing queued")
end

Harness.group("drink refuses tainted water in the container and at the source")
do
  local s = scene({ tainted_bottle = true })
  local accepted, code, detail = Drink:validate({ item_ref = BOTTLE }, s.ctx)
  isNil(accepted, "a tainted bottle is not a drink")
  equal(code, REASON.NO_SAFE_DRINK, "which is the domain's own refusal")
  contains(detail, "tainted", "and the detail says why")

  local source = scene({ tainted = true })
  local _, sourceCode, sourceDetail = Drink:validate({ item_ref = BOTTLE, refill_from = SINK }, source.ctx)
  equal(sourceCode, REASON.NO_SAFE_DRINK, "and a tainted source is refused before anything is queued")
  contains(sourceDetail, "source", "naming the source rather than the bottle")

  local dry = scene({ source_water = 0 })
  local _, dryCode = Drink:validate({ item_ref = BOTTLE, refill_from = SINK }, dry.ctx)
  equal(dryCode, REASON.NO_SAFE_DRINK, "a source with no water is not a source")
end

Harness.group("drink verifies against thirst, and falls back to the container")
do
  local s = scene()
  local args = { item_ref = BOTTLE, fraction = 0.5 }
  ok(Drink:validate(args, s.ctx), "the command validates")
  ok(Drink:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)

  local nothing, code = Drink:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(nothing, "an untouched bottle and an unmoved thirst prove nothing")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed")

  s.drink.actions[1]:perform()
  local proof = Drink:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "thirst falling is evidence")
  equal(proof.kind, "thirst_fell", "named for the stat that moved")
  equal(proof.thirst_before, 0.5, "carrying the reading before")

  local blind = scene({ no_stats = true })
  local blindBefore = Toolkit.observe(blind.player)
  ok(Drink:validate(args, blind.ctx), "a build with no thirst reading still validates")
  ok(Drink:begin(args, blind.ctx), "and starts")
  blind.drink.actions[1]:perform()
  local blindProof = Drink:verify(blindBefore, Toolkit.observe(blind.player), args, blind.ctx)
  ok(blindProof ~= nil, "and a container with one unit fewer is the observation that is left")
  equal(blindProof.kind, "container_drained", "named for it")
  equal(blindProof.uses_before, 10, "with the count before")
  equal(blindProof.uses_after, 9, "and after")
end

Harness.group("a refill needs the water action and names the source in the evidence")
do
  local s = scene()
  Support.removeAction("ISTakeWaterAction")
  local accepted, code, detail = Drink:validate({ item_ref = BOTTLE, refill_from = SINK }, s.ctx)
  isNil(accepted, "without the water action there is no refill to run")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "which is a capability gap")
  contains(detail, "ISTakeWaterAction", "naming the experimental symbol §12.4 flags")

  local refill = scene()
  local args = { item_ref = BOTTLE, refill_from = SINK }
  ok(Drink:validate(args, refill.ctx), "with the action present the refill validates")
  ok(Drink:prepare(args, refill.ctx), "and prepares")
  equal(#refill.water.actions, 1, "one fill was queued")
  equal(refill.water.actions[1].args[4], refill.bottle, "against the bottle that was named")

  ok(Drink:begin(args, refill.ctx), "the drink starts behind it")
  local before = Toolkit.observe(refill.player)
  refill.water.actions[1]:perform()
  refill.drink.actions[1]:perform()
  local proof = Drink:verify(before, Toolkit.observe(refill.player), args, refill.ctx)
  ok(proof ~= nil, "thirst fell, so the drink happened")
  equal(proof.source_ref, SINK, "and the evidence names the source it was drawn from")
end

Harness.group("a refilled container witnesses nothing on its own")
do
  local s = scene({ no_stats = true })
  local args = { item_ref = BOTTLE, refill_from = SINK }
  ok(Drink:validate(args, s.ctx), "the command validates")
  ok(Drink:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  -- The fill runs and the drink does not: the bottle now holds *more*, which
  -- would satisfy no test, and could not distinguish a drink from a top-up even
  -- if it held less.
  s.water.actions[1]:perform()
  local evidence, code, detail = Drink:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "a fuller bottle is not proof that anything was drunk")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command fails rather than guessing")
  contains(detail, "refilled container proves nothing", "and says exactly why the fallback does not apply")
end

Harness.group("the adapters are registered where the dispatcher looks for them")
do
  equal(PZ.Adapters.BY_NAME["consume.eat"], Eat, "eat is registered under its action name")
  equal(PZ.Adapters.BY_NAME["consume.drink"], Drink, "and so is drink")
  ok(PZ.Protocol.isKnownAction(Eat.name), "both names are in the protocol whitelist")
  equal(Eat.capability, "eat_percentage", "eat declares the capability the sidecar gates on")
  equal(Drink.capability, "drink_carried", "and drink declares its own")
end

Harness.group("the declaration is what the dispatcher builds the args from")
do
  -- An adapter that declared nothing would not be refused: it would run with
  -- every argument silently dropped, eating a whole item where half was asked
  -- for. So the bounds are asserted here, on the table the adapter is handed,
  -- rather than only on the checks it makes of that table afterwards.
  local checkArgs = PZ.CommandDispatcher.checkArgs
  local eaten = checkArgs(Eat, { item_ref = APPLE, fraction = 0.5 }, Support.SESSION)
  equal(eaten.item_ref, APPLE, "the item reference survives the rebuild")
  equal(eaten.fraction, 0.5, "and so does the portion that was asked for")

  local _, wideCode = checkArgs(Eat, { item_ref = APPLE, fraction = 1.5 }, Support.SESSION)
  equal(wideCode, REASON.INVALID_ARGUMENT, "more than a whole item never reaches the adapter")

  local _, smallCode = checkArgs(Eat, { item_ref = APPLE, fraction = 0.01 }, Support.SESSION)
  equal(smallCode, REASON.INVALID_ARGUMENT, "nor does a portion too small to observe")

  local _, unknownCode = checkArgs(Eat, { item_ref = APPLE, portion = 0.5 }, Support.SESSION)
  equal(unknownCode, REASON.INVALID_ARGUMENT, "an undeclared argument is refused rather than dropped")

  local _, kindCode = checkArgs(Eat, { item_ref = SINK }, Support.SESSION)
  equal(kindCode, REASON.INVALID_REF, "and a square is not something to eat")

  local drunk = checkArgs(Drink, { item_ref = BOTTLE, refill_from = SINK }, Support.SESSION)
  equal(drunk.refill_from, SINK, "drink's optional source survives too")

  local _, sourceCode = checkArgs(Drink, { item_ref = BOTTLE, refill_from = BOTTLE }, Support.SESSION)
  equal(sourceCode, REASON.INVALID_REF, "but a bottle is not a place to walk to")
end

Harness.finish("adapter_consumption")
