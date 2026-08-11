--[[
PZAgent.Adapters.Crafting -- crafting.inspect and crafting.craft.

Crafting is the first action in this mod that destroys something. A walk can be
walked back, a door can be closed again, an equipped weapon can be unequipped;
a plank consumed by the wrong recipe is gone. Everything below is shaped by
that one fact.

* **One command makes what it was asked for, once.** MAX_COUNT is one this
  wave, the count is fixed when the command is accepted, and the work is queued
  in one go. There is no retry here at all: a recipe that could have run again
  is a line in the report, never something this file starts a second time. The
  next run is the mission's decision, taken at the seam where safety.stop and
  the reflex guard can reach it. The arithmetic is written for a count of N
  because a check that special-cased one would be the wrong shape the day the
  bound moves -- and moving it is a change on both halves of the wire.
* **The postcondition is the product in the inventory, never the queue.** A
  craft action that was accepted proves nothing. `succeeded` is minted only when
  the re-observed inventory holds more of the product than it did AND something
  the recipe consumes is measurably gone. Both halves are required: a product
  that appeared while nothing was spent did not come out of this recipe, and
  saying it did would be the fabricated success this project exists to refuse.
* **Materials are checked before anything is queued.** RECIPE_MATERIALS_MISSING
  names what is short and how short, because the alternative is a timed action
  that runs, fails silently and leaves the character standing at a bench.
* **Every recipe symbol here is unverified against Build 42.** That build
  rewrote crafting, and none of these spellings has been seen answering in a
  live session, so each is probed through a short closed list and an absent one
  costs a CAPABILITY_UNAVAILABLE naming every candidate. The capability declares
  itself experimental for that reason and for the destructiveness above; the
  sidecar therefore withholds these tools on every install until a live run
  promotes them.

`crafting.inspect` mutates nothing -- it reads the recipe tables the character
already knows and the items already on the person -- so the protocol lists it
among the read-only actions, and like the other three read-only actions it
declares no capability at all: the accessors it needs never appear in the
game's Lua, so a probe over them would report `unsupported` on a healthy
install. What it does instead is refuse, naming the reader, when the build has
not got one. Only `crafting.craft` rides the capability, which is the half a
live run can confirm.

Nothing here reads a crafting surface or a world container. Materials are
counted on the character, which is the rung this file implements; a recipe that
needs a bench the character must walk to is the sidecar policy's escalation,
taken from the arguments a command carries, exactly as move_to escalates off
its radius (docs/SAFETY.md).

The recipe readers below duplicate what PZAgent.Observe reads for the
observation rather than calling into it. Adapters reach the engine through
PZAgent.Adapters.Toolkit and nothing else, and the observation reader is the
other side of the mod; two short guarded probes in two files is the price of
that, and it is the same trade Combat.lua makes with Movement's walk.
]]

-- Load-order guard, live-proven 2026-08-08 on Build 42.20.2: the engine walks
-- adapters/ in an order this file does not control. The statement form is
-- deliberate -- the paren form is banned as dynamic loading -- and the test
-- harness pre-resolves this module, so there the require is a no-op.
require "PZAgent/adapters/Toolkit"

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Crafting = {}
PZAgent.Adapters.Crafting = Crafting

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

-- ---------------------------------------------------------------------------
-- bounds
-- ---------------------------------------------------------------------------

--- Known recipes read out of the engine for one command. A character late in a
--- run knows hundreds; this is the walk that finds the one a command named, and
--- a report that stopped short says so rather than answering "unknown".
Crafting.MAX_SCAN = 128

--- Recipes one inspect reports. The listing goes out in an ack, so the ceiling
--- belongs on the wire and not only in the reader.
Crafting.MAX_LISTED = 24

--- Ingredient entries read off one recipe, and item types read off one
--- ingredient. Both are the shape of a recipe rather than a guess: a script
--- that wants more than this is not something this file can honestly summarise.
Crafting.MAX_INPUTS = 8

--- Shortfalls named in one refusal, and in one report entry.
Crafting.MAX_MISSING = 8

