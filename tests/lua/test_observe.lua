-- PZAgent.Observe: the half that touches the game.
--
-- The doubles in support/observe_support.lua expose exactly the accessors a
-- test names, so removing one is how "this build does not have that method" is
-- expressed. Every group below is therefore about the same question: when the
-- engine does not answer, does the reader leave the field out, or does it make
-- one up?

local Harness = dofile((arg[0]:match("^(.*)test_observe%.lua$") or "") .. "support/harness.lua")
local Mock = dofile((arg[0]:match("^(.*)test_observe%.lua$") or "") .. "support/mock_game.lua")
local Support = dofile((arg[0]:match("^(.*)test_observe%.lua$") or "") .. "support/observe_support.lua")
local PZ = Harness.loadModules()
Support.loadModules(Harness.root)

local Observe = PZ.Observe
local Model = PZ.ObserveModel
local Json = PZ.Json

local equal, ok, isNil, same = Harness.equal, Harness.ok, Harness.isNil, Harness.same

local NOW = 1700000000000
local SESSION = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33"

--- Install a build probe, since PZAgent.Heartbeat reads the version from the
--- game and an observation without a build is refused.
local function installCore(version)
  local core = {
    getVersionNumber = function()
      return version
    end,
  }
  getCore = function()
    return core
  end
  return function()
    getCore = nil
  end
end

--- An agent over a mock filesystem, with an accepted session.
local function newAgent(options)
  options = options or {}
  local fs = Mock.newFilesystem()
  local agent = {
    ipc = PZ.Ipc.new({ fileApi = fs.api, clock = function()
      return NOW
    end }),
    safety = PZ.Safety.newState(),
    session = PZ.Session.new(),
    sequence = PZ.Sequence.new(),
  }
  if options.session ~= false then
    agent.session:offer({
      protocol_version = "1.0",
      session_id = SESSION,
      created_at_ms = NOW - 10,
      nonce = "nonce-one",
      mode = "observe",
    }, NOW)
  end
  return agent, fs
end

--- A player with an inventory, a worn bag, wounds and moodles.
local function furnishedPlayer()
  local bottle = Support.item({ id = 5, full_type = "Base.WaterBottleFull", name = "Water Bottle", weight = 0.8 })
  local satchel = Support.item({
    id = 99,
    full_type = "Base.Bag_Satchel",
    name = "Satchel",
    weight = 1,
    contents = { bottle },
  })
  local apple = Support.item({
    id = 1,
    full_type = "Base.Apple",
    name = "Apple",
    category = "Food",
    weight = 0.3,
    hunger_change = -15,
    cooked = false,
  })
  local backpack = Support.item({ id = 77, full_type = "Base.Bag_BigHikingBag", name = "Big Hiking Bag", contents = {
    Support.item({ id = 8, full_type = "Base.Nails", name = "Nails", weight = 0.1 }),
  } })
  return Support.player({
    x = 100.5,
    y = 200.5,
    z = 0,
    angle = 90,
    stats = Support.stats({ hunger = 0.4, thirst = 0.1, endurance = 0.9 }),
    body = Support.bodyDamage({
      Support.bodyPart({ part = "Left_Arm", bleeding = true, bitten = true, health = 40 }),
      Support.bodyPart({ part = "Torso", health = 100 }),
    }, 78),
    moodles = Support.moodles({ { "Hungry", 2 }, { "Bored", 1 } }),
    inventory = Support.container({ apple, satchel }),
    worn = { Support.worn("Back", backpack) },
    primary = bottle,
    can_see = true,
  }), bottle
end

Harness.group("outside the game every probe reports its own absence")
do
  local fields, buildReason = Observe.gameFields()
  isNil(fields.build, "with no getCore there is no build to report")
  Harness.contains(buildReason, "getCore", "and the probe's own reason travels with the gap, not a vague restatement")
  isNil(fields.save_key, "with no getWorld there is no save identity")
  isNil(fields.speed, "and no clock to read a speed from")

  isNil(Observe.positionOf(nil), "an object that is not there has no position")
  local playerFields, reason = Observe.playerFields({})
  isNil(playerFields, "a character that reports no coordinates yields no player block")
  Harness.contains(reason, "position", "and the reason says so")

  same(Observe.playerStats({}), {}, "a character with no stats object contributes no stats")
  same(Observe.playerMoodles({}), {}, "and no moodles")
  same(Observe.playerWounds({}), {}, "and no wounds")

  local roots, rootsReason = Observe.inventoryRoots({})
  isNil(roots, "a character with no inventory yields no container roots")
  Harness.contains(rootsReason, "inventory", "and the reason names it")

  local nearby = Observe.nearbyObjects({ x = 0, y = 0, z = 0 })
  same(nearby.objects, {}, "with no cell there is nothing nearby")
  equal(nearby.truncated, false, "and nothing was cut short either")
end

Harness.group("the game block reports what the engine actually answered")
do
  local removeCore = installCore("42.20")
  local removeWorld = Support.installWorld("Muldraugh, KY/survivor")
  local removeTime = Support.installGameTime({ speed = 2, hour = 13, minute = 45, paused = false })

  local fields = Observe.gameFields()
  equal(fields.build, "42.20", "the build comes from the game")
  equal(fields.save_key, "Muldraugh, KY/survivor", "the save key is read whole")
  equal(fields.speed, 2, "the speed multiplier is read")
  equal(fields.paused, false, "and the pause flag")
  equal(fields.world_time, "1993-06-09T13:45", "the world clock is formatted from the components it read")

  local game = Model.game(fields)
  ok(game.save_id:match("^%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x$") ~= nil, "and only its hash reaches the document")
  Harness.notEqual(game.save_id, Model.saveId("Muldraugh, KY/other"), "two saves hash differently")

  removeTime()
  Support.installGameTime({ speed = 0 })
  local stopped = Observe.gameFields()
  equal(stopped.paused, true, "with no pause accessor a zero multiplier is the game's own reading of paused")

  getGameTime = nil
  local blind = Observe.gameFields()
  isNil(blind.paused, "with no clock at all nothing is claimed about the pause state")

  removeWorld()
  removeCore()
end

