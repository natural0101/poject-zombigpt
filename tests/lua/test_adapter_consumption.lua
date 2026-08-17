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
local DrinkSource = Consumption.DrinkSource
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
    burnt = options.burnt,
    poison = options.poison,
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
  -- A drainable vessel: a thirst effect and uses, and no fluid container at
  -- all, which is the shape the item-level taint and poison readers are the
  -- only guard for.
  local flask = Support.item({
    id = 6,
    full_type = "Base.WaterBottleFull",
    name = "Flask",
    thirst_change = -20,
    uses = 4,
    poison = options.poison_drink,
    tainted_water = options.tainted_flask,
  })
  local biscuit = Support.item({ id = 3, full_type = "Base.Biscuit", name = "Biscuit", hunger_change = -8 })
  local satchel = Support.item({ id = 99, full_type = "Base.Bag_Satchel", name = "Satchel", contents = { biscuit } })
  local main = Support.container({ apple, bottle, rock, flask, satchel })
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
    flask = flask,
    main = main,
    stats = stats,
  }
end

local APPLE = Support.itemRef("player-main", 1)
local BOTTLE = Support.itemRef("player-main", 5)
local ROCK = Support.itemRef("player-main", 9)
local BISCUIT = Support.itemRef("carried:99", 3)
local FLASK = Support.itemRef("player-main", 6)
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

  -- The other half of the same loop. Only the rotten probe was ever driven, so
  -- un-rolling the loop to `isRotten` alone -- the tidy-up a reviewer waves
  -- through -- deleted the burnt refusal with every suite still green.
  local burnt = scene({ burnt = true })
  local burntCode, burntDetail = select(2, Eat:validate({ item_ref = APPLE }, burnt.ctx))
  equal(burntCode, REASON.NO_SAFE_FOOD, "and burnt food is refused by the same rule")
  contains(burntDetail, "burnt", "naming the state it was refused for")

  -- The sharpest of the three, and the one nothing could reach: `Support.item`
  -- granted no `isPoison` reader at all, so the branch was structurally absent
  -- from this harness. There is no second lever -- `policy.food`'s filters run
  -- only when the *planner* picks the item, and `EatAdapter.validate` on the
  -- Python side says in so many words that rot, poison and reserves are
  -- policy.food's decision and already made by the time a reference reaches it.
  -- A directly-issued consume.eat with an arbitrary item_ref meets this line
  -- and nothing else, and what it costs is harm to the character.
  local poisoned = scene({ poison = true })
  local poisonCode, poisonDetail = select(2, Eat:validate({ item_ref = APPLE }, poisoned.ctx))
  equal(poisonCode, REASON.NO_SAFE_FOOD, "poisoned food is refused before anything is queued")
  contains(poisonDetail, "poisonous", "and the detail says what it is")
  equal(#poisoned.queue.added, 0, "with nothing in the game's queue")
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

Harness.group("an inventory that stopped answering is not an item eaten to nothing")
do
  -- The mirror of the group above. "The item left the inventory" is read off
  -- the after snapshot as an absence, and an absence is only evidence when the
  -- walk that would have found it was actually made. A character whose
  -- inventory stopped answering mid-command has eaten nothing, and an ack that
  -- says otherwise is what mints `verified` for eat on a build that never ate.
  local s = scene({ no_stats = true })
  local args = { item_ref = APPLE, fraction = 1.0 }
  ok(Eat:validate(args, s.ctx), "the command validates")
  ok(Eat:begin(args, s.ctx), "and starts")
  local before = Toolkit.observe(s.player)
  ok(before.items[1] ~= nil, "the apple was on the character when the eat began")

  Support.drainQueue(s.queue)
  s.player.getInventory = function()
    error("kahlua gap", 0)
  end
  local after = Toolkit.observe(s.player)
  isNil(after.items[1], "the apple is absent from a snapshot that walked nothing")
  local evidence, code, detail = Eat:verify(before, after, args, s.ctx)
  isNil(evidence, "which is not the apple having been eaten")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability, not a consumed portion")
  contains(detail, "getInventory", "naming the accessor that stopped answering")

  local drinker = scene({ no_stats = true })
  local drinkArgs = { item_ref = BOTTLE, fraction = 0.5 }
  ok(Drink:validate(drinkArgs, drinker.ctx), "the same for a drink")
  ok(Drink:begin(drinkArgs, drinker.ctx), "which starts")
  local wasFull = Toolkit.observe(drinker.player)
  Support.drainQueue(drinker.queue)
  drinker.player.getInventory = nil
  local _, drinkCode = Drink:verify(wasFull, Toolkit.observe(drinker.player), drinkArgs, drinker.ctx)
  equal(drinkCode, REASON.CAPABILITY_UNAVAILABLE, "a container nobody could look at was not drained")
end

Harness.group("an item in neither snapshot was not eaten to nothing")
do
  -- The group above covers the case where the *walk failed outright* and
  -- `unread.items` is set. This is the other one: the walk succeeded and simply
  -- did not reach the item. `Toolkit.observe` bounds its inventory walk by
  -- depth and by item budget, while `Toolkit.resolveItem` walks the named
  -- container directly -- so an item in a bag inside a bag inside a bag
  -- resolves and validates fine and is in neither snapshot.
  --
  -- `itemShrank`'s floor is the only thing between that and
  -- `item_consumed = true`. Delete the two-line `was == nil` return and control
  -- falls to the `now == nil` branch, which reads an absence nobody observed as
  -- the item having been eaten. The runtime cannot catch it either: the
  -- evidence bag carries no before/after pair, so the unchanged-readings gate
  -- counts zero pairs and lets it through.
  local s = scene({ no_stats = true })
  local deep = Support.item({ id = 21, full_type = "Base.Biscuit", name = "Biscuit", hunger_change = -8 })
  local inner = Support.item({ id = 22, full_type = "Base.Bag_Satchel", name = "Inner", contents = { deep } })
  local middle = Support.item({ id = 23, full_type = "Base.Bag_Satchel", name = "Middle", contents = { inner } })
  local outer = Support.item({ id = 24, full_type = "Base.Bag_Satchel", name = "Outer", contents = { middle } })
  table.insert(s.main.entries, outer)

  local args = { item_ref = Support.itemRef("carried:22", 21), fraction = 1.0 }
  ok(Eat:validate(args, s.ctx), "the biscuit resolves through the named bag, so the command validates")

  local before = Toolkit.observe(s.player)
  isNil(before.items[21], "yet the bounded snapshot never reached it")
  isNil(Toolkit.unread(before, "items"), "and the walk itself did not fail -- it simply stopped short")

  local after = Toolkit.observe(s.player)
  local evidence, code, detail = Eat:verify(before, after, args, s.ctx)
  isNil(evidence, "an item absent from both snapshots proves nothing about eating")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command fails its postcondition")
  contains(detail, "unchanged", "on the reading that did not move")
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
  local _, sourceCode, sourceDetail =
    DrinkSource:validate({ item_ref = BOTTLE, source_ref = SINK }, source.ctx)
  equal(sourceCode, REASON.NO_SAFE_DRINK, "and a tainted source is refused before anything is queued")
  contains(sourceDetail, "source", "naming the source rather than the bottle")

  local dry = scene({ source_water = 0 })
  local _, dryCode = DrinkSource:validate({ item_ref = BOTTLE, source_ref = SINK }, dry.ctx)
  equal(dryCode, REASON.NO_SAFE_DRINK, "a source with no water is not a source")

  -- The gate that keeps an unproven capability out of a proven one: the
  -- carried-container action does not know the argument at all.
  local mixed = scene()
  local _, mixedCode, mixedDetail =
    Drink:validate({ item_ref = BOTTLE, source_ref = SINK }, mixed.ctx)
  equal(mixedCode, REASON.INVALID_ARGUMENT, "consume.drink cannot be talked onto the source path")
  contains(mixedDetail, "source_ref", "and says which argument it does not take")
end

Harness.group("taint and poison read off the item, not only off the fluid inside it")
do
  -- The group above reads like coverage of the item-level check and is not.
  -- The mock bottle carries both `tainted` and `fluid`, and `Support.item` hung
  -- `isTainted` on the *fluid container* it builds -- so the whole assertion was
  -- satisfied by the fluid check three lines further down, and deleting the
  -- item-level refusal left it green. A test can name the right property,
  -- assert the right code, and never touch the line that makes it true.
  --
  -- The flask is the shape that separates them: a drainable vessel with a
  -- thirst effect and uses and no `getFluidContainer` at all. For tainted water
  -- in a fluid container the two checks are genuinely redundant; here, and for
  -- `isPoison` in any shape, this line is the only one there is.
  local tainted = scene({ tainted_flask = true })
  local accepted, code, detail = Drink:validate({ item_ref = FLASK }, tainted.ctx)
  isNil(accepted, "a vessel that reports taint on itself is not a drink")
  equal(code, REASON.NO_SAFE_DRINK, "which is the domain's own refusal")
  contains(detail, "tainted", "and the detail says why")
  equal(#tainted.queue.added, 0, "with nothing queued")

  local poisoned = scene({ poison_drink = true })
  local _, poisonCode, poisonDetail = Drink:validate({ item_ref = FLASK }, poisoned.ctx)
  equal(poisonCode, REASON.NO_SAFE_DRINK, "and a poisoned one is refused by the same line")
  contains(poisonDetail, "tainted", "under the refusal it shares")

  local clean = scene()
  ok(Drink:validate({ item_ref = FLASK }, clean.ctx), "while the same flask untainted still drinks")
end

Harness.group("the water-source scan stops at the toolkit's bound")
do
  -- The third copy of the same square walk, after `Rest.seatOn` and
  -- `Sleep.bedOn`. Each sink scene in this file puts one object on the square,
  -- so the cap never binds and its deletion is invisible; the two suites that
  -- do pin `MAX_SQUARE_OBJECTS` pin it in other adapters. A square's object
  -- list carries an entry per dropped item, which makes its length the
  -- player's, and each turn is a pcall'd engine call.
  local crowd = {}
  for index = 1, Toolkit.MAX_SQUARE_OBJECTS do
    crowd[index] = Support.worldObject({ name = "Crate " .. index })
  end
  crowd[#crowd + 1] = Support.worldObject({ name = "sink", water = 40 })
  local crowded = Support.square(101, 200, 0, crowd)

  isNil(Consumption.waterSourceOn(crowded), "a source past the bound is not reached")

  local reachable = {}
  for index = 1, Toolkit.MAX_SQUARE_OBJECTS - 1 do
    reachable[index] = Support.worldObject({ name = "Crate " .. index })
  end
  reachable[#reachable + 1] = crowd[#crowd]
  local near = Support.square(101, 200, 0, reachable)
  equal(Consumption.waterSourceOn(near), crowd[#crowd], "while the last object inside the bound still is")
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
  local accepted, code, detail =
    DrinkSource:validate({ item_ref = BOTTLE, source_ref = SINK }, s.ctx)
  isNil(accepted, "without the water action there is no refill to run")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "which is a capability gap")
  contains(detail, "ISTakeWaterAction", "naming the experimental symbol §12.4 flags")

  local refill = scene()
  local args = { item_ref = BOTTLE, source_ref = SINK }
  ok(DrinkSource:validate(args, refill.ctx), "with the action present the refill validates")
  ok(DrinkSource:prepare(args, refill.ctx), "and prepares")
  equal(#refill.water.actions, 1, "one fill was queued")
  equal(refill.water.actions[1].args[4], refill.bottle, "against the bottle that was named")

  ok(DrinkSource:begin(args, refill.ctx), "the drink starts behind it")
  local before = Toolkit.observe(refill.player)
  refill.water.actions[1]:perform()
  refill.drink.actions[1]:perform()
  local proof = DrinkSource:verify(before, Toolkit.observe(refill.player), args, refill.ctx)
  ok(proof ~= nil, "thirst fell, so the drink happened")
  equal(proof.source_ref, SINK, "and the evidence names the source it was drawn from")
end

Harness.group("a refilled container witnesses nothing on its own")
do
  local s = scene({ no_stats = true })
  local args = { item_ref = BOTTLE, source_ref = SINK }
  ok(DrinkSource:validate(args, s.ctx), "the command validates")
  ok(DrinkSource:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  -- The fill runs and the drink does not: the bottle now holds *more*, which
  -- would satisfy no test, and could not distinguish a drink from a top-up even
  -- if it held less.
  s.water.actions[1]:perform()
  local evidence, code, detail = DrinkSource:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "a fuller bottle is not proof that anything was drunk")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command fails rather than guessing")
  contains(detail, "refilled container proves nothing", "and says exactly why the fallback does not apply")
end

Harness.group("the adapters are registered where the dispatcher looks for them")
do
  equal(PZ.Adapters.BY_NAME["consume.eat"], Eat, "eat is registered under its action name")
  equal(PZ.Adapters.BY_NAME["consume.drink"], Drink, "and so is drink")
  equal(PZ.Adapters.BY_NAME["consume.drink_source"], DrinkSource, "and the world-source drink")
  equal(
    DrinkSource.capability,
    "drink_world_source",
    "under the capability §12.4 caps, not the one the scan verifies"
  )
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

  local drawn = checkArgs(DrinkSource, { item_ref = BOTTLE, source_ref = SINK }, Support.SESSION)
  equal(drawn.source_ref, SINK, "the world source survives the dispatcher too")

  local _, sourceCode =
    checkArgs(DrinkSource, { item_ref = BOTTLE, source_ref = BOTTLE }, Support.SESSION)
  equal(sourceCode, REASON.INVALID_REF, "but a bottle is not a place to walk to")

  local _, missingCode = checkArgs(DrinkSource, { item_ref = BOTTLE }, Support.SESSION)
  equal(missingCode, REASON.INVALID_ARGUMENT, "and the source is required, never defaulted")
end

Harness.finish("adapter_consumption")
