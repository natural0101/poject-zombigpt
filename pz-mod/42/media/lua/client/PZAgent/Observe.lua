--[[
PZAgent.Observe -- the engine-coupled half of the observation.

Invariant: this file reads the game and converts what it read into the plain
Lua values PZAgent.ObserveModel shapes. It decides nothing. Every accessor is
probed before it is called and every call crosses into the engine inside a
pcall, so a build that names a method differently costs one field rather than
the whole snapshot -- and the field it costs is *absent*, never defaulted.
That asymmetry is the point: `PlayerState.hunger` on the sidecar reads a
missing hunger as "not hungry", so a stat this file could not read must not
arrive as a number.

Everything walked here is something the player controls the size of -- the
inventory of a hoarder's base, a horde, a warehouse full of shelves -- so every
loop has a bound, and reaching one is reported to ObserveModel rather than
absorbed.

Nothing here writes to the game. Nothing here emits chat, radio or book text:
display names are carried because the planner and the player both need to know
what an item is called, and they are inert data at every layer above.
]]

PZAgent = PZAgent or {}

local Observe = {}
PZAgent.Observe = Observe

--- Squares scanned around the player, in each direction. The scan is
--- (2R+1)^2 grid lookups on the game thread, so the radius is small and the
--- square budget below caps it again.
Observe.RADIUS = 6

Observe.MAX_SQUARES = 256
Observe.MAX_OBJECTS_PER_SQUARE = 16
Observe.MAX_ZOMBIE_SCAN = 256

--- Dead bodies inspected per square. A massacre site can stack dozens of
--- corpses on one tile; past this many, the rest are counted as dropped
--- rather than silently unread.
Observe.MAX_BODIES_PER_SQUARE = 8

--- Objects one nearby scan may read out of the engine, shared across every
--- square it visits.
---
--- The per-square cap does not bound the walk, for the same reason the
--- per-container cap does not bound the inventory: the number of squares is the
--- other factor, and (2R+1)^2 of them at MAX_OBJECTS_PER_SQUARE is thousands of
--- descriptors for a document that keeps ObserveModel.MAX_OBJECTS of them. The
--- rest is engine work on the game thread that the model's sort throws away a
--- moment later, four times a second.
---
--- Four times the document's cap rather than exactly it: the model discards an
--- object whose kind is not a reference-safe token, so a budget of exactly
--- MAX_OBJECTS could leave the snapshot short of what it had room for. Spending
--- it nearest-square-first is what makes the bound safe -- see nearbyObjects.
Observe.MAX_OBJECTS_SCANNED = 256

--- Squares described one by one, out from the character in each direction, and
--- the ceiling on how many of those descriptions travel.
---
--- Deliberately smaller than Observe.RADIUS. This reading publishes one entry
--- per SQUARE rather than per object, so the object radius would put
--- (2*6+1)^2 = 169 of them beside the objects in the same array. Three squares
--- out is a seven-by-seven block, which is every square a build can be reached
--- from plus the room to show a way out of it -- and it is the whole of what
--- the sidecar's trap check can see, which is why that check only ever claims
--- that a placement does not remove the last exit *inside this window*.
---
--- The cap is exactly the window, so reaching it means the window was widened
--- without widening the cap rather than a square going unread by accident.
Observe.DESCRIBE_RADIUS = 3
Observe.MAX_DESCRIBED_SQUARES = 49

--- Worn slots inspected. Build 42 gives a fully dressed character rather more
--- than a dozen body locations, so this sits above what a player can wear
--- rather than in the middle of it -- a backpack dropped for being the
--- seventeenth worn item is a bag the agent would simply never see.
Observe.MAX_WORN = 32
Observe.MAX_BODY_PARTS = 24

--- Known recipes read out of the engine per tick, and entries published from
--- them. Both are deliberately small. The tick runs on the game thread four
--- times a second, and the published entries ride `player.stats` -- the one
--- open scalar map in schemas/observation.schema.json, which has no recipe
--- tier at all -- so twelve of them must never crowd the character's real
--- stats out of ObserveModel.MAX_STATS.
Observe.MAX_RECIPE_SCAN = 24
Observe.MAX_RECIPES = 12

--- Ingredient entries read off one recipe, and recipes read off one book.
Observe.MAX_RECIPE_INPUTS = 8
Observe.MAX_TAUGHT_RECIPES = 32

--- Recipe entries stamped onto one carried item. The readout rides the items
--- it consumes -- see attachRecipes -- so this is what bounds one item's share
--- of it.
Observe.MAX_RECIPES_PER_ITEM = 8

--- Entries walked when the known-recipe collection exposes no `contains`.
--- Past this the set is not materialised at all and every question about it
--- answers "could not tell" rather than a count off half a list.
Observe.MAX_KNOWN_WALK = 256

--- Items counted when tallying what the character carries for a recipe. The
--- tally is built from the walk the tick already did, so this only guards
--- against a malformed node tree, never against the engine.
Observe.MAX_TALLY_ITEMS = 512

--- Health scales the game reports out of 100.
local PERCENT = 100

local function model()
  return PZAgent.ObserveModel
end

-- ---------------------------------------------------------------------------
-- probing
--
-- Kahlua exposes Java objects whose members may be absent, and indexing some of
-- them can itself raise. Every lookup and every call therefore goes through
-- these two helpers.
-- ---------------------------------------------------------------------------

local function member(owner, name)
  if owner == nil then
    return nil
  end
  local ok, value = pcall(function()
    return owner[name]
  end)
  if not ok or type(value) ~= "function" then
    return nil
  end
  return value
end

--- Call `owner:name(...)`. Returns the value, or nil plus a reason.
local function invoke(owner, name, ...)
  local method = member(owner, name)
  if method == nil then
    return nil, string.format("%s is not available in this build", tostring(name))
  end
  local ok, result = pcall(method, owner, ...)
  if not ok then
    return nil, string.format("%s failed: %s", tostring(name), tostring(result))
  end
  return result
end

Observe.invoke = invoke

--- The first of `names` that exists and returns a value of `wanted` type.
---
--- Several of the accessors below are spelled differently across builds and
--- across the classes that implement them. Trying a short, closed list is what
--- keeps a rename costing one field instead of the section it belongs to.
local function firstOf(owner, names, wanted)
  for index = 1, #names do
    local value = invoke(owner, names[index])
    if type(value) == wanted then
      return value, names[index]
    end
  end
  return nil
end

local function readNumber(owner, names)
  return firstOf(owner, names, "number")
end

local function readBoolean(owner, names)
  return firstOf(owner, names, "boolean")
end

local function readString(owner, names)
  return firstOf(owner, names, "string")
end

--- The first accessor that answers with a non-negative whole number.
---
--- Identity is read through this rather than through readNumber because Java
--- answers -1 for "there is no id here" -- `getOnlineID` does exactly that
--- outside multiplayer. Taking that -1 would give every zombie in the horde the
--- same reference, so the probe falls through to the next accessor instead.
local function readIdentity(owner, names)
  for index = 1, #names do
    local value = invoke(owner, names[index])
    if type(value) == "number" and value == value and value >= 0 and value < math.huge then
      return math.floor(value)
    end
  end
  return nil
end

--- The size of a Java collection, or nil when it is not one.
local function listSize(list)
  local size = invoke(list, "size")
  if type(size) ~= "number" then
    return nil
  end
  return math.floor(size)
end

--- Entry `index` of a Java collection, which is zero-based.
local function listGet(list, index)
  return (invoke(list, "get", index))
end

--- The first of `names` that answers with something list-shaped, plus the name
--- that worked.
---
--- `firstOf` cannot serve here: Kahlua hands a Java collection over as a value
--- whose Lua `type` is not something this side should assert on, so "is it a
--- list" is asked the only way that means anything -- by asking it its size.
local function firstList(owner, names)
  for index = 1, #names do
    local value = invoke(owner, names[index])
    if value ~= nil and listSize(value) ~= nil then
      return value, names[index]
    end
  end
  return nil
end

--- The name of an enum-like Java object, as a token.
local function enumName(value)
  if type(value) == "string" then
    return value
  end
  return (readString(value, { "name", "toString", "getName" }))
end

-- ---------------------------------------------------------------------------
-- game
-- ---------------------------------------------------------------------------