Harness.group("multiplayer is read, and silence is never read as single player")
do
  -- The sidecar arms only on a positive false and refuses on nil exactly as it
  -- refuses on true, so a reading of "no accessor" must never come back false.
  isClient = nil
  isServer = nil
  isNil(Observe.multiplayer(), "with neither accessor the answer is unknown, not single player")

  isClient = function() return false end
  isServer = function() return false end
  equal(Observe.multiplayer(), false, "both answering false is single player")

  isClient = function() return true end
  isServer = function() return false end
  equal(Observe.multiplayer(), true, "a multiplayer client says so")

  isClient = function() return false end
  isServer = function() return true end
  equal(Observe.multiplayer(), true, "and so does a host")

  isClient = function() error("boom") end
  isServer = nil
  isNil(Observe.multiplayer(), "an accessor that throws leaves the answer unknown rather than false")

  isClient = function() return "yes" end
  isServer = nil
  isNil(Observe.multiplayer(), "and a non-boolean answer is no answer at all")

  -- Only one of the two present is still a real reading: a single-player
  -- session is neither a client nor a server, so one negative is enough.
  isClient = function() return false end
  isServer = nil
  equal(Observe.multiplayer(), false, "one accessor answering false is a reading")

  isClient = nil
  isServer = nil
  local unknown = Model.game({ build = "42.20", save_key = "a/b", speed = 1, paused = false })
  isNil(unknown.multiplayer, "an unread value is omitted from the document, never written as false")
  local single = Model.game({ build = "42.20", save_key = "a/b", speed = 1, paused = false, multiplayer = false })
  equal(single.multiplayer, false, "and a read value is carried")
end

Harness.group("the player block carries the scalars and omits the rest")
do
  local player = furnishedPlayer()
  local fields = Observe.playerFields(player)
  equal(fields.present, true, "the character is present")
  equal(fields.alive, true, "and alive, because isDead answered")
  equal(fields.position.x, 100.5, "x")
  equal(fields.position.z, 0, "the floor is an integer")
  equal(fields.position.direction, "E", "ninety degrees is east")
  equal(fields.stats.hunger, 0.4, "hunger as the game reports it")
  equal(fields.stats.health, 0.78, "overall body health scaled out of a hundred")
  isNil(fields.stats.fatigue, "a stat the Stats object does not expose is absent")
  equal(fields.moodles.Hungry, 2, "moodles are keyed by their type name")

  local dead = Observe.playerFields(Support.player({ dead = true }))
  equal(dead.alive, false, "a dead character says so")

  local mute = Observe.playerFields({
    getX = function() return 1 end,
    getY = function() return 2 end,
    getZ = function() return 0 end,
  })
  equal(mute.alive, false, "a character whose liveness cannot be read is not assumed to be alive")
  isNil(mute.position.direction, "and its facing is omitted")
end

