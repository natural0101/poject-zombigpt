--[[
PZAgent.Adapters.Movement -- movement.move_to and movement.move_near.

Invariant: the character is moved by the game's own pathfinding walk action and
by nothing else. There is no coordinate write anywhere in this file, and the
only thing that may call either command done is the character's *observed*
position: inside the radius and on the target's floor. A constructed action, a
tagged enqueue and an emptied action queue are all statements about the queue
rather than about where the character is standing.

The distinction is not academic, because the failure that actually happens is a
character wedged against a fence: the walk action stays queued indefinitely
while the pathfinder retries, so "the queue is still busy" proves nothing at
all. What proves something is the remaining distance, which is why
PZAgent.Adapters.Toolkit.trackProgress watches it. A distance that stops falling
for Toolkit.DEFAULT_STALL_MS is a stuck character and is reported as such
instead of being waited out until the lease expires.

The floor is compared exactly and separately from the plane distance. A
character on the storey above the target is nowhere near it, however short the
plane distance reads, so `z` is never folded into the radius.
]]

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

-- Guarded the way Toolkit.lua guards them: the engine loads
-- media/lua/client/**.lua in an order this file does not control, so whichever
-- adapter file the game reads first is the one that creates the registry.
PZAgent.Adapters.ALL = PZAgent.Adapters.ALL or {}
PZAgent.Adapters.BY_NAME = PZAgent.Adapters.BY_NAME or {}

local Movement = {}
PZAgent.Adapters.Movement = Movement

--- Squares one command may cover. Longer journeys are the planner's job to cut
--- into waypoints: one thirty-square walk that failed halfway says far less
--- about the world than three ten-square walks of which one failed.
Movement.MAX_DISTANCE = 30

--- World units. The character stands at the centre of a square while a square
--- is named by its corner, so a character standing exactly on the target square
--- still reads 0.707 away -- half a diagonal. The default has to clear that or
--- "arrived" would be unreachable; the ceiling keeps it from meaning "somewhere
--- in the room".
Movement.DEFAULT_RADIUS = 0.75
Movement.MAX_RADIUS = 3.0

--- How much closer the character has to get before the gap counts as closing.
--- Below this a pathfinder shuffling on the spot would read as progress.
Movement.PROGRESS_EPSILON = 0.1

Movement.TIMEOUT_MS = 30000

--- Engine symbols both commands stand on. Every one is probed through
--- PZAgent.Adapters.Toolkit before it is touched, so a build that spells one
--- differently costs a CAPABILITY_UNAVAILABLE naming it rather than an error.
local REQUIRED_SYMBOLS = {
  "getCell",
  "ISWalkToTimedAction",
  "ISWalkToTimedAction.new",
  "ISTimedActionQueue.add",
}

--- Argument kinds, spelled out rather than read from
--- PZAgent.CommandDispatcher.ARG, because the dispatcher is a sibling file and
--- nothing here may dereference a module at load time. Drift is not silent: the
--- dispatcher refuses to register a declaration whose type it does not know, so
--- a typo costs a refused registration at load rather than a loosened check at
--- dispatch.
local ARG = {
  NUMBER = "number",
  REF = "ref",
}

--- Mirrors PZAgent.Adapters.Toolkit.CAPABILITY.MOVE_TO_SQUARE, for the same
--- load-order reason. The sidecar gates its write tools on this exact token.
local CAPABILITY_MOVE = "move_to_square"

-- ---------------------------------------------------------------------------
-- shared helpers
-- ---------------------------------------------------------------------------

--- The character's position, or a refusal naming what could not be read.
---
--- A move whose starting point is unknown has no distance to track and no
--- postcondition to check, so it never starts.
local function positionOf(ctx)
  local Toolkit = PZAgent.Adapters.Toolkit
  local snapshot = Toolkit.snapshot(ctx)
  if type(snapshot) ~= "table" or type(snapshot.position) ~= "table" then
    return nil, Toolkit.reasons().PRECONDITION_FAILED, "the character reports no position"
  end
  return snapshot.position
end

--- Is `position` inside `radius` of `goal` *and* on its floor?
local function arrived(position, goal, radius)
  local Toolkit = PZAgent.Adapters.Toolkit
  if position.z ~= goal.z then
    return false
  end
  return Toolkit.planeDistance(position, goal.x, goal.y) <= radius
end

local function copyPosition(position)
  return { x = position.x, y = position.y, z = position.z }
end

--- Open the evidence record with the "before" half of every measure.
---
--- Written on ctx.state so the runtime can put it on the ack whatever the
--- outcome is. A success carrying an "after" with no "before" would be a claim
--- nobody can check, which is the dishonesty this project exists to prevent.
local function noteDeparture(ctx, goal, radius, position, extra)
  local Toolkit = PZAgent.Adapters.Toolkit
  local state = Toolkit.state(ctx)
  state.goal = goal
  state.radius = radius
  local evidence = {
    target = { x = goal.x, y = goal.y, z = goal.z },
    radius = radius,
    position_before = copyPosition(position),
    distance_before = Toolkit.planeDistance(position, goal.x, goal.y),
    floor_before = position.z,
  }
  if extra ~= nil then
    for key, value in pairs(extra) do
      evidence[key] = value
    end
  end
  state.evidence = evidence
  return evidence
end

--- Close the evidence record with the "after" half.
---
--- Called on every exit that can still read a position, including an
--- interrupted one: where the character actually got to is worth acking whether
--- the command succeeded, stalled or was called off, and `arrived` states
--- plainly which of those the numbers add up to.
local function noteArrival(ctx, position)
  local Toolkit = PZAgent.Adapters.Toolkit
  local state = Toolkit.state(ctx)
  local evidence = state.evidence
  if type(evidence) ~= "table" or type(state.goal) ~= "table" then
    return nil
  end
  evidence.position_after = copyPosition(position)
  evidence.distance_after = Toolkit.planeDistance(position, state.goal.x, state.goal.y)
  evidence.floor_after = position.z
  evidence.arrived = arrived(position, state.goal, state.radius)
  return evidence
end

--- The square the character should stand on to be beside `square`.
---
--- AdjacentFreeTileFinder is what the game's own context menus walk to. When
--- this build does not expose it the target square itself is used, which is the
--- behaviour of walking onto a floor tile rather than up to a counter -- and
--- the fallback is reported in the evidence, so "we stood on it rather than
--- beside it" is never an unstated assumption.
local function adjacentSquare(ctx, square)
  -- Build 42: AdjacentFreeTileFinder.Find(square, character) -> IsoGridSquare.
  if type(AdjacentFreeTileFinder) ~= "table" or type(AdjacentFreeTileFinder.Find) ~= "function" then
    return square, false
  end
  local ok, found = pcall(AdjacentFreeTileFinder.Find, square, ctx.player)
  if not ok or found == nil then
    return square, false
  end
  return found, true
end

--- Queue one tagged walk to `square`.
local function enqueueWalk(ctx, square)
  local Toolkit = PZAgent.Adapters.Toolkit
  -- Build 42: ISWalkToTimedAction:new(character, IsoGridSquare).
  local action, code, detail = Toolkit.construct("ISWalkToTimedAction", ctx.player, square)
  if action == nil then
    return nil, code, detail
  end
  return Toolkit.enqueue(ctx, action)
end

--- Everything both commands do between "the arguments are valid" and "a walk is
--- under way", in the order the checks have to happen.
---
--- Returns "done" when the character was already there -- queueing a walk to
--- where somebody is standing puts a mod-owned entry in the queue that a panic
--- stop would then have to cancel for no reason -- true when a walk was queued,
--- or nil plus a reason code.
local function beginWalk(ctx, goal, radius, options)
  options = options or {}
  local Toolkit = PZAgent.Adapters.Toolkit
  local reasons = Toolkit.reasons()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  if ctx.player == nil then
    return nil, reasons.PRECONDITION_FAILED, "no player character"
  end
  local ready, symbolCode, symbolDetail = Toolkit.requireSymbols(REQUIRED_SYMBOLS)
  if ready == nil then
    return nil, symbolCode, symbolDetail
  end
  local position, positionCode, positionDetail = positionOf(ctx)
  if position == nil then
    return nil, positionCode, positionDetail
  end
  local squares = math.max(math.abs(position.x - goal.x), math.abs(position.y - goal.y))
  if squares > Movement.MAX_DISTANCE then
    return nil,
      reasons.TARGET_OUT_OF_RANGE,
      string.format("the target is %.0f squares away, past the %d one move covers", squares, Movement.MAX_DISTANCE)
  end
  noteDeparture(ctx, goal, radius, position, options.evidence)
  if arrived(position, goal, radius) then
    noteArrival(ctx, position)
    return "done"
  end
  local square, missing = Toolkit.gridSquare(goal.x, goal.y, goal.z)
  if square == nil then
    if missing == "IsoCell.getGridSquare" then
      return nil, reasons.TARGET_NOT_LOADED, "the target square is not loaded"
    end
    return Toolkit.unavailable(missing)
  end
  local destination = square
  if options.beside then
    local found, usedFinder = adjacentSquare(ctx, square)
    destination = found
    Toolkit.state(ctx).evidence.approached_adjacent = usedFinder
  end
  local queued, queueCode, queueDetail = enqueueWalk(ctx, destination)
  if queued == nil then
    return nil, queueCode, queueDetail
  end
  return true
end

--- One progress step, shared by both commands.
---
--- The order of the three tests is the whole point of this function. Arrival is
--- checked first, because a character who is standing on the target has arrived
--- whatever the queue says. The stall check comes next, because it is the only
--- thing that catches a walk that will never finish. The queue is consulted
--- last and only to tell "still walking" from "the walk ended short", which is a
--- path that did not go where the command asked.
local function pollWalk(ctx)
  local Toolkit = PZAgent.Adapters.Toolkit
  local reasons = Toolkit.reasons()
  local state = Toolkit.state(ctx)
  local goal, radius = state.goal, state.radius
  if type(goal) ~= "table" then
    return nil, reasons.INTERNAL_ERROR, "the walk was polled before it recorded a target"
  end
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    local interrupted = positionOf(ctx)
    if interrupted ~= nil then
      noteArrival(ctx, interrupted)
    end
    return nil, code, detail
  end
  local position, positionCode, positionDetail = positionOf(ctx)
  if position == nil then
    return nil, positionCode, positionDetail
  end
  if arrived(position, goal, radius) then
    noteArrival(ctx, position)
    return "done"
  end
  local distance = Toolkit.planeDistance(position, goal.x, goal.y)
  local movement, elapsed = Toolkit.trackProgress(ctx, "distance", distance, { epsilon = Movement.PROGRESS_EPSILON })
  if movement == "stalled" then
    noteArrival(ctx, position)
    -- PATH_STUCK rather than the generic precondition failure: the sidecar's
    -- retry policy keys on this code, and "the character is wedged" is a
    -- different instruction to the planner than "the world was not ready".
    return nil,
      reasons.PATH_STUCK,
      string.format(
        "the character has not closed the gap for %d ms and is still %.2f squares short",
        elapsed,
        distance
      )
  end
  local queue, queueCode, queueDetail = Toolkit.queueProgress(ctx)
  if queue == nil then
    noteArrival(ctx, position)
    return nil, queueCode, queueDetail
  end
  if queue == "done" then
    noteArrival(ctx, position)
    if position.z ~= goal.z then
      -- Directly above or below the target. The plane distance can read zero
      -- here, which is exactly why the floor is a separate test.
      return nil,
        reasons.POSTCONDITION_FAILED,
        string.format("the walk ended on floor %d, not floor %d", position.z, goal.z)
    end
    return nil,
      reasons.PATH_NOT_FOUND,
      string.format("the walk ended %.2f squares short of the target", distance)
  end
  -- `true`, not "running". This poll is handed to PZAgent.ActionRuntime
  -- directly -- these two adapters keep their own two-call shape instead of
  -- going through Toolkit.declare, whose poll wrapper is what translates a
  -- progress step's "running" for every declared adapter -- so it must speak
  -- normaliseOutcome's vocabulary itself: a table, "done", true or nil.
  -- Anything else, "running" included, is normalised to INTERNAL_ERROR, which
  -- terminated every walk that actually had ground to cover on its first poll.
  return true
end

--- Record where the character got to when the runtime calls the command off.
---
--- The action queue is deliberately left alone. Cancelling entries belongs to
--- PZAgent.Safety, which only touches what PZAgent.Ownership proves is ours; an
--- adapter clearing the queue on its way out could take work the player queued
--- by hand with it.
local function interruptWalk(ctx)
  local position = positionOf(ctx)
  if position ~= nil then
    noteArrival(ctx, position)
  end
  return true
end

Movement.positionOf = positionOf
Movement.arrived = arrived

-- ---------------------------------------------------------------------------
-- movement.move_to
-- ---------------------------------------------------------------------------

--- Walk to a named square.
---
--- The target arrives as three scalars rather than one `{x, y, z}` object
--- because PZAgent.CommandDispatcher accepts only scalar arguments -- a nested
--- object is refused before an adapter is reached, so a `target` table could
--- never get here in the first place.
local MoveTo = {
  action = "movement.move_to",
  capability = CAPABILITY_MOVE,
  required_symbols = REQUIRED_SYMBOLS,
  timeout_ms = Movement.TIMEOUT_MS,
  args = {
    -- Integers: a square is named by its corner, and a fractional coordinate
    -- would make "the same square" two different targets depending on rounding.
    x = { type = ARG.NUMBER, required = true, integer = true },
    y = { type = ARG.NUMBER, required = true, integer = true },
    z = { type = ARG.NUMBER, required = true, integer = true, min = -32, max = 32 },
    radius = { type = ARG.NUMBER, min = 0.1, max = Movement.MAX_RADIUS },
  },
}

function MoveTo.start(ctx, args)
  local goal = { x = args.x, y = args.y, z = args.z }
  return beginWalk(ctx, goal, args.radius or Movement.DEFAULT_RADIUS)
end

MoveTo.poll = pollWalk
MoveTo.interrupt = interruptWalk

-- ---------------------------------------------------------------------------
-- movement.move_near
-- ---------------------------------------------------------------------------

--- Where the thing a reference names is standing.
---
--- Only the reference kinds PZAgent.Refs can actually parse are resolved here.
--- An `object:` reference is refused rather than guessed at: neither side ships
--- a parser for one, so accepting it would promise a resolution nothing
--- implements.
local function targetPoint(ctx, ref)
  local Toolkit = PZAgent.Adapters.Toolkit
  local reasons = Toolkit.reasons()
  local kind = PZAgent.Refs.kindOf(ref)
  if kind == PZAgent.Refs.KIND.SQUARE then
    local parsed, parseError = PZAgent.Refs.parseSquare(ref)
    if parsed == nil then
      return nil, reasons.INVALID_REF, parseError
    end
    return { x = parsed.x, y = parsed.y, z = parsed.z }
  end
  local tail
  if kind == PZAgent.Refs.KIND.CONTAINER then
    local parsed, parseError = PZAgent.Refs.parseContainer(ref)
    if parsed == nil then
      return nil, reasons.INVALID_REF, parseError
    end
    tail = parsed.tail
  elseif kind == PZAgent.Refs.KIND.ITEM then
    local parsed, parseError = PZAgent.Refs.parseItem(ref)
    if parsed == nil then
      return nil, reasons.INVALID_REF, parseError
    end
    tail = parsed.container_tail
  else
    return nil,
      reasons.INVALID_REF,
      string.format("this build approaches square, container and item references; %q is neither", tostring(kind))
  end
  if PZAgent.Refs.isOnPersonTail(tail) then
    -- A bag on the character's back travels with them: the postcondition is
    -- already true, and the reach test below passes against their own position.
    local position, code, detail = positionOf(ctx)
    if position == nil then
      return nil, code, detail
    end
    return { x = position.x, y = position.y, z = position.z, on_person = true }
  end
  local x, y, z = tail:match("^world:(-?%d+):(-?%d+):(-?%d+):%d+:%d+$")
  if x == nil then
    return nil, reasons.INVALID_REF, "the reference names no square this build can walk to"
  end
  return { x = tonumber(x), y = tonumber(y), z = tonumber(z) }
end

Movement.targetPoint = targetPoint

--- Walk up to something until it is within reach on its own floor.
local MoveNear = {
  action = "movement.move_near",
  capability = CAPABILITY_MOVE,
  required_symbols = REQUIRED_SYMBOLS,
  timeout_ms = Movement.TIMEOUT_MS,
  args = {
    ref = {
      type = ARG.REF,
      required = true,
      -- No `zombie`: walking up to one is never automatic. No `object` either,
      -- but not for the reason an earlier comment here gave -- both sides do
      -- parse that kind. The real reason is that nothing ever mints one:
      -- PZAgent.ObserveModel describes a nearby thing as a container reference
      -- when it holds a container and as a square reference otherwise, so an
      -- object reference could only arrive from a caller that invented it.
      kinds = { square = true, container = true, item = true },
    },
    reach = { type = ARG.NUMBER, min = 0.1, max = Movement.MAX_RADIUS },
  },
}

function MoveNear.start(ctx, args)
  local Toolkit = PZAgent.Adapters.Toolkit
  -- Ahead of resolving the reference: when the player has taken the controls or
  -- the reflex guard has seen a horde, that is the fact the ack has to carry,
  -- whatever else is also wrong with the command.
  local stop, stopDetail = Toolkit.interruption(ctx)
  if stop ~= nil then
    return nil, stop, stopDetail
  end
  local goal, code, detail = targetPoint(ctx, args.ref)
  if goal == nil then
    return nil, code, detail
  end
  -- The reach default lives on the Toolkit rather than in the declaration
  -- above, because that table is built at load time and the Toolkit may not be
  -- loaded yet when this file is read.
  local reach = args.reach or Toolkit.DEFAULT_REACH
  return beginWalk(ctx, goal, reach, {
    beside = not goal.on_person,
    evidence = { target_ref = args.ref, on_person = goal.on_person == true },
  })
end

MoveNear.poll = pollWalk
MoveNear.interrupt = interruptWalk

-- ---------------------------------------------------------------------------
-- registration
-- ---------------------------------------------------------------------------

--- Publish an adapter where the loader that feeds PZAgent.CommandDispatcher
--- looks for it.
---
--- Replacing by action name rather than always appending: the engine reads this
--- file once, but a reload during development would otherwise leave two copies
--- of the same adapter in ALL, and the dispatcher refuses a second registration
--- for an action it already serves.
local function publish(adapter)
  local all = PZAgent.Adapters.ALL
  for index = 1, #all do
    if all[index].action == adapter.action then
      all[index] = adapter
      PZAgent.Adapters.BY_NAME[adapter.action] = adapter
      return adapter
    end
  end
  all[#all + 1] = adapter
  PZAgent.Adapters.BY_NAME[adapter.action] = adapter
  return adapter
end

Movement.MoveTo = publish(MoveTo)
Movement.MoveNear = publish(MoveNear)

return Movement
