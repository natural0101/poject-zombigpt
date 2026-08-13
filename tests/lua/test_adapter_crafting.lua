-- PZAgent.Adapters.Crafting: the two crafting commands, one bounded window at
-- a time.
--
-- Crafting is the first thing this mod does that cannot be undone, so the cases
-- that matter most here are the ones where it must refuse: a recipe the
-- character does not know, materials it does not have, a build that will not
-- say what a recipe produces -- and, above all, a craft action that was queued
-- and changed nothing. Every success below is minted off a re-observed
-- inventory holding more of the product AND less of what makes it; neither
-- half on its own is allowed to mint one.

local ROOT = arg[0]:match("^(.*)test_adapter_crafting%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
-- The observation model is loaded for one assertion and one only: the recipe
-- token this adapter accepts has to be the token the observation publishes, and
-- the only way to prove that is to run both spellings over the same names.
dofile(Harness.root .. "pz-mod/42/media/lua/shared/PZAgent/ObserveModel.lua")
Support.loadModules(Harness.root, { "Crafting" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local Toolkit = PZ.Adapters.Toolkit
local Crafting = PZ.Adapters.Crafting
local Inspect = Crafting.Inspect
local Craft = Crafting.Craft
local REASON = PZ.Protocol.REASON

-- ---------------------------------------------------------------------------
-- doubles
-- ---------------------------------------------------------------------------

--- A recipe script. Each reader exists only when the case names it, so
--- withholding `inputs` or `product` is how "this build will not say what this
--- recipe needs" and "... what it makes" are expressed.
local function recipe(fields)
  local object = { fields = fields }
  object.getName = function()
    return fields.name
  end
  if fields.inputs ~= nil then
    local entries = {}
    for index = 1, #fields.inputs do
      local input = fields.inputs[index]
      entries[index] = {
        getItems = function()
          return Support.list(input.types)
        end,
        getCount = function()
          return input.count
        end,
      }
    end
    object.getInputs = function()
      return Support.list(entries)
    end
  end
  if fields.product ~= nil then
    local output = {
      getItems = function()
        return Support.list({ fields.product })
      end,
    }
    object.getOutputs = function()
      return Support.list({ output })
    end
  end
  return object
end

--- Install a script manager that answers `getCraftRecipe` over `byName`.
local function installScriptManager(byName)
  local manager = {
    getCraftRecipe = function(_, name)
      return byName[name]
    end,
  }
  _G["getScriptManager"] = function()
    return manager
  end
  return manager
end

local function removeScriptManager()
  _G["getScriptManager"] = nil
end

local PLANK = "Base.Plank"
local NAILS = "Base.Nails"
local CRATE = "Base.WoodenCrate"

local nextId = 500

local function carried(fullType, name)
  nextId = nextId + 1
  return Support.item({ id = nextId, full_type = fullType, name = name or fullType, weight = 1 })
end

--- One crafting scene: a character carrying planks and nails, a recipe that
--- turns them into a crate, a queue and a craft class -- each removable through
--- `options` so a missing engine surface is one deleted field away.
local function scene(options)
  options = options or {}
  local items = options.items
  if items == nil then
    items = {
      carried(PLANK, "Plank"),
      carried(PLANK, "Plank"),
      carried(PLANK, "Plank"),
      carried(NAILS, "Nails"),
      carried(NAILS, "Nails"),
    }
  end
  local main = Support.container(items)
  local player = Support.player({ inventory = main })

  local crate = recipe({
    name = options.recipe_name or "MakeCrate",
    inputs = options.no_inputs ~= true and { { types = { PLANK }, count = 2 }, { types = { NAILS }, count = 1 } }
      or nil,
    product = options.no_product ~= true and CRATE or nil,
  })
  local scripts = { [crate.fields.name] = crate }
  local recipes = options.recipes or { crate }
  if options.no_known ~= true then
    local entries = {}
    for index = 1, #recipes do
      -- Known recipes arrive as bare names by default, which is the shape that
      -- needs the script manager to become anything; `known_objects` is the
      -- other shape the engine might hand over.
      entries[index] = options.known_objects and recipes[index] or recipes[index].fields.name
      scripts[recipes[index].fields.name] = recipes[index]
    end
    player.getKnownRecipes = function()
      return Support.list(entries)
    end
  end
  if options.no_scripts ~= true then
    installScriptManager(scripts)
  else
    removeScriptManager()
  end

  local queue = Support.installQueue(options.queue)
  --- The craft action, performing exactly what a craft does to an inventory:
  --- the materials leave it and the product arrives. `options.craft_noop` makes
  --- it return while nothing moves, and `options.craft_free` makes the product
  --- appear with nothing spent -- the two cases every verify here exists for.
  local craftAction = Support.installAction(options.craft_class or "ISCraftRecipeAction", function()
    if options.craft_noop then
      return
    end
    if not options.craft_free then
      local removed = { [PLANK] = 2, [NAILS] = 1 }
      for index = #main.entries, 1, -1 do
        local entry = main.entries[index]
        local fullType = entry.fields.full_type
        if (removed[fullType] or 0) > 0 then
          removed[fullType] = removed[fullType] - 1
          table.remove(main.entries, index)
        end
      end
    end
    main.entries[#main.entries + 1] = carried(CRATE, "Wooden Crate")
  end)

  local ctx = Support.context({ player = player, safety = options.safety })
  return {
    player = player,
    ctx = ctx,
    main = main,
    queue = queue,
    craftAction = craftAction,
    crate = crate,
    items = items,
  }
end

local function craftAll(s)
  for index = 1, #s.craftAction.actions do
    s.craftAction.actions[index]:perform()
  end
end

-- ---------------------------------------------------------------------------
-- the token
-- ---------------------------------------------------------------------------

Harness.group("a recipe is named by the same token on both sides of the wire")
do
  equal(Crafting.recipeToken("MakeCrate"), "MakeCrate", "an identifier-shaped name is its own token")
  equal(Crafting.recipeToken("Make Bread Dough"), "Make_Bread_Dough", "spaces become underscores")
  isNil(Crafting.recipeToken("Ragù di manzo"), "a name the reference alphabet cannot carry has no token")
  isNil(Crafting.recipeToken(""), "and neither does an empty name")
  isNil(Crafting.recipeToken(string.rep("a", 65)), "nor one past the segment bound")

  -- The agreement that makes the round trip possible: the observation publishes
  -- a recipe under ObserveModel.recipeToken and a command names it back here.
  for _, name in ipairs({ "MakeCrate", "Make Bread Dough", "Base.MakeStew", "Ragù di manzo" }) do
    equal(
      Crafting.recipeToken(name),
      PZ.ObserveModel.recipeToken(name),
      "the adapter and the observation spell " .. name .. " the same way"
    )
  end
end

-- ---------------------------------------------------------------------------
-- crafting.inspect
-- ---------------------------------------------------------------------------

Harness.group("inspect answers what can be made now, and says what it could not judge")
do
  local s = scene()
  ok(Inspect:validate({}, s.ctx), "the read validates with no arguments")
  equal(Inspect:begin({}, s.ctx), "done", "and finishes inside begin, queueing nothing")
  local report = Inspect:verify(nil, Toolkit.observe(s.player), {}, s.ctx)
  ok(report ~= nil, "the reading itself is the evidence")
  equal(report.kind, "known_recipes", "named for what was read")
  equal(report.listed, 1, "one known recipe was listed")
  equal(report.known_total, 1, "beside the engine's own count")
  equal(report.recipes[1].recipe, "MakeCrate", "carrying the token a command would name")
  equal(report.recipes[1].product, CRATE, "and what it makes")
  equal(report.recipes[1].ready, true, "three planks and two nails cover two planks and one nail")
  equal(report.ready, 1, "so one recipe is ready")
  equal(report.judged, 1, "and one was judged at all")
  isNil(report.recipes[1].missing, "a ready recipe names nothing missing")

  local short = scene({ items = { carried(PLANK, "Plank") } })
  local shortReport = Inspect:verify(nil, Toolkit.observe(short.player), {}, short.ctx)
  equal(shortReport.recipes[1].ready, false, "one plank does not cover the recipe")
  equal(shortReport.ready, 0, "nothing is ready")
  equal(#shortReport.recipes[1].missing, 2, "both shortfalls are named")
  equal(shortReport.recipes[1].missing[1].need, 2, "with what the recipe needs")
  equal(shortReport.recipes[1].missing[1].held, 1, "and what the character holds")

  -- The third state: the recipe is known and its ingredients cannot be read.
  local blind = scene({ no_inputs = true })
  local blindReport = Inspect:verify(nil, Toolkit.observe(blind.player), {}, blind.ctx)
  equal(blindReport.listed, 1, "the recipe is still listed")
  isNil(blindReport.recipes[1].ready, "with no verdict at all -- absent never reads as ready")
  equal(blindReport.judged, 0, "and the report says none was judged")
end

Harness.group("inspect answers about one named recipe when it is asked to")
do
  local s = scene()
  local args = { recipe = "MakeCrate" }
  ok(Inspect:validate(args, s.ctx), "a named recipe validates")
  equal(Inspect:begin(args, s.ctx), "done", "and still queues nothing")
  local report = Inspect:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  equal(report.requested, "MakeCrate", "the report names what was asked about")
  equal(report.found, true, "says the character knows it")
  equal(report.listed, 1, "and carries that one entry only")
  equal(report.recipes[1].ready, true, "with the verdict on its materials")

  local absent = Inspect:verify(nil, Toolkit.observe(s.player), { recipe = "MakeAnvil" }, s.ctx)
  equal(absent.found, false, "a recipe the character has not learned is reported absent")
  equal(absent.listed, 0, "with no entry")
  ok(absent ~= nil, "and the reading is still a success -- it answered the question it was asked")

  local malformed, code = Inspect:validate({ recipe = "Ragù" }, s.ctx)
  isNil(malformed, "a recipe name the token alphabet cannot carry is refused")
  equal(code, REASON.INVALID_ARGUMENT, "as an invalid argument")
end

Harness.group("inspect is bounded, and honest about stopping short")
do
  local many = {}
  for index = 1, Crafting.MAX_LISTED + 5 do
    many[index] = recipe({
      name = string.format("Recipe%03d", index),
      inputs = { { types = { PLANK }, count = 1 } },
      product = CRATE,
    })
  end
  local s = scene({ recipes = many })
  local report = Inspect:verify(nil, Toolkit.observe(s.player), {}, s.ctx)
  equal(report.listed, Crafting.MAX_LISTED, "the listing stops at the declared cap")
  equal(report.known_total, Crafting.MAX_LISTED + 5, "while the engine's count travels whole")
  equal(report.truncated, true, "and the report says it stopped short")

  local limited = scene({ recipes = many })
  local small = Inspect:verify(nil, Toolkit.observe(limited.player), { limit = 3 }, limited.ctx)
  equal(small.listed, 3, "a caller may ask for fewer")
  equal(small.truncated, true, "and is still told the rest exist")

  local refused, code = Inspect:validate({ limit = Crafting.MAX_LISTED + 1 }, limited.ctx)
  isNil(refused, "a limit past the ceiling is refused")
  equal(code, REASON.INVALID_ARGUMENT, "as an invalid argument")

  local unknownArg, unknownCode = Inspect:validate({ count = 2 }, limited.ctx)
  isNil(unknownArg, "an argument inspect does not declare is refused")
  equal(unknownCode, REASON.INVALID_ARGUMENT, "rather than ignored")
end

Harness.group("a build that will not list recipes is a capability gap, never an empty list")
do
  local s = scene({ no_known = true })
  local refused, code, detail = Inspect:validate({}, s.ctx)
  isNil(refused, "with no known-recipe reader there is nothing to inspect")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "getKnownRecipes", "naming the symbol, never 'this character knows nothing'")

  local playerless = scene()
  playerless.ctx.player = nil
  local none, noneCode = Inspect:validate({}, playerless.ctx)
  isNil(none, "and with no character there is nothing to read")
  equal(noneCode, REASON.PRECONDITION_FAILED, "which is a precondition, not a capability")
end

-- ---------------------------------------------------------------------------
-- crafting.craft
-- ---------------------------------------------------------------------------

Harness.group("craft refuses before it spends anything, naming what is wrong")
do
  local s = scene()
  local function refuse(args, ctx)
    local accepted, code, detail = Craft:validate(args, ctx or s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "crafting nothing in particular is not a command")

  code = refuse({ recipe = "MakeCrate", count = 0 })
  equal(code, REASON.INVALID_ARGUMENT, "a count below the floor is refused")

  code = refuse({ recipe = "MakeCrate", count = Crafting.MAX_COUNT + 1 })
  equal(code, REASON.INVALID_ARGUMENT, "and so is one past the ceiling")

  code = refuse({ recipe = "MakeCrate", speed = "fast" })
  equal(code, REASON.INVALID_ARGUMENT, "an undeclared argument is refused rather than dropped")

  local detail
  code, detail = refuse({ recipe = "MakeAnvil" })
  equal(code, REASON.RECIPE_UNKNOWN, "a recipe this character has not learned is refused as unknown")
  contains(detail, "MakeAnvil", "naming the recipe that was asked for")

  local bare = scene({ items = { carried(PLANK, "Plank") } })
  code, detail = refuse({ recipe = "MakeCrate" }, bare.ctx)
  equal(code, REASON.RECIPE_MATERIALS_MISSING, "materials short of the recipe refuse it")
  contains(detail, "Base.Plank", "naming what is short")
  contains(detail, "1 of 2", "with how short it is")

  local nailless = scene({ items = { carried(PLANK, "Plank"), carried(PLANK, "Plank") } })
  code, detail = refuse({ recipe = "MakeCrate" }, nailless.ctx)
  equal(code, REASON.RECIPE_MATERIALS_MISSING, "one ingredient short is short")
  contains(detail, "Base.Nails", "and the refusal names which one")

  local productless = scene({ no_product = true })
  code, detail = refuse({ recipe = "MakeCrate" }, productless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "a recipe whose product cannot be read cannot be verified")
  contains(detail, "getOutputs", "so the refusal names the reader that was missing")

  local inputless = scene({ no_inputs = true })
  code, detail = refuse({ recipe = "MakeCrate" }, inputless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "and one whose ingredients cannot be read is not judged ready")
  contains(detail, "getInputs", "naming that reader instead")

  local scriptless = scene({ no_scripts = true })
  code, detail = refuse({ recipe = "MakeCrate" }, scriptless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "a name with nothing to look it up in is a capability gap")
  contains(detail, "getCraftRecipe", "naming the lookup that was probed")

  local listless = scene({ no_known = true })
  code, detail = refuse({ recipe = "MakeCrate" }, listless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "an unreadable known-recipe list is a capability gap")
  contains(detail, "getKnownRecipes", "never 'this character knows no such recipe'")

  local threatened = scene({ safety = Support.threatened() })
  code = refuse({ recipe = "MakeCrate" }, threatened.ctx)
  equal(code, REASON.THREAT_INTERRUPTED, "crafting holds the character still, so a horde refuses it")

  local takenOver = scene({ safety = Support.takenOver() })
  code = refuse({ recipe = "MakeCrate" }, takenOver.ctx)
  equal(code, REASON.USER_TAKEOVER, "and the player at the controls outranks the command")

  local queueless = scene()
  Support.removeQueue()
  code, detail = refuse({ recipe = "MakeCrate" }, queueless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "with no action queue there is nowhere to put the work")
  contains(detail, "ISTimedActionQueue.add", "naming the symbol the command needs")
  Support.installQueue()
end

Harness.group("a craft is proved by the product and the materials, never by the queue")
do
  local args = { recipe = "MakeCrate" }
  local s = scene()
  ok(Craft:validate(args, s.ctx), "a known recipe with its materials validates")
  ok(Craft:begin(args, s.ctx), "and the work is queued")
  equal(#s.craftAction.actions, 1, "one craft action for one item")
  equal(#s.queue.added, 1, "and it went to the game's own queue")

  -- Queued and nothing performed: the inventory is unchanged.
  local nothing, code, detail = Craft:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  isNil(nothing, "an action that was queued and never ran proves nothing")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed its postcondition")
  contains(detail, "holds 0 more", "with the honest count")

  craftAll(s)
  local proof = Craft:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "the crate in the inventory is the evidence")
  equal(proof.kind, "crafted", "named for what was observed")
  equal(proof.recipe, "MakeCrate", "carrying the recipe token")
  equal(proof.product, CRATE, "and the product type")
  equal(proof.count_requested, 1, "with what was asked for")
  equal(proof.count_crafted, 1, "and what was counted afterwards")
  equal(proof.product_before, 0, "off a measured before")
  equal(proof.product_after, 1, "and a measured after")
  ok(#proof.materials_consumed >= 1, "and the materials that were spent")
  equal(proof.materials_consumed[1].before, 3, "counted before")
  equal(proof.materials_consumed[1].after, 1, "and after")

  -- The anti-lie case: the product appears and nothing was spent.
  local free = scene({ craft_free = true })
  ok(Craft:validate(args, free.ctx), "the free-product case validates")
  ok(Craft:begin(args, free.ctx), "and starts")
  craftAll(free)
  local unearned, freeCode, freeDetail = Craft:verify(nil, Toolkit.observe(free.player), args, free.ctx)
  isNil(unearned, "a product with nothing consumed is not an observed craft")
  equal(freeCode, REASON.POSTCONDITION_FAILED, "it failed its postcondition")
  contains(freeDetail, "nothing the recipe consumes", "with the reason spelled out")

  local silent = scene({ craft_noop = true })
  ok(Craft:validate(args, silent.ctx), "the swallowed-action case validates")
  ok(Craft:begin(args, silent.ctx), "and starts")
  craftAll(silent)
  local swallowed, silentCode = Craft:verify(nil, Toolkit.observe(silent.player), args, silent.ctx)
  isNil(swallowed, "an action that ran and moved nothing is not a success")
  equal(silentCode, REASON.POSTCONDITION_FAILED, "and says so")
end

Harness.group("one command runs one recipe once, and nothing re-queues it")
do
  local s = scene()
  local args = { recipe = "MakeCrate", count = 1 }
  equal(Crafting.MAX_COUNT, 1, "the ceiling is one item, pinned here as well as declared")
  ok(Craft:validate(args, s.ctx), "an explicit count of one validates")
  ok(Craft:begin(args, s.ctx), "and the work is queued")
  equal(#s.craftAction.actions, 1, "as exactly one action")

  local nothing = Craft:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  isNil(nothing, "an unperformed action is not a craft")
  equal(#s.craftAction.actions, 1, "and the failure re-queued nothing")

  craftAll(s)
  local proof = Craft:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "the performed one is")
  equal(proof.count_crafted, 1, "counted as one")

  -- Raising the ceiling has to be a visible change on both halves of the wire,
  -- so the mod refuses a count the sidecar's own bound would never send.
  local refused, code = Craft:validate({ recipe = "MakeCrate", count = 2 }, scene().ctx)
  isNil(refused, "a second item in one command is refused")
  equal(code, REASON.INVALID_ARGUMENT, "as an argument past its declared ceiling")
end

Harness.group("the window is bounded by the clock as well as by the queue")
do
  local args = { recipe = "MakeCrate" }
  local s = scene()
  ok(Craft:validate(args, s.ctx), "the command validates")
  ok(Craft:begin(args, s.ctx), "and starts")
  equal(Craft:progress(args, s.ctx), "running", "an unfinished craft keeps the window open")

  craftAll(s)
  equal(Craft:progress(args, s.ctx), "done", "the observed product closes it")

  local slow = scene()
  ok(Craft:validate(args, slow.ctx), "the slow case validates")
  ok(Craft:begin(args, slow.ctx), "and starts")
  slow.ctx.now_ms = Support.NOW + Crafting.CRAFT_WINDOW_MS
  equal(Craft:progress(args, slow.ctx), "done", "and the clock closes it even with nothing made")
  local nothing, code = Craft:verify(nil, Toolkit.observe(slow.player), args, slow.ctx)
  isNil(nothing, "closing the window is not succeeding")
  equal(code, REASON.POSTCONDITION_FAILED, "the postcondition still has to hold")

  local interrupted = scene()
  ok(Craft:validate(args, interrupted.ctx), "the interruption case validates")
  ok(Craft:begin(args, interrupted.ctx), "and starts")
  interrupted.ctx.safety = Support.takenOver()
  local status, takeoverCode = Craft:progress(args, interrupted.ctx)
  isNil(status, "a takeover mid-craft stops the poll")
  equal(takeoverCode, REASON.USER_TAKEOVER, "naming the player's own decision")
end

Harness.group("known recipes may arrive as objects instead of names")
do
  local s = scene({ known_objects = true, no_scripts = true })
  local report = Inspect:verify(nil, Toolkit.observe(s.player), {}, s.ctx)
  equal(report.listed, 1, "a collection of recipe objects lists the same way")
  equal(report.recipes[1].ready, true, "and is judged without a script manager to look anything up in")
  ok(Craft:validate({ recipe = "MakeCrate" }, s.ctx), "and a craft against it validates")
end

Harness.group("both commands declare themselves the way the runtime demands")
do
  equal(Inspect.action, "crafting.inspect", "the inspect names its protocol action")
  equal(Craft.action, "crafting.craft", "and so does the craft")
  ok(PZ.Protocol.isKnownAction(Inspect.action), "the protocol knows the inspect")
  ok(PZ.Protocol.isKnownAction(Craft.action), "and the craft")
  ok(PZ.Protocol.READ_ONLY_ACTIONS[Inspect.action] == true, "the inspect is read-only in the protocol")
  isNil(PZ.Protocol.READ_ONLY_ACTIONS[Craft.action], "and the craft is not")

  -- The read-only half declares no capability, exactly as world.inspect,
  -- container.inspect and inventory.search do and for their reason: a probe
  -- over accessors that never appear in the game's Lua would report the whole
  -- thing unsupported on a healthy install. The craft is the half a live run
  -- can confirm, so it is the half that rides the capability -- and the
  -- sidecar's adapters name the same split.
  isNil(Inspect.capability, "the read-only half is gated on the reader it needs, not on a probe")
  equal(Craft.capability, "crafting", "while the craft rides the crafting capability")
  equal(Craft.experimental, true, "which is experimental until a live craft promotes it")
  equal(Inspect.experimental, false, "and the ungated half claims no ceiling it has nowhere to publish")

  -- The agreement Combat states for its engage window: the runtime's lease and
  -- the clock the progress step watches are the same number.
  equal(Craft.timeout_ms, Crafting.CRAFT_WINDOW_MS, "the declared timeout is the craft window")
  equal(Craft.args.count.max, Crafting.MAX_COUNT, "the count ceiling is declared on the wire")
  equal(Craft.args.recipe.required, true, "and a craft must name its recipe")
end

Harness.finish("adapter_crafting")
