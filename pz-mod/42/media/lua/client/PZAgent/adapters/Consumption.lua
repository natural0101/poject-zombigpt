--[[
PZAgent.Adapters.Consumption -- consume.eat and consume.drink.

Invariant: nourishment is applied by the game's own ISEatFoodAction and
ISDrinkFromBottle. Neither hunger nor thirst is ever written here, and a stat
that moved is the only reason either command may succeed -- with one deliberate
fallback, described below, that is a *different* observation rather than a
weaker one.

The fallback exists because PZAgent.Observe leaves a stat out entirely when the
build did not expose its accessor, and "hunger is absent" must never read as
"hunger is zero". So verify looks first at the stat and then, only if the stat
could not be read at all, at the item: a portion eaten leaves less nourishment
behind, a drink leaves fewer units, and a fully consumed item is gone from the
inventory. Both are observations of the world after the fact; neither is the
adapter believing its own ack.

Drinking from a world source is a *separate action*, `consume.drink_source`,
and that separation is the gate rather than a naming preference. The capability
is `drink_world_source`, which the probe caps at `experimental` because §12.4
lists the action as unconfirmed; while it was an optional `refill_from`
argument on `consume.drink` the whole path ran under `drink_carried`, so a
capability the blueprint calls unproven was reachable through one the scan
verifies. One action, one capability, checked before the adapter is entered.

The source itself must still report untainted water before anything is queued
-- an agent that drinks from a tainted rain barrel because the field was missing
has poisoned the character on the strength of an absent reading.
]]

-- Load-order guard, live-proven 2026-08-08 on Build 42.20.2: the engine walked
-- adapters/ in an order that ran this file before Toolkit.lua. The statement
-- form is deliberate -- the paren form is banned as dynamic loading -- and the
-- test harness pre-resolves this module, so there the require is a no-op.
require "PZAgent/adapters/Toolkit"

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Consumption = {}
PZAgent.Adapters.Consumption = Consumption

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

--- Portions. Below a tenth the game rounds the effect away, and asking for a
--- portion whose effect cannot be observed is asking for a command that cannot
--- succeed.
Consumption.MIN_FRACTION = 0.1
Consumption.MAX_FRACTION = 1.0

--- Stat movement below this is noise from the game's own per-tick drift rather
--- than the effect of the action.
Consumption.STAT_EPSILON = 0.001

Consumption.TIMEOUT_MS = 20000
Consumption.POLL_MS = 250

local EAT_REQUIRES = { "ISEatFoodAction", "ISEatFoodAction.new", "ISTimedActionQueue.add" }
local DRINK_REQUIRES = { "ISDrinkFromBottle", "ISDrinkFromBottle.new", "ISTimedActionQueue.add" }

--- Named by consume.drink_source alone, so a build with no water action can
--- still drink from a bottle.
local REFILL_REQUIRES = { "ISTakeWaterAction", "ISTakeWaterAction.new" }

