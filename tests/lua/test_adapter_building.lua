-- PZAgent.Adapters.Building: the two building commands, one bounded window and
-- one square at a time.
--
-- Building is the first thing this mod does that leaves something standing in
-- the world, and this wave ships no way to take one down again. So the cases
-- that carry the weight here are the refusals: a square with something already
-- on it, materials the character does not have, a build that will not say what
-- a blueprint puts down -- and, above all, a build action that was queued and
-- changed nothing. Every success below is minted off a re-read square that
-- carries the blueprint's own sprite AND no longer reads clear; neither half on
-- its own is allowed to mint one.
--
-- Two things this file also pins, because they are the wave's safety argument
-- rather than its behaviour: nothing here queues a second action, and nothing
-- in the mod offers a way to remove what was built.

local ROOT = arg[0]:match("^(.*)test_adapter_building%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
-- The observation model is loaded for one assertion and one only: the token
-- this adapter accepts has to be the token the observation publishes, and the
-- only way to prove that is to run both spellings over the same names.
dofile(Harness.root .. "pz-mod/42/media/lua/shared/PZAgent/ObserveModel.lua")
Support.loadModules(Harness.root, { "Building" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local Toolkit = PZ.Adapters.Toolkit
local Building = PZ.Adapters.Building
local Inspect = Building.Inspect
local Build = Building.Build
local REASON = PZ.Protocol.REASON

-- ---------------------------------------------------------------------------
-- doubles
-- ---------------------------------------------------------------------------

local PLANK = "Base.Plank"
local NAILS = "Base.Nails"
local WALL_SPRITE = "walls_exterior_wooden_01_0"

--- The square a build is aimed at: one step east of where Support.player
--- stands, which is inside Building.BUILD_REACH.
local TARGET = { x = 101, y = 200, z = 0 }
local FAR = { x = 120, y = 200, z = 0 }

--- A blueprint script. Each reader exists only when the case names it, so
--- withholding `sprite` is how "this build will not say what this recipe puts
--- on the square" is expressed, and withholding `inputs` is how "... what it
--- costs" is.
local function blueprint(fields)
  local object = { fields = fields }
  object.getName = function()
    return fields.name
  end
  if fields.sprite ~= nil then
    object.getSpriteName = function()
      return fields.sprite
    end
  end
  if fields.inputs ~= nil then
    local entries = {}
    for index = 1, #fields.inputs do
      local input = fields.inputs[index]
      -- `types = nil` is how "this entry will not say what item types it takes"
      -- is expressed. Every entry used to be built complete, which made the
      -- whole-blueprint refusal unreachable from this file -- the same gap the
      -- crafting suite had, in the file that carries the same reader.
      entries[index] = {
        getCount = function()
          return input.count
        end,
      }
      if input.types ~= nil then
        entries[index].getItems = function()
          return Support.list(input.types)
        end
      end
    end
    object.getInputs = function()
      return Support.list(entries)
    end
  end
  return object
end

--- A structure standing on a square: a sprite to recognise it by and, unless
--- the case withholds it, the solidity that makes the square stop reading
--- clear. `sprite_only` is the phantom case -- something appeared and the
--- square is still open -- which is not a wall and must not be called one.
local function structure(fields)
  fields = fields or {}
  local object = {}
  object.getObjectName = function()
    return fields.name or "Wooden Wall"
  end
  object.getSprite = function()
    return {
      getName = function()
        return fields.sprite or WALL_SPRITE
      end,
    }
  end
  if fields.sprite_only ~= true then
    object.isSolid = function()
      return true
    end
  end
  return object
end

local function installScriptManager(byName)
  local manager = {
    getBuildRecipe = function(_, name)
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

local nextId = 900

local function carried(fullType, name)
  nextId = nextId + 1
  return Support.item({ id = nextId, full_type = fullType, name = name or fullType, weight = 1 })
end

--- One building scene: a character carrying planks and nails one step from a
--- clear square, a blueprint that turns them into a wall, a queue and a build
--- class -- each removable through `options` so a missing engine surface, an
--- occupied square or a swallowed action is one field away.
local function scene(options)
  options = options or {}
  local items = options.items
  if items == nil then
    items = {
      carried(PLANK, "Plank"),
      carried(PLANK, "Plank"),
      carried(PLANK, "Plank"),
      carried(NAILS, "Nails"),
    }
  end
  local main = Support.container(items)
  local player = Support.player({ inventory = main })

  local wall = blueprint({
    name = options.recipe_name or "WoodenWall",
    sprite = options.no_sprite ~= true and WALL_SPRITE or nil,
    inputs = options.no_inputs ~= true and { { types = { PLANK }, count = 2 }, { types = { NAILS }, count = 1 } }
      or nil,
  })
  local scripts = { [wall.fields.name] = wall }
  local recipes = options.recipes or { wall }
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

  local target = Support.square(TARGET.x, TARGET.y, TARGET.z, options.target_objects or {}, options.free)
  if options.blind_square == true then
    -- A square that will not list what is on it: the reading a postcondition
    -- would have to be checked against simply is not there.
    target = { getX = target.getX, getY = target.getY, getZ = target.getZ }
  end
  local squares = {}
  if options.no_square ~= true then
    squares[Support.squareKey(TARGET.x, TARGET.y, TARGET.z)] = target
  end
  squares[Support.squareKey(FAR.x, FAR.y, FAR.z)] = Support.square(FAR.x, FAR.y, FAR.z, {})
  Support.installCell(squares)

  local queue = Support.installQueue(options.queue)
  --- The build action, doing what a build does to a square: the structure ends
  --- up standing on it. `build_noop` makes it return while nothing appears, and
  --- `build_phantom` makes something appear that leaves the square reading
  --- clear -- the two cases every verify here exists for.
  local buildAction = Support.installAction(options.build_class or "ISBuildAction", function()
    if options.build_noop then
      return
    end
    target.objects[#target.objects + 1] = structure({ sprite_only = options.build_phantom })
  end)

  local ctx = Support.context({ player = player, safety = options.safety })
  return {
    player = player,
    ctx = ctx,
    main = main,
    queue = queue,
    buildAction = buildAction,
    target = target,
    wall = wall,
    items = items,
  }
end

local function buildAll(s)
  for index = 1, #s.buildAction.actions do
    s.buildAction.actions[index]:perform()
  end
end

local function targetRef()
  return Support.squareRef(TARGET.x, TARGET.y, TARGET.z)
end

-- ---------------------------------------------------------------------------
-- the token
-- ---------------------------------------------------------------------------

Harness.group("the engine's own answer is asked when the object walk found nothing")
do
  -- Movers are not in `getObjects`. A character, an NPC or a zombie standing on
  -- the tile is invisible to the walk above, and `IsoGridSquare.isFree(false)`
  -- is the only mod-side reading that sees them.
  --
  -- Until now `Support.square` exposed no `isFree` at all, so
  -- `Toolkit.call(square, "isFree", false)` always answered ok=false and the
  -- branch was structurally unreachable: deleting the whole block left every
  -- suite green, and the group named for this very question passed entirely on
  -- the object walk and the truncation branch. That is the harness trap again,
  -- and it is why the double now grants the reader when a case names it.
  --
  -- The sidecar's trap check is a real second lever, but it reads the square
  -- descriptions `PZAgent.Observe` published and decides on a snapshot that is
  -- by construction older than this re-read; and `buildVerify` is no lever at
  -- all, because a wall that really did go up satisfies both its halves. The
  -- mod ships no demolition action, so the wall stays.
  local body = scene({ free = false })
  local state = Building.squareState(body.target)
  equal(state.occupied, true, "a square the engine does not call free is not a clear square")
  contains(state.blocker, "does not read as free", "and the reason says which reading refused it")

  local buildArgs = { blueprint = "WoodenWall", square = targetRef() }
  local refused, code, detail = Build:validate(buildArgs, body.ctx)
  isNil(refused, "so the build is refused before anything is queued")
  equal(code, REASON.SQUARE_OCCUPIED, "with the occupied-square reason")
  contains(detail, "free", "naming what stood in the way")
  equal(#body.queue.added, 0, "and nothing reached the game's queue")

  -- The control: the same square with the engine answering free is buildable,
  -- so the assertions above are about the reading and not about the scene.
  local clear = scene({ free = true })
  equal(Building.squareState(clear.target).occupied, false, "a square the engine calls free reads clear")
  ok(Build:validate(buildArgs, clear.ctx), "and the build validates on it")
end

Harness.group("a blueprint half of whose inputs answer is not a readable blueprint")
do
  -- `Building.blueprintInputs` is `Crafting.recipeInputs` copied, not shared --
  -- the two files duplicate the reader -- so the refusal has to be pinned
  -- twice. Here the cost is higher than a wasted craft: `building.build` raises
  -- a structure, the mod ships no demolition action, and a build judged on a
  -- subset of its requirements is one the character starts and cannot finish.
  local partly = blueprint({
    name = "WoodenWall",
    sprite = WALL_SPRITE,
    inputs = { { types = { PLANK }, count = 2 }, { count = 1 } },
  })
  isNil(Building.blueprintInputs(partly), "one unreadable input makes the whole blueprint unreadable")

  local whole = blueprint({
    name = "WoodenWall",
    sprite = WALL_SPRITE,
    inputs = { { types = { PLANK }, count = 2 }, { types = { NAILS }, count = 1 } },
  })
  local inputs = Building.blueprintInputs(whole)
  ok(inputs ~= nil, "a blueprint every input of which answers is readable")
  equal(#inputs, 2, "carrying both")
end

Harness.group("a blueprint is named by the same token on both sides of the wire")
do
  equal(Building.blueprintToken("WoodenWall"), "WoodenWall", "an identifier-shaped name is its own token")
  equal(Building.blueprintToken("Wooden Wall Level 1"), "Wooden_Wall_Level_1", "spaces become underscores")
  isNil(Building.blueprintToken("Mur en bois brûlé"), "a name the reference alphabet cannot carry has no token")
  isNil(Building.blueprintToken(""), "and neither does an empty name")
  isNil(Building.blueprintToken(string.rep("a", 65)), "nor one past the segment bound")

  for _, name in ipairs({ "WoodenWall", "Wooden Wall Level 1", "Base.WoodenDoor", "Mur en bois brûlé" }) do
    equal(
      Building.blueprintToken(name),
      PZ.ObserveModel.recipeToken(name),
      "the adapter and the observation spell " .. name .. " the same way"
    )
  end
end

-- ---------------------------------------------------------------------------
-- reading a square
-- ---------------------------------------------------------------------------

Harness.group("a square is read before it is built on, and 'not clear' is wider than 'named'")
do
  local s = scene()
  local clear = Building.squareState(s.target)
  equal(clear.occupied, false, "an empty square reads clear")
  equal(clear.objects, 0, "with nothing on it")
  isNil(clear.blocker, "and nothing to name")

  local occupied = scene({ target_objects = { structure({ name = "Wooden Wall" }) } })
  local state = Building.squareState(occupied.target)
  equal(state.occupied, true, "a square with something solid on it does not")
  equal(state.blocker, "Wooden Wall", "and the thing in the way is named")

  local shelf = scene({
    target_objects = { Support.worldObject({ name = "Shelf", container = Support.container({}) }) },
  })
  equal(Building.squareState(shelf.target).occupied, true, "a container is in the way too")

  local blind = scene({ blind_square = true })
  local unreadable, symbol = Building.squareState(blind.target)
  isNil(unreadable, "a square that will not list its objects is not reported clear")
  equal(symbol, Building.OBJECTS_SYMBOL, "it names the reader that was missing")

  -- More objects than one reading walks. Nothing was named, and the square is
  -- still not shown to be clear: erring toward the refusal is what this whole
  -- adapter is arranged around.
  local crowd = {}
  for index = 1, Toolkit.MAX_SQUARE_OBJECTS + 1 do
    crowd[index] = Support.worldObject({ name = "Rubble " .. index })
  end
  local crowded = scene({ target_objects = crowd })
  local packed = Building.squareState(crowded.target)
  equal(packed.truncated, true, "the walk says it stopped short")
  equal(packed.occupied, true, "and a square nobody could read whole is not a clear one")
end

-- ---------------------------------------------------------------------------
-- building.inspect
-- ---------------------------------------------------------------------------

Harness.group("inspect answers what could be built and what it would cost")
do
  local s = scene()
  ok(Inspect:validate({}, s.ctx), "the read validates with no arguments")
  equal(Inspect:begin({}, s.ctx), "done", "and finishes inside begin, queueing nothing")
  equal(#s.queue.added, 0, "nothing reached the game's queue")

  local report = Inspect:verify(nil, Toolkit.observe(s.player), {}, s.ctx)
  ok(report ~= nil, "the reading itself is the evidence")
  equal(report.kind, "buildable", "named for what was read")
  equal(report.listed, 1, "one blueprint was listed")
  equal(report.blueprints[1].blueprint, "WoodenWall", "carrying the token a command would name")
  equal(report.blueprints[1].sprite, WALL_SPRITE, "and the sprite that will prove it went up")
  equal(report.blueprints[1].ready, true, "three planks and a nail cover two planks and a nail")
  equal(report.ready, 1, "so one blueprint is ready")
  equal(report.judged, 1, "and one was judged at all")
  isNil(report.blueprints[1].missing, "a ready blueprint names nothing missing")
  isNil(report.square, "with no square named the answer is about the character, not a place")

  local short = scene({ items = { carried(PLANK, "Plank") } })
  local shortReport = Inspect:verify(nil, Toolkit.observe(short.player), {}, short.ctx)
  equal(shortReport.blueprints[1].ready, false, "one plank does not cover the blueprint")
  equal(#shortReport.blueprints[1].missing, 2, "both shortfalls are named")
  equal(shortReport.blueprints[1].missing[1].need, 2, "with what it needs")
  equal(shortReport.blueprints[1].missing[1].held, 1, "and what the character holds")

  -- The third state: the blueprint is known and its materials cannot be read.
  local blind = scene({ no_inputs = true })
  local blindReport = Inspect:verify(nil, Toolkit.observe(blind.player), {}, blind.ctx)
  equal(blindReport.listed, 1, "the blueprint is still listed")
  isNil(blindReport.blueprints[1].ready, "with no verdict at all -- absent never reads as ready")
  equal(blindReport.judged, 0, "and the report says none was judged")

  -- A recipe with no sprite is a craft, not a blueprint. It is not listed here
  -- and nothing about it went unread, so it is not counted as unreadable
  -- either -- crafting.inspect is where that recipe is answered for.
  local craft = scene({ no_sprite = true })
  local craftReport = Inspect:verify(nil, Toolkit.observe(craft.player), {}, craft.ctx)
  equal(craftReport.listed, 0, "a recipe that places nothing is no blueprint")
  equal(craftReport.unreadable, 0, "and it is not reported as something nobody could read")
  equal(craftReport.known_total, 1, "while the engine's own count still travels")

  -- Known, and nothing to look at. That IS an unread entry, and saying so is
  -- what keeps a short list from reading as a small repertoire.
  local scriptless = scene({ no_scripts = true })
  local scriptlessReport = Inspect:verify(nil, Toolkit.observe(scriptless.player), {}, scriptless.ctx)
  equal(scriptlessReport.listed, 0, "nothing could be listed")
  equal(scriptlessReport.unreadable, 1, "and the entry nobody could resolve is counted")
end

Harness.group("inspect answers about one square when it is given one")
do
  local s = scene()
  local args = { square = targetRef() }
  ok(Inspect:validate(args, s.ctx), "a named square validates")
  local report = Inspect:verify(nil, Toolkit.observe(s.player), args, s.ctx)
  equal(report.square.ref, targetRef(), "the report names the square it was asked about")
  equal(report.square.free, true, "an empty square is free to build on")
  equal(report.square.x, TARGET.x, "carrying the coordinates a build would name back")
  isNil(report.square.blocker, "with nothing in the way")

  local taken = scene({ target_objects = { structure({ name = "Wooden Wall" }) } })
  local takenReport = Inspect:verify(nil, Toolkit.observe(taken.player), args, taken.ctx)
  equal(takenReport.square.free, false, "an occupied square is not")
  equal(takenReport.square.blocker, "Wooden Wall", "and the answer names what is in the way")

  local gone = scene({ no_square = true })
  local refused, code = Inspect:validate(args, gone.ctx)
  isNil(refused, "a square the cell will not hand over is refused")
  equal(code, REASON.TARGET_NOT_LOADED, "as unloaded rather than as a capability gap")

  local other = scene()
  local foreign = { square = Support.squareRef(TARGET.x, TARGET.y, TARGET.z, "11111111-2222-3333-4444-555555555555") }
  local stale, staleCode = Inspect:validate(foreign, other.ctx)
  isNil(stale, "a reference minted by another session is refused")
  equal(staleCode, REASON.INVALID_REF, "because its coordinates belong to another reading")
end

Harness.group("inspect is bounded, and honest about stopping short")
do
  local many = {}
  for index = 1, Building.MAX_LISTED + 3 do
    many[index] = blueprint({
      name = string.format("Wall%03d", index),
      sprite = WALL_SPRITE,
      inputs = { { types = { PLANK }, count = 1 } },
    })
  end
  local s = scene({ recipes = many })
  local report = Inspect:verify(nil, Toolkit.observe(s.player), {}, s.ctx)
  equal(report.listed, Building.MAX_LISTED, "the listing stops at the declared cap")
  equal(report.known_total, Building.MAX_LISTED + 3, "while the engine's count travels whole")
  equal(report.truncated, true, "and the report says it stopped short")

  local small = Inspect:verify(nil, Toolkit.observe(s.player), { limit = 2 }, s.ctx)
  equal(small.listed, 2, "a caller may ask for fewer")
  equal(small.truncated, true, "and is still told the rest exist")

  local whole = scene()
  local one = Inspect:verify(nil, Toolkit.observe(whole.player), {}, whole.ctx)
  equal(one.truncated, false, "a listing that reached the end says so too")

  local refused, code = Inspect:validate({ limit = Building.MAX_LISTED + 1 }, s.ctx)
  isNil(refused, "a limit past the ceiling is refused")
  equal(code, REASON.INVALID_ARGUMENT, "as an invalid argument")

  local unknownArg, unknownCode = Inspect:validate({ blueprint = "WoodenWall" }, s.ctx)
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
-- building.build
-- ---------------------------------------------------------------------------

Harness.group("build refuses before it raises anything, naming what is wrong")
do
  local s = scene()
  local function refuse(args, ctx)
    local accepted, code, detail = Build:validate(args, ctx or s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "building nothing in particular is not a command")

  code = refuse({ blueprint = "WoodenWall" })
  equal(code, REASON.INVALID_ARGUMENT, "and neither is building it nowhere in particular")

  code = refuse({ square = targetRef() })
  equal(code, REASON.INVALID_ARGUMENT, "nor naming a square with nothing to put on it")

  code = refuse({ blueprint = "WoodenWall", square = targetRef(), north = true })
  equal(code, REASON.INVALID_ARGUMENT, "an undeclared argument is refused rather than dropped")

  code = refuse({ blueprint = "Mur brûlé", square = targetRef() })
  equal(code, REASON.INVALID_ARGUMENT, "a name the token alphabet cannot carry is not a blueprint token")

  local detail
  code, detail = refuse({ blueprint = "WoodenFence", square = targetRef() })
  equal(code, REASON.RECIPE_UNKNOWN, "a blueprint this character has not learned is refused as unknown")
  contains(detail, "WoodenFence", "naming what was asked for")

  local args = { blueprint = "WoodenWall", square = targetRef() }

  local taken = scene({ target_objects = { structure({ name = "Wooden Wall" }) } })
  code, detail = refuse(args, taken.ctx)
  equal(code, REASON.SQUARE_OCCUPIED, "a square with something on it is refused")
  contains(detail, "Wooden Wall", "naming what stands there")
  contains(detail, "does not clear squares", "and saying plainly that clearing it is not on offer")

  local blind = scene({ blind_square = true })
  code, detail = refuse(args, blind.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "a square nobody can read is never built on")
  contains(detail, "getObjects", "because the postcondition could not be checked afterwards")

  local gone = scene({ no_square = true })
  code = refuse(args, gone.ctx)
  equal(code, REASON.TARGET_NOT_LOADED, "an unloaded square is a place to come back to, not a missing API")

  local far = scene()
  code, detail = refuse({ blueprint = "WoodenWall", square = Support.squareRef(FAR.x, FAR.y, FAR.z) }, far.ctx)
  equal(code, REASON.TARGET_OUT_OF_RANGE, "a square out of reach is refused rather than walked to")
  contains(detail, "movement command", "saying whose job the walk is")

  local bare = scene({ items = { carried(PLANK, "Plank") } })
  code, detail = refuse(args, bare.ctx)
  equal(code, REASON.RECIPE_MATERIALS_MISSING, "materials short of the blueprint refuse it")
  contains(detail, "Base.Plank", "naming what is short")
  contains(detail, "1 of 2", "with how short it is")

  local nailless = scene({ items = { carried(PLANK, "Plank"), carried(PLANK, "Plank") } })
  code, detail = refuse(args, nailless.ctx)
  equal(code, REASON.RECIPE_MATERIALS_MISSING, "one ingredient short is short")
  contains(detail, "Base.Nails", "and the refusal names which one")

  local spriteless = scene({ no_sprite = true })
  code, detail = refuse(args, spriteless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "a blueprint whose sprite cannot be read cannot be verified")
  contains(detail, "getSpriteName", "so the refusal names the readers that were probed")

  local inputless = scene({ no_inputs = true })
  code, detail = refuse(args, inputless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "and one whose materials cannot be read is not judged ready")
  contains(detail, "getInputs", "naming that reader instead")

  local scriptless = scene({ no_scripts = true })
  code, detail = refuse(args, scriptless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "a name with nothing to look it up in is a capability gap")
  contains(detail, "getBuildRecipe", "naming the lookup that was probed")

  local listless = scene({ no_known = true })
  code, detail = refuse(args, listless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "an unreadable known-recipe list is a capability gap")
  contains(detail, "getKnownRecipes", "never 'this character knows no such blueprint'")

  local threatened = scene({ safety = Support.threatened() })
  code = refuse(args, threatened.ctx)
  equal(code, REASON.THREAT_INTERRUPTED, "building holds the character still, so a horde refuses it")

  local takenOver = scene({ safety = Support.takenOver() })
  code = refuse(args, takenOver.ctx)
  equal(code, REASON.USER_TAKEOVER, "and the player at the controls outranks the command")

  local queueless = scene()
  Support.removeQueue()
  code, detail = refuse(args, queueless.ctx)
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "with no action queue there is nowhere to put the work")
  contains(detail, "ISTimedActionQueue.add", "naming the symbol the command needs")
  Support.installQueue()

  -- Nothing above reached the queue, which is the whole point of doing all of
  -- it before begin: a wall raised by mistake stays raised.
  equal(#s.queue.added, 0, "not one refusal queued anything")
end

Harness.group("a build is proved by the structure on the square, never by the queue")
do
  local args = { blueprint = "WoodenWall", square = targetRef() }
  local s = scene()
  ok(Build:validate(args, s.ctx), "a known blueprint on a clear square in reach validates")
  ok(Build:begin(args, s.ctx), "and the work is queued")
  equal(#s.buildAction.actions, 1, "one build action for one structure")
  equal(#s.queue.added, 1, "and it went to the game's own queue")

  -- Queued and nothing performed: the square is as it was.
  local nothing, code, detail = Build:verify(nil, nil, args, s.ctx)
  isNil(nothing, "an action that was queued and never ran proves nothing")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed its postcondition")
  contains(detail, WALL_SPRITE, "with the sprite that was looked for")
  contains(detail, "holds 0 object(s)", "and the honest count")

  buildAll(s)
  local proof = Build:verify(nil, nil, args, s.ctx)
  ok(proof ~= nil, "the wall on the square is the evidence")
  equal(proof.kind, "built", "named for what was observed")
  equal(proof.blueprint, "WoodenWall", "carrying the blueprint token")
  equal(proof.sprite, WALL_SPRITE, "and the sprite it was recognised by")
  equal(proof.square, targetRef(), "with the square it stands on")
  equal(proof.structure, "Wooden Wall", "named as the game names it")
  equal(proof.objects_before, 0, "off a measured before")
  equal(proof.objects_after, 1, "and a measured after")

  -- The anti-lie case: something appeared and the square is still open ground.
  local phantom = scene({ build_phantom = true })
  ok(Build:validate(args, phantom.ctx), "the phantom case validates")
  ok(Build:begin(args, phantom.ctx), "and starts")
  buildAll(phantom)
  local unearned, phantomCode, phantomDetail = Build:verify(nil, nil, args, phantom.ctx)
  isNil(unearned, "a sprite on a square that still reads clear is not a wall")
  equal(phantomCode, REASON.POSTCONDITION_FAILED, "it failed its postcondition")
  contains(phantomDetail, "still reads clear", "with the reason spelled out")

  local silent = scene({ build_noop = true })
  ok(Build:validate(args, silent.ctx), "the swallowed-action case validates")
  ok(Build:begin(args, silent.ctx), "and starts")
  buildAll(silent)
  local swallowed, silentCode = Build:verify(nil, nil, args, silent.ctx)
  isNil(swallowed, "an action that ran and changed nothing is not a success")
  equal(silentCode, REASON.POSTCONDITION_FAILED, "and says so")
end

Harness.group("one command raises one structure, and nothing re-queues it")
do
  local args = { blueprint = "WoodenWall", square = targetRef() }
  local s = scene()
  ok(Build:validate(args, s.ctx), "the command validates")
  ok(Build:begin(args, s.ctx), "and the work is queued")
  equal(#s.buildAction.actions, 1, "as exactly one action")

  local nothing = Build:verify(nil, nil, args, s.ctx)
  isNil(nothing, "an unperformed action is not a build")
  equal(#s.buildAction.actions, 1, "and the failure re-queued nothing")
  equal(#s.queue.added, 1, "the queue still holds the one entry it was given")

  buildAll(s)
  ok(Build:verify(nil, nil, args, s.ctx) ~= nil, "the performed one is a build")
  equal(#s.buildAction.actions, 1, "and the success queued nothing further")

  isNil(Build.args.count, "there is no count argument to fan one command out with")
  isNil(Build.args.radius, "and nothing that would let one command cover an area")

  -- The square went from clear to occupied between accept and begin. Nothing
  -- has been queued yet, so this is still a refusal.
  local raced = scene()
  ok(Build:validate(args, raced.ctx), "a clear square validates")
  raced.target.objects[1] = structure({ name = "Wooden Crate" })
  local refused, code, detail = Build:begin(args, raced.ctx)
  isNil(refused, "and a square that filled up in between is refused at begin")
  equal(code, REASON.SQUARE_OCCUPIED, "as an occupied square")
  contains(detail, "Wooden Crate", "naming what arrived")
  equal(#raced.buildAction.actions, 0, "with nothing queued")
end

Harness.group("the window is bounded by the clock as well as by the queue")
do
  local args = { blueprint = "WoodenWall", square = targetRef() }
  local s = scene()
  ok(Build:validate(args, s.ctx), "the command validates")
  ok(Build:begin(args, s.ctx), "and starts")
  equal(Build:progress(args, s.ctx), "running", "an unfinished build keeps the window open")

  buildAll(s)
  equal(Build:progress(args, s.ctx), "done", "the observed structure closes it")

  local slow = scene()
  ok(Build:validate(args, slow.ctx), "the slow case validates")
  ok(Build:begin(args, slow.ctx), "and starts")
  slow.ctx.now_ms = Support.NOW + Building.BUILD_WINDOW_MS
  equal(Build:progress(args, slow.ctx), "done", "and the clock closes it even with nothing raised")
  local nothing, code = Build:verify(nil, nil, args, slow.ctx)
  isNil(nothing, "closing the window is not succeeding")
  equal(code, REASON.POSTCONDITION_FAILED, "the postcondition still has to hold")

  local interrupted = scene()
  ok(Build:validate(args, interrupted.ctx), "the interruption case validates")
  ok(Build:begin(args, interrupted.ctx), "and starts")
  interrupted.ctx.safety = Support.takenOver()
  local status, takeoverCode = Build:progress(args, interrupted.ctx)
  isNil(status, "a takeover mid-build stops the poll")
  equal(takeoverCode, REASON.USER_TAKEOVER, "naming the player's own decision")

  local threatened = scene()
  ok(Build:validate(args, threatened.ctx), "the threat case validates")
  ok(Build:begin(args, threatened.ctx), "and starts")
  threatened.ctx.safety = Support.threatened()
  local halted, threatCode = Build:progress(args, threatened.ctx)
  isNil(halted, "and a horde arriving mid-build stops it too")
  equal(threatCode, REASON.THREAT_INTERRUPTED, "at the rung the reflex guard names")
end

Harness.group("known blueprints may arrive as objects instead of names")
do
  local s = scene({ known_objects = true, no_scripts = true })
  local report = Inspect:verify(nil, Toolkit.observe(s.player), {}, s.ctx)
  equal(report.listed, 1, "a collection of recipe objects lists the same way")
  equal(report.blueprints[1].ready, true, "and is judged without a script manager to look anything up in")
  ok(
    Build:validate({ blueprint = "WoodenWall", square = targetRef() }, s.ctx),
    "and a build against it validates"
  )
end

Harness.group("both commands declare themselves the way the runtime demands")
do
  equal(Inspect.action, "building.inspect", "the inspect names its protocol action")
  equal(Build.action, "building.build", "and so does the build")
  ok(PZ.Protocol.isKnownAction(Inspect.action), "the protocol knows the inspect")
  ok(PZ.Protocol.isKnownAction(Build.action), "and the build")
  ok(PZ.Protocol.READ_ONLY_ACTIONS[Inspect.action] == true, "the inspect is read-only in the protocol")
  isNil(PZ.Protocol.READ_ONLY_ACTIONS[Build.action], "and the build is not")

  -- The read-only half declares no capability, exactly as world.inspect,
  -- container.inspect, inventory.search and crafting.inspect do and for their
  -- reason: a probe over accessors that never appear in the game's Lua would
  -- report the whole thing unsupported on a healthy install. The build is the
  -- half a live run can confirm, so it is the half that rides the capability,
  -- and it is the half withheld until one does.
  isNil(Inspect.capability, "the read-only half is gated on the readers it needs, not on a probe")
  equal(Build.capability, "building", "while the build rides the building capability")
  equal(Build.experimental, true, "which is experimental until a live build promotes it")
  equal(Inspect.experimental, false, "and the ungated half claims no ceiling it has nowhere to publish")

  -- The agreement Combat states for its engage window: the runtime's lease and
  -- the clock the progress step watches are the same number.
  equal(Build.timeout_ms, Building.BUILD_WINDOW_MS, "the declared timeout is the build window")
  equal(Build.args.blueprint.required, true, "a build must name its blueprint")
  equal(Build.args.square.required, true, "and the square it goes on")
  ok(Build.args.square.kinds.square == true, "which is a square reference and nothing else")
end

Harness.group("nothing in this wave can take down what was built")
do
  -- The permanence is the safety argument, so it is pinned rather than
  -- described. If a demolition action is ever added it will be a wave of its
  -- own, with its own authority, and this assertion is where that conversation
  -- starts.
  local forbidden = {
    "building.demolish",
    "building.remove",
    "building.destroy",
    "building.dismantle",
    "world.demolish",
  }
  for index = 1, #forbidden do
    isNil(PZ.Adapters.BY_NAME[forbidden[index]], "no adapter implements " .. forbidden[index])
    ok(not PZ.Protocol.isKnownAction(forbidden[index]), "and the protocol knows no " .. forbidden[index])
  end

  for name in pairs(PZ.Adapters.BY_NAME) do
    ok(
      name:find("demolish", 1, true) == nil
        and name:find("dismantle", 1, true) == nil
        and name:find("destroy", 1, true) == nil,
      "no published adapter is named for taking something down: " .. name
    )
  end
end

Harness.finish("adapter_building")