--- The string that identifies this save, before hashing.
---
--- Never emitted: ObserveModel hashes it, because the raw value is a directory
--- fragment that can carry the profile name (§3.13).
function Observe.saveKey()
  if type(getWorld) ~= "function" then
    return nil
  end
  local ok, world = pcall(getWorld)
  if not ok or world == nil then
    return nil
  end
  local parts = {}
  local count = 0
  for _, name in ipairs({ "getWorld", "getMap", "getSaveFolderName" }) do
    local value = invoke(world, name)
    if type(value) == "string" and #value > 0 then
      count = count + 1
      parts[count] = value
    end
  end
  if count == 0 then
    return nil
  end
  return table.concat(parts, "/")
end

--- Everything the game block needs, as plain values.
---
--- Returns the fields plus the reason the build could not be read, if it could
--- not: ObserveModel refuses an observation with no build, and "the game build
--- is unknown" is a far worse thing to put on the HUD than the probe's own
--- account of which accessor was missing.
function Observe.gameFields()
  local build, buildError = PZAgent.Heartbeat.detectBuild()
  local fields = {
    build = build,
    save_key = Observe.saveKey(),
  }
  if type(getGameTime) == "function" then
    local ok, gameTime = pcall(getGameTime)
    if ok and gameTime ~= nil then
      fields.speed = readNumber(gameTime, { "getTrueMultiplier", "getMultiplier" })
      fields.world_time = model().worldTime({
        year = readNumber(gameTime, { "getYear" }),
        month = readNumber(gameTime, { "getMonth" }),
        day = readNumber(gameTime, { "getDay" }),
        hour = readNumber(gameTime, { "getHour" }),
        minute = readNumber(gameTime, { "getMinutes" }),
      })
      local paused = readBoolean(gameTime, { "isPaused" })
      if paused ~= nil then
        fields.paused = paused
      end
    end
  end
  if fields.paused == nil and fields.speed ~= nil then
    -- The speed control is the only reading of "is time running" this build is
    -- known to expose; zero multiplier is the game's own definition of paused.
    fields.paused = fields.speed <= 0
  end
  fields.multiplayer = Observe.multiplayer()
  return fields, buildError
end

--- Is this a multiplayer session? true, false, or nil when it cannot be told.
---
--- The three values are all meaningful and the sidecar treats them as such: it
--- arms only on a positive `false`, and refuses on nil exactly as it refuses on
--- true. So this function must never answer false to mean "no reading" -- that
--- would turn an unknown into a permission.
---
--- `isClient` is true on a multiplayer client and `isServer` on a host; a
--- single-player session is neither. Both are vanilla globals rather than
--- methods on an object, so they are read directly rather than through
--- readBoolean.
function Observe.multiplayer()
  local seen = false
  for _, name in ipairs({ "isClient", "isServer" }) do
    local fn = _G[name]
    if type(fn) == "function" then
      local ok, value = pcall(fn)
      if ok and type(value) == "boolean" then
        seen = true
        if value then
          return true
        end
      end
    end
  end
  if not seen then
    -- Neither accessor answered. Say so, rather than reporting single player on
    -- the strength of two functions that were not there.
    return nil
  end
  return false
end

-- ---------------------------------------------------------------------------
-- player
-- ---------------------------------------------------------------------------

local function positionOf(object)
  local x = readNumber(object, { "getX" })
  local y = readNumber(object, { "getY" })
  local z = readNumber(object, { "getZ" })
  if x == nil or y == nil or z == nil then
    return nil
  end
  return { x = x, y = y, z = math.floor(z) }
end

Observe.positionOf = positionOf

--- The room and building of a square, as raw strings, or nils.
---
--- The caller hands the square in and calls this ONCE per square: the nearby
--- scan shares the answer across every object on the square, because asking
--- the engine the same question MAX_OBJECTS_PER_SQUARE times over is pure
--- game-thread work for one reading.
---
--- A nil room and an absent room reader produce the same pair of nils on
--- purpose. Outdoors has no room; a build with no reader has no reading; and a
--- scope decision downstream must not mistake the second for "outside", so
--- neither is distinguishable here and both simply emit no field. The building
--- identifier is the numeric id where the build exposes one -- formatted as a
--- digit string -- else the name on the building's definition; the raw strings
--- go to ObserveModel.place, which normalises or drops them under the token
--- rules.
function Observe.placeOf(square)
  local room = invoke(square, "getRoom")
  if room == nil then
    return nil, nil
  end
  local name = readString(room, { "getName" })
  if name == nil then
    -- Build 42 also spells the room's name on its definition object.
    local roomDef = invoke(room, "getRoomDef")
    name = readString(roomDef, { "getName" })
  end
  local buildingId = nil
  local building = invoke(room, "getBuilding")
  if building ~= nil then
    local id = readIdentity(building, { "getID" })
    if id ~= nil then
      buildingId = string.format("%.0f", id)
    else
      local buildingDef = invoke(building, "getDef")
      buildingId = readString(buildingDef, { "getName" })
    end
  end
  return name, buildingId
end

--- Character stats. A name whose accessor this build does not expose is left
--- out of the table entirely, never set to a plausible number.
function Observe.playerStats(player)
  local stats = {}
  local body = invoke(player, "getBodyDamage")
  local health = readNumber(body, { "getOverallBodyHealth" })
  if health ~= nil then
    stats.health = health / PERCENT
  end
  local raw = invoke(player, "getStats")
  if raw ~= nil then
    stats.endurance = readNumber(raw, { "getEndurance" })
    stats.hunger = readNumber(raw, { "getHunger" })
    stats.thirst = readNumber(raw, { "getThirst" })
    stats.fatigue = readNumber(raw, { "getFatigue" })
    stats.stress = readNumber(raw, { "getStress" })
    stats.panic = readNumber(raw, { "getPanic" })
  end
  -- The equipped weapon's wear, carried under the open stats map because the
  -- item tier has no condition field in the schema. Read off the primary hand
  -- item only -- the one combat.engage would swing -- and absent whenever the
  -- hand is empty or this build does not expose the reader: a fabricated
  -- condition would let the deterministic combat policy clear a weapon nobody
  -- measured.
  local primary = invoke(player, "getPrimaryHandItem")
  if primary ~= nil then
    local condition = readNumber(primary, { "getCondition" })
    if condition ~= nil then
      stats.weapon_condition = condition
    end
    local conditionMax = readNumber(primary, { "getConditionMax" })
    if conditionMax ~= nil then
      stats.weapon_condition_max = conditionMax
    end
  end
  return stats
end

--- The zombie's body state as a token, or nil when no reader answered.
---
--- Tri-state on purpose: "prone" when a floor reader answered true, "crawling"
--- when the crawl reader did, and "standing" ONLY when at least one reader
--- answered at all. A build with none of the readers yields nil and the field
--- stays absent -- absent must never read as standing, because the combat
--- postcondition treats "prone" as the observed success and a defaulted
--- "standing" would turn "could not be read" into "the shove did nothing".
function Observe.zombieState(zombie)
  local prone = readBoolean(zombie, { "isOnFloor", "isProne" })
  local crawling = readBoolean(zombie, { "isCrawling" })
  if prone == nil and crawling == nil then
    return nil
  end
  if prone == true then
    return "prone"
  end
  if crawling == true then
    return "crawling"
  end
  return "standing"
end

--- Active moodles, keyed by their type name.
---
--- A moodle whose type name cannot be read is skipped: an unnamed level is not
--- something the sidecar can key a decision on, and inventing a name would put
--- a made-up moodle into the planner's view.
function Observe.playerMoodles(player)
  local moodles = {}
  local holder = invoke(player, "getMoodles")
  if holder == nil then
    return moodles
  end
  local count = readNumber(holder, { "getNumMoodles" })
  if count == nil then
    return moodles
  end
  local scanned = math.min(math.floor(count), model().MAX_MOODLES)
  for index = 0, scanned - 1 do
    local level = invoke(holder, "getMoodleLevel", index)
    local name = enumName(invoke(holder, "getMoodleType", index))
    if type(level) == "number" and name ~= nil then
      moodles[name] = level
    end
  end
  return moodles
end