--- The world-source action fills a vessel and then drinks from it, so it needs
--- both sets. Built here rather than concatenated at declaration time because
--- Toolkit.requireSymbols reads the list the adapter carries, and a list
--- assembled twice is a list that can differ.
local DRINK_SOURCE_REQUIRES = {}
for _, entry in ipairs(DRINK_REQUIRES) do
  DRINK_SOURCE_REQUIRES[#DRINK_SOURCE_REQUIRES + 1] = entry
end
for _, entry in ipairs(REFILL_REQUIRES) do
  DRINK_SOURCE_REQUIRES[#DRINK_SOURCE_REQUIRES + 1] = entry
end

--- Argument kinds, spelled out rather than read from
--- PZAgent.CommandDispatcher.ARG, because the dispatcher is a sibling file and
--- nothing here may dereference a module at load time. A typo costs a refused
--- registration at load rather than a loosened check at dispatch.
local ARG = {
  NUMBER = "number",
  REF = "ref",
}

-- ---------------------------------------------------------------------------
-- shared
-- ---------------------------------------------------------------------------

local function readFraction(args)
  return toolkit().readNumber(args, "fraction", {
    default = Consumption.MAX_FRACTION,
    minimum = Consumption.MIN_FRACTION,
    maximum = Consumption.MAX_FRACTION,
  })
end

--- The item record for `identity` in a snapshot, or nil.
local function recordOf(snapshot, identity)
  if type(snapshot) ~= "table" or type(snapshot.items) ~= "table" then
    return nil
  end
  return snapshot.items[identity]
end

--- Did a stat fall between the two snapshots?
---
--- Returns true/false plus both readings, or nil when the build did not report
--- the stat at all -- which is the case the item fallback exists for.
local function statFell(before, after, name)
  local Toolkit = toolkit()
  local first = Toolkit.stat(before, name)
  local second = Toolkit.stat(after, name)
  if first == nil or second == nil then
    return nil
  end
  return second < (first - Consumption.STAT_EPSILON), first, second
end

Consumption.statFell = statFell

--- Did the item itself shrink? The three shapes a partly consumed item takes.
local function itemShrank(before, after, identity)
  local was = recordOf(before, identity)
  local now = recordOf(after, identity)
  if was == nil then
    return false
  end
  if now == nil then
    -- Eaten or drunk to nothing: the item left the inventory.
    return true, { item_consumed = true }
  end
  if was.uses ~= nil and now.uses ~= nil and now.uses < was.uses then
    return true, { uses_before = was.uses, uses_after = now.uses }
  end
  if was.fluid ~= nil and now.fluid ~= nil and now.fluid < was.fluid then
    return true, { fluid_before = was.fluid, fluid_after = now.fluid }
  end
  -- Hunger and thirst change are negative numbers that shrink towards zero as
  -- the portion is consumed, so "less nourishment left" is a rise.
  if was.hunger_change ~= nil and now.hunger_change ~= nil and now.hunger_change > was.hunger_change then
    return true, { hunger_change_before = was.hunger_change, hunger_change_after = now.hunger_change }
  end
  if was.thirst_change ~= nil and now.thirst_change ~= nil and now.thirst_change > was.thirst_change then
    return true, { thirst_change_before = was.thirst_change, thirst_change_after = now.thirst_change }
  end
  return false
end

Consumption.itemShrank = itemShrank

--- Resolve the item a consume command names, with its identity.
local function consumableSpec(args, ctx, allowed, required)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, allowed, required)
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local itemRef, itemCode, itemDetail = Toolkit.readRef(args, "item_ref", PZAgent.Refs.KIND.ITEM, ctx)
  if itemRef == nil then
    return nil, itemCode, itemDetail
  end
  local fraction, fractionCode, fractionDetail = readFraction(args)
  if fraction == nil then
    return nil, fractionCode, fractionDetail
  end
  local parsed, parseError = PZAgent.Refs.parseItem(itemRef)
  if parsed == nil then
    return nil, reasons.INVALID_REF, parseError
  end
  local identity = tonumber(parsed.runtime_id)
  if identity == nil then
    return nil, reasons.INVALID_REF, "the item reference carries no numeric identity"
  end
  return { item_ref = itemRef, identity = identity, fraction = fraction, container_tail = parsed.container_tail }
end

-- ---------------------------------------------------------------------------
-- consume.eat
-- ---------------------------------------------------------------------------

local EAT_ARGS = { "item_ref", "fraction" }

local Eat = nil

local function eatValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Eat.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = consumableSpec(args, ctx, EAT_ARGS, { "item_ref" })
  if spec == nil then
    return nil, code, detail
  end
  local item, itemCode, itemDetail = Toolkit.resolveItem(ctx, spec.item_ref)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  if Toolkit.readNumberOf(item, { "getHungerChange" }) == nil then
    return nil, reasons.PRECONDITION_FAILED, "the item reports no hunger effect, so it is not food"
  end
  -- Spoiled food is refused here rather than left to the planner: the sidecar's
  -- policy chooses *which* item, and this is the mod refusing to run an action
  -- whose observable effect on the character is harm.
  for _, probe in ipairs({ { "isRotten", "rotten" }, { "isBurnt", "burnt" } }) do
    if Toolkit.readBooleanOf(item, { probe[1] }) == true then
      return nil, reasons.NO_SAFE_FOOD, string.format("the item is %s", probe[2])
    end
  end
  if Toolkit.readBooleanOf(item, { "isPoison", "isTaintedWater" }) == true then
    return nil, reasons.NO_SAFE_FOOD, "the item is poisonous"
  end
  Toolkit.state(ctx).eat = spec
  return true
end

--- ISEatFoodAction wants the item in the character's own inventory, so a
--- portion in a bag on the floor is fetched with the same tagged transfer any
--- other move uses.
local function eatPrepare(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = consumableSpec(args, ctx, EAT_ARGS, { "item_ref" })
  if spec == nil then
    return nil, specCode, specDetail
  end
  local outcome, moveCode, moveDetail = PZAgent.Adapters.Inventory.bringToMain(ctx, spec.item_ref)
  if outcome == nil then
    return nil, moveCode, moveDetail
  end
  Toolkit.state(ctx).fetched = outcome == "moving"
  return true
end

local function eatStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = consumableSpec(args, ctx, EAT_ARGS, { "item_ref" })
  if spec == nil then
    return nil, specCode, specDetail
  end
  local item, itemCode, itemDetail = Toolkit.resolveItem(ctx, spec.item_ref)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  local action, actionCode, actionDetail =
    Toolkit.construct("ISEatFoodAction", ctx.player, item, spec.fraction)
  if action == nil then
    return nil, actionCode, actionDetail
  end
  return Toolkit.enqueue(ctx, action)
end

local function eatVerify(_, before, after, args, ctx)
  local Toolkit = toolkit()
  local spec, code, detail = consumableSpec(args, ctx, EAT_ARGS, { "item_ref" })
  if spec == nil then
    return nil, code, detail
  end
  local observed = { kind = "hunger_fell", item_ref = spec.item_ref, fraction = spec.fraction }
  local fell, hungerBefore, hungerAfter = statFell(before, after, "hunger")
  if fell ~= nil then
    observed.hunger_before = hungerBefore
    observed.hunger_after = hungerAfter
    if fell then
      return observed
    end
  end
  local shrank, evidence = itemShrank(before, after, spec.identity)
  if shrank then
    observed.kind = "portion_consumed"
    for key, value in pairs(evidence) do
      observed[key] = value
    end
    return observed
  end
  if fell == nil then
    return nil,
      Toolkit.reasons().POSTCONDITION_FAILED,
      "this build reports no hunger, and the item is unchanged, so nothing was observed to happen"
  end
  return nil,
    Toolkit.reasons().POSTCONDITION_FAILED,
    string.format("hunger stayed at %.4f and the item is unchanged", hungerAfter)
end

Eat = toolkit().declare({
  name = "consume.eat",
  capability = toolkit().CAPABILITY.EAT_PERCENTAGE,
  requires = EAT_REQUIRES,
  timeout_ms = Consumption.TIMEOUT_MS,
  poll_interval_ms = Consumption.POLL_MS,
  args = {
    item_ref = { type = ARG.REF, required = true, kinds = { item = true } },
    -- The bounds readFraction enforces, read from the same constants so the
    -- declaration and the check cannot drift: a portion outside them is a
    -- portion size the game has no meaning for.
    fraction = { type = ARG.NUMBER, min = Consumption.MIN_FRACTION, max = Consumption.MAX_FRACTION },
  },
  validate = eatValidate,
  prepare = eatPrepare,
  begin = eatStart,
  verify = eatVerify,
})

Consumption.Eat = Eat
toolkit().register(Eat)

-- ---------------------------------------------------------------------------
-- consume.drink
-- ---------------------------------------------------------------------------

local DRINK_ARGS = { "item_ref", "fraction" }
local DRINK_SOURCE_ARGS = { "item_ref", "fraction", "source_ref" }

--- The first object on `square` that reports water, with how much it holds.
---
--- Nothing without a positive reading counts: a source whose amount could not be
--- read is not a source this adapter will drink from.
function Consumption.waterSourceOn(square)
  local Toolkit = toolkit()
  local ok, objects = Toolkit.call(square, "getObjects")
  if not ok or objects == nil then
    return nil, "IsoGridSquare.getObjects"
  end
  local size = Toolkit.listSize(objects)
  if size == nil then
    return nil, "IsoGridSquare.getObjects().size"
  end
  local scanned = math.min(size, Toolkit.MAX_SQUARE_OBJECTS)
  for index = 0, scanned - 1 do
    local object = Toolkit.listGet(objects, index)
    local amount = Toolkit.readNumberOf(object, { "getWaterAmount" })
    if amount ~= nil and amount > 0 then
      return object, nil, amount
    end
  end
  return nil
end

--- Resolve and vet the world source a refill names.
local function refillSource(ctx, ref)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local parsed, parseError = PZAgent.Refs.parseSquare(ref)
  if parsed == nil then
    return nil, reasons.INVALID_REF, parseError
  end
  if parsed.session_id ~= ctx.session_id then
    return nil, reasons.INVALID_REF, "the source reference was minted by another session"
  end
  local square, missing = Toolkit.gridSquare(parsed.x, parsed.y, parsed.z)
  if square == nil then
    if missing == "IsoCell.getGridSquare" then
      return nil, reasons.TARGET_NOT_LOADED, "the square the water source stands on is not loaded"
    end
    return Toolkit.unavailable(missing)
  end
  local object, symbol, amount = Consumption.waterSourceOn(square)
  if object == nil then
    if symbol ~= nil then
      return Toolkit.unavailable(symbol)
    end
    return nil, reasons.NO_SAFE_DRINK, "nothing on that square reports any water"
  end
  if Toolkit.readBooleanOf(object, { "isTaintedWater", "isTainted" }) == true then
    return nil, reasons.NO_SAFE_DRINK, "the source reports tainted water"
  end
  return { object = object, square = square, amount = amount, x = parsed.x, y = parsed.y, z = parsed.z }
end

Consumption.refillSource = refillSource


--- Read a drink command, in either of the two shapes.
---
--- `fromSource` decides which argument list is legal and whether the world
--- source is mandatory. It is passed in rather than inferred from the presence
--- of `source_ref`, because inferring it would let a `consume.drink` command
--- reach the world-source path by naming an extra argument -- which is exactly
--- the gate this split exists to close.
local function drinkSpec(args, ctx, fromSource)
  local Toolkit = toolkit()
  local allowed = fromSource and DRINK_SOURCE_ARGS or DRINK_ARGS
  local spec, code, detail = consumableSpec(args, ctx, allowed, { "item_ref" })
  if spec == nil then
    return nil, code, detail
  end
  if fromSource then
    local ref, refCode, refDetail = Toolkit.readRef(args, "source_ref", PZAgent.Refs.KIND.SQUARE, ctx)
    if ref == nil then
      return nil, refCode, refDetail
    end
    spec.source_ref = ref
  end
  return spec
end

local function drinkValidate(self, args, ctx, fromSource)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(self.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = drinkSpec(args, ctx, fromSource)
  if spec == nil then
    return nil, code, detail
  end
  local item, itemCode, itemDetail = Toolkit.resolveItem(ctx, spec.item_ref)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  local thirst = Toolkit.readNumberOf(item, { "getThirstChange" })
  local ok, fluid = Toolkit.call(item, "getFluidContainer")
  local uses = Toolkit.readNumberOf(item, { "getDrainableUsesInt", "getCurrentUses" })
  if thirst == nil and not (ok and fluid ~= nil) and uses == nil then
    return nil, reasons.PRECONDITION_FAILED, "the item reports neither a thirst effect nor anything to hold liquid"
  end
  if Toolkit.readBooleanOf(item, { "isTaintedWater", "isPoison" }) == true then
    return nil, reasons.NO_SAFE_DRINK, "the container holds tainted water"
  end
  if ok and fluid ~= nil and Toolkit.readBooleanOf(fluid, { "isTainted" }) == true then
    return nil, reasons.NO_SAFE_DRINK, "the fluid in the container is tainted"
  end
  if fromSource then
    local source, sourceCode, sourceDetail = refillSource(ctx, spec.source_ref)
    if source == nil then
      return nil, sourceCode, sourceDetail
    end
    Toolkit.state(ctx).source = source
  elseif thirst == nil and uses == nil then
    -- An empty vessel with nothing to fill it from cannot quench anything.
    local amount = ok and fluid ~= nil and Toolkit.readNumberOf(fluid, { "getAmount" }) or nil
    if amount ~= nil and amount <= 0 then
      return nil, reasons.NO_SAFE_DRINK, "the container is empty and no source was named"
    end
  end
  Toolkit.state(ctx).drink = spec
  return true
end

--- Fetch the vessel, walk to the source and fill it, in that order.
---
--- The refill is a separate timed action queued ahead of the drink rather than
--- something folded into it, so a build that refuses the water action fails
--- with the water action's own reason instead of silently drinking nothing.
local function drinkPrepare(_, args, ctx, fromSource)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = drinkSpec(args, ctx, fromSource)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local outcome, moveCode, moveDetail = PZAgent.Adapters.Inventory.bringToMain(ctx, spec.item_ref)
  if outcome == nil then
    return nil, moveCode, moveDetail
  end
  if not fromSource then
    return true
  end
  local source, sourceCode, sourceDetail = refillSource(ctx, spec.source_ref)
  if source == nil then
    return nil, sourceCode, sourceDetail
  end
  local approach, approachCode, approachDetail = Toolkit.approach(ctx, source)
  if approach == nil then
    return nil, approachCode, approachDetail
  end
  if approach == "walking" then
    -- The fill is queued behind the walk; both are tagged, so a stop cancels
    -- both or neither.
    Toolkit.state(ctx).walking_to_source = true
  end
  local item, itemCode, itemDetail = Toolkit.resolveItem(ctx, spec.item_ref)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  -- (character, waterObject, amount, item). This project has no Build 42.20 to
  -- check that against, and three places in the repository once stated three
  -- different orders -- see docs/GAME_API_VERIFICATION.md, where this is the
  -- first row a live run must confirm. A build that orders them differently is
  -- a wrong fill, not a crash, and the postcondition below is what refuses to
  -- call it a success.
  local action, actionCode, actionDetail =
    Toolkit.construct("ISTakeWaterAction", ctx.player, source.object, 50, item)
  if action == nil then
    return nil, actionCode, actionDetail
  end
  local queued, queueCode, queueDetail = Toolkit.enqueue(ctx, action)
  if queued == nil then
    return nil, queueCode, queueDetail
  end
  Toolkit.state(ctx).refilled = true
  return true
end

local function drinkStart(_, args, ctx, fromSource)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = drinkSpec(args, ctx, fromSource)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local item, itemCode, itemDetail = Toolkit.resolveItem(ctx, spec.item_ref)
  if item == nil then
    return nil, itemCode, itemDetail
  end
  local action, actionCode, actionDetail =
    Toolkit.construct("ISDrinkFromBottle", ctx.player, item, spec.fraction)
  if action == nil then
    return nil, actionCode, actionDetail
  end
  return Toolkit.enqueue(ctx, action)
end

local function drinkVerify(_, before, after, args, ctx, fromSource)
  local Toolkit = toolkit()
  local spec, code, detail = drinkSpec(args, ctx, fromSource)
  if spec == nil then
    return nil, code, detail
  end
  local observed = { kind = "thirst_fell", item_ref = spec.item_ref, fraction = spec.fraction }
  if fromSource then
    observed.source_ref = spec.source_ref
  end
  local fell, thirstBefore, thirstAfter = statFell(before, after, "thirst")
  if fell ~= nil then
    observed.thirst_before = thirstBefore
    observed.thirst_after = thirstAfter
    if fell then
      return observed
    end
  end
  if fromSource then
    -- A refill raises the vessel's contents and the drink lowers them again, so
    -- the item is no longer a witness either way. Thirst is the only honest
    -- reading left, and if it could not be read there is nothing to report.
    return nil,
      Toolkit.reasons().POSTCONDITION_FAILED,
      "a refilled container proves nothing about drinking, and thirst did not fall"
  end
  local shrank, evidence = itemShrank(before, after, spec.identity)
  if shrank then
    observed.kind = "container_drained"
    for key, value in pairs(evidence) do
      observed[key] = value
    end
    return observed
  end
  if fell == nil then
    return nil,
      Toolkit.reasons().POSTCONDITION_FAILED,
      "this build reports no thirst, and the container is unchanged, so nothing was observed to happen"
  end
  return nil,
    Toolkit.reasons().POSTCONDITION_FAILED,
    string.format("thirst stayed at %.4f and the container is unchanged", thirstAfter)
end

--- The shared argument declaration, minus the source. Written once so the two
--- adapters cannot drift on the bounds a fraction is checked against.
local function drinkArgs()
  return {
    item_ref = { type = ARG.REF, required = true, kinds = { item = true } },
    fraction = { type = ARG.NUMBER, min = Consumption.MIN_FRACTION, max = Consumption.MAX_FRACTION },
  }
end

local Drink = toolkit().declare({
  name = "consume.drink",
  capability = toolkit().CAPABILITY.DRINK_CARRIED,
  requires = DRINK_REQUIRES,
  timeout_ms = Consumption.TIMEOUT_MS,
  poll_interval_ms = Consumption.POLL_MS,
  args = drinkArgs(),
  validate = function(self, args, ctx)
    return drinkValidate(self, args, ctx, false)
  end,
  prepare = function(self, args, ctx)
    return drinkPrepare(self, args, ctx, false)
  end,
  begin = function(self, args, ctx)
    return drinkStart(self, args, ctx, false)
  end,
  verify = function(self, before, after, args, ctx)
    return drinkVerify(self, before, after, args, ctx, false)
  end,
})

local sourceArgs = drinkArgs()
-- A square, and mandatory: the source is a place to stand next to, and
-- refillSource vets what stands on it before a drop is drawn. Mandatory rather
-- than optional because an action that can run without its source is an action
-- that can quietly become an ordinary sip under a capability §12.4 has not
-- confirmed.
sourceArgs.source_ref = { type = ARG.REF, required = true, kinds = { square = true } }

local DrinkSource = toolkit().declare({
  name = "consume.drink_source",
  capability = toolkit().CAPABILITY.DRINK_WORLD_SOURCE,
  -- §12.4 lists the world water action as unconfirmed, so the ceiling is
  -- `experimental` rather than `available_unverified` even when every symbol
  -- resolves. The file header has said so since this adapter was written; this
  -- is the line that makes the published report say it too.
  experimental = true,
  requires = DRINK_SOURCE_REQUIRES,
  timeout_ms = Consumption.TIMEOUT_MS,
  poll_interval_ms = Consumption.POLL_MS,
  args = sourceArgs,
  validate = function(self, args, ctx)
    return drinkValidate(self, args, ctx, true)
  end,
  prepare = function(self, args, ctx)
    return drinkPrepare(self, args, ctx, true)
  end,
  begin = function(self, args, ctx)
    return drinkStart(self, args, ctx, true)
  end,
  verify = function(self, before, after, args, ctx)
    return drinkVerify(self, before, after, args, ctx, true)
  end,
})

Consumption.Drink = Drink
Consumption.DrinkSource = DrinkSource
Consumption.REFILL_REQUIRES = REFILL_REQUIRES
toolkit().register(Drink)
toolkit().register(DrinkSource)

return Consumption
