-- PZAgent.Adapters.Toolkit: the parts every adapter rests on and no adapter
-- suite drives.
--
-- Toolkit is the shared floor. `inReach` decides, for eight callers, whether the
-- character may act on a thing without moving; `containerItems`, `listSize` and
-- the square walks bound reads over lists the player fills. The adapter suites
-- exercise all of it *incidentally* -- through Building's reach gate, Doors'
-- re-check, Containers' two checks -- which is not the same as pinning it, and
-- a refusal reached only incidentally is a refusal whose deletion shows up
-- nowhere. Every group here drives the shared function directly and names the
-- callers that would have inherited the defect.

local ROOT = arg[0]:match("^(.*)test_adapter_toolkit%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
Support.loadModules(Harness.root, {})

local equal, ok, isNil = Harness.equal, Harness.ok, Harness.isNil

local Toolkit = PZ.Adapters.Toolkit

--- A character standing at (100, 200, 0) with an empty inventory.
local function context(x, y, z)
  local player = Support.player({
    x = x or 100,
    y = y or 200,
    z = z or 0,
    inventory = Support.container({}),
  })
  return Support.context({ player = player }), player
end

Harness.group("reach compares floors before it compares distance")
do
  -- The one reading that makes "directly above me" different from "at my feet".
  -- The plane distance between (100,200,0) and (100,200,1) is exactly 0.0, so
  -- without the floor comparison every caller of this function treats a square
  -- one storey up as touchable: `building.build` raises a wall on the floor
  -- above, `container.open` opens a crate through a ceiling, and
  -- `Toolkit.approach` answers "in_reach" instead of queueing the walk -- so the
  -- adapter acts on something a storey away without ever moving.
  --
  -- No adapter suite asks this question. Building's TARGET and FAR are both at
  -- z=0; Combat's "a zombie on another floor is out of range" case is caught
  -- earlier by `resolveTarget`'s own separate z comparison, not by this one;
  -- and Movement's arrival floor check is Movement's own code, guarding arrival
  -- rather than reach. Eight callers, one reading, and nothing drove it.
  local ctx = context(100, 200, 0)

  local here, hereDistance = Toolkit.inReach(ctx, { x = 100, y = 200, z = 0 })
  equal(here, true, "the square under the character is in reach")
  equal(hereDistance, 0, "at no distance at all")

  local above, aboveDistance = Toolkit.inReach(ctx, { x = 100, y = 200, z = 1 })
  equal(above, false, "the same square one storey up is not")
  equal(aboveDistance, 0, "even though the plane distance is identically zero")

  local below = Toolkit.inReach(ctx, { x = 100, y = 200, z = -1 })
  equal(below, false, "and neither is the one below")

  -- The floor rule is not a distance rule wearing a hat: a generous radius
  -- does not buy a storey.
  local generous = Toolkit.inReach(ctx, { x = 100, y = 200, z = 1 }, 30)
  equal(generous, false, "a reach of thirty squares still does not reach through a ceiling")

  -- The control, so the assertions above are about the floor and not about the
  -- function refusing everything: on the same storey the radius is what decides.
  local near = Toolkit.inReach(ctx, { x = 101, y = 200, z = 0 })
  equal(near, true, "one square away on the same floor is within the default reach")
  local far = Toolkit.inReach(ctx, { x = 105, y = 200, z = 0 })
  equal(far, false, "five squares away is not")

  -- A character whose position cannot be read is not a character standing
  -- anywhere: no distance is computed and no reach is claimed.
  local blind, player = context()
  player.getX = nil
  local unread, code = Toolkit.inReach(blind, { x = 100, y = 200, z = 0 })
  isNil(unread, "a character with no readable position proves no reach")
  equal(code, PZ.Protocol.REASON.PRECONDITION_FAILED, "which is a precondition, not a distance")
end

Harness.group("approach inherits the floor rule rather than restating it")
do
  -- `Toolkit.approach` is the path Inventory, Rest, Sleep and Consumption take
  -- to the same decision, and it has no z comparison of its own -- it asks
  -- `inReach`. Pinned here so the group above is known to be load-bearing for
  -- the callers that never touch `inReach` by name.
  local ctx = context(100, 200, 0)
  Support.installAction("ISWalkToTimedAction")
  Support.installQueue()
  Support.installCell({
    [Support.squareKey(100, 200, 1)] = Support.square(100, 200, 1, {}),
  })

  equal(Toolkit.approach(ctx, { x = 100, y = 200, z = 0 }), "in_reach",
    "the square under the character needs no walking to")

  local outcome = Toolkit.approach(ctx, { x = 100, y = 200, z = 1 })
  ok(outcome ~= "in_reach", "the same square one storey up is not somewhere the character already is")
end

Harness.finish("adapter_toolkit")
