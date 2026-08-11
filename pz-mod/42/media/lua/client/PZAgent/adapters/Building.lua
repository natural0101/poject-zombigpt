--[[
PZAgent.Adapters.Building -- building.inspect and building.build.

Crafting was the first thing this mod does that cannot be undone. Building is
its sibling and one rung stricter, because what a craft destroys was the
character's own and what a build leaves behind stands in the world afterwards.
Everything below is shaped by that difference.

* **Nothing here removes anything.** This wave ships no demolition action, and
  this file offers no way back: a wall the agent raises is a wall the agent
  cannot take down. That is why every refusal happens BEFORE the work is
  queued, and it is why the sidecar keeps `building.build` at P4 -- the one
  tier this codebase gives no autonomous path at all, in any mode.
* **One command raises one structure, once.** One blueprint, one square, one
  timed action. There is no loop in this file and no retry: a structure that
  could have been raised again is a line in the report, never something this
  file starts a second time. The next one is the mission's decision, taken at
  the seam where safety.stop and the reflex guard can reach it.
* **The postcondition is the structure standing on the square.** A build that
  was accepted proves nothing and a queued action proves nothing. `succeeded`
  is minted only when the re-observed square carries an object that was not
  there before AND the square, which read clear beforehand, no longer does.
  Both halves, because an object that appeared on a square that still reads
  open is not a wall, and calling it one would be the fabricated success this
  project exists to refuse.
* **The square is read before anything is queued.** SQUARE_OCCUPIED names what
  is in the way; the agent never clears a square, so anything already standing
  there ends the command rather than becoming something to move. A square this
  build will not describe at all is CAPABILITY_UNAVAILABLE naming the reader,
  because a build whose postcondition could not be checked is never started.
* **The trap check is deliberately NOT here.** Whether a placement would leave
  the character with no way out is computed by the sidecar, from the bounded
  square descriptions PZAgent.Observe publishes, and it arrives here as a command
  that was never sent. Duplicating it in the mod would put the same rule in two
  places and let them disagree. What this file owes that check is the honest
  square reading it rests on, and the refusals above are exactly that.
* **Every engine symbol here is unverified against Build 42.** That build
  rewrote both crafting and construction, and none of these spellings has been
  seen answering in a live session, so each is probed through a short closed
  list and an absent one costs a CAPABILITY_UNAVAILABLE naming every candidate.
  The capability declares itself experimental for that reason and for the
  permanence above; the sidecar therefore withholds `building.build` on every
  install until a live run promotes it.

`building.inspect` mutates nothing -- it reads the blueprint tables the
character already knows, the items already on the person, and, when it is given
one, the square as it stands -- so the protocol lists it among the read-only
actions, and like the other read-only actions it declares no capability at all:
the accessors it needs are methods on Java objects that no scan of the install
can see, so a probe over them would report `unsupported` on a healthy build.
What it does instead is refuse, naming the reader, when the build has not got
one. Only `building.build` rides the capability, which is the half a live run
can confirm.

The blueprint readers below duplicate what PZAgent.Adapters.Crafting reads for
recipes rather than calling into it. Adapters do not depend on each other --
Combat states the same rule about Movement's walk -- and two short guarded
probes in two files is the price of that.
]]

-- Load-order guard, live-proven 2026-08-08 on Build 42.20.2: the engine walks
-- adapters/ in an order this file does not control. The statement form is
-- deliberate -- the paren form is banned as dynamic loading -- and the test
-- harness pre-resolves this module, so there the require is a no-op.
require "PZAgent/adapters/Toolkit"

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Building = {}
PZAgent.Adapters.Building = Building

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

-- ---------------------------------------------------------------------------
-- bounds
-- ---------------------------------------------------------------------------

--- Known recipes read out of the engine for one command. A character late in a
--- run knows hundreds of them and only some are blueprints; this is the walk
--- that finds the one a command named, and a report that stopped short says so
--- rather than answering "unknown".
Building.MAX_SCAN = 128

--- Blueprints one inspect reports. The listing goes out in an ack, so the
--- ceiling belongs on the wire and not only in the reader.
Building.MAX_LISTED = 16