--- Items one command may ask for: one. The argument exists so the number is on
--- the wire rather than a default this file picked, and so that raising the
--- ceiling later is a visible change on both halves rather than a bound that
--- quietly moved -- the sidecar's MAX_CRAFT_COUNT is the same one for the same
--- reason. The arithmetic below is written for N anyway: a materials check that
--- special-cased one would be the wrong shape the day the bound moves.
Crafting.MIN_COUNT = 1
Crafting.MAX_COUNT = 1
Crafting.DEFAULT_COUNT = 1

--- One craft window, in milliseconds. The window is bounded by this clock as
--- well as by the queue emptying, and the declared timeout below is this same
--- number so the runtime's lease closes the command on the clock the progress
--- step watches -- the agreement Combat.lua states for its engage window.
Crafting.CRAFT_WINDOW_MS = 30000

Crafting.INSPECT_TIMEOUT_MS = 5000
Crafting.POLL_MS = 250

--- How the refusals name the probed surfaces: one spelling on the wire per
--- surface, listing every candidate that was looked for. These are the least
--- certain rows this mod has -- Build 42 rewrote crafting and nothing here has
--- been seen answering in a live session -- and docs/GAME_API_VERIFICATION.md
--- is where they belong as rows.
Crafting.KNOWN_SYMBOL = "IsoGameCharacter.getKnownRecipes"
Crafting.RECIPE_SYMBOL = "getScriptManager().getCraftRecipe / getRecipe"
Crafting.INPUT_SYMBOL = "CraftRecipe.getInputs / getSource"
Crafting.OUTPUT_SYMBOL = "CraftRecipe.getOutputs / getResult"
Crafting.ACTION_SYMBOL = "ISCraftRecipeAction / ISCraftAction"

--- Candidate spellings, probed in order, for each recipe surface.
local KNOWN_NAMES = { "getKnownRecipes" }
local LOOKUP_NAMES = { "getCraftRecipe", "getRecipe" }
local INPUT_NAMES = { "getInputs", "getSource" }
local OUTPUT_NAMES = { "getOutputs" }
local TYPE_NAMES = { "getItems", "getItemTypes" }
local ACTION_NAMES = { "ISCraftRecipeAction", "ISCraftAction" }

--- Argument kinds, spelled the way PZAgent.CommandDispatcher.ARG spells them.
--- Held here rather than read off the dispatcher because the engine chooses the
--- order it walks media/lua in, and a declaration that indexed another client
--- module while this file was loading would break on a build that reaches
--- adapters/ first.
local ARG = { NUMBER = "number", STRING = "string" }

local INSPECT_ARGS = { "recipe", "limit" }
local CRAFT_ARGS = { "recipe", "count" }

-- ---------------------------------------------------------------------------
-- reading recipes
-- ---------------------------------------------------------------------------

--- The first of `names` that answers with something list-shaped.
---
--- "Is it a list" is asked by asking it its size, because Kahlua hands a Java
--- collection over as a value whose Lua type this side must not assert on.
local function listOf(owner, names)
  local Toolkit = toolkit()
  for index = 1, #names do
    local ok, value = Toolkit.call(owner, names[index])
    if ok and value ~= nil and Toolkit.listSize(value) ~= nil then
      return value
    end
  end
  return nil
end

--- A recipe's name, whether the collection held strings or recipe objects.
local function recipeNameOf(entry)
  if type(entry) == "string" then
    return entry
  end
  return (toolkit().readStringOf(entry, { "getName", "getOriginalname" }))
end

--- The token a recipe is named by on the wire.
---
--- One substitution -- every space becomes an underscore -- and then the
--- reference alphabet, which is exactly what ObserveModel.recipeToken does when
--- it publishes the same recipe in the observation. The two must agree: the
--- token the planner reads out of a snapshot is the token it sends back here,
--- and a recipe named one way and accepted another is a command that can never
--- be issued. A name the alphabet cannot carry has no token at all rather than
--- a mangled one, because two recipes behind one token is worse than a recipe
--- this agent cannot name.
function Crafting.recipeToken(name)
  if type(name) ~= "string" or #name == 0 or #name > 64 then
    return nil
  end
  local token = (name:gsub(" ", "_"))
  if not PZAgent.Refs.isSafeSegment(token) then
    return nil
  end
  return token
end

