-- Reference tests for PZAgent.Refs.
--
-- The golden strings below were produced by the Python implementation
-- (pz_agent_core.protocol.refs) and pasted in verbatim. That is deliberate: the
-- only thing that makes the two sides agree is that they produce the same
-- bytes, and a test that recomputed the expectation from the Lua code would
-- pass no matter how far the two drifted apart.

local Harness = dofile((arg[0]:match("^(.*)test_refs%.lua$") or "") .. "support/harness.lua")
local PZ = Harness.loadModules()
local Refs = PZ.Refs

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local SESSION = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33"

-- Golden values, from the Python side.
local GOLDEN = {
  player_main = "container:3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33:player-main",
  worn = "container:3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33:worn:Back:77",
  world = "container:3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33:world:10726:9455:0:3:1",
  item_main = "item:3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33:player-main:deadbeef:0",
  item_world = "item:3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33:world:10726:9455:0:3:1:1a2b3c:7",
  square = "square:3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33:-12:34:-1",
  zombie = "zombie:3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33:z-99:2",
  -- The object format is fixed by the protocol contract: the kind existed on
  -- both sides with no builder, and door references are the first use of it.
  object = "object:3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33:-12:34:0:5",
}

Harness.group("container refs agree with the Python format")
do
  equal(Refs.playerMainContainer(SESSION), GOLDEN.player_main, "player-main container")
  equal(Refs.wornContainer(SESSION, "Back", "77"), GOLDEN.worn, "worn container")
  equal(Refs.worldContainer(SESSION, 10726, 9455, 0, 3, 1), GOLDEN.world, "world container")
  equal(Refs.carriedContainer(SESSION, "42"), "container:" .. SESSION .. ":carried:42", "carried container")

  local parsed = Refs.parseContainer(GOLDEN.world)
  ok(parsed ~= nil, "the world container parses")
  equal(parsed.session_id, SESSION, "session id survives the parse")
  equal(parsed.tail, "world:10726:9455:0:3:1", "the tail keeps its embedded colons")
  ok(Refs.isWorldTail(parsed.tail), "the tail is recognised as a world tail")
  ok(not Refs.isOnPersonTail(parsed.tail), "a world container is not on the person")
  ok(Refs.isOnPersonTail("player-main"), "player-main is on the person")
  ok(Refs.isOnPersonTail("worn:Back:77"), "a worn container is on the person")
  ok(Refs.isOnPersonTail("carried:42"), "a carried container is on the person")
end

Harness.group("item refs split from the ends, not from the left")
do
  equal(Refs.buildItem(SESSION, "player-main", "deadbeef", 0), GOLDEN.item_main, "item in the main inventory")
  equal(Refs.buildItem(SESSION, "world:10726:9455:0:3:1", "1a2b3c", 7), GOLDEN.item_world, "item in a world container")

  -- This is the case a naive split(":") gets wrong: the container tail here
  -- holds five colons of its own, so parsing left to right would report the
  -- runtime id as "10726" and point the reference at a different object.
  local parsed = Refs.parseItem(GOLDEN.item_world)
  ok(parsed ~= nil, "the world item ref parses")
  equal(parsed.session_id, SESSION, "session id")
  equal(parsed.container_tail, "world:10726:9455:0:3:1", "the container tail is recovered whole")
  equal(parsed.runtime_id, "1a2b3c", "the runtime id is the next-to-last segment")
  equal(parsed.generation, 7, "the generation is the last segment")
  equal(Refs.containerRefOf(parsed), GOLDEN.world, "the owning container ref is reconstructed exactly")

  local simple = Refs.parseItem(GOLDEN.item_main)
  equal(simple.container_tail, "player-main", "a colon-free tail is recovered too")
  equal(simple.runtime_id, "deadbeef", "runtime id of a main-inventory item")
  equal(simple.generation, 0, "generation zero is a real generation, not a missing one")
  equal(Refs.containerRefOf(simple), GOLDEN.player_main, "owning container of a main-inventory item")