--- Ingredient entries read off one blueprint, and item types read off one
--- ingredient. Both are the shape of a recipe rather than a guess: a script
--- that wants more than this is not something this file can honestly summarise.
Building.MAX_INPUTS = 8

--- Shortfalls named in one refusal, and in one report entry.
Building.MAX_MISSING = 8

--- One build window, in milliseconds. The window is bounded by this clock as
--- well as by the queue emptying, and the declared timeout below is this same
--- number so the runtime's lease closes the command on the clock the progress
--- step watches -- the agreement Combat.lua states for its engage window.
Building.BUILD_WINDOW_MS = 30000

Building.INSPECT_TIMEOUT_MS = 5000
Building.POLL_MS = 250

--- How far the character may be from the square and still build on it. The
--- shared reach plus one square, the same "reach+1" Combat allows a swing.
--- Anything farther is refused rather than walked to: a walk is a movement
--- command the mission plans, and improvising one here would put a P2 action
--- inside a P4 one.
Building.BUILD_REACH = toolkit().DEFAULT_REACH + 1

--- How the refusals name the probed surfaces: one spelling on the wire per
--- surface, listing every candidate that was looked for. These are among the
--- least certain rows this mod has -- Build 42 rewrote construction and nothing
--- here has been seen answering in a live session -- and
--- docs/GAME_API_VERIFICATION.md is where they belong as rows.
Building.KNOWN_SYMBOL = "IsoGameCharacter.getKnownRecipes"
Building.BLUEPRINT_SYMBOL = "getScriptManager().getBuildRecipe / getCraftRecipe / getRecipe"
Building.INPUT_SYMBOL = "CraftRecipe.getInputs / getSource"
Building.SPRITE_SYMBOL = "CraftRecipe.getSpriteName / getTileName / getSprite"
Building.OBJECTS_SYMBOL = "IsoGridSquare.getObjects"
Building.ACTION_SYMBOL = "ISBuildAction / ISBuildIsoEntityAction"

--- Candidate spellings, probed in order, for each surface.
local KNOWN_NAMES = { "getKnownRecipes" }
local LOOKUP_NAMES = { "getBuildRecipe", "getCraftRecipe", "getRecipe" }
local INPUT_NAMES = { "getInputs", "getSource" }
local TYPE_NAMES = { "getItems", "getItemTypes" }
local SPRITE_NAMES = { "getSpriteName", "getTileName" }
local NAME_NAMES = { "getObjectName", "getName" }
local ACTION_NAMES = { "ISBuildAction", "ISBuildIsoEntityAction" }

--- Argument kinds, spelled the way PZAgent.CommandDispatcher.ARG spells them.
--- Held here rather than read off the dispatcher because the engine chooses the
--- order it walks media/lua in, and a declaration that indexed another client
--- module while this file was loading would break on a build that reaches
--- adapters/ first.
local ARG = { NUMBER = "number", STRING = "string", REF = "ref" }

local INSPECT_ARGS = { "square", "limit" }
local BUILD_ARGS = { "blueprint", "square" }

-- ---------------------------------------------------------------------------
-- reading blueprints
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

--- The token a blueprint is named by on the wire.
---
--- One substitution -- every space becomes an underscore -- and then the
--- reference alphabet, which is exactly what ObserveModel.recipeToken does and
--- exactly what Crafting.recipeToken does. All three must agree: the token a
--- planner reads out of a snapshot is the token it sends back here, and a
--- blueprint named one way and accepted another is a command that can never be
--- issued. A name the alphabet cannot carry has no token at all rather than a
--- mangled one, because two blueprints behind one token is worse than a
--- blueprint this agent cannot name.
function Building.blueprintToken(name)
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
---
--- Which of these are blueprints is not decided here, because it cannot be:
--- the answer is on the recipe object, which a name-shaped entry needs the
--- script lookup to reach. The callers classify each entry as they resolve it,
--- and both of them count what they could not classify rather than dropping it
--- silently.
function Building.knownRecipes(player)
  local Toolkit = toolkit()
  local known = listOf(player, KNOWN_NAMES)
  local size = known ~= nil and Toolkit.listSize(known) or nil
  if size == nil then
    return nil, Building.KNOWN_SYMBOL
  end
  local scanned = math.min(size, Building.MAX_SCAN)
  local entries = {}
  for index = 0, scanned - 1 do
    local entry = Toolkit.listGet(known, index)
    local name = recipeNameOf(entry)
    local token = Building.blueprintToken(name)
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
--- is looked up through the script manager, which is reached through the
--- guarded global lookup rather than as a bare global -- an unverified symbol
--- must not become one this file depends on being present.
function Building.blueprintObject(entry)
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