--- Every recipe the character knows, bounded.
---
--- Returns the entries, how many the engine reported, and whether the walk
--- stopped short -- or nil plus the symbol that was missing. Never an empty
--- list for a missing reader: "this character knows nothing" and "this build
--- would not say" are opposite facts, and only the first is a refusal a player
--- can act on.
function Crafting.knownRecipes(player)
  local Toolkit = toolkit()
  local known = listOf(player, KNOWN_NAMES)
  local size = known ~= nil and Toolkit.listSize(known) or nil
  if size == nil then
    return nil, Crafting.KNOWN_SYMBOL
  end
  local scanned = math.min(size, Crafting.MAX_SCAN)
  local entries = {}
  for index = 0, scanned - 1 do
    local entry = Toolkit.listGet(known, index)
    local name = recipeNameOf(entry)
    local token = Crafting.recipeToken(name)
    if token ~= nil then
      entries[#entries + 1] = {
        name = name,
        token = token,
        recipe = type(entry) ~= "string" and entry or nil,
      }
    end
  end
  return entries, size, size > scanned
end

--- The recipe object behind a known-recipe entry, or nil.
---
--- An entry that already is a recipe object is used as it stands; a bare name
--- is looked up through the script manager, which is reached through the guarded
--- global lookup rather than as a bare global -- an unverified symbol must not
--- become one this file depends on being present.
function Crafting.recipeObject(entry)
  local Toolkit = toolkit()
  if entry.recipe ~= nil then
    return entry.recipe
  end
  local accessor = Toolkit.globalNamed("getScriptManager")
  if type(accessor) ~= "function" then
    return nil
  end
  local ok, manager = pcall(accessor)
  if not ok or manager == nil then
    return nil
  end
  for index = 1, #LOOKUP_NAMES do
    local found, recipe = Toolkit.call(manager, LOOKUP_NAMES[index], entry.name)
    if found and recipe ~= nil then
      return recipe
    end
  end
  return nil
end