end

Harness.group("square and zombie refs")
do
  equal(Refs.buildSquare(SESSION, -12, 34, -1), GOLDEN.square, "square with negative coordinates")
  local square = Refs.parseSquare(GOLDEN.square)
  ok(square ~= nil, "the square parses")
  equal(square.x, -12, "x")
  equal(square.y, 34, "y")
  equal(square.z, -1, "z is the floor and may be negative")
  equal(
    Refs.chebyshevDistance({ x = 0, y = 0 }, { x = -3, y = 2 }),
    3,
    "chebyshev distance takes the larger axis"
  )

  equal(Refs.buildZombie(SESSION, "z-99", 2), GOLDEN.zombie, "zombie ref")
  local zombie = Refs.parseZombie(GOLDEN.zombie)
  equal(zombie.runtime_id, "z-99", "zombie runtime id")
  equal(zombie.generation, 2, "zombie generation")
end

Harness.group("object refs name one world object by square and index")
do
  equal(Refs.buildObject(SESSION, -12, 34, 0, 5), GOLDEN.object, "object ref with a negative coordinate")
  local parsed = Refs.parseObject(GOLDEN.object)
  ok(parsed ~= nil, "the object ref parses")
  equal(parsed.session_id, SESSION, "session id survives the parse")
  equal(parsed.x, -12, "x may be negative")
  equal(parsed.y, 34, "y")
  equal(parsed.z, 0, "z is the floor")
  equal(parsed.object_index, 5, "the object index is the last segment")

  equal(Refs.kindOf(GOLDEN.object), "object", "object kind")
  ok(Refs.belongsToSession(GOLDEN.object, SESSION), "an object ref from this session belongs to it")
  ok(
    not Refs.belongsToSession(GOLDEN.object, "00000000-0000-0000-0000-000000000000"),
    "and one from another session does not"
  )
  equal(Refs.parse(GOLDEN.object).object_index, 5, "the generic parser dispatches object refs too")
end
do
  equal(Refs.kindOf(GOLDEN.item_world), "item", "item kind")
  equal(Refs.kindOf(GOLDEN.world), "container", "container kind")
  equal(Refs.kindOf(GOLDEN.square), "square", "square kind")
  isNil(Refs.kindOf("banana:1:2"), "an unknown kind is refused")
  isNil(Refs.kindOf(42), "a non-string is refused")

  ok(Refs.belongsToSession(GOLDEN.item_world, SESSION), "a ref from this session belongs to it")
  ok(
    not Refs.belongsToSession(GOLDEN.item_world, "00000000-0000-0000-0000-000000000000"),
    "a ref from another session does not belong to this one"
  )
  ok(not Refs.belongsToSession("banana:" .. SESSION .. ":x", SESSION), "an unknown kind never belongs")

  local generic = Refs.parse(GOLDEN.item_world)
  equal(generic.runtime_id, "1a2b3c", "the generic parser dispatches on kind")
  isNil(Refs.parse("wound:" .. SESSION .. ":1"), "a kind with no parser is reported, not guessed")
end