--- The tile a blueprint puts on the square, as a sprite name, or nil.
---
--- Two jobs at once, which is why it is the field a build cannot start without.
--- It is the classifier: a recipe this build describes with a sprite is one
--- that places something in the world, and a recipe it will not describe that
--- way is simply not listed as a blueprint rather than guessed at. And it is
--- the postcondition: what proves the wall went up is an object standing on the
--- target square under this name, so a blueprint whose sprite cannot be read is
--- refused before anything is queued.
function Building.blueprintSprite(recipe)
  local Toolkit = toolkit()
  local direct = Toolkit.readStringOf(recipe, SPRITE_NAMES)
  if direct ~= nil then
    return direct
  end
  local ok, sprite = Toolkit.call(recipe, "getSprite")
  if not ok or sprite == nil then
    return nil
  end
  return (Toolkit.readStringOf(sprite, { "getName" }))
end

--- What one blueprint consumes: `{ { types = {...}, count = n }, ... }`, or nil.
---
--- A blueprint whose ingredient list is only partly readable answers nil as a
--- whole. A verdict computed from the ingredients that happened to answer would
--- call a build ready on the strength of requirements nobody read, and that is
--- the one wrong answer that spends materials for nothing.
function Building.blueprintInputs(recipe)
  local Toolkit = toolkit()
  local list = listOf(recipe, INPUT_NAMES)
  local size = list ~= nil and Toolkit.listSize(list) or nil
  if size == nil or size > Building.MAX_INPUTS then
    return nil
  end
  local inputs = {}
  for index = 0, size - 1 do
    local entry = Toolkit.listGet(list, index)
    local types = listOf(entry, TYPE_NAMES)
    local typeCount = types ~= nil and Toolkit.listSize(types) or nil
    if typeCount == nil or typeCount == 0 or typeCount > Building.MAX_INPUTS then
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

-- ---------------------------------------------------------------------------
-- counting what the character carries
-- ---------------------------------------------------------------------------

--- Count one item type in a snapshot, under its full name or its tail.
---
--- A recipe names an ingredient either way -- "Base.Plank" and "Plank" are the
--- same plank -- so both spellings count, and the ambiguity is stated here
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

Building.matchesType = matchesType

--- How many of `wanted` the snapshot shows on the character.
---
--- Walks the snapshot PZAgent.Adapters.Toolkit built, which is already bounded
--- at MAX_SNAPSHOT_ITEMS, rather than the engine: the shortfall a refusal names
--- and the inventory a person can see must be the same walk.
function Building.countType(snapshot, wanted)
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