Harness.group("a bleeding limb reaches the document, because the guard keys on it")
do
  local player = furnishedPlayer()
  local wounds = Observe.playerWounds(player)
  equal(#wounds, 2, "every body part is described")
  equal(wounds[1].part, "Left_Arm", "the part names itself")
  equal(wounds[1].bleeding, true, "and reports that it is bleeding")
  equal(wounds[1].bitten, true, "and that it was bitten")
  ok(wounds[1].severity > 0.5, "with a severity derived from its health")

  local shaped = Model.wounds(SESSION, wounds, { wounds_truncated = false, wounds_omitted = 0 })
  equal(#shaped, 1, "the undamaged part produces no wound")
  equal(shaped[1].kind, "bite", "the bite names the wound")
  equal(shaped[1].bleeding, true, "and the bleeding survives into the document")

  local unnamed = Observe.playerWounds(Support.player({
    body = Support.bodyDamage({ Support.bodyPart({ health = 10 }) }),
  }))
  equal(unnamed[1].part, "part-0", "a part whose type cannot be read is identified by position, not by a guess")
end

Harness.group("the inventory walk nests, names its slots and finds the hands")
do
  local player = furnishedPlayer()
  local roots = Observe.inventoryRoots(player)
  equal(#roots, 2, "the main inventory and the worn bag are separate roots")
  equal(roots[1].kind, "player_main", "the first root is the main inventory")
  equal(roots[2].kind, "worn", "the worn bag is a worn container")
  equal(roots[2].slot, "Back", "carrying the slot it is worn in")
  equal(roots[2].runtime_id, 77, "and the id of the item that provides it")

  local satchel = roots[1].items[2]
  equal(satchel.container.kind, "carried", "a bag in the inventory is a carried container")
  equal(satchel.container.runtime_id, 99, "identified by the bag item")
  equal(satchel.container.items[1].hand, "primary", "the item in the primary hand is marked wherever it lives")
  equal(satchel.container.items[1].equipped, true, "and counts as equipped even though isEquipped says otherwise")

  -- Two bottles the game would describe identically. Only the one the hand
  -- actually holds may be marked, or a transfer moves the wrong object.
  local held = Support.item({ id = 5, full_type = "Base.WaterBottleFull", name = "Water Bottle", weight = 0.8 })
  local twin = Support.item({ id = 5, full_type = "Base.WaterBottleFull", name = "Water Bottle", weight = 0.8 })
  local twinned = Observe.inventoryRoots(Support.player({
    inventory = Support.container({ twin, held }),
    primary = held,
  }))[1]
  isNil(twinned.items[1].hand, "an identical-looking item is not the held one")
  equal(twinned.items[2].hand, "primary", "the hand is matched by object identity, not by what the item is called")

  equal(roots[1].items[1].food.hunger_change, -15, "a food item carries its domain payload")
  isNil(roots[1].items[2].food, "and an item that is not food carries none")

  local document = Model.build({
    session_id = SESSION,
    seq = 0,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(player),
    inventory = roots,
  })
  local containers = document.inventory.containers
  equal(#containers, 3, "three containers reach the document")
  equal(containers[1].ref, "container:" .. SESSION .. ":carried:99", "the nested bag")
  equal(containers[1].parent_ref, "container:" .. SESSION .. ":player-main", "whose parent is the main inventory")
  equal(document.player.hands.primary, "item:" .. SESSION .. ":carried:99:5:0", "and the held bottle is referenced")

  local many = {}
  for index = 1, Model.MAX_ITEMS_PER_CONTAINER + 5 do
    many[index] = Support.item({ id = index, name = "Nail" })
  end
  local bounded = Support.container(many)
  local node = Observe.inventoryRoots(Support.player({ inventory = bounded }))[1]
  equal(#node.items, Model.MAX_ITEMS_PER_CONTAINER, "the reader stops at the cap")
  equal(node.truncated, true, "and declares that it did")
  equal(node.dropped, 5, "with the number it did not look at")
end

Harness.group("the inventory walk is bounded before the engine is asked, not after")
do
  -- Bags inside bags inside bags: the per-container cap alone would let this
  -- cost hundreds of thousands of engine calls on the game thread for a
  -- document that keeps sixty-four containers.
  local top = {}
  local nextId = 0
  for index = 1, 20 do
    top[index], nextId = Support.bagTree(2, 20, nextId)
  end
  local deepRoots = Observe.inventoryRoots(Support.player({ inventory = Support.container(top) }))
  local containers, items = 0, 0
  local function count(node)
    containers = containers + 1
    for _, entry in ipairs(node.items or {}) do
      items = items + 1
      if entry.container ~= nil then
        count(entry.container)
      end
    end
  end
  count(deepRoots[1])
  ok(containers <= Model.MAX_CONTAINERS, "the reader opens no more containers than the document can hold")
  ok(items <= Model.MAX_ITEMS, "and reads no more items than it can emit")
  ok(deepRoots.containers_dropped > 0, "the bags it never opened are counted")

  local document = Model.build({
    session_id = SESSION,
    seq = 0,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(Support.player({})),
    inventory = deepRoots,
  })
  equal(
    document.player.stats[Model.LIMIT_PREFIX .. "containers_truncated"],
    true,
    "and the document says the tree is incomplete rather than presenting a pruned one as whole"
  )
  ok(
    document.player.stats[Model.LIMIT_PREFIX .. "containers_omitted"] >= deepRoots.containers_dropped,
    "with a count that includes what the reader never handed over"
  )

  local nameless = Observe.inventoryRoots(Support.player({
    inventory = Support.container({}),
    worn = { Support.worn(nil, Support.item({ id = 3, name = "Backpack", contents = {} })) },
  }))
  equal(#nameless, 1, "a worn bag whose slot cannot be read has no reference, so it is not emitted")
  equal(nameless.containers_dropped, 1, "and its absence is counted, not passed off as an empty back")

  local wornMany = {}
  for index = 1, Observe.MAX_WORN + 3 do
    wornMany[index] = Support.worn("Slot" .. index, Support.item({ id = 500 + index, name = "Sock" }))
  end
  local overflow = Observe.inventoryRoots(Support.player({
    inventory = Support.container({}),
    worn = wornMany,
  }))
  equal(overflow.containers_dropped, 3, "worn slots past the cap are counted, since any of them could be a bag")
end

Harness.group("nearby distinguishes what it saw from what it could not tell")
do
  local player = furnishedPlayer()
  local position = { x = 100, y = 200, z = 0 }
  local squares = {
    ["102,200,0"] = Support.square({ Support.worldObject({ name = "Fridge", container_type = "fridge" }) }),
    ["100,203,0"] = Support.square({ Support.worldObject({ name = "Door" }) }),
    ["101,200,0"] = Support.square({ Support.worldObject({ name = "Sink", water = 40 }) }),
  }
  local zombies = {
    Support.zombie({ id = 42, x = 104, y = 200, has_target = true, target = player }),
    Support.zombie({ id = 43, x = 101, y = 200, has_target = true }),
    Support.zombie({ id = 44, x = 300, y = 200, has_target = true }),
    Support.zombie({ id = 45, x = 102, y = 200 }),
  }
  local removeCell = Support.installCell(squares, zombies)

  local objects = Observe.nearbyObjects(position)
  equal(#objects.objects, 3, "every named object inside the radius is reported")
  equal(objects.truncated, false, "and nothing was cut short")

  local nearbyZombies = Observe.nearbyZombies(player, position)
  equal(#nearbyZombies.zombies, 3, "a zombie beyond the radius is not reported at all")
  equal(nearbyZombies.zombies[1].chasing, true, "a zombie whose target is the player is chasing")
  equal(nearbyZombies.zombies[2].chasing, false, "a zombie with no target is not")
  isNil(nearbyZombies.zombies[3].chasing, "and one whose target cannot be read makes no claim either way")
  equal(nearbyZombies.zombies[1].visible, true, "visibility comes from the character's own line of sight")

  local document = Model.build({
    session_id = SESSION,
    seq = 1,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(player),
    nearby = Observe.nearbyFields(player, position),
  })
  local emitted = document.nearby.objects
  local byKind = {}
  for index = 1, #emitted do
    byKind[emitted[index].kind] = emitted[index]
  end
  local fridgeRef = "container:" .. SESSION .. ":world:102:200:0:0:0"
  equal(byKind.fridge.ref, fridgeRef, "a container is referenced as a container")
  same(byKind.fridge.semantics, { "container" }, "and says so in its semantics")
  equal(byKind.door.ref, "object:" .. SESSION .. ":100:203:0:0", "a door is referenced as its own object")
  same(byKind.door.semantics, { "door", "obstacle" }, "with the semantics its kind implies")
  isNil(byKind.door.open, "a door whose build exposes no readers claims no state")
  same(byKind.sink.semantics, { "water_source" }, "a water source is flagged for the drink policy")
  equal(byKind.sink.ref, "square:" .. SESSION .. ":101:200:0", "everything else is referenced by its square")

  equal(
    document.player.stats[Model.LIMIT_PREFIX .. "chasing_unknown"],
    true,
    "the zombie nobody could read is declared, because an absent chasing flag reads as safe"
  )

  -- getOnlineID answers -1 outside multiplayer. Taking it would give every
  -- zombie the reference "zombie:<session>:-1:0" -- one handle for the horde.
  Support.installCell({}, {
    Support.zombie({ id = 71, online_id = -1, x = 101, y = 200 }),
    Support.zombie({ id = 72, online_id = -1, x = 102, y = 200 }),
  })
  local sentinelZombies = Observe.nearbyZombies(player, position).zombies
  equal(sentinelZombies[1].runtime_id, 71, "a -1 online id falls through to the id that identifies")
  Harness.notEqual(sentinelZombies[1].runtime_id, sentinelZombies[2].runtime_id, "so two zombies stay two zombies")
  Support.installCell(squares, zombies)

  -- The square budget cannot bite at the shipped radius, which is exactly why
  -- it is worth proving it holds: a later radius is what would reach it.
  local squareBudget = Observe.MAX_SQUARES
  Observe.MAX_SQUARES = 4
  local starved = Observe.nearbyObjects(position)
  equal(starved.truncated, true, "past the square budget the scan stops and says so")
  ok(starved.dropped >= (2 * Observe.RADIUS + 1) ^ 2 - 4, "counting every square it never looked at")
  Observe.MAX_SQUARES = squareBudget

  local perSquare = Observe.MAX_OBJECTS_PER_SQUARE
  Observe.MAX_OBJECTS_PER_SQUARE = 1
  local crowded = Observe.nearbyObjects(position)
  equal(crowded.truncated, false, "a square with one object is not truncated by a budget of one")
  Observe.MAX_OBJECTS_PER_SQUARE = perSquare
  removeCell()
end

Harness.group("a door carries the state its build will answer, never a default")
do
  -- Doubles built inline because each grants exactly the readers its case is
  -- about: removing one is how "this build cannot read that" is expressed, and
  -- the assertion that matters most is that the missing field stays *absent*.
  local position = { x = 100, y = 200, z = 0 }
  local fullDoor = {
    getObjectName = function()
      return "Door"
    end,
    IsOpen = function()
      return false
    end,
    isLockedByKey = function()
      return true
    end,
    isBarricaded = function()
      return false
    end,
    getNorth = function()
      return true
    end,
  }
  local lockless = {
    getObjectName = function()
      return "Door"
    end,
    -- The lower-case spelling, which the probe must also answer to.
    isOpen = function()
      return true
    end,
    getNorth = function()
      return false
    end,
  }
  local bench = {
    getObjectName = function()
      return "Bench"
    end,
    -- A bench that happens to answer a door-shaped reader must still carry no
    -- door fields: the probe runs for doors only.
    IsOpen = function()
      return true
    end,
  }
  local removeCell = Support.installCell({
    ["101,200,0"] = Support.square({ bench, fullDoor }),
    ["100,201,0"] = Support.square({ lockless }),
  }, {})

  local scanned = Observe.nearbyObjects(position).objects
  local byIndex = {}
  for index = 1, #scanned do
    byIndex[scanned[index].kind .. ":" .. scanned[index].object_index] = scanned[index]
  end

  local full = byIndex["door:1"]
  equal(full.object_index, 1, "a door carries its position in the square's object list")
  equal(full.open, false, "a read false is carried as false")
  equal(full.locked, true, "the lock state is read")
  equal(full.barricaded, false, "and the barricade state")
  equal(full.orientation, "north", "getNorth true is the north wall")

  local partial = byIndex["door:0"]
  equal(partial.open, true, "the lower-case isOpen spelling answers too")
  isNil(partial.locked, "a build with no lock reader leaves the field absent, never false")
  isNil(partial.barricaded, "and the same for the barricade")
  equal(partial.orientation, "west", "getNorth false is the west wall")

  local seat = byIndex["bench:0"]
  equal(seat.object_index, 0, "every scanned object carries its index")
  isNil(seat.open, "a non-door never carries door fields, whatever it answers to")
  isNil(seat.locked, "not the lock")
  isNil(seat.orientation, "and no orientation")
  removeCell()
end

Harness.group("a room is read once per square, and outdoors reports nothing")
do
  local position = { x = 100, y = 200, z = 0 }
  local spy = { count = 0 }
  local kitchen = Support.room({ name = "kitchen", building = Support.building({ id = 42 }) })
  local removeCell = Support.installCell({
    ["100,200,0"] = Support.square({
      Support.worldObject({ name = "Fridge", container_type = "fridge" }),
      Support.worldObject({ name = "Counter", container_type = "counter" }),
      Support.worldObject({ name = "Stove" }),
    }, { room = kitchen, room_spy = spy }),
    ["101,200,0"] = Support.square({ Support.worldObject({ name = "Chair" }) }),
  }, {})

  local scanned = Observe.nearbyObjects(position).objects
  equal(spy.count, 1, "three objects on the square cost one room read, not three")
  local byKind = {}
  for index = 1, #scanned do
    byKind[scanned[index].kind] = scanned[index]
  end
  equal(byKind.fridge.room, "kitchen", "an object in a room carries the room's name")
  equal(byKind.fridge.building, "42", "and the building's numeric id, as a digit string")
  equal(byKind.stove.room, "kitchen", "every object on the square shares the one reading")
  isNil(byKind.chair.room, "a square whose build has no room reader claims none")
  isNil(byKind.chair.building, "and no building")
  removeCell()

  local housed = Observe.playerFields(Support.player({ square = Support.square({}, { room = kitchen }) }))
  equal(housed.room, "kitchen", "the player's own room hangs on getCurrentSquare")
  equal(housed.building, "42", "with the building")

  local outdoors = Observe.playerFields(Support.player({
    -- getRoom exists and answers nil: the character is standing outside.
    square = Support.square({}, { room_spy = { count = 0 } }),
  }))
  isNil(outdoors.room, "outdoors -- a nil room -- reports no room")
  isNil(outdoors.building, "and no building")

  local blind = Observe.playerFields(Support.player({}))
  isNil(blind.room, "a build with no getCurrentSquare reports the identical absence")
  isNil(blind.building, "for both fields, so 'no reader' can never read as 'outside'")

  local defNamed = Observe.playerFields(Support.player({
    square = Support.square({}, {
      room = Support.room({ def_name = "bedroom", building = Support.building({ def_name = "farmhouse" }) }),
    }),
  }))
  equal(defNamed.room, "bedroom", "a room named only on its definition still names itself")
  equal(defNamed.building, "farmhouse", "and a building with no id falls back to its definition's name")

  local nameless = Observe.playerFields(Support.player({
    square = Support.square({}, { room = Support.room({ building = Support.building({ id = 7 }) }) }),
  }))
  isNil(nameless.room, "a room that answers no name under either spelling stays absent")
  equal(nameless.building, "7", "while the building it did answer is still carried")
end

Harness.group("a corpse with loot is a container to look at, not one to reference")
do
  local position = { x = 100, y = 200, z = 0 }
  local removeCell = Support.installCell({
    ["100,200,0"] = Support.square({
      Support.worldObject({ name = "Crate", container_type = "crate" }),
    }, {
      room = Support.room({ name = "kitchen" }),
      bodies = {
        Support.deadBody({ loot = { Support.item({ id = 9, name = "Wallet" }) } }),
        Support.deadBody({}),
      },
    }),
  }, {})

  local scanned = Observe.nearbyObjects(position).objects
  local corpses = {}
  for index = 1, #scanned do
    if scanned[index].kind == "corpse" then
      corpses[#corpses + 1] = scanned[index]
    end
  end
  equal(#corpses, 1, "a body that answers no container is not emitted as one")
  same(corpses[1].semantics, { "container" }, "a looted body carries the container semantic the loot goal filters on")
  isNil(corpses[1].object_index, "and no object index -- a dead body is not in getObjects()")
  isNil(corpses[1].container_index, "so no world-container reference can honestly be minted for its loot")
  equal(corpses[1].room, "kitchen", "the corpse shares the square's one room reading")

  local document = Model.build({
    session_id = SESSION,
    seq = 3,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(furnishedPlayer()),
    nearby = Observe.nearbyFields(furnishedPlayer(), position),
  })
  local emitted = nil
  for index = 1, #document.nearby.objects do
    if document.nearby.objects[index].kind == "corpse" then
      emitted = document.nearby.objects[index]
    end
  end
  equal(
    emitted.ref,
    "square:" .. SESSION .. ":100:200:0",
    "observation-only: the corpse is named by its square, the coarsest honest reference"
  )
  removeCell()

  Support.installCell({
    ["100,200,0"] = Support.square({}, { body = Support.deadBody({ loot = {} }) }),
  }, {})
  local single = Observe.nearbyObjects(position).objects
  equal(#single, 1, "a build spelling only the singular accessor still reports the body")
  equal(single[1].kind, "corpse", "as a corpse")

  local pile = {}
  for index = 1, Observe.MAX_BODIES_PER_SQUARE + 2 do
    pile[index] = Support.deadBody({ loot = {} })
  end
  Support.installCell({ ["100,200,0"] = Support.square({}, { bodies = pile }) }, {})
  local heaped = Observe.nearbyObjects(position)
  equal(#heaped.objects, Observe.MAX_BODIES_PER_SQUARE, "a pile of corpses is read to its cap")
  equal(heaped.truncated, true, "and the scan says it stopped")
  ok(heaped.dropped >= 2, "counting the bodies it never looked at")

  local sharedBudget = Observe.MAX_OBJECTS_SCANNED
  Observe.MAX_OBJECTS_SCANNED = 1
  local removeStarved = Support.installCell({
    ["100,200,0"] = Support.square({
      Support.worldObject({ name = "Crate", container_type = "crate" }),
    }, { bodies = { Support.deadBody({ loot = {} }) } }),
  }, {})
  local starved = Observe.nearbyObjects(position)
  equal(#starved.objects, 1, "corpses spend the same shared object budget as everything else")
  equal(starved.truncated, true, "which therefore reports the body it had no budget left for")
  ok(starved.dropped >= 1, "in the dropped count")
  Observe.MAX_OBJECTS_SCANNED = sharedBudget
  removeStarved()
end

Harness.group("the nearby scan spends one object budget, nearest squares first")
do
  -- A cluttered warehouse: every square inside the radius holding as many
  -- objects as the per-square cap allows. The per-square cap alone does not
  -- bound this walk -- the square count is the other factor -- and the document
  -- keeps only the nearest MAX_OBJECTS of whatever the walk produced, so
  -- everything past that was engine work on the game thread thrown away by the
  -- sort a moment later.
  local position = { x = 500, y = 600, z = 0 }
  local radius = Observe.RADIUS
  local squares = {}
  for dx = -radius, radius do
    for dy = -radius, radius do
      local objects = {}
      for n = 1, Observe.MAX_OBJECTS_PER_SQUARE do
        objects[n] = Support.worldObject({ name = "Shelf", container_type = "shelf" })
      end
      squares[string.format("%d,%d,%d", position.x + dx, position.y + dy, position.z)] = Support.square(objects)
    end
  end
  local removeCell = Support.installCell(squares, {})

  local scanned = Observe.nearbyObjects(position)

  ok(
    #scanned.objects <= 4 * Model.MAX_OBJECTS,
    "the walk stops within a small multiple of the document's cap, not (2R+1)^2 * per-square: "
      .. tostring(#scanned.objects)
  )
  equal(scanned.truncated, true, "and says the scan was cut short")
  ok(scanned.dropped > 0, "counting what it never looked at")
  equal(Observe.MAX_OBJECTS_SCANNED, 4 * Model.MAX_OBJECTS, "the budget is stated against that cap")

  -- Ordering is what makes a total budget safe: raster order would spend it on
  -- the far corner it starts in and never reach the square under the player.
  local nearest = nil
  for index = 1, #scanned.objects do
    if nearest == nil or scanned.objects[index].distance < nearest then
      nearest = scanned.objects[index].distance
    end
  end
  equal(nearest, 0, "the square the player stands on is read before the far corners")

  local document = Model.build({
    session_id = SESSION,
    seq = 9,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(furnishedPlayer()),
    nearby = Observe.nearbyFields(furnishedPlayer(), position),
  })
  equal(#document.nearby.objects, Model.MAX_OBJECTS, "and the document is still filled to its cap")
  removeCell()
end

Harness.group("a tick publishes a snapshot, or says why it did not")
do
  local removeCore = installCore("42.20")
  local removeWorld = Support.installWorld("Muldraugh, KY/survivor")
  local removeTime = Support.installGameTime({ speed = 1, paused = false })
  local removeCell = Support.installCell({}, {})

  local agent, fs = newAgent()
  agent.player = furnishedPlayer()
  agent.queue_description = PZ.Ownership.describe({}, SESSION)

  local document, err = Observe.tick(agent, NOW)
  ok(document ~= nil, "the tick produced a document: " .. tostring(err))
  equal(document.seq, 0, "numbered from the observation stream")
  equal(document.session_id, SESSION, "and stamped with the session")

  local pointer = Json.decode(fs:read(PZ.Ipc.pathFor("snapshot_pointer")))
  equal(pointer.slot, "a", "the first snapshot lands in slot a")
  equal(pointer.seq, 0, "and the pointer names its sequence")
  local slot = Json.decode(fs:read(PZ.Ipc.pathFor("snapshot_a")))
  equal(slot.player.stats.hunger, 0.4, "the published document is the one that was built")
  ok(
    fs:lastWriteIndex(PZ.Ipc.pathFor("snapshot_pointer")) > fs:lastWriteIndex(PZ.Ipc.pathFor("snapshot_a")),
    "and the pointer is written after the slot it commits"
  )

  local second = Observe.tick(agent, NOW + 1000)
  equal(second.seq, 1, "the next tick advances the observation sequence")
  equal(Json.decode(fs:read(PZ.Ipc.pathFor("snapshot_pointer"))).slot, "b", "and alternates slots")

  local bagless, baglessFs = newAgent()
  bagless.player = Support.player({ x = 1, y = 2, z = 0 })
  local partial = Observe.tick(bagless, NOW)
  ok(partial ~= nil, "a character whose inventory cannot be read still produces a snapshot")
  isNil(partial.inventory, "with no inventory section, rather than an empty one")
  Harness.contains(bagless.safety.last_error, "inventory", "and the reason is still surfaced")
  ok(baglessFs:read(PZ.Ipc.pathFor("snapshot_a")) ~= nil, "and the snapshot was published")

  local sessionless = newAgent({ session = false })
  sessionless.player = furnishedPlayer()
  local none, sessionReason = Observe.tick(sessionless, NOW)
  isNil(none, "with no session nothing is published")
  Harness.contains(sessionReason, "session", "and the reason says why")
  isNil(sessionless.safety.last_error, "which is not an error worth showing the player")

  local blind = newAgent()
  blind.player = { getX = function() return 1 end }
  local nothing, blindReason = Observe.tick(blind, NOW)
  isNil(nothing, "a character the reader cannot place produces no snapshot")
  equal(blind.safety.last_error, blindReason, "and the failure reaches the HUD instead of passing silently")

  removeCore()
  local buildless = newAgent()
  buildless.player = furnishedPlayer()
  local unbuilt, buildReason = Observe.tick(buildless, NOW)
  isNil(unbuilt, "a game that will not name its own build produces no snapshot")
  Harness.contains(buildReason, "getCore", "and the reason is the probe's, not a restatement of the missing field")
  equal(buildless.safety.last_error, buildReason, "which reaches the HUD")
  removeCore = installCore("42.20")

  local unwritable, unwritableFs = newAgent()
  unwritable.player = furnishedPlayer()
  unwritableFs:failWritesTo(PZ.Ipc.pathFor("snapshot_a"))
  local unpublished = Observe.tick(unwritable, NOW)
  isNil(unpublished, "a snapshot that could not be written is not reported as published")
  Harness.contains(unwritable.safety.last_error, "snapshot", "and the disk failure is recorded")

  removeCell()
  removeTime()
  removeWorld()
  removeCore()
end

Harness.group("a zombie's body state is a tri-state reading, and absent never reads as standing")
do
  -- Doubles built inline because each grants exactly the readers its case is
  -- about: withholding all of them is how "this build reports no body state"
  -- is expressed, and the assertion that matters most is that the field then
  -- stays absent -- a defaulted "standing" would turn "could not be read"
  -- into "the shove did nothing".
  local player = furnishedPlayer()
  local position = { x = 100, y = 200, z = 0 }
  local function zombieAt(id, x, readers)
    local zombie = {
      getX = function()
        return x
      end,
      getY = function()
        return 200
      end,
      getZ = function()
        return 0
      end,
      getID = function()
        return id
      end,
    }
    for name, value in pairs(readers or {}) do
      zombie[name] = function()
        return value
      end
    end
    return zombie
  end
  local removeCell = Support.installCell({}, {
    zombieAt(80, 101, { isOnFloor = true, isCrawling = false }),
    zombieAt(81, 102, { isCrawling = true }),
    zombieAt(82, 103, { isOnFloor = false, isCrawling = false }),
    zombieAt(83, 104, {}),
    zombieAt(84, 105, { isProne = true }),
  })

  local zombies = Observe.nearbyZombies(player, position).zombies
  equal(zombies[1].state, "prone", "a floor reader answering true is prone")
  equal(zombies[2].state, "crawling", "the crawl reader answering true is crawling")
  equal(zombies[3].state, "standing", "readers that answered with nothing positive are standing")
  isNil(zombies[4].state, "all readers absent means NO state field -- absent never reads as standing")
  equal(zombies[5].state, "prone", "the isProne spelling answers the same reading")

  -- Crawling outranked by the floor: a crawler knocked down reads prone.
  Support.installCell({}, { zombieAt(86, 101, { isOnFloor = true, isCrawling = true }) })
  equal(Observe.nearbyZombies(player, position).zombies[1].state, "prone", "a downed crawler is prone first")
  Support.installCell({}, {
    zombieAt(80, 101, { isOnFloor = true, isCrawling = false }),
    zombieAt(83, 104, {}),
  })

  local document = Model.build({
    session_id = SESSION,
    seq = 11,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(player),
    nearby = Observe.nearbyFields(player, position),
  })
  equal(document.nearby.zombies[1].state, "prone", "the token survives into the document")
  isNil(document.nearby.zombies[2].state, "and the honest absence survives with it")
  removeCell()
end

Harness.group("the equipped weapon's wear reaches the stats map, absent-honest")
do
  local sword = {
    getID = function()
      return 7
    end,
    getCondition = function()
      return 7
    end,
    getConditionMax = function()
      return 10
    end,
  }
  local armed = Observe.playerStats(Support.player({ primary = sword, stats = Support.stats({ hunger = 0.1 }) }))
  equal(armed.weapon_condition, 7, "the primary weapon's condition is read")
  equal(armed.weapon_condition_max, 10, "with its maximum")

  local bare = {
    getID = function()
      return 8
    end,
  }
  local blind = Observe.playerStats(Support.player({ primary = bare }))
  isNil(blind.weapon_condition, "a build with no condition reader leaves the field absent, never zero")
  isNil(blind.weapon_condition_max, "and its maximum with it")

  local empty = Support.player({})
  empty.getPrimaryHandItem = function()
    return nil
  end
  isNil(Observe.playerStats(empty).weapon_condition, "an empty hand claims no weapon at all")

  local halved = {
    getID = function()
      return 9
    end,
    getCondition = function()
      return 4
    end,
  }
  local partial = Observe.playerStats(Support.player({ primary = halved }))
  equal(partial.weapon_condition, 4, "a readable condition is carried")
  isNil(partial.weapon_condition_max, "while its unreadable maximum stays out, never invented")

  -- Through the document: player.stats is the one open scalar map, so the two
  -- keys ride it without a schema change.
  local document = Model.build({
    session_id = SESSION,
    seq = 12,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(Support.player({ primary = sword })),
  })
  equal(document.player.stats.weapon_condition, 7, "the condition reaches the document's stats")
  equal(document.player.stats.weapon_condition_max, 10, "beside its maximum")
end

-- ---------------------------------------------------------------------------
-- literature and recipes
--
-- The doubles for these live here rather than in support/observe_support.lua
-- for the reason that file gives for its own: a reader is proved by what it
-- does when an accessor is missing, so each case builds exactly the accessors
-- it wants to talk about.
-- ---------------------------------------------------------------------------

--- A book. `taught` is the recipes it would teach, absent when this build has
--- no reader for them.
local function book(fields)
  local item = {
    getID = function()
      return fields.id
    end,
    getFullType = function()
      return fields.full_type or "Base.Magazine"
    end,
    getName = function()
      return fields.name or "Magazine"
    end,
    getDisplayCategory = function()
      return "Literature"
    end,
    getUnequippedWeight = function()
      return 0.2
    end,
    getNumberOfPages = function()
      return fields.pages or 10
    end,
    getAlreadyReadPages = function()
      return fields.pages_read or 0
    end,
  }
  if fields.skill ~= nil then
    item.getSkillTrained = function()
      return fields.skill
    end
    item.getLvlSkillTrained = function()
      return fields.min_level or 0
    end
    item.getMaxLevelTrained = function()
      return fields.max_level or 2
    end
  end
  if fields.taught ~= nil then
    item.getTeachedRecipes = function()
      return Support.list(fields.taught)
    end
  end
  return item
end

--- A character carrying `items`, knowing `known` recipes. `contains` chooses
--- which shape the known collection takes: a Java set that answers membership,
--- or a bare list that has to be walked.
local function reader(items, known, options)
  options = options or {}
  local player = Support.player({ inventory = Support.container(items) })
  if known ~= nil then
    local collection = Support.list(known)
    if options.contains ~= false then
      collection.contains = function(_, name)
        for index = 1, #known do
          if known[index] == name then
            return true
          end
        end
        return false
      end
    end
    if options.unlistable then
      collection.size = nil
    end
    player.getKnownRecipes = function()
      return collection
    end
  end
  return player
end

Harness.group("the literature payload is spelled the way the sidecar reads it")
do
  local magazine = book({ id = 1, skill = "Carpentry", min_level = 1, max_level = 3, pages = 12, pages_read = 4 })
  local roots = Observe.inventoryRoots(reader({ magazine }, { "MakeCrate" }))
  local payload = roots[1].items[1].literature
  ok(payload ~= nil, "a book carries a literature payload")
  equal(payload.pages_total, 12, "the page count arrives under the key Python reads")
  equal(payload.pages_read, 4, "beside the pages already read")
  equal(payload.min_level, 1, "the level window's floor is min_level")
  equal(payload.max_level, 3, "and its ceiling is max_level")
  equal(payload.skill, "Carpentry", "with the skill it trains")
  isNil(payload.pages, "the old `pages` spelling is gone")
  isNil(payload.skill_level_min, "and so are the two level keys nothing on the other side read")
  isNil(payload.skill_level_max, "either of them")

  -- Through the document, which is what actually reaches the sidecar.
  local document = Model.build({
    session_id = SESSION,
    seq = 20,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(reader({ magazine }, { "MakeCrate" })),
    inventory = roots,
  })
  equal(document.inventory.items[1].literature.pages_total, 12, "the key survives into the document")
  equal(document.inventory.items[1].literature.min_level, 1, "with the window the policy filters on")
end

Harness.group("unread recipes are counted, and absent whenever they cannot be")
do
  local taught = book({ id = 2, taught = { "MakeCrate", "MakeChair" } })
  local known = Observe.inventoryRoots(reader({ taught }, { "MakeCrate" }))
  equal(known[1].items[1].literature.unread_recipes, 1, "one of the two recipes is still unknown")

  local learned = Observe.inventoryRoots(reader({ taught }, { "MakeCrate", "MakeChair" }))
  equal(learned[1].items[1].literature.unread_recipes, 0, "a magazine whose recipes are known has none unread")

  local walked = Observe.inventoryRoots(reader({ taught }, { "MakeCrate" }, { contains = false }))
  equal(walked[1].items[1].literature.unread_recipes, 1, "a list with no membership test is walked instead")

  -- The three ways the count cannot be established, all of them absent rather
  -- than zero: zero is what makes the sidecar refuse the book.
  local blind = Observe.inventoryRoots(reader({ taught }, nil))
  isNil(blind[1].items[1].literature.unread_recipes, "a build with no known-recipe reader counts nothing")

  local mute = reader({ taught }, { "MakeCrate" }, { contains = false, unlistable = true })
  local unlistable = Observe.inventoryRoots(mute)
  isNil(
    unlistable[1].items[1].literature.unread_recipes,
    "and neither does a collection that can be neither asked nor walked"
  )

  local plain = book({ id = 3 })
  local unreadable = Observe.inventoryRoots(reader({ plain }, { "MakeCrate" }))
  isNil(unreadable[1].items[1].literature.unread_recipes, "a book with no taught-recipe reader carries no count")

  local teaches = book({ id = 4, taught = {} })
  local nothing = Observe.inventoryRoots(reader({ teaches }, { "MakeCrate" }))
  equal(nothing[1].items[1].literature.unread_recipes, 0, "but a book that teaches nothing genuinely has none")
end

Harness.group("what the character can make rides the stats map, or nothing does")
do
  --- A recipe script over `inputs`, each `{ types = {...}, count = n }`.
  local function recipe(name, inputs)
    local entries = {}
    for index = 1, #(inputs or {}) do
      local input = inputs[index]
      entries[index] = {
        getItems = function()
          return Support.list(input.types)
        end,
        getCount = function()
          return input.count
        end,
      }
    end
    local object = {
      getName = function()
        return name
      end,
    }
    if inputs ~= nil then
      object.getInputs = function()
        return Support.list(entries)
      end
    end
    return object
  end

  local function plank(id)
    return Support.item({ id = id, full_type = "Base.Plank", name = "Plank" })
  end

  local crate = recipe("MakeCrate", { { types = { "Base.Plank" }, count = 2 } })
  local chair = recipe("Make Chair", { { types = { "Base.Plank" }, count = 8 } })
  local blind = recipe("MakeMystery", nil)

  local manager = {
    getCraftRecipe = function(_, name)
      return ({ MakeCrate = crate, ["Make Chair"] = chair, MakeMystery = blind })[name]
    end,
  }
  _G["getScriptManager"] = function()
    return manager
  end

  local player = reader({ plank(1), plank(2), plank(3) }, { "MakeCrate", "Make Chair" })
  local roots = Observe.inventoryRoots(player)
  local fields = Observe.craftingFields(player, roots)
  ok(fields ~= nil, "a build that lists recipes publishes a crafting reading")
  equal(fields.known, 2, "with the engine's own count of what is known")
  equal(#fields.recipes, 2, "and an entry per published recipe")
  equal(fields.recipes[1].ready, false, "eight planks are not three, so the chair is not ready")
  equal(fields.recipes[2].ready, true, "while the crate's two planks are on the character")

  local document = Model.build({
    session_id = SESSION,
    seq = 21,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(player),
  })
  isNil(document.player.stats["crafting.known"], "playerFields alone publishes nothing about recipes")

  local stats = Model.applyCrafting({}, fields)
  equal(stats["crafting.known"], 2, "the reading folds into the stats map")
  equal(stats["crafting.listed"], 2, "naming how many keys followed")
  equal(stats["crafting.ready"], 1, "and how many of them can be made now")
  equal(stats["crafting.recipe.MakeCrate"], true, "the ready recipe is keyed by its token")
  equal(stats["crafting.recipe.Make_Chair"], false, "and the unready one by its normalised token")

  -- A build that names the recipes and will not say what they need.
  local mystery = reader({ plank(4) }, { "MakeMystery" })
  local mysteryFields = Observe.craftingFields(mystery, Observe.inventoryRoots(mystery))
  equal(#mysteryFields.recipes, 1, "the recipe is still named")
  isNil(mysteryFields.recipes[1].ready, "with no verdict on its materials")
  local mysteryStats = Model.applyCrafting({}, mysteryFields)
  equal(mysteryStats["crafting.known"], 1, "the count still travels")
  isNil(mysteryStats["crafting.recipe.MakeMystery"], "but no key claims a verdict nobody made")
  equal(mysteryStats["crafting.materials_unknown"], true, "and the silence is declared as its own fact")

  local silent = reader({ plank(5) }, nil)
  isNil(Observe.craftingFields(silent, nil), "a build with no known-recipe reader publishes nothing at all")

  _G["getScriptManager"] = nil
end

Harness.group("the crafting reading is bounded and deterministic")
do
  local names = {}
  for index = 1, Observe.MAX_RECIPES + 6 do
    names[index] = string.format("Recipe%03d", index)
  end
  local player = reader({}, names)
  local fields = Observe.craftingFields(player, {})
  equal(fields.known, #names, "the engine's count is reported whole")
  equal(#fields.recipes, Observe.MAX_RECIPES, "while the published entries stop at the cap")
  equal(fields.truncated, true, "and the reading says it stopped short")
  equal(fields.recipes[1].name, "Recipe001", "the published entries are the first in name order")
  equal(fields.recipes[Observe.MAX_RECIPES].name, string.format("Recipe%03d", Observe.MAX_RECIPES), "and stay sorted")

  local stats = Model.applyCrafting({}, fields)
  equal(stats["crafting.truncated"], true, "which the document carries too")
  isNil(stats["crafting.listed"], "no recipe was judged, so no key was published")
  equal(stats["crafting.materials_unknown"], true, "and the reason is stated")
end

Harness.group("the recipes ride the ingredients they consume, once each")
do
  local function ingredientRecipe(name, types, count, near)
    local object = {
      getName = function()
        return name
      end,
      getInputs = function()
        return Support.list({
          {
            getItems = function()
              return Support.list(types)
            end,
            getCount = function()
              return count
            end,
          },
        })
      end,
      getOutputs = function()
        return Support.list({
          {
            getItems = function()
              return Support.list({ "Base.WoodenCrate" })
            end,
          },
        })
      end,
    }
    if near ~= nil then
      object.getNearItem = function()
        return near
      end
    end
    return object
  end

  local crate = ingredientRecipe("MakeCrate", { "Base.Plank" }, 2)
  local player = reader({
    Support.item({ id = 71, full_type = "Base.Plank", name = "Plank" }),
    Support.item({ id = 72, full_type = "Base.Plank", name = "Plank" }),
    Support.item({ id = 73, full_type = "Base.Nails", name = "Nails" }),
  }, { crate })
  local roots = Observe.inventoryRoots(player)
  local crafting = Observe.craftingFields(player, roots)
  Observe.attachRecipes(roots, crafting)

  local items = roots[1].items
  ok(items[1].crafting ~= nil, "the first plank carries the recipe it feeds")
  equal(items[1].crafting.recipes[1].name, "MakeCrate", "named as the recipe")
  equal(items[1].crafting.recipes[1].product, "Base.WoodenCrate", "with what it makes")
  equal(items[1].crafting.recipes[1].materials[1].count, 2, "and how much of this item it takes")
  equal(items[1].crafting.recipes[1].known, true, "read off the character's own known recipes")
  isNil(items[2].crafting, "the second plank carries no copy of it")
  isNil(items[3].crafting, "and an item the recipe does not consume carries none at all")

  -- Through the document: the item tier is where the sidecar's crafting policy
  -- reads this, so it has to survive the model's shaping.
  local document = Model.build({
    session_id = SESSION,
    seq = 22,
    timestamp_ms = NOW,
    game = { build = "42.20" },
    player = Observe.playerFields(player),
    inventory = roots,
  })
  local carried = nil
  for index = 1, #document.inventory.items do
    if document.inventory.items[index].crafting ~= nil then
      carried = document.inventory.items[index]
    end
  end
  ok(carried ~= nil, "exactly one item in the document carries the readout")
  equal(carried.crafting.recipe_count, 1, "with the count of entries on it")
  equal(carried.crafting.known_recipe_count, 1, "and how many of them the character knows")
  equal(carried.crafting.recipes[1].materials[1].full_type, "Base.Plank", "naming the type it consumes")
  isNil(carried.crafting.recipes[1].needs_surface, "a build with no near-item reader claims nothing about surfaces")

  -- The surface reading is tri-state, and only a positive answer is published.
  local bench = ingredientRecipe("MakeTable", { "Base.Plank" }, 1, "Workbench")
  local free = ingredientRecipe("MakeStake", { "Base.Plank" }, 1, "")
  equal(Observe.recipeNeedsSurface(bench), true, "a named near item is a surface the character must stand at")
  equal(Observe.recipeNeedsSurface(free), false, "an empty one is a positive reading of no surface")
  isNil(Observe.recipeNeedsSurface(crate), "and a build with no reader says nothing either way")

  local unreadable = {
    getName = function()
      return "MakeMystery"
    end,
  }
  local blindPlayer = reader({ Support.item({ id = 74, full_type = "Base.Plank", name = "Plank" }) }, { unreadable })
  local blindRoots = Observe.inventoryRoots(blindPlayer)
  Observe.attachRecipes(blindRoots, Observe.craftingFields(blindPlayer, blindRoots))
  isNil(blindRoots[1].items[1].crafting, "a recipe whose product and inputs cannot be read is stamped nowhere")
end

Harness.group("the crafting reading reaches the document through the tick")
do
  local restore = installCore("42.20")
  local agent = newAgent()
  local plank = Support.item({ id = 61, full_type = "Base.Plank", name = "Plank" })
  local crate = {
    getName = function()
      return "MakeCrate"
    end,
    getInputs = function()
      return Support.list({
        {
          getItems = function()
            return Support.list({ "Base.Plank" })
          end,
          getCount = function()
            return 1
          end,
        },
      })
    end,
  }
  local player = reader({ plank }, { crate })
  agent.player = player
  agent.capability_revision = 1
  agent.queue_description = { ownership = "none", busy = false }

  local document = Observe.tick(agent, NOW)
  ok(document ~= nil, "the tick publishes")
  equal(document.player.stats["crafting.known"], 1, "and the character's recipes ride the stats map")
  equal(document.player.stats["crafting.recipe.MakeCrate"], true, "with the verdict on each published one")
  restore()
end

Harness.finish("observe")