--- What one recipe consumes: `{ { types = {...}, count = n }, ... }`, or nil.
---
--- A recipe whose ingredient list is only partly readable answers nil as a
--- whole. A verdict computed from the ingredients that happened to answer would
--- call a recipe ready on the strength of requirements nobody read, and that is
--- the one wrong answer that spends materials for nothing.
function Crafting.recipeInputs(recipe)
  local Toolkit = toolkit()
  local list = listOf(recipe, INPUT_NAMES)
  local size = list ~= nil and Toolkit.listSize(list) or nil
  if size == nil or size > Crafting.MAX_INPUTS then
    return nil
  end
  local inputs = {}
  for index = 0, size - 1 do
    local entry = Toolkit.listGet(list, index)
    local types = listOf(entry, TYPE_NAMES)
    local typeCount = types ~= nil and Toolkit.listSize(types) or nil
    if typeCount == nil or typeCount == 0 or typeCount > Crafting.MAX_INPUTS then
      return nil
    end
    local names = {}
    for typeIndex = 0, typeCount - 1 do
      local name = Toolkit.listGet(types, typeIndex)
      if type(name) ~= "string" then
        name = Toolkit.readStringOf(name, { "getFullType", "getName" })
      end
      if name == nil then
        return nil
      end
      names[#names + 1] = name
    end
    inputs[#inputs + 1] = {
      types = names,
      count = Toolkit.readNumberOf(entry, { "getCount", "getAmount" }) or 1,
    }
  end
  return inputs
end

--- What one recipe produces, as an item type, or nil when it cannot be read.
---
--- Without this there is no postcondition: the whole proof a craft happened is
--- more of this type in the inventory than before, so a recipe whose product
--- cannot be named is refused before anything is queued rather than run and
--- then guessed at.
function Crafting.recipeProduct(recipe)
  local Toolkit = toolkit()
  local outputs = listOf(recipe, OUTPUT_NAMES)
  if outputs ~= nil and (Toolkit.listSize(outputs) or 0) > 0 then
    local first = Toolkit.listGet(outputs, 0)
    local types = listOf(first, TYPE_NAMES)
    if types ~= nil and (Toolkit.listSize(types) or 0) > 0 then
      local name = Toolkit.listGet(types, 0)
      if type(name) == "string" then
        return name
      end
      local read = Toolkit.readStringOf(name, { "getFullType", "getName" })
      if read ~= nil then
        return read
      end
    end
    local direct = Toolkit.readStringOf(first, { "getFullType", "getName" })
    if direct ~= nil then
      return direct
    end
  end
  local ok, result = Toolkit.call(recipe, "getResult")
  if not ok or result == nil then
    return nil
  end
  return (Toolkit.readStringOf(result, { "getFullType", "getType", "getName" }))
end

-- ---------------------------------------------------------------------------
-- counting what the character carries
-- ---------------------------------------------------------------------------

--- Count one item type in a snapshot, under its full name or its tail.
---
--- A recipe names an ingredient either way -- "Base.Nails" and "Nails" are the
--- same nail -- so both spellings count, and the ambiguity is stated here
--- rather than guessed at each call site.
local function matchesType(fullType, wanted)
  if type(fullType) ~= "string" or type(wanted) ~= "string" then
    return false
  end
  if fullType == wanted then
    return true
  end
  return fullType:match("([^%.]+)$") == wanted:match("([^%.]+)$")
end

Crafting.matchesType = matchesType

--- How many of `wanted` the snapshot shows on the character.
---
--- Walks the snapshot PZAgent.Adapters.Toolkit built, which is already bounded
--- at MAX_SNAPSHOT_ITEMS, rather than the engine: the postcondition compares
--- two of these, and a count taken any other way would be comparing two
--- different walks.
function Crafting.countType(snapshot, wanted)
  if type(snapshot) ~= "table" or type(snapshot.items) ~= "table" then
    return 0
  end
  local held = 0
  for _, record in pairs(snapshot.items) do
    if matchesType(record.full_type, wanted) then
      held = held + 1
    end
  end
  return held
end

--- What `inputs` is short of, given a snapshot and how many runs are wanted.
---
--- Returns a bounded list of `{ types, need, held }`, empty when everything is
--- there. The alternatives one ingredient lists are summed rather than tried
--- separately: a recipe that takes a plank or a log is satisfied by one of
--- each, and asking each type on its own would refuse the mixed bag the game
--- accepts.
function Crafting.missingFor(inputs, snapshot, multiplier)
  local missing = {}
  for index = 1, #inputs do
    local input = inputs[index]
    local held = 0
    for typeIndex = 1, #input.types do
      held = held + Crafting.countType(snapshot, input.types[typeIndex])
    end
    local need = input.count * multiplier
    if held < need and #missing < Crafting.MAX_MISSING then
      missing[#missing + 1] = { types = input.types, need = need, held = held }
    end
  end
  return missing
end

--- The shortfalls as one sentence, for the refusal a person reads.
local function describeMissing(missing)
  local parts = {}
  for index = 1, #missing do
    local entry = missing[index]
    parts[index] = string.format(
      "%s (%d of %d)",
      table.concat(entry.types, " or "),
      entry.held,
      entry.need
    )
  end
  return table.concat(parts, ", ")
end

Crafting.describeMissing = describeMissing

-- ---------------------------------------------------------------------------
-- crafting.inspect
-- ---------------------------------------------------------------------------

--- `recipe` is optional and the sidecar always sends it: a mission asks about
--- the one recipe it is considering, and the whole bounded listing is what a
--- caller gets when it names none. Both shapes are one reading -- the listing
--- with a filter over it -- so there is one parser and one report.
local function inspectSpec(args)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, INSPECT_ARGS, {})
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local spec = {}
  if args.recipe ~= nil then
    local name, nameCode, nameDetail = Toolkit.readText(args, "recipe", { maximum = 64 })
    if name == nil then
      return nil, nameCode, nameDetail
    end
    spec.token = Crafting.recipeToken(name)
    if spec.token == nil then
      return nil, reasons.INVALID_ARGUMENT, "argument \"recipe\" is not a recipe token"
    end
  end
  local limit, limitCode, limitDetail = Toolkit.readCount(args, "limit", {
    default = Crafting.MAX_LISTED,
    minimum = 1,
    maximum = Crafting.MAX_LISTED,
  })
  if limit == nil then
    return nil, limitCode, limitDetail
  end
  spec.limit = limit
  return spec
end

local function inspectValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec, code, detail = inspectSpec(args)
  if spec == nil then
    return nil, code, detail
  end
  if ctx.player == nil then
    return nil, reasons.PRECONDITION_FAILED, "no player character"
  end
  local known, symbolOrTotal = Crafting.knownRecipes(ctx.player)
  if known == nil then
    return Toolkit.unavailable(symbolOrTotal)
  end
  Toolkit.state(ctx).inspect = spec
  return true
end

--- Reading the recipe tables queues nothing, so the whole command finishes
--- inside begin and the reading itself happens in verify -- against the
--- character as it is at that moment, which is the only moment the answer is
--- about.
local function inspectBegin(_, args, _)
  local spec, code, detail = inspectSpec(args)
  if spec == nil then
    return nil, code, detail
  end
  return "done"
end

local function inspectProgress(_, args, _)
  local spec, code, detail = inspectSpec(args)
  if spec == nil then
    return nil, code, detail
  end
  return "done"
end

local function inspectVerify(_, _, after, args, ctx)
  local Toolkit = toolkit()
  local spec, code, detail = inspectSpec(args)
  if spec == nil then
    return nil, code, detail
  end
  local known, totalOrSymbol, truncated = Crafting.knownRecipes(ctx.player)
  if known == nil then
    return Toolkit.unavailable(totalOrSymbol)
  end
  local snapshot = type(after) == "table" and after or Toolkit.observe(ctx.player)
  -- One recipe asked about is the listing with a filter over it, so the walk
  -- below is the same walk either way and the entry a caller gets back is the
  -- same entry the full listing would have carried.
  local wanted = known
  if spec.token ~= nil then
    wanted = {}
    for index = 1, #known do
      if known[index].token == spec.token then
        wanted[#wanted + 1] = known[index]
      end
    end
  end
  local listed = {}
  local ready = 0
  local judged = 0
  for index = 1, math.min(#wanted, spec.limit) do
    local entry = wanted[index]
    local record = { recipe = entry.token, display_name = entry.name }
    local recipe = Crafting.recipeObject(entry)
    if recipe ~= nil then
      record.product = Crafting.recipeProduct(recipe)
      local inputs = Crafting.recipeInputs(recipe)
      if inputs ~= nil then
        local missing = Crafting.missingFor(inputs, snapshot, 1)
        judged = judged + 1
        record.ready = #missing == 0
        if record.ready then
          ready = ready + 1
        else
          record.missing = missing
        end
      end
    end
    -- A record with no `ready` is the third state: the recipe is known and its
    -- ingredients could not be read. It is listed as such rather than dropped,
    -- because "we cannot tell" is an answer the planner has to be able to see.
    listed[#listed + 1] = record
  end
  local report = {
    kind = "known_recipes",
    recipes = listed,
    listed = #listed,
    ready = ready,
    judged = judged,
    known_total = totalOrSymbol,
    truncated = truncated == true or #wanted > #listed,
  }
  if spec.token ~= nil then
    -- A recipe that was asked about and is not among the known ones is
    -- reported as absent rather than refused: the reading succeeded, and what
    -- it found is that this character has not learned it. `crafting.craft` is
    -- where that same fact becomes RECIPE_UNKNOWN, because there it stops a
    -- command instead of answering one.
    report.requested = spec.token
    report.found = #listed > 0
  end
  return report
end

local Inspect = toolkit().declare({
  name = "crafting.inspect",
  -- No capability, and the same reason world.inspect, container.inspect and
  -- inventory.search declare none: this action only reads, and what it reads
  -- is behind Java accessors no scan of the install can see, so a probe over
  -- them would report `unsupported` on a healthy build. It gates on the reader
  -- it actually needs, at validate, and refuses naming the symbol when the
  -- build has not got it. The sidecar leaves this action ungated for the same
  -- reason; the two halves have to name the same capability, and this is the
  -- one they name.
  capability = nil,
  requires = {},
  timeout_ms = Crafting.INSPECT_TIMEOUT_MS,
  poll_interval_ms = Crafting.POLL_MS,
  args = {
    -- The recipe a mission is considering. Optional here and always sent by
    -- the sidecar, which asks about one recipe at a time; an undeclared key is
    -- a refused command, so this declaration is what lets that command exist.
    recipe = { type = ARG.STRING, max_bytes = 64 },
    -- The listing goes out in an ack, so the ceiling belongs on the wire as
    -- well as in the reader.
    limit = { type = ARG.NUMBER, integer = true, min = 1, max = Crafting.MAX_LISTED },
  },
  validate = inspectValidate,
  begin = inspectBegin,
  progress = inspectProgress,
  verify = inspectVerify,
})

Crafting.Inspect = Inspect
toolkit().register(Inspect)

-- ---------------------------------------------------------------------------
-- crafting.craft
-- ---------------------------------------------------------------------------

local Craft = nil

local function craftSpec(args)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, CRAFT_ARGS, { "recipe" })
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local name, nameCode, nameDetail = Toolkit.readText(args, "recipe", { maximum = 64 })
  if name == nil then
    return nil, nameCode, nameDetail
  end
  local token = Crafting.recipeToken(name)
  if token == nil then
    return nil, reasons.INVALID_ARGUMENT, "argument \"recipe\" is not a recipe token"
  end
  local count, countCode, countDetail = Toolkit.readCount(args, "count", {
    default = Crafting.DEFAULT_COUNT,
    minimum = Crafting.MIN_COUNT,
    maximum = Crafting.MAX_COUNT,
  })
  if count == nil then
    return nil, countCode, countDetail
  end
  return { token = token, count = count }
end

Crafting.craftSpec = craftSpec

local function craftValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Craft.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = craftSpec(args)
  if spec == nil then
    return nil, code, detail
  end
  if ctx.player == nil then
    return nil, reasons.PRECONDITION_FAILED, "no player character"
  end
  -- Crafting holds the character still and busy; starting one next to a horde
  -- is the situation the reflex guard exists for.
  local threatCode, threatDetail = Toolkit.interruption(ctx)
  if threatCode ~= nil then
    return nil, threatCode, threatDetail
  end
  local known, totalOrSymbol, truncated = Crafting.knownRecipes(ctx.player)
  if known == nil then
    return Toolkit.unavailable(totalOrSymbol)
  end
  local entry = nil
  for index = 1, #known do
    if known[index].token == spec.token then
      entry = known[index]
      break
    end
  end
  if entry == nil then
    -- A walk that stopped short says so inside the refusal: "not among the
    -- first 128 recipes read" is a different fact from "not known", and a
    -- player told the second when the first is true would go looking for a
    -- magazine they do not need.
    local scope = truncated
        and string.format(" among the %d of %d recipes read", #known, totalOrSymbol)
      or ""
    return nil,
      reasons.RECIPE_UNKNOWN,
      string.format("this character knows no recipe called %q%s", spec.token, scope)
  end
  local recipe = Crafting.recipeObject(entry)
  if recipe == nil then
    return Toolkit.unavailable(Crafting.RECIPE_SYMBOL)
  end
  local product = Crafting.recipeProduct(recipe)
  if product == nil then
    -- Without the product there is no postcondition to check, and a craft
    -- nobody could verify must not be started at all.
    return Toolkit.unavailable(Crafting.OUTPUT_SYMBOL)
  end
  local inputs = Crafting.recipeInputs(recipe)
  if inputs == nil then
    return Toolkit.unavailable(Crafting.INPUT_SYMBOL)
  end
  local snapshot = Toolkit.observe(ctx.player)
  local missing = Crafting.missingFor(inputs, snapshot, spec.count)
  if #missing > 0 then
    return nil,
      reasons.RECIPE_MATERIALS_MISSING,
      string.format(
        "%s needs %s",
        entry.name,
        describeMissing(missing)
      )
  end
  spec.name = entry.name
  spec.recipe = recipe
  spec.product = product
  spec.inputs = inputs
  Toolkit.state(ctx).craft = spec
  return true
end

--- Queue the work, once.
---
--- One timed action per item asked for, all of them queued here and none of
--- them queued later: the fan-out is fixed at accept time, bounded by
--- MAX_COUNT, and no branch below re-queues anything. What comes after is
--- watching and re-observing.
local function craftBegin(_, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).craft
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no recipe"
  end
  local before = Toolkit.observe(ctx.player)
  spec.product_before = Crafting.countType(before, spec.product)
  spec.materials_before = {}
  for index = 1, #spec.inputs do
    local input = spec.inputs[index]
    local held = 0
    for typeIndex = 1, #input.types do
      held = held + Crafting.countType(before, input.types[typeIndex])
    end
    spec.materials_before[index] = held
  end
  for _ = 1, spec.count do
    local action, actionCode, actionDetail = Toolkit.constructFirst(ACTION_NAMES, ctx.player, spec.recipe)
    if action == nil then
      return nil, actionCode, actionDetail
    end
    local queued, queueCode, queueDetail = Toolkit.enqueue(ctx, action)
    if queued == nil then
      return nil, queueCode, queueDetail
    end
  end
  spec.started_ms = ctx.now_ms or 0
  return true
end

--- One poll of the window. Interruption first, so a takeover or a reflex-guard
--- rung stops it mid-craft; then the observed product, which ends the window as
--- soon as what was asked for exists; then the clock. Closing the window is
--- always "done", and whether "done" is a success is verify's question against
--- the re-observed inventory, never this function's.
local function craftProgress(_, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).craft
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no recipe"
  end
  local now = Toolkit.observe(ctx.player)
  if (Crafting.countType(now, spec.product) - spec.product_before) >= spec.count then
    return "done"
  end
  local elapsed = (ctx.now_ms or 0) - (spec.started_ms or 0)
  if elapsed >= Crafting.CRAFT_WINDOW_MS then
    return "done"
  end
  return Toolkit.queueProgress(ctx)
end

--- The postcondition: more of the product, and less of what makes it.
---
--- Both halves, and both re-observed. The product alone is not enough -- a bag
--- opened mid-command puts items in the same inventory -- and materials falling
--- alone is not enough either. A recipe that consumes nothing this walk can see
--- therefore fails here, with the counts in the failure, rather than being
--- claimed on the strength of an item that appeared.
local function craftVerify(_, _, after, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec = Toolkit.state(ctx).craft
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no recipe"
  end
  local snapshot = type(after) == "table" and after or Toolkit.observe(ctx.player)
  local crafted = Crafting.countType(snapshot, spec.product) - spec.product_before
  local consumed = {}
  local spent = 0
  for index = 1, #spec.inputs do
    local input = spec.inputs[index]
    local held = 0
    for typeIndex = 1, #input.types do
      held = held + Crafting.countType(snapshot, input.types[typeIndex])
    end
    local fell = (spec.materials_before[index] or 0) - held
    if fell > 0 then
      spent = spent + fell
      consumed[#consumed + 1] = {
        types = input.types,
        before = spec.materials_before[index],
        after = held,
      }
    end
  end
  if crafted < spec.count then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format(
        "%s was queued %d time(s) and the inventory holds %d more %s, not %d",
        spec.name,
        spec.count,
        crafted,
        spec.product,
        spec.count
      )
  end
  if spent == 0 then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format(
        "%d more %s appeared but nothing the recipe consumes left the inventory, "
          .. "so this was not observed to be the craft",
        crafted,
        spec.product
      )
  end
  return {
    kind = "crafted",
    recipe = spec.token,
    display_name = spec.name,
    product = spec.product,
    count_requested = spec.count,
    count_crafted = crafted,
    product_before = spec.product_before,
    product_after = spec.product_before + crafted,
    materials_consumed = consumed,
  }
end

Craft = toolkit().declare({
  name = "crafting.craft",
  capability = toolkit().CAPABILITY.CRAFTING,
  experimental = true,
  -- The craft classes themselves are a probed candidate list rather than a
  -- requirement, because the two spellings are the uncertainty: what this
  -- action cannot do without is a queue to put one on.
  requires = { "ISTimedActionQueue.add" },
  -- The declared timeout IS the window: the runtime's lease closes the command
  -- on the same clock the progress step watches.
  timeout_ms = Crafting.CRAFT_WINDOW_MS,
  poll_interval_ms = Crafting.POLL_MS,
  args = {
    recipe = { type = ARG.STRING, required = true, max_bytes = 64 },
    -- Whole items, with a small ceiling: this is what the character is
    -- committed to in one window, and the materials for all of it are checked
    -- before any of it is queued.
    count = {
      type = ARG.NUMBER,
      integer = true,
      min = Crafting.MIN_COUNT,
      max = Crafting.MAX_COUNT,
      default = Crafting.DEFAULT_COUNT,
    },
  },
  validate = craftValidate,
  begin = craftBegin,
  progress = craftProgress,
  verify = craftVerify,
})

Crafting.Craft = Craft
toolkit().register(Craft)

return Crafting