--- What `inputs` is short of, given a snapshot.
---
--- Returns a bounded list of `{ types, need, held }`, empty when everything is
--- there. There is no multiplier: one command raises one structure, and the day
--- that changes is a change on both halves of the wire rather than an argument
--- that quietly appeared here. The alternatives one ingredient lists are summed
--- rather than tried separately -- a requirement that takes a plank or a log is
--- satisfied by one of each, and asking each type on its own would refuse the
--- mixed bag the game accepts.
function Building.missingFor(inputs, snapshot)
  local missing = {}
  for index = 1, #inputs do
    local input = inputs[index]
    local held = 0
    for typeIndex = 1, #input.types do
      held = held + Building.countType(snapshot, input.types[typeIndex])
    end
    if held < input.count and #missing < Building.MAX_MISSING then
      missing[#missing + 1] = { types = input.types, need = input.count, held = held }
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

Building.describeMissing = describeMissing

-- ---------------------------------------------------------------------------
-- reading the square
-- ---------------------------------------------------------------------------

--- What an object on a square is called, as far as this build will say.
local function nameOf(object)
  local Toolkit = toolkit()
  local name = Toolkit.readStringOf(object, NAME_NAMES)
  if name ~= nil then
    return name
  end
  local ok, sprite = Toolkit.call(object, "getSprite")
  if not ok or sprite == nil then
    return nil
  end
  return (Toolkit.readStringOf(sprite, { "getName" }))
end

Building.nameOf = nameOf

--- The sprite name of an object, which is what identifies a tile.
local function spriteNameOf(object)
  local Toolkit = toolkit()
  local ok, sprite = Toolkit.call(object, "getSprite")
  if not ok or sprite == nil then
    return nil
  end
  return (Toolkit.readStringOf(sprite, { "getName" }))
end

Building.spriteNameOf = spriteNameOf

--- Does `object` stand in the way of a build?
---
--- Solid, or holding a container, or answering the readers a door and a window
--- answer. Anything of the three is something the agent would have to remove to
--- build here, and removing is an authority this wave does not have.
local function occupies(object)
  local Toolkit = toolkit()
  if Toolkit.readBooleanOf(object, { "isSolid" }) == true then
    return true
  end
  if Toolkit.readBooleanOf(object, { "isSolidTrans" }) == true then
    return true
  end
  local ok, container = Toolkit.call(object, "getContainer")
  if ok and container ~= nil then
    return true
  end
  -- A door or a window means a wall already stands on this square. Classified
  -- by the readers rather than by `instanceof`, which several builds spell
  -- differently and which World.lua already treats as optional.
  if Toolkit.method(object, "IsOpen") ~= nil or Toolkit.method(object, "isSmashed") ~= nil then
    return true
  end
  return false
end

Building.occupies = occupies

--- What stands on `square`, or nil plus the symbol the build has not got.
---
--- Returns `{ objects, scanned, truncated, occupied, blocker }`. `occupied`
--- means "this square did not read as clear", which is deliberately wider than
--- "something was named": a square holding more objects than one reading walks
--- is not clear either, and a square the engine's own `isFree` refuses is not
--- clear whatever the walk found. Erring that way costs a refused wall; erring
--- the other way costs a wall raised on top of something.
---
--- The floor is never an occupant. Every square has one, and a build that
--- refused every square with a floor would refuse every square.
function Building.squareState(square)
  local Toolkit = toolkit()
  local ok, objects = Toolkit.call(square, "getObjects")
  if not ok or objects == nil then
    return nil, Building.OBJECTS_SYMBOL
  end
  local size = Toolkit.listSize(objects)
  if size == nil then
    return nil, Building.OBJECTS_SYMBOL
  end
  local floor = nil
  local okFloor, found = Toolkit.call(square, "getFloor")
  if okFloor then
    floor = found
  end
  local scanned = math.min(size, Toolkit.MAX_SQUARE_OBJECTS)
  local state = {
    objects = size,
    scanned = scanned,
    truncated = size > scanned,
    occupied = false,
  }
  for index = 0, scanned - 1 do
    local object = Toolkit.listGet(objects, index)
    if object ~= nil and not (floor ~= nil and rawequal(object, floor)) then
      if occupies(object) then
        state.occupied = true
        state.blocker = nameOf(object)
        break
      end
    end
  end
  if not state.occupied and state.truncated then
    state.occupied = true
    state.blocker = string.format("%d objects, more than one reading walks", size)
  end
  if not state.occupied then
    -- The engine's own answer to the same question, and the one that catches
    -- what the object walk cannot name -- a character standing there among
    -- them. Absent on a build without the reader, which changes nothing: the
    -- walk above has already answered.
    local okFree, free = Toolkit.call(square, "isFree", false)
    if okFree and free == false then
      state.occupied = true
      state.blocker = state.blocker or "the square does not read as free"
    end
  end
  return state
end

--- The object standing on `square` under the blueprint's sprite, or nil.
local function structureOn(square, sprite)
  local Toolkit = toolkit()
  local ok, objects = Toolkit.call(square, "getObjects")
  if not ok or objects == nil then
    return nil
  end
  local size = Toolkit.listSize(objects)
  if size == nil then
    return nil
  end
  local scanned = math.min(size, Toolkit.MAX_SQUARE_OBJECTS)
  for index = 0, scanned - 1 do
    local object = Toolkit.listGet(objects, index)
    if object ~= nil and spriteNameOf(object) == sprite then
      return object
    end
  end
  return nil
end

Building.structureOn = structureOn

--- The square a command named, or a typed refusal.
---
--- An unloaded square is TARGET_NOT_LOADED rather than a capability gap: the
--- build could be issued again once the character is near it, and a caller told
--- the wrong one of those two would go looking for a missing mod.
local function squareAt(point)
  local Toolkit = toolkit()
  local square, missing = Toolkit.gridSquare(point.x, point.y, point.z)
  if square == nil then
    if missing == "IsoCell.getGridSquare" then
      return nil, Toolkit.reasons().TARGET_NOT_LOADED, "the square the build names is not loaded"
    end
    return Toolkit.unavailable(missing)
  end
  return square
end

Building.squareAt = squareAt

--- The square a command named, parsed off a reference of this session.
local function squareOf(args, key, ctx)
  local Toolkit = toolkit()
  local ref, refCode, refDetail = Toolkit.readRef(args, key, PZAgent.Refs.KIND.SQUARE, ctx)
  if ref == nil then
    return nil, refCode, refDetail
  end
  local parsed, parseError = PZAgent.Refs.parseSquare(ref)
  if parsed == nil then
    return nil, Toolkit.reasons().INVALID_REF, parseError
  end
  return { x = parsed.x, y = parsed.y, z = parsed.z, ref = ref }
end

-- ---------------------------------------------------------------------------
-- building.inspect
-- ---------------------------------------------------------------------------

--- `square` is optional. With one, the answer is about that square -- what
--- stands on it and what could go there; without one, it is the bounded listing
--- of what this character could build anywhere, with the cost of each. Both
--- shapes are one reading, so there is one parser and one report.
local function inspectSpec(args, ctx)
  local Toolkit = toolkit()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, INSPECT_ARGS, {})
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local spec = {}
  if args.square ~= nil then
    local point, pointCode, pointDetail = squareOf(args, "square", ctx)
    if point == nil then
      return nil, pointCode, pointDetail
    end
    spec.point = point
  end
  local limit, limitCode, limitDetail = Toolkit.readCount(args, "limit", {
    default = Building.MAX_LISTED,
    minimum = 1,
    maximum = Building.MAX_LISTED,
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
  local spec, code, detail = inspectSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  if ctx.player == nil then
    return nil, reasons.PRECONDITION_FAILED, "no player character"
  end
  local known, symbolOrTotal = Building.knownRecipes(ctx.player)
  if known == nil then
    return Toolkit.unavailable(symbolOrTotal)
  end
  if spec.point ~= nil then
    local square, squareCode, squareDetail = squareAt(spec.point)
    if square == nil then
      return nil, squareCode, squareDetail
    end
  end
  Toolkit.state(ctx).inspect = spec
  return true
end

--- Reading the blueprint tables queues nothing, so the whole command finishes
--- inside begin and the reading itself happens in verify -- against the
--- character and the square as they are at that moment, which is the only
--- moment the answer is about.
local function inspectBegin(_, args, ctx)
  local spec, code, detail = inspectSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  return "done"
end

local function inspectProgress(_, args, ctx)
  local spec, code, detail = inspectSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  return "done"
end

local function inspectVerify(_, _, after, args, ctx)
  local Toolkit = toolkit()
  local spec, code, detail = inspectSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local known, totalOrSymbol, truncated = Building.knownRecipes(ctx.player)
  if known == nil then
    return Toolkit.unavailable(totalOrSymbol)
  end
  local snapshot = type(after) == "table" and after or Toolkit.observe(ctx.player)
  local listed = {}
  local ready = 0
  local judged = 0
  local unreadable = 0
  local stopped = false
  for index = 1, #known do
    if #listed >= spec.limit then
      stopped = true
      break
    end
    local entry = known[index]
    local recipe = Building.blueprintObject(entry)
    local sprite = recipe ~= nil and Building.blueprintSprite(recipe) or nil
    if recipe == nil then
      -- Known, and nothing this build will hand over to look at. Counted
      -- rather than dropped: "we could not tell whether that is a blueprint"
      -- is an answer the planner has to be able to see.
      unreadable = unreadable + 1
    elseif sprite ~= nil then
      local record = { blueprint = entry.token, display_name = entry.name, sprite = sprite }
      local inputs = Building.blueprintInputs(recipe)
      if inputs ~= nil then
        local missing = Building.missingFor(inputs, snapshot)
        judged = judged + 1
        record.ready = #missing == 0
        if record.ready then
          ready = ready + 1
        else
          record.missing = missing
        end
      end
      -- A record with no `ready` is the third state: the blueprint is known and
      -- its materials could not be read. It is listed as such rather than
      -- dropped, for the same reason.
      listed[#listed + 1] = record
    end
    -- A recipe that resolved and carries no sprite is simply not a blueprint --
    -- it is a craft, and crafting.inspect is where it is answered for. It is
    -- neither listed nor counted as unreadable, because nothing about it went
    -- unread.
  end
  local report = {
    kind = "buildable",
    blueprints = listed,
    listed = #listed,
    ready = ready,
    judged = judged,
    -- Known recipes this build would not hand over to look at, so it could not
    -- be told whether they are blueprints. Separate from `listed` on purpose:
    -- a planner reading a short list has to know whether it is short because
    -- the character knows little or because the build says little.
    unreadable = unreadable,
    known_total = totalOrSymbol,
    truncated = truncated == true or stopped,
  }
  if spec.point ~= nil then
    local square, squareCode, squareDetail = squareAt(spec.point)
    if square == nil then
      return nil, squareCode, squareDetail
    end
    local state, missing = Building.squareState(square)
    if state == nil then
      return Toolkit.unavailable(missing)
    end
    -- What the square says, exactly as the build command would read it, so a
    -- caller can see the refusal coming instead of discovering it.
    report.square = {
      ref = spec.point.ref,
      x = spec.point.x,
      y = spec.point.y,
      z = spec.point.z,
      objects = state.objects,
      free = not state.occupied,
      blocker = state.blocker,
    }
  end
  return report
end

local Inspect = toolkit().declare({
  name = "building.inspect",
  -- No capability, and the same reason world.inspect, container.inspect,
  -- inventory.search and crafting.inspect declare none: this action only reads,
  -- and what it reads is behind Java accessors no scan of the install can see,
  -- so a probe over them would report `unsupported` on a healthy build. It
  -- gates on the readers it actually needs, at validate, and refuses naming the
  -- symbol when the build has not got one. The sidecar leaves this action
  -- ungated for the same reason; the two halves have to name the same
  -- capability, and this is the one they name.
  capability = nil,
  requires = {},
  timeout_ms = Building.INSPECT_TIMEOUT_MS,
  poll_interval_ms = Building.POLL_MS,
  args = {
    -- The square a mission is considering. Optional here; an undeclared key is
    -- a refused command, so this declaration is what lets that command exist.
    square = { type = ARG.REF, kinds = { square = true } },
    -- The listing goes out in an ack, so the ceiling belongs on the wire as
    -- well as in the reader.
    limit = { type = ARG.NUMBER, integer = true, min = 1, max = Building.MAX_LISTED },
  },
  validate = inspectValidate,
  begin = inspectBegin,
  progress = inspectProgress,
  verify = inspectVerify,
})

Building.Inspect = Inspect
toolkit().register(Inspect)

-- ---------------------------------------------------------------------------
-- building.build
-- ---------------------------------------------------------------------------

local Build = nil

local function buildSpec(args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, BUILD_ARGS, BUILD_ARGS)
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local name, nameCode, nameDetail = Toolkit.readText(args, "blueprint", { maximum = 64 })
  if name == nil then
    return nil, nameCode, nameDetail
  end
  local token = Building.blueprintToken(name)
  if token == nil then
    return nil, reasons.INVALID_ARGUMENT, "argument \"blueprint\" is not a blueprint token"
  end
  local point, pointCode, pointDetail = squareOf(args, "square", ctx)
  if point == nil then
    return nil, pointCode, pointDetail
  end
  return { token = token, point = point }
end

Building.buildSpec = buildSpec

--- Everything that must hold before one square gets a structure on it.
---
--- The order is the order the refusals matter in: what the command says, then
--- who is at the controls, then the square, then the blueprint, then the
--- materials. Every one of them happens before anything is queued, because
--- there is no demolition action in this mod and a wall raised by mistake stays
--- raised.
local function buildValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(Build.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = buildSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  if ctx.player == nil then
    return nil, reasons.PRECONDITION_FAILED, "no player character"
  end
  -- Building holds the character still and busy in the open; starting one next
  -- to a horde is the situation the reflex guard exists for.
  local threatCode, threatDetail = Toolkit.interruption(ctx)
  if threatCode ~= nil then
    return nil, threatCode, threatDetail
  end
  local close, distanceOrCode, reachDetail = Toolkit.inReach(ctx, spec.point, Building.BUILD_REACH)
  if close == nil then
    return nil, distanceOrCode, reachDetail
  end
  if not close then
    return nil,
      reasons.TARGET_OUT_OF_RANGE,
      string.format(
        "the square is %.1f away and a build reaches %.1f; walking there is a movement command",
        distanceOrCode,
        Building.BUILD_REACH
      )
  end
  local square, squareCode, squareDetail = squareAt(spec.point)
  if square == nil then
    return nil, squareCode, squareDetail
  end
  local state, missingSymbol = Building.squareState(square)
  if state == nil then
    -- The square will not say what is on it, so no postcondition could be
    -- checked afterwards, so the build is never started.
    return Toolkit.unavailable(missingSymbol)
  end
  if state.occupied then
    return nil,
      reasons.SQUARE_OCCUPIED,
      string.format(
        "%s stands on %d:%d:%d, and this agent does not clear squares",
        state.blocker or "something this build would not name",
        spec.point.x,
        spec.point.y,
        spec.point.z
      )
  end
  local known, totalOrSymbol, truncated = Building.knownRecipes(ctx.player)
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
    -- first 128 recipes read" is a different fact from "not known".
    local scope = truncated
        and string.format(" among the %d of %d recipes read", #known, totalOrSymbol)
      or ""
    return nil,
      reasons.RECIPE_UNKNOWN,
      string.format("this character knows no blueprint called %q%s", spec.token, scope)
  end
  local recipe = Building.blueprintObject(entry)
  if recipe == nil then
    return Toolkit.unavailable(Building.BLUEPRINT_SYMBOL)
  end
  local sprite = Building.blueprintSprite(recipe)
  if sprite == nil then
    -- Without the sprite there is nothing to look for on the square afterwards,
    -- and a build nobody could verify must not be started at all.
    return Toolkit.unavailable(Building.SPRITE_SYMBOL)
  end
  local inputs = Building.blueprintInputs(recipe)
  if inputs == nil then
    return Toolkit.unavailable(Building.INPUT_SYMBOL)
  end
  local snapshot = Toolkit.observe(ctx.player)
  local missing = Building.missingFor(inputs, snapshot)
  if #missing > 0 then
    return nil,
      reasons.RECIPE_MATERIALS_MISSING,
      string.format("%s needs %s", entry.name, describeMissing(missing))
  end
  spec.name = entry.name
  spec.recipe = recipe
  spec.sprite = sprite
  spec.inputs = inputs
  Toolkit.state(ctx).build = spec
  return true
end

--- Queue the work, once.
---
--- One timed action, queued here and nowhere else: no branch below re-queues
--- anything, and there is no count argument to fan this out. What comes after
--- is watching and re-reading the square.
local function buildBegin(_, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).build
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no blueprint"
  end
  local square, squareCode, squareDetail = squareAt(spec.point)
  if square == nil then
    return nil, squareCode, squareDetail
  end
  local state, missingSymbol = Building.squareState(square)
  if state == nil then
    return Toolkit.unavailable(missingSymbol)
  end
  if state.occupied then
    -- The square was clear at validate and is not now. Nothing has been queued
    -- yet, so this is still a refusal rather than a failed build.
    return nil,
      reasons.SQUARE_OCCUPIED,
      string.format(
        "%s stands on the square now, which was clear when the command was accepted",
        state.blocker or "something this build would not name"
      )
  end
  spec.objects_before = state.objects
  -- The constructor's argument order is as unverified as the class name, so it
  -- is probed as a closed list and every failure names every candidate.
  local action, actionCode, actionDetail = Toolkit.constructFirst(
    ACTION_NAMES,
    ctx.player,
    spec.recipe,
    spec.point.x,
    spec.point.y,
    spec.point.z
  )
  if action == nil then
    return nil, actionCode, actionDetail
  end
  local queued, queueCode, queueDetail = Toolkit.enqueue(ctx, action)
  if queued == nil then
    return nil, queueCode, queueDetail
  end
  spec.started_ms = ctx.now_ms or 0
  return true
end

--- One poll of the window. Interruption first, so a takeover or a reflex-guard
--- rung stops it mid-build; then the observed square, which ends the window as
--- soon as the structure is standing; then the clock. Closing the window is
--- always "done", and whether "done" is a success is verify's question against
--- the re-read square, never this function's.
local function buildProgress(_, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec = Toolkit.state(ctx).build
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no blueprint"
  end
  local square = squareAt(spec.point)
  if square ~= nil and structureOn(square, spec.sprite) ~= nil then
    return "done"
  end
  local elapsed = (ctx.now_ms or 0) - (spec.started_ms or 0)
  if elapsed >= Building.BUILD_WINDOW_MS then
    return "done"
  end
  return Toolkit.queueProgress(ctx)
end

--- The postcondition: the structure is standing on the square, and the square
--- that read clear beforehand does not any more.
---
--- Both halves, both re-read. The sprite alone is not enough -- a tile that was
--- already there would answer to it, which is why validate refused an occupied
--- square in the first place -- and a square that merely stopped reading clear
--- is not proof this blueprint is what did it. A build that cannot show both is
--- POSTCONDITION_FAILED with the counts, never a success minted off the queue.
local function buildVerify(_, _, _, _, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec = Toolkit.state(ctx).build
  if spec == nil then
    return nil, reasons.INTERNAL_ERROR, "the command recorded no blueprint"
  end
  local square, squareCode, squareDetail = squareAt(spec.point)
  if square == nil then
    return nil, squareCode, squareDetail
  end
  local state, missingSymbol = Building.squareState(square)
  if state == nil then
    return Toolkit.unavailable(missingSymbol)
  end
  local structure = structureOn(square, spec.sprite)
  if structure == nil then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format(
        "%s was queued and no %s stands on %d:%d:%d; the square holds %d object(s), not %d",
        spec.name,
        spec.sprite,
        spec.point.x,
        spec.point.y,
        spec.point.z,
        state.objects,
        (spec.objects_before or 0) + 1
      )
  end
  if not state.occupied then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format(
        "a %s is on %d:%d:%d but the square still reads clear, so this was not observed to be the build",
        spec.sprite,
        spec.point.x,
        spec.point.y,
        spec.point.z
      )
  end
  return {
    kind = "built",
    blueprint = spec.token,
    display_name = spec.name,
    sprite = spec.sprite,
    square = spec.point.ref,
    x = spec.point.x,
    y = spec.point.y,
    z = spec.point.z,
    structure = nameOf(structure) or spec.sprite,
    objects_before = spec.objects_before,
    objects_after = state.objects,
  }
end

Build = toolkit().declare({
  name = "building.build",
  capability = toolkit().CAPABILITY.BUILDING,
  experimental = true,
  -- The build classes themselves are a probed candidate list rather than a
  -- requirement, because their spellings are the uncertainty: what this action
  -- cannot do without is a queue to put one on and a cell to read the square
  -- out of.
  requires = { "ISTimedActionQueue.add", "getCell" },
  -- The declared timeout IS the window: the runtime's lease closes the command
  -- on the same clock the progress step watches.
  timeout_ms = Building.BUILD_WINDOW_MS,
  poll_interval_ms = Building.POLL_MS,
  args = {
    blueprint = { type = ARG.STRING, required = true, max_bytes = 64 },
    -- One square, named by a reference this session minted. There is no
    -- orientation argument and no count: one command raises one structure on
    -- one square, and every extra degree of freedom here is another thing a P4
    -- approval would have to cover.
    square = { type = ARG.REF, required = true, kinds = { square = true } },
  },
  validate = buildValidate,
  begin = buildBegin,
  progress = buildProgress,
  verify = buildVerify,
})

Building.Build = Build
toolkit().register(Build)

return Building