--- One body part, as the flags ObserveModel turns into a wound.
local function bodyPartFields(part, index)
  local name = enumName(invoke(part, "getType")) or string.format("part-%d", index)
  local health = readNumber(part, { "getHealth" })
  local fracture = readNumber(part, { "getFractureTime" })
  local burn = readNumber(part, { "getBurnTime" })
  return {
    part = name,
    bleeding = readBoolean(part, { "bleeding", "isBleeding" }) == true,
    bitten = readBoolean(part, { "bitten", "isBitten" }) == true,
    scratched = readBoolean(part, { "scratched", "isScratched" }) == true,
    cut = readBoolean(part, { "isCut", "haveGlass" }) == true,
    deep_wounded = readBoolean(part, { "isDeepWounded", "deepWounded" }) == true,
    fractured = (fracture ~= nil and fracture > 0) or readBoolean(part, { "isFractured" }) == true,
    burnt = (burn ~= nil and burn > 0) or readBoolean(part, { "isBurnt" }) == true,
    severity = health ~= nil and (PERCENT - health) / PERCENT or nil,
  }
end

--- Every body part, bounded, or nil plus a reason when none could be read.
---
--- The nil is the point: an empty list means "every part was inspected and none
--- of them is hurt", which is a reading the sidecar acts on -- it is what makes
--- `bleeding_observed == 0` a postcondition. A build that exposes no body
--- damage at all must not produce that same empty list, so it produces no list.
function Observe.playerWounds(player)
  local wounds = {}
  local count = 0
  local body = invoke(player, "getBodyDamage")
  local parts = invoke(body, "getBodyParts")
  local size = listSize(parts)
  if size == nil then
    return nil, "the character's body parts could not be read"
  end
  local scanned = math.min(size, Observe.MAX_BODY_PARTS)
  for index = 0, scanned - 1 do
    local part = listGet(parts, index)
    if part ~= nil then
      count = count + 1
      wounds[count] = bodyPartFields(part, index)
    end
  end
  return wounds
end

--- The player block's plain values. `alive` is read, not assumed.
function Observe.playerFields(player)
  local position = positionOf(player)
  if position == nil then
    return nil, "the player reports no position"
  end
  local angle = readNumber(player, { "getDirectionAngle", "getForwardDirection" })
  position.direction = model().direction(angle)
  local dead = readBoolean(player, { "isDead" })
  local square = invoke(player, "getCurrentSquare")
  -- Both nil outdoors, and both nil when the reader is missing: the fields
  -- stay absent either way, never defaulted -- see Observe.placeOf.
  local room, building = Observe.placeOf(square)
  return {
    present = true,
    -- Unknown liveness reads as dead, which suspends mutating work rather than
    -- letting the agent act on a character it cannot confirm is alive.
    alive = dead == false,
    position = position,
    stats = Observe.playerStats(player),
    moodles = Observe.playerMoodles(player),
    wounds = Observe.playerWounds(player),
    room = room,
    building = building,
  }
end

-- ---------------------------------------------------------------------------
-- recipes
--
-- Every engine symbol in this section is UNVERIFIED against Build 42. The
-- crafting system was rewritten for that build and none of these spellings has
-- been seen answering in a live session, so each is probed through a short
-- closed list, each call is guarded, and a reader that does not answer costs
-- the field rather than producing a number. "This character knows no recipes"
-- and "this build would not say" are opposite facts and only one of them is
-- safe to plan against. The candidate spellings are:
--
--   IsoGameCharacter.getKnownRecipes   the character's known-recipe collection
--   InventoryItem.getTeachedRecipes    what a book would teach
--   CraftRecipe.getInputs / getSource  what a recipe consumes
--   InputScript.getItems / getCount    one ingredient and how much of it
--   getScriptManager().getCraftRecipe / getRecipe   name -> recipe
--
-- docs/GAME_API_VERIFICATION.md is where they belong as rows; nothing here
-- claims any of them exists.
-- ---------------------------------------------------------------------------

--- A recipe's name, whether the collection held strings or recipe objects.
local function recipeNameOf(entry)
  if type(entry) == "string" then
    return entry
  end
  return (readString(entry, { "getName", "getOriginalname" }))
end

Observe.recipeNameOf = recipeNameOf

--- How to ask whether the character already knows a recipe, by name.
---
--- Returns a query `name -> true | false | nil`, or nil when this build exposes
--- no known-recipe reader at all. The inner nil is the third state and it is
--- load-bearing: a collection that answered neither `contains` nor a bounded
--- walk cannot rule a recipe out, and reading that as "not known" would make
--- every magazine in the game look unread.
function Observe.knownRecipeQuery(player)
  local known = invoke(player, "getKnownRecipes")
  if known == nil then
    return nil
  end
  if member(known, "contains") ~= nil then
    return function(name)
      local answer = invoke(known, "contains", name)
      if type(answer) == "boolean" then
        return answer
      end
      return nil
    end
  end
  -- No membership test, so the set is materialised once -- bounded, and only
  -- when the whole of it fits inside the bound. Half a set answers "not known"
  -- for everything it did not reach, which is the fabrication this avoids.
  local size = listSize(known)
  if size == nil or size > Observe.MAX_KNOWN_WALK then
    return nil
  end
  local names = {}
  for index = 0, size - 1 do
    local name = recipeNameOf(listGet(known, index))
    if name ~= nil then
      names[name] = true
    end
  end
  return function(name)
    return names[name] == true
  end
end

--- The known recipe names, in engine order, plus how many the engine reported.
---
--- Returns nil when the collection could not be enumerated -- a `contains`-only
--- collection can answer questions but cannot be listed, and listing it from
--- nothing is not an option this file has.
function Observe.knownRecipeNames(player)
  local known = invoke(player, "getKnownRecipes")
  local size = listSize(known)
  if size == nil then
    return nil
  end
  local scanned = math.min(size, Observe.MAX_RECIPE_SCAN)
  local names = {}
  local count = 0
  for index = 0, scanned - 1 do
    local entry = listGet(known, index)
    local name = recipeNameOf(entry)
    if name ~= nil then
      count = count + 1
      names[count] = { name = name, recipe = type(entry) ~= "string" and entry or nil }
    end
  end
  return names, size, size > scanned
end

--- How many recipes this item would teach that the character does not know.
---
--- nil, never 0, whenever the count cannot be established: the taught list is
--- unreadable, it runs past the bound, or the known-recipe query could not
--- answer. `pz_agent_core.policy.literature` picks a recipe magazine on exactly
--- this field -- a fabricated 0 refuses every magazine in the game, and a
--- fabricated count sends the character off to read one it has already learned.
local function unreadRecipes(item, isKnown)
  if isKnown == nil then
    return nil
  end
  local taught = firstList(item, { "getTeachedRecipes" })
  local size = listSize(taught)
  if size == nil or size > Observe.MAX_TAUGHT_RECIPES then
    return nil
  end
  local unread = 0
  for index = 0, size - 1 do
    local name = recipeNameOf(listGet(taught, index))
    if name == nil then
      return nil
    end
    local known = isKnown(name)
    if known == nil then
      return nil
    end
    if not known then
      unread = unread + 1
    end
  end
  return unread
end

Observe.unreadRecipes = unreadRecipes

--- The recipe object behind a known-recipe entry, or nil.
---
--- An entry that is already a recipe object is used as it stands; a name is
--- looked up through the script manager, which is reached through `_G` rather
--- than as a bare global because an unverified symbol must not become one this
--- file depends on being present.
local function recipeObject(entry)
  if entry.recipe ~= nil then
    return entry.recipe
  end
  local accessor = _G["getScriptManager"]
  if type(accessor) ~= "function" then
    return nil
  end
  local ok, manager = pcall(accessor)
  if not ok or manager == nil then
    return nil
  end
  for _, name in ipairs({ "getCraftRecipe", "getRecipe" }) do
    local recipe = invoke(manager, name, entry.name)
    if recipe ~= nil then
      return recipe
    end
  end
  return nil
end