Harness.group("malformed references are refused")
do
  isNil(Refs.parseItem("container:" .. SESSION .. ":player-main"), "a container ref is not an item ref")
  isNil(Refs.parseItem("item:" .. SESSION), "an item ref with no generation")
  isNil(Refs.parseItem("item:" .. SESSION .. ":only-one-tail-part"), "an item ref with no runtime id")
  isNil(Refs.parseItem("item:" .. SESSION .. ":player-main:x:seven"), "a non-numeric generation")
  isNil(Refs.parseItem("item:" .. SESSION .. ":player-main:x:-1"), "a negative generation")
  isNil(Refs.parseItem("item:bad session:player-main:x:1"), "a session with a space")
  isNil(Refs.parseItem("item:" .. SESSION .. ":player-main:sla/sh:1"), "a runtime id with a path separator")
  isNil(Refs.parseItem(""), "an empty string")
  isNil(Refs.parseItem(nil), "a nil reference")
  isNil(Refs.parseItem(string.rep("a", 2000)), "a reference past the length bound")
  contains(select(2, Refs.parseItem(string.rep("a", 2000))), "exceeds", "the length refusal says so")

  isNil(Refs.parseContainer("container:" .. SESSION), "a container ref with no tail")
  isNil(Refs.parseContainer("container:" .. SESSION .. ":world::9455:0:3:1"), "an empty tail segment")
  isNil(Refs.parseContainer("container:" .. SESSION .. ":../escape"), "a tail segment with a traversal")

  isNil(Refs.parseSquare("square:" .. SESSION .. ":1:2"), "a square with too few coordinates")
  isNil(Refs.parseSquare("square:" .. SESSION .. ":1:2:3:4"), "a square with too many coordinates")
  isNil(Refs.parseSquare("square:" .. SESSION .. ":1:2:x"), "a square with a non-integer coordinate")
  isNil(Refs.parseSquare("square:" .. SESSION .. ":0x10:2:3"), "a hexadecimal coordinate is not an integer")
  isNil(Refs.parseSquare("square:" .. SESSION .. ": 1:2:3"), "a padded coordinate is refused")

  isNil(Refs.parseZombie("zombie:" .. SESSION .. ":z-99"), "a zombie ref with no generation")
  isNil(Refs.parseZombie("zombie:" .. SESSION .. ":z-99:x"), "a non-numeric zombie generation")

  isNil(Refs.parseObject("object:" .. SESSION .. ":1:2:0"), "an object ref with no index")
  isNil(Refs.parseObject("object:" .. SESSION .. ":1:2:0:5:9"), "an object ref with too many segments")
  isNil(Refs.parseObject("object:" .. SESSION .. ":1:2:0:-1"), "a negative object index is not an index")
  isNil(Refs.parseObject("object:" .. SESSION .. ":1:x:0:5"), "an object ref with a non-integer coordinate")
  isNil(Refs.parseObject("object:bad session:1:2:0:5"), "an object ref with a malformed session")
  isNil(Refs.parseObject("square:" .. SESSION .. ":1:2:0"), "a square ref is not an object ref")
end

Harness.group("builders refuse what they cannot represent")
do
  isNil(Refs.buildItem("bad session", "player-main", "x", 0), "an invalid session is refused")
  isNil(Refs.buildItem(SESSION, "", "x", 0), "an empty container tail is refused")
  isNil(Refs.buildItem(SESSION, "player-main", "x", -1), "a negative generation is refused")
  isNil(Refs.buildItem(SESSION, "player-main", "x", 1.5), "a fractional generation is refused")
  isNil(Refs.buildItem(SESSION, "player-main", "a:b", 0), "a runtime id containing the separator is refused")
  isNil(Refs.buildContainer(SESSION, "world:1:2:3:4:oops/../.."), "a traversal in a tail is refused")
  isNil(Refs.worldContainer(SESSION, 1.5, 2, 3, 4, 5), "fractional world coordinates are refused")
  isNil(Refs.buildSquare(SESSION, "1", 2, 3), "a string coordinate is refused")
  isNil(Refs.buildZombie(SESSION, "z 99", 1), "a runtime id with a space is refused")
  isNil(Refs.buildContainer(string.rep("a", 65), "player-main"), "a session id past 64 bytes is refused")
  isNil(Refs.buildObject("bad session", 1, 2, 0, 5), "an object ref with an invalid session is refused")
  isNil(Refs.buildObject(SESSION, 1.5, 2, 0, 5), "a fractional object coordinate is refused")
  isNil(Refs.buildObject(SESSION, 1, 2, 0, -1), "a negative object index is refused")
  isNil(Refs.buildObject(SESSION, 1, 2, 0, 1.5), "and so is a fractional one")
end

Harness.finish("test_refs")