--- What one recipe consumes: `{ { types = {...}, count = n }, ... }`, or nil
--- when this build would not say.
---
--- A recipe whose ingredient list is only partly readable answers nil as a
--- whole. A materials verdict computed from the ingredients that happened to
--- answer would call a recipe ready on the strength of the requirements nobody
--- read, which is the one answer that gets materials destroyed for nothing.
local function recipeInputs(recipe)
  local list = firstList(recipe, { "getInputs", "getSource" })
  local size = listSize(list)
  if size == nil then
    return nil
  end
  local scanned = math.min(size, Observe.MAX_RECIPE_INPUTS)
  if size > scanned then
    return nil
  end
  local inputs = {}
  for index = 0, scanned - 1 do
    local entry = listGet(list, index)
    local types = firstList(entry, { "getItems", "getItemTypes" })
    local typeCount = listSize(types)
    if typeCount == nil or typeCount == 0 then
      return nil
    end
    local names = {}
    for typeIndex = 0, math.min(typeCount, Observe.MAX_RECIPE_INPUTS) - 1 do
      local name = listGet(types, typeIndex)
      if type(name) ~= "string" then
        name = readString(name, { "getFullType", "getName" })
      end
      if name == nil then
        return nil
      end
      names[#names + 1] = name
    end
    inputs[#inputs + 1] = { types = names, count = readNumber(entry, { "getCount", "getAmount" }) or 1 }
  end
  return inputs
end

Observe.recipeInputs = recipeInputs

--- Whether the recipe needs something the character has to stand at.
---
--- Tri-state, and the third state is the point: `false` is only ever returned
--- when a reader actually answered that there is no such requirement. The
--- sidecar escalates the permission tier on anything that is not a positive
--- `false`, so an absent reader must stay absent -- it buys one tier of
--- caution, while a fabricated `false` buys a craft queued where it cannot run.
function Observe.recipeNeedsSurface(recipe)
  if member(recipe, "getNearItem") == nil then
    return nil
  end
  local near = invoke(recipe, "getNearItem")
  if type(near) == "string" then
    return #near > 0
  end
  if near == nil then
    return false
  end
  return nil
end

--- What one recipe produces, as an item type, or nil when it cannot be read.
function Observe.recipeProduct(recipe)
  local outputs = firstList(recipe, { "getOutputs" })
  local size = listSize(outputs)
  if size ~= nil and size > 0 then
    local first = listGet(outputs, 0)
    local types = firstList(first, { "getItems", "getItemTypes" })
    if listSize(types) ~= nil and listSize(types) > 0 then
      local name = listGet(types, 0)
      if type(name) == "string" then
        return name
      end
      return (readString(name, { "getFullType", "getName" }))
    end
    local direct = readString(first, { "getFullType", "getName" })
    if direct ~= nil then
      return direct
    end
  end
  local result = invoke(recipe, "getResult")
  if result == nil then
    return nil
  end
  return (readString(result, { "getFullType", "getType", "getName" }))
end

-- ---------------------------------------------------------------------------
-- inventory
-- ---------------------------------------------------------------------------

--- The domain payloads a policy needs, or nil when the item carries none.
local function itemFood(item)
  local hunger = readNumber(item, { "getHungerChange" })
  if hunger == nil then
    return nil
  end
  return {
    hunger_change = hunger,
    thirst_change = readNumber(item, { "getThirstChange" }),
    calories = readNumber(item, { "getCalories" }),
    uses_remaining = readNumber(item, { "getDrainableUsesInt" }),
    cooked = readBoolean(item, { "isCooked" }),
    rotten = readBoolean(item, { "isRotten" }),
    burnt = readBoolean(item, { "isBurnt" }),
    poisonous = readBoolean(item, { "isPoison", "isTaintedWater" }),
  }
end

--- The literature payload, spelled the way the sidecar reads it.
---
--- The key names here are Python's, not the engine's, and that is deliberate:
--- `pz_agent_core.policy.literature.LiteratureView.from_item` reads
--- `pages_total`, `min_level` and `max_level`, while this reader emitted
--- `pages`, `skill_level_min` and `skill_level_max` -- three keys that never
--- met, so every level window on this side arrived as the Python default on
--- that side. Worse, it never emitted `unread_recipes` at all, which is the one
--- field the learn_recipe goal selects on, so that goal rejected every recipe
--- magazine in the game and was dead end to end. Renaming here rather than
--- there is what keeps one spelling on the wire; the drift is named in this
--- comment so the next reader knows why the Lua keys are the Python ones.
---
--- `unread_recipes` stays absent whenever it could not be counted -- see
--- unreadRecipes. Absent is not zero: the sidecar reads a missing count as
--- "no unread recipes" and refuses the book, which is the safe direction to be
--- wrong in, and a fabricated count is not.
local function itemLiterature(item, isKnown)
  local pages = readNumber(item, { "getNumberOfPages" })
  if pages == nil then
    return nil
  end
  return {
    pages_total = pages,
    pages_read = readNumber(item, { "getAlreadyReadPages" }),
    skill = readString(item, { "getSkillTrained" }),
    min_level = readNumber(item, { "getLvlSkillTrained" }),
    max_level = readNumber(item, { "getMaxLevelTrained" }),
    unread_recipes = unreadRecipes(item, isKnown),
  }
end

local function itemFluid(item)
  local container = invoke(item, "getFluidContainer")
  if container == nil then
    return nil
  end
  return {
    amount = readNumber(container, { "getAmount" }),
    capacity = readNumber(container, { "getCapacity" }),
    tainted = readBoolean(container, { "isTainted" }),
  }
end

--- One item, as the descriptor ObserveModel consumes.
---
--- `context` carries what the walk needs but the item cannot answer for itself:
--- which items are in the character's hands, and how to ask whether a recipe is
--- already known. Both are read once per tick and shared, because asking the
--- character the same question once per item is pure game-thread work.
local function itemFields(item, context)
  local hands = context.hands
  local descriptor = {
    runtime_id = readIdentity(item, { "getID" }),
    full_type = readString(item, { "getFullType" }),
    display_name = readString(item, { "getName", "getDisplayName" }),
    category = readString(item, { "getDisplayCategory", "getCategory" }),
    weight = readNumber(item, { "getUnequippedWeight", "getActualWeight", "getWeight" }),
    favorite = readBoolean(item, { "isFavorite" }) == true,
    equipped = readBoolean(item, { "isEquipped" }) == true,
    food = itemFood(item),
    literature = itemLiterature(item, context.is_known),
    fluid = itemFluid(item),
  }
  if hands.primary ~= nil and rawequal(hands.primary, item) then
    descriptor.hand = "primary"
    descriptor.equipped = true
  elseif hands.secondary ~= nil and rawequal(hands.secondary, item) then
    descriptor.hand = "secondary"
    descriptor.equipped = true
  end
  return descriptor
end

local walkItemContainer

--- The budget one inventory walk shares across every container it opens.
---
--- The per-container cap alone does not bound the walk: bags nest, so
--- MAX_ITEMS_PER_CONTAINER items each holding a bag is that many containers
--- again at the next depth, and MAX_DEPTH of that is millions of engine calls
--- on the game thread for a document that keeps sixty-four containers. The
--- totals are the model's own caps, because a container or an item the model
--- would refuse anyway is not worth reading out of the engine.
local function newWalkBudget()
  return {
    containers = model().MAX_CONTAINERS,
    items = model().MAX_ITEMS,
    containers_dropped = 0,
  }
end

--- Read one ItemContainer into a container node, recursing into carried bags.
---
--- `depth` is bounded here as well as in ObserveModel: the model refuses a node
--- that is too deep, but the walk that produced it would already have run. The
--- same reasoning is why `budget` exists -- see newWalkBudget.
walkItemContainer = function(container, node, context, depth, budget)
  local items = invoke(container, "getItems")
  local size = listSize(items)
  node.capacity = readNumber(container, { "getCapacity", "getMaxWeight" })
  node.used_capacity = readNumber(container, { "getContentsWeight", "getCapacityWeight" })
  node.items = {}
  if size == nil then
    return node
  end
  local limit = model().MAX_ITEMS_PER_CONTAINER
  local scanned = math.min(size, limit)
  local count = 0
  -- Where the shared item budget ran out, if it did. Everything from there to
  -- the end of the engine's list was never looked at, and saying so is what
  -- keeps "the bag holds four things" off a bag whose fifth was never read.
  local stopped = nil
  for index = 0, scanned - 1 do
    if budget.items <= 0 then
      stopped = index
      break
    end
    local item = listGet(items, index)
    if item ~= nil then
      budget.items = budget.items - 1
      local descriptor = itemFields(item, context)
      count = count + 1
      node.items[count] = descriptor
      local nested = invoke(item, "getInventory")
      if nested ~= nil and depth < model().MAX_DEPTH then
        if budget.containers <= 0 then
          -- The bag is reported as an item; its contents are not reported at
          -- all, which is a container the sidecar must be told about.
          budget.containers_dropped = budget.containers_dropped + 1
        else
          budget.containers = budget.containers - 1
          descriptor.container = walkItemContainer(nested, {
            kind = model().CONTAINER_KIND.CARRIED,
            runtime_id = descriptor.runtime_id,
            name = descriptor.display_name,
            accessible = true,
          }, context, depth + 1, budget)
        end
      end
    end
  end
  local unread = size - scanned
  if stopped ~= nil then
    unread = size - stopped
  end
  if unread > 0 then
    node.truncated = true
    node.dropped = unread
  end
  return node
end

--- The container roots: the main inventory first, then everything worn.
---
--- Worn and carried stay distinct kinds. A backpack on the back is reachable
--- without unequipping anything; a backpack lying in the main inventory is a
--- different proposition for a transfer, and collapsing the two would make the
--- reference for one resolve to the other.
function Observe.inventoryRoots(player)
  local roots = { containers_dropped = 0 }
  local budget = newWalkBudget()
  -- Read once and shared across every item the walk visits: both questions
  -- are about the character, not the item, and asking them per item is the
  -- same answer bought several hundred times on the game thread.
  local context = {
    hands = {
      primary = invoke(player, "getPrimaryHandItem"),
      secondary = invoke(player, "getSecondaryHandItem"),
    },
    is_known = Observe.knownRecipeQuery(player),
  }
  local main = invoke(player, "getInventory")
  if main == nil then
    return nil, "the character exposes no main inventory"
  end
  budget.containers = budget.containers - 1
  roots[1] = walkItemContainer(main, {
    kind = model().CONTAINER_KIND.PLAYER_MAIN,
    name = "Inventory",
    accessible = true,
  }, context, 1, budget)

  local worn = invoke(player, "getWornItems")
  local size = listSize(worn)
  if size ~= nil then
    local scanned = math.min(size, Observe.MAX_WORN)
    for index = 0, scanned - 1 do
      local entry = listGet(worn, index)
      local item = invoke(entry, "getItem") or entry
      local slot = readString(entry, { "getLocation", "getBodyLocation" })
      local container = invoke(item, "getInventory")
      if container ~= nil then
        if slot == nil or budget.containers <= 0 then
          -- A worn bag whose slot this build does not name has no reference the
          -- sidecar could resolve, so it cannot be emitted -- but it is a bag
          -- the player is wearing, and a snapshot that simply omitted it would
          -- read as a character carrying nothing on their back.
          budget.containers_dropped = budget.containers_dropped + 1
        else
          budget.containers = budget.containers - 1
          roots[#roots + 1] = walkItemContainer(container, {
            kind = model().CONTAINER_KIND.WORN,
            slot = slot,
            runtime_id = readIdentity(item, { "getID" }),
            name = readString(item, { "getName", "getDisplayName" }),
            accessible = true,
          }, context, 1, budget)
        end
      end
    end
    if size > scanned then
      -- Worn slots past the cap were never inspected; any of them could have
      -- been a bag, so the count travels rather than the assumption.
      budget.containers_dropped = budget.containers_dropped + (size - scanned)
    end
  end
  roots.containers_dropped = budget.containers_dropped
  return roots
end

-- ---------------------------------------------------------------------------
-- crafting
-- ---------------------------------------------------------------------------

--- What the character carries, tallied by item type.
---
--- Built from the container nodes this tick already walked rather than from a
--- second pass over the engine: it costs no engine call at all, and it makes
--- the materials verdict agree with the inventory the same document publishes.
--- A recipe called ready against items the snapshot does not show would be
--- unexplainable to anyone reading the two side by side.
---
--- Each type is counted under its full name and under the part after the last
--- dot, because a recipe names an ingredient either way -- "Base.Nails" and
--- "Nails" are the same nail -- and neither side of that is worth guessing at
--- match time.
local function carriedTally(roots)
  local tally = {}
  if type(roots) ~= "table" then
    return nil
  end
  local budget = Observe.MAX_TALLY_ITEMS

  local function count(name)
    if type(name) ~= "string" or #name == 0 then
      return
    end
    tally[name] = (tally[name] or 0) + 1
    local tail = name:match("([^%.]+)$")
    if tail ~= nil and tail ~= name then
      tally[tail] = (tally[tail] or 0) + 1
    end
  end

  local function walk(node)
    if type(node) ~= "table" or type(node.items) ~= "table" then
      return
    end
    for index = 1, #node.items do
      if budget <= 0 then
        return
      end
      budget = budget - 1
      local descriptor = node.items[index]
      count(descriptor.full_type)
      walk(descriptor.container)
    end
  end

  for index = 1, #roots do
    walk(roots[index])
  end
  return tally
end

Observe.carriedTally = carriedTally

--- Are every one of `inputs` on the character right now?
---
--- Returns true or false. The alternatives one ingredient lists are summed
--- rather than tried one at a time: a recipe that accepts a plank or a log is
--- satisfied by two planks, one log and one plank, or two logs, and asking
--- each type on its own would refuse the mixed bag the game accepts.
local function materialsReady(inputs, tally, multiplier)
  for index = 1, #inputs do
    local input = inputs[index]
    local held = 0
    for typeIndex = 1, #input.types do
      held = held + (tally[input.types[typeIndex]] or 0)
    end
    if held < (input.count * multiplier) then
      return false, input
    end
  end
  return true
end

Observe.materialsReady = materialsReady

--- What the character can make, as plain values, or nil when this build says
--- nothing about recipes at all.
---
--- Three separate facts, kept separate: how many recipes the character knows,
--- which of the published ones have their materials on the person right now,
--- and whether the ingredient lists could be read at all. A build that names
--- the recipes but not their ingredients publishes the names with no verdict
--- rather than a verdict nobody measured -- crafting destroys materials, and
--- "ready" is the word that spends them.
---
--- Materials are counted on the character only. A recipe that also needs a
--- crafting surface or a world container is not something this reading can see,
--- and the risk escalation for that lives in the sidecar's policy, per the
--- arguments a command carries.
function Observe.craftingFields(player, roots)
  local names, known, truncated = Observe.knownRecipeNames(player)
  if names == nil then
    return nil
  end
  -- The first MAX_RECIPE_SCAN the engine listed, then ordered by name: the
  -- second half is what makes the published set the same from one tick to the
  -- next, and `truncated` is what stops the shorter list from reading as the
  -- whole of what this character knows.
  table.sort(names, function(left, right)
    return left.name < right.name
  end)
  local tally = carriedTally(roots)
  local fields = {
    known = known,
    truncated = truncated == true or #names > Observe.MAX_RECIPES,
    recipes = {},
  }
  local listed = math.min(#names, Observe.MAX_RECIPES)
  for index = 1, listed do
    local entry = names[index]
    local record = { name = entry.name }
    local recipe = recipeObject(entry)
    if recipe ~= nil then
      record.product = Observe.recipeProduct(recipe)
      record.needs_surface = Observe.recipeNeedsSurface(recipe)
      local inputs = recipeInputs(recipe)
      if inputs ~= nil then
        record.inputs = inputs
        if tally ~= nil then
          -- `ready` is true, false, or absent, and the third one is a reading
          -- of its own: the recipe is known and its ingredients could not be
          -- read. What the document makes of that is applyCrafting's decision,
          -- so the counting happens there rather than being decided twice.
          record.ready = materialsReady(inputs, tally, 1)
        end
      end
    end
    fields.recipes[index] = record
  end
  return fields
end

--- Stamp the recipe readout onto the items that are its ingredients.
---
--- The sidecar's crafting policy reads recipes off the item tier, which is one
--- of the two places schemas/observation.schema.json leaves open, and it
--- deduplicates them by name across the whole inventory. So each recipe is
--- stamped on the FIRST carried item of each type it consumes rather than on
--- every one of them: a hundred planks each carrying the same entry would
--- multiply a document that already carries the hundred planks, and the
--- sidecar would throw ninety-nine of the copies away.
---
--- Pure, and bounded twice over: the recipes were already capped when they were
--- read, and no item takes more than MAX_RECIPES_PER_ITEM of them. A recipe
--- whose product or ingredient list could not be read is stamped nowhere -- an
--- entry that cannot say what it makes has no postcondition, and one whose
--- requirements are half-read would understate what the craft spends.
function Observe.attachRecipes(roots, crafting)
  if type(roots) ~= "table" or type(crafting) ~= "table" or type(crafting.recipes) ~= "table" then
    return
  end
  local index = {}
  local budget = Observe.MAX_TALLY_ITEMS

  local function remember(descriptor)
    local fullType = descriptor.full_type
    if type(fullType) ~= "string" or #fullType == 0 then
      return
    end
    if index[fullType] == nil then
      index[fullType] = descriptor
    end
    local tail = fullType:match("([^%.]+)$")
    if tail ~= nil and index[tail] == nil then
      index[tail] = descriptor
    end
  end

  local function walk(node)
    if type(node) ~= "table" or type(node.items) ~= "table" then
      return
    end
    for position = 1, #node.items do
      if budget <= 0 then
        return
      end
      budget = budget - 1
      remember(node.items[position])
      walk(node.items[position].container)
    end
  end

  for position = 1, #roots do
    walk(roots[position])
  end

  for position = 1, #crafting.recipes do
    local record = crafting.recipes[position]
    if record.product ~= nil and record.inputs ~= nil then
      -- One type per requirement line, because that is the shape the sidecar's
      -- crafting policy reads. An ingredient the recipe would accept several
      -- types for is published under the first of them, which can only make
      -- the requirement look harder to meet than it is: the sidecar refuses a
      -- craft it could have run, rather than spending materials on one it
      -- could not. The adapter's own check is the one that sums alternatives.
      local materials = {}
      for inputIndex = 1, #record.inputs do
        local input = record.inputs[inputIndex]
        materials[inputIndex] = { full_type = input.types[1], count = input.count }
      end
      local stamped = {}
      for inputIndex = 1, #record.inputs do
        for typeIndex = 1, #record.inputs[inputIndex].types do
          local descriptor = index[record.inputs[inputIndex].types[typeIndex]]
          if descriptor ~= nil and stamped[descriptor] == nil then
            stamped[descriptor] = true
            local block = descriptor.crafting
            if block == nil then
              block = { recipes = {} }
              descriptor.crafting = block
            end
            if #block.recipes < Observe.MAX_RECIPES_PER_ITEM then
              block.recipes[#block.recipes + 1] = {
                name = record.name,
                product = record.product,
                display_name = record.name,
                -- Everything published here came out of the character's own
                -- known-recipe collection, so this is a reading rather than an
                -- assumption; a recipe read any other way would carry no
                -- `known` at all.
                known = true,
                needs_surface = record.needs_surface,
                materials = materials,
              }
            end
          end
        end
      end
    end
  end
end

-- ---------------------------------------------------------------------------
-- nearby
-- ---------------------------------------------------------------------------

--- One world object, or nil when it is nothing the agent can name.
local function objectFields(object, objectIndex, position, distance)
  local container = invoke(object, "getContainer")
  local semantics = {}
  local kind = nil
  if container ~= nil then
    kind = readString(container, { "getType", "getContainerType" })
    semantics[#semantics + 1] = "container"
  end
  if kind == nil then
    kind = readString(object, { "getObjectName", "getName" })
  end
  local water = readNumber(object, { "getWaterAmount" })
  if water ~= nil and water > 0 then
    semantics[#semantics + 1] = "water_source"
    kind = kind or "water"
  end
  if type(kind) ~= "string" then
    return nil
  end
  local fields = {
    kind = kind:lower(),
    distance = distance,
    x = position.x,
    y = position.y,
    z = position.z,
    semantics = semantics,
    -- Every scanned object carries its position in the square's object list, so
    -- ObserveModel can mint a per-object reference for the kinds that need one.
    object_index = objectIndex,
  }
  if container ~= nil then
    -- A container object is referenced as a container, because that is the
    -- reference an inventory transfer can actually name.
    fields.container_index = 0
  end
  if fields.kind == "door" then
    -- Each reading in its own guarded probe, and an absent reader leaves the
    -- field out entirely: the sidecar treats a missing `locked` as "the lock
    -- could not be read", and a defaulted false would read as "unlocked" --
    -- which is a permission, not a gap. Build 42 capitalises some of these
    -- differently across classes, so each is a short closed list of spellings.
    local open = readBoolean(object, { "IsOpen", "isOpen" })
    if open ~= nil then
      fields.open = open
    end
    local locked = readBoolean(object, { "isLockedByKey", "isLocked" })
    if locked ~= nil then
      fields.locked = locked
    end
    local barricaded = readBoolean(object, { "isBarricaded" })
    if barricaded ~= nil then
      fields.barricaded = barricaded
    end
    -- The classic IsoDoor convention: getNorth() true is a door in the north
    -- wall of its square, false one in the west wall. The token is emitted only
    -- when the reader answered -- an unread orientation stays unread.
    local north = readBoolean(object, { "getNorth" })
    if north ~= nil then
      fields.orientation = north and "north" or "west"
    end
  end
  return fields
end

--- The cell, or nil plus the reason it could not be reached.
---
--- Separated from the two scans below because "the world could not be looked
--- at" is a different fact from "the world holds nothing", and only this
--- function can tell them apart.
local function currentCell()
  if type(getCell) ~= "function" then
    return nil, "getCell is not available in this build"
  end
  local ok, cell = pcall(getCell)
  if not ok then
    return nil, string.format("getCell() failed: %s", tostring(cell))
  end
  if cell == nil then
    return nil, "getCell() returned no cell"
  end
  return cell
end

Observe.cell = currentCell

--- One dead body as a corpse descriptor, or nil when it holds no container.
---
--- A corpse is observed for its loot, so a body that answers no ItemContainer
--- is scenery this scan has nothing to say about: it is not emitted at all,
--- rather than emitted as a container it is not.
---
--- Deliberately no object_index and no container_index. A dead body lives in
--- the square's dead-body list, not in getObjects(), so the world-container
--- reference scheme -- x:y:z:object_index:container_index into getObjects() --
--- cannot address its loot: an index minted here would resolve to whatever
--- object happens to sit at that position in the *other* list, which is the
--- exact failure the reference scheme exists to prevent. The corpse is
--- therefore observation-only, named by its square, until a reference surface
--- for dead bodies exists; docs/GAME_API_VERIFICATION.md records the gap.
local function corpseFields(body, position, distance)
  local container = invoke(body, "getContainer")
  if container == nil then
    return nil
  end
  return {
    kind = "corpse",
    distance = distance,
    x = position.x,
    y = position.y,
    z = position.z,
    semantics = { "container" },
  }
end

--- The square's dead bodies, through whichever accessor this build spells.
---
--- `getDeadBodys` -- the engine's own plural -- is probed first because it is
--- the complete answer; `getDeadBody` second, for a build that names only the
--- top of the pile. Both spend the walk's shared object budget, and a body the
--- budget or the per-square cap left unread is counted dropped like any other
--- object.
local function scanBodies(square, position, distance, keep, result, budget)
  local bodies = invoke(square, "getDeadBodys")
  local size = listSize(bodies)
  if size == nil then
    local body = invoke(square, "getDeadBody")
    if body == nil then
      return
    end
    if budget.objects <= 0 then
      result.truncated = true
      result.dropped = result.dropped + 1
      return
    end
    budget.objects = budget.objects - 1
    keep(corpseFields(body, position, distance))
    return
  end
  local scanned = math.min(size, Observe.MAX_BODIES_PER_SQUARE)
  local stopped = nil
  for index = 0, scanned - 1 do
    if budget.objects <= 0 then
      stopped = index
      break
    end
    budget.objects = budget.objects - 1
    keep(corpseFields(listGet(bodies, index), position, distance))
  end
  local unread = size - scanned
  if stopped ~= nil then
    unread = size - stopped
  end
  if unread > 0 then
    result.truncated = true
    result.dropped = result.dropped + unread
  end
end

--- Read one square into `result`, spending the walk's shared object budget.
---
--- `stopped` mirrors walkItemContainer: where the shared budget ran out, if it
--- did, so "the square holds four things" is never said of a square whose fifth
--- was not read.
---
--- The room is read ONCE here and shared across everything on the square --
--- objects and corpses alike -- rather than once per object: the same answer
--- MAX_OBJECTS_PER_SQUARE times over would be pure engine work on the game
--- thread.
local function scanSquare(cell, playerPosition, x, y, result, budget)
  local square = invoke(cell, "getGridSquare", x, y, playerPosition.z)
  if square == nil then
    return
  end
  local position = { x = x, y = y, z = playerPosition.z }
  local distance = PZAgent.Refs.chebyshevDistance(playerPosition, position)
  local room, building = Observe.placeOf(square)

  --- Stamp the square's shared room reading onto a descriptor and keep it.
  --- An absent reading stamps nothing: the fields stay out entirely.
  local function keep(fields)
    if fields == nil then
      return
    end
    fields.room = room
    fields.building = building
    budget.count = budget.count + 1
    result.objects[budget.count] = fields
  end

  local objects = invoke(square, "getObjects")
  local size = listSize(objects)
  if size ~= nil then
    local scanned = math.min(size, Observe.MAX_OBJECTS_PER_SQUARE)
    local stopped = nil
    for index = 0, scanned - 1 do
      if budget.objects <= 0 then
        stopped = index
        break
      end
      budget.objects = budget.objects - 1
      keep(objectFields(listGet(objects, index), index, position, distance))
    end
    local unread = size - scanned
    if stopped ~= nil then
      unread = size - stopped
    end
    if unread > 0 then
      result.truncated = true
      result.dropped = result.dropped + unread
    end
  end

  scanBodies(square, position, distance, keep, result, budget)
end

--- Objects on the squares around the player, nearest ring first.
---
--- The order is load-bearing, not cosmetic. The walk spends one shared object
--- budget (Observe.MAX_OBJECTS_SCANNED) across every square, and a budget is
--- only safe to spend if what it runs out on is what the document was going to
--- discard anyway. Raster order starts in the far corner, so a budget spent
--- that way would drop the square under the player's feet; walking outward in
--- Chebyshev rings means the squares that go unread are the farthest, which is
--- exactly the end ObserveModel's nearest-first sort cuts off.
function Observe.nearbyObjects(playerPosition)
  local result = { objects = {}, truncated = false, dropped = 0 }
  local radius = Observe.RADIUS
  local visited = 0
  -- A cell that cannot be reached, or one whose squares cannot be asked for,
  -- leaves `visited` at zero, and the accounting at the end of this function
  -- then reports every square as unread. Returning early instead -- which is
  -- what this did -- published `truncated = false, dropped = 0`: a complete
  -- scan of an empty world, produced without reading one square of it.
  local cell = currentCell()
  if cell ~= nil and member(cell, "getGridSquare") ~= nil then
    local originX = math.floor(playerPosition.x)
    local originY = math.floor(playerPosition.y)
    local budget = { squares = Observe.MAX_SQUARES, objects = Observe.MAX_OBJECTS_SCANNED, count = 0 }
    for ring = 0, radius do
      if budget.squares <= 0 or budget.objects <= 0 then
        break
      end
      for dx = -ring, ring do
        for dy = -ring, ring do
          local onRing = dx == -ring or dx == ring or dy == -ring or dy == ring
          if onRing and budget.squares > 0 and budget.objects > 0 then
            budget.squares = budget.squares - 1
            visited = visited + 1
            scanSquare(cell, playerPosition, originX + dx, originY + dy, result, budget)
          end
        end
      end
    end
  end
  local unvisited = (2 * radius + 1) * (2 * radius + 1) - visited
  if unvisited > 0 then
    -- One per square nobody looked at. A lower bound on what was missed, which
    -- is the only honest number available without looking.
    result.truncated = true
    result.dropped = result.dropped + unvisited
  end
  return result
end

--- Zombies within the observation radius.
---
--- `chasing` comes from the zombie's own target, not from its distance: a
--- distant zombie that has noticed the player is a live interrupt and a close
--- one that has not may be safely read past.
---
--- `unscanned` marks the returns where no zombie was looked at at all -- no
--- cell, or a cell with no readable zombie list. The list is empty in that case
--- exactly as it is on an empty street, and the two must not read alike:
--- ObserveModel.dangerFloor turns this table into the danger level the mod's own
--- gate acts on, and "nobody looked" is not "nothing is there".
function Observe.nearbyZombies(player, playerPosition)
  local result = { zombies = {}, truncated = false, dropped = 0 }
  local cell = currentCell()
  if cell == nil then
    -- Nobody counted. An empty list here would be read as "no zombies", and
    -- the floor would turn that into DANGER.NONE -- calm the scan never saw.
    result.unscanned = true
    return result
  end
  local list = invoke(cell, "getZombieList")
  local size = listSize(list)
  if size == nil then
    result.unscanned = true
    return result
  end
  local scanned = math.min(size, Observe.MAX_ZOMBIE_SCAN)
  if size > scanned then
    result.truncated = true
    result.dropped = size - scanned
  end
  local count = 0
  for index = 0, scanned - 1 do
    local zombie = listGet(list, index)
    local position = positionOf(zombie)
    if position ~= nil then
      local distance = PZAgent.Refs.chebyshevDistance(playerPosition, position)
      if distance <= Observe.RADIUS then
        local chasing = nil
        if member(zombie, "getTarget") ~= nil then
          -- An accessor that answered with no target is a reading of "not
          -- chasing"; an accessor this build does not have at all is not, and
          -- must stay absent so the sidecar is told it could not be read.
          chasing = rawequal(invoke(zombie, "getTarget"), player)
        end
        local visible = nil
        if member(player, "CanSee") ~= nil then
          local seen = invoke(player, "CanSee", zombie)
          if type(seen) == "boolean" then
            visible = seen
          end
        end
        count = count + 1
        result.zombies[count] = {
          runtime_id = readIdentity(zombie, { "getOnlineID", "getID" }),
          distance = distance,
          visible = visible,
          chasing = chasing,
          -- Tri-state body reading; nil leaves the field out entirely.
          state = Observe.zombieState(zombie),
          x = position.x,
          y = position.y,
          z = position.z,
        }
      end
    end
  end
  return result
end

-- ---------------------------------------------------------------------------
-- squares
--
-- One description per square, in the vocabulary the rest of the system already
-- speaks: `kind = "square"` entries in nearby.objects, carrying the semantics
-- `pz_agent_core.actions.adapters.movement` has been reading since it was
-- written. Nothing published them until now, so `movement.move_to`'s square
-- check has been finding no square to check; the build policy is what finally
-- needs the reading, and it needs the same one rather than a second view of
-- the same tiles.
--
-- Why a build needs it at all: a structure this agent raises is one it has no
-- action to remove, so the sidecar refuses a placement that would leave the
-- character's own square with no remaining path to open ground -- and that
-- check is a walk over exactly what this section publishes.
--
-- Every symbol here is UNVERIFIED against Build 42:
--
--   IsoGridSquare.isSolid / isSolidTrans   is the square itself a wall
--   IsoGridSquare.isFree(false)            is anything standing on it
--   IsoGridSquare.getFloor                 is there a floor, or is it a fall
--
-- so each is probed, each call is guarded, and a reader that does not answer
-- costs the fact rather than producing a boolean. ObserveModel is where an
-- unread fact becomes an absent semantic and a declared limit rather than a
-- token nobody measured. docs/GAME_API_VERIFICATION.md is where these belong
-- as rows.
-- ---------------------------------------------------------------------------

--- Can a character cross this square? true, false, or nil when nothing said.
---
--- Read off the square's own solidity rather than off what happens to be
--- standing on it: this is the terrain question an escape route walks, and a
--- square someone is standing on this second is not a square a wall has sealed.
--- The third state is load-bearing, and ObserveModel spends it: a `false` here
--- becomes the `blocked` semantic, while a nil becomes no semantic at all plus
--- a declared limit, because "there is a wall" and "nobody could tell" are
--- opposite facts and only one of them was observed.
function Observe.squarePassable(square)
  local solid = readBoolean(square, { "isSolid" })
  local solidTrans = readBoolean(square, { "isSolidTrans" })
  if solid == nil and solidTrans == nil then
    return nil
  end
  return solid ~= true and solidTrans ~= true
end

--- Is the square clear of anything already standing on it? Tri-state.
---
--- `isFree` takes the engine's own "is this for a zombie" flag, so it is called
--- with the flag rather than probed through readBoolean. A false here is what
--- becomes SQUARE_OCCUPIED at command time; absent stays absent, because
--- "nobody could tell" is not "go ahead".
function Observe.squareFree(square)
  local free = invoke(square, "isFree", false)
  if type(free) ~= "boolean" then
    return nil
  end
  return free
end

--- Does the square have a floor? true, false, or nil when there is no reader.
---
--- The distinction the tri-state buys is the whole point: a reader that exists
--- and answered nothing is a square with no floor -- a fall, which movement
--- already refuses to walk onto and which an escape route must not run
--- through -- while a build with no reader at all has said nothing about the
--- floor, and a `drop` semantic invented there would refuse ground that is
--- perfectly solid.
function Observe.squareFloor(square)
  if member(square, "getFloor") == nil then
    return nil
  end
  return invoke(square, "getFloor") ~= nil
end

--- One description per square in the bounded window.
---
--- What travels is what only a square can answer. What STANDS on a square is
--- not restated here: the object entries the same scan publishes carry that,
--- keyed by the same coordinates, and one fact in two places is two facts that
--- can disagree.
---
--- The single inference the sidecar draws from this is stated here rather than
--- published as a second reading nobody measured: a structure raised on a
--- square makes that square impassable. So "would this placement block the
--- square" is answered by the placement itself, and what this reading supplies
--- is the map around it.
---
--- A square the cell would not hand over is published all the same, marked as
--- not loaded, rather than left out. Left out it would be a square nobody
--- described, and a square nobody described sits at the edge of the window --
--- which is exactly where the trap check stops and calls it a way out. Said
--- plainly: we looked, and it could not be read.
---
--- Bounded twice: the window is DESCRIBE_RADIUS squares out and at most
--- MAX_DESCRIBED_SQUARES entries leave here. It rings outward from the
--- character, so what a spent budget costs is the farthest squares, which is
--- the end the model's nearest-first sort cuts off anyway.
function Observe.describeSquares(playerPosition)
  local result = { squares = {}, truncated = false, dropped = 0 }
  if type(getCell) ~= "function" then
    return result
  end
  local ok, cell = pcall(getCell)
  if not ok or cell == nil then
    return result
  end
  local originX = math.floor(playerPosition.x)
  local originY = math.floor(playerPosition.y)
  local count = 0
  for ring = 0, Observe.DESCRIBE_RADIUS do
    for dx = -ring, ring do
      for dy = -ring, ring do
        if dx == -ring or dx == ring or dy == -ring or dy == ring then
          if count >= Observe.MAX_DESCRIBED_SQUARES then
            result.truncated = true
            result.dropped = result.dropped + 1
          else
            local x, y = originX + dx, originY + dy
            local position = { x = x, y = y, z = playerPosition.z }
            local square = invoke(cell, "getGridSquare", x, y, playerPosition.z)
            local fields = {
              x = x,
              y = y,
              z = playerPosition.z,
              distance = PZAgent.Refs.chebyshevDistance(playerPosition, position),
              loaded = square ~= nil,
            }
            if square ~= nil then
              -- Written out rather than folded into the constructor: each of
              -- these is tri-state, and the `a and b or c` idiom turns a read
              -- `false` into a nil, which is the one substitution this whole
              -- section exists to prevent.
              fields.passable = Observe.squarePassable(square)
              fields.free = Observe.squareFree(square)
              fields.floor = Observe.squareFloor(square)
            end
            count = count + 1
            result.squares[count] = fields
          end
        end
      end
    end
  end
  return result
end

--- The nearby block's plain values, or nil plus the reason there is no block.
---
--- Nil when the world itself could not be reached: ObserveModel then leaves the
--- section out, and the sidecar reads an absent `nearby` as "not walked"
--- (`available: false`) instead of as an observation of an empty world. A cell
--- that answered but could not be read in full is a section that exists and
--- declares its own gaps -- the objects it did read are still worth having.
function Observe.nearbyFields(player, playerPosition)
  local cell, cellError = currentCell()
  if cell == nil then
    return nil, cellError
  end
  local objects = Observe.nearbyObjects(playerPosition)
  local zombies = Observe.nearbyZombies(player, playerPosition)
  local squares = Observe.describeSquares(playerPosition)
  return {
    objects = objects.objects,
    objects_truncated = objects.truncated,
    objects_dropped = objects.dropped,
    zombies = zombies.zombies,
    zombies_truncated = zombies.truncated,
    zombies_dropped = zombies.dropped,
    zombies_unscanned = zombies.unscanned,
    squares = squares.squares,
    squares_truncated = squares.truncated,
    squares_dropped = squares.dropped,
  }
end

-- ---------------------------------------------------------------------------
-- the tick
-- ---------------------------------------------------------------------------

--- Assemble the context ObserveModel.build consumes.
function Observe.context(agent, player, sessionId, seq, nowMs)
  local playerFields, playerError = Observe.playerFields(player)
  if playerFields == nil then
    return nil, playerError
  end
  -- The build is the one field ObserveModel refuses to do without, so its own
  -- probe's reason is caught here rather than replaced downstream by the
  -- builder's much vaguer "the game build is unknown".
  local gameFields, buildError = Observe.gameFields()
  if gameFields.build == nil then
    return nil, buildError or "the game build is unknown"
  end
  -- An unreadable inventory costs the section, not the snapshot: the sidecar
  -- reads an absent `inventory` as "not walked". The reason still surfaces,
  -- because an agent that silently stops seeing its own bag looks healthy.
  local roots, rootsError = Observe.inventoryRoots(player)
  if roots == nil and rootsError ~= nil then
    agent.safety.last_error = rootsError
  end
  -- What the character can make, folded into `player.stats` by ObserveModel:
  -- the observation schema has no recipe tier, and the stats map is the one
  -- open scalar map in it -- the same place the observer already reports its
  -- own limits and the equipped weapon's wear. Absent when this build says
  -- nothing about recipes, never an empty reading.
  --
  -- Nothing is published about nearby crafting surfaces. Build 42 exposes no
  -- reader this file could ask "is this object a station some recipe needs",
  -- and naming one off an object's own display name would be a guess dressed
  -- as an observation, so the field simply does not exist -- see
  -- docs/GAME_API_VERIFICATION.md for the gap.
  playerFields.crafting = Observe.craftingFields(player, roots)
  -- The same reading, stamped on the items it consumes. The sidecar's crafting
  -- policy reads its recipes off the item tier -- the other place the schema
  -- leaves open -- because a requirement list is a list of objects and the
  -- stats map holds scalars. Both come from one read of the engine.
  Observe.attachRecipes(roots, playerFields.crafting)
  -- Same asymmetry as the inventory above, and for the sharper reason: an
  -- unreachable world costs the section rather than filling it with an empty
  -- one, because an empty `nearby` is an observation -- the sidecar's threat
  -- assessment reads it as no zombies at all.
  local nearby, nearbyError = Observe.nearbyFields(player, playerFields.position)
  if nearby == nil and nearbyError ~= nil then
    agent.safety.last_error = nearbyError
  end
  -- The safety snapshot is taken *after* this, because until it was set here
  -- `danger_level` was written by nothing and read by three consumers: the
  -- mod's own gate in Safety.mayStart, the action engine's threat threshold,
  -- and the compact view the planner sees. All three were told the situation
  -- was calm during a horde. The floor is deliberately coarser than
  -- `pz_agent_core.safety.threat`, which stays authoritative for policy.
  -- The timestamp travels with the level because this is the only place that
  -- writes it: an observation that fails before reaching this line leaves the
  -- previous reading behind, and without its age the gate cannot tell that
  -- reading from one taken this tick.
  PZAgent.Safety.setDanger(
    agent.safety,
    PZAgent.ObserveModel.dangerFloor(nearby, playerFields.position),
    nowMs
  )
  return {
    session_id = sessionId,
    seq = seq,
    timestamp_ms = nowMs,
    full = true,
    capability_revision = agent.capability_revision,
    active_goal_id = agent.active_goal_id,
    game = gameFields,
    player = playerFields,
    inventory = roots,
    nearby = nearby,
    action = agent.queue_description,
    safety = PZAgent.Safety.snapshot(agent.safety, nowMs),
  }
end

--- Build and publish one full snapshot.
---
--- Returns the document, or nil plus a reason. The reason is recorded on the
--- safety state so it reaches the HUD and the next heartbeat: a mod that stops
--- observing while the sidecar keeps receiving heartbeats would look healthy
--- and be blind.
function Observe.tick(agent, nowMs)
  local function fail(reason)
    agent.safety.last_error = reason
    return nil, reason
  end

  local sessionId = agent.session:id()
  if sessionId == nil then
    return nil, "no open session"
  end
  local player = agent.player
  if player == nil then
    local found, lookupError = PZAgent.Runtime.currentPlayer()
    player = found
    agent.player = found
    if player == nil then
      -- Not recorded on the safety state: a tick between characters is routine,
      -- and the reason is still returned so a caller that cares can say why.
      return nil, lookupError or "no player character"
    end
  end
  local seq, seqError = agent.sequence:next("observation")
  if seq == nil then
    return fail(seqError)
  end
  local context, contextError = Observe.context(agent, player, sessionId, seq, nowMs)
  if context == nil then
    return fail(contextError)
  end
  local document, buildError = PZAgent.ObserveModel.build(context)
  if document == nil then
    return fail(buildError)
  end
  local slot, publishError = agent.ipc:publishSnapshot(document)
  if slot == nil then
    return fail(publishError)
  end
  return document
end

return Observe
