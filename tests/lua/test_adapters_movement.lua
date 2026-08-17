-- PZAgent.Adapters.Movement: walking, and refusing to claim it happened.
--
-- Two groups carry most of the weight. "a wedged character" is the failure that
-- actually happens in a save -- the walk action stays queued forever while the
-- character shuffles against a fence -- and the group that follows it checks the
-- opposite mistake: a walk that finished, emptied the queue and left the
-- character somewhere else still has to fail.
--
-- Arguments are pushed through PZAgent.CommandDispatcher rather than handed to
-- the adapter directly, because the declaration is half of the contract: an
-- adapter that validated a key the dispatcher would have refused would be
-- testing a path no command can take.

local ROOT = arg[0]:match("^(.*)test_adapters_movement%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
Support.loadModules(Harness.root, { "Movement" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains
local same = Harness.same

local Toolkit = PZ.Adapters.Toolkit
local Movement = PZ.Adapters.Movement
local MoveTo = Movement.MoveTo
local MoveNear = Movement.MoveNear
local REASON = PZ.Protocol.REASON

--- The arguments a command would actually reach the adapter with.
local function accept(adapter, raw)
  return PZ.CommandDispatcher.checkArgs(adapter, raw, Support.SESSION)
end

--- The reason code a malformed argument object is refused with.
local function refuse(adapter, raw)
  local args, code, detail = accept(adapter, raw)
  isNil(args, "the arguments are refused")
  return code, detail
end

--- A world with one loaded square per named coordinate.
local function world(coordinates)
  local squares = {}
  for index = 1, #coordinates do
    local entry = coordinates[index]
    squares[Support.squareKey(entry[1], entry[2], entry[3])] = Support.square(entry[1], entry[2], entry[3], entry[4])
  end
  return Support.installCell(squares)
end

--- A walk action that puts the character on the square it was built with.
---
--- `options.keepFloor` makes it move in the plane only, which is how a
--- character ends up directly above the square they were sent to.
local function installWalk(options)
  options = options or {}
  return Support.installAction("ISWalkToTimedAction", function(_, args)
    local player, square = args[1], args[2]
    player.state.x = square.getX()
    player.state.y = square.getY()
    if not options.keepFloor then
      player.state.z = square.getZ()
    end
  end)
end

--- A character, a loaded world, a walk class and an empty action queue.
local function scene(options)
  options = options or {}
  local player = Support.player({ x = options.x or 100, y = options.y or 200, z = options.z or 0 })
  world(options.squares or { { 105, 200, 0 } })
  local walk = installWalk(options)
  local queue = Support.installQueue(options.queue)
  local ctx = Support.context({ player = player, safety = options.safety })
  return player, ctx, walk, queue
end

--- Run every queued action and empty the queue, as the game would.
local function finishQueue(walk, queue)
  for index = 1, #walk.actions do
    walk.actions[index]:perform()
  end
  Support.drainQueue(queue)
end

--- A door standing on a square, built here rather than in adapter_support so
--- this file owns the double it pins behaviour against. Only the readers the
--- fields imply are defined: deleting one is how "this build cannot read that"
--- is expressed, and the adapter must then leave the door alone.
local function door(fields)
  fields = fields or {}
  local state = { open = fields.open == true }
  local object = { state = state }
  object.getObjectName = function()
    return "Door"
  end
  if not fields.no_open_reader then
    object.IsOpen = function()
      return state.open
    end
  end
  if not fields.no_lock_reader then
    object.isLocked = function()
      return fields.locked == true
    end
  end
  if not fields.no_barricade_reader then
    object.isBarricaded = function()
      return fields.barricaded == true
    end
  end
  object.ToggleDoor = function()
    -- `toggle_sticks` is the door the game refused to move: the call lands and
    -- nothing changes, which the adapter may only discover by re-reading.
    if not fields.toggle_sticks then
      state.open = not state.open
    end
  end
  return object
end

--- Poll to mark the gap, then poll again with the stall window elapsed.
local function stallPoll(ctx, adapter, from)
  ctx.now_ms = from
  equal(adapter.poll(ctx), true, "the first poll only marks where the gap stood")
  ctx.now_ms = from + Toolkit.DEFAULT_STALL_MS
  return adapter.poll(ctx)
end

Harness.group("the adapters declare themselves the way the dispatcher demands")
do
  local registry = PZ.CommandDispatcher.new()
  local registered, reason = registry:register(MoveTo)
  equal(registered, true, "move_to registers: " .. tostring(reason))
  registered, reason = registry:register(MoveNear)
  equal(registered, true, "move_near registers: " .. tostring(reason))
  equal(registry:adapterFor("movement.move_to"), MoveTo, "and the dispatcher resolves the action to it")

  equal(PZ.Adapters.BY_NAME["movement.move_to"], MoveTo, "move_to is published under its action name")
  equal(PZ.Adapters.BY_NAME["movement.move_near"], MoveNear, "and so is move_near")
  local found = 0
  for index = 1, #PZ.Adapters.ALL do
    if PZ.Adapters.ALL[index] == MoveTo or PZ.Adapters.ALL[index] == MoveNear then
      found = found + 1
    end
  end
  equal(found, 2, "both appear exactly once in the list the loader walks")
  equal(type(MoveTo.poll), "function", "a walk takes time, so move_to declares a poll")
  equal(type(MoveTo.interrupt), "function", "and a way to be called off")
end

Harness.group("move_to refuses an argument object it cannot act on")
do
  local code = refuse(MoveTo, {})
  equal(code, REASON.INVALID_ARGUMENT, "a move with no target is an invalid argument")

  code = refuse(MoveTo, { x = 105, y = 200, z = 0, speed = "fast" })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  code = refuse(MoveTo, { x = 105, y = 200 })
  equal(code, REASON.INVALID_ARGUMENT, "a target with no floor is not a target")

  code = refuse(MoveTo, { x = 105, y = 200, z = 0.5 })
  equal(code, REASON.INVALID_ARGUMENT, "a fractional floor is refused; floors are compared exactly")

  code = refuse(MoveTo, { x = 105.5, y = 200, z = 0 })
  equal(code, REASON.INVALID_ARGUMENT, "a square is named by whole coordinates")

  code = refuse(MoveTo, { x = 105, y = 200, z = 0, radius = 9 })
  equal(code, REASON.INVALID_ARGUMENT, "a radius wider than a room does not mean arrived")

  code = refuse(MoveTo, { x = 105, y = 200, z = 0, radius = "close" })
  equal(code, REASON.INVALID_ARGUMENT, "a radius that is not a number is refused")

  code = refuse(MoveTo, { x = 105, y = 200, z = 0, allow_doors = "yes" })
  equal(code, REASON.INVALID_ARGUMENT, "allow_doors takes a boolean, not a word that sounds like one")

  local _, _, detail = accept(MoveTo, { target = { x = 105, y = 200, z = 0 } })
  contains(detail, "target", "a nested target object never reaches the adapter")

  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  same(args, { x = 105, y = 200, z = 0, allow_doors = true },
    "a valid move carries the declared keys, doors allowed by default, and nothing else")

  args = accept(MoveTo, { x = 105, y = 200, z = 0, allow_doors = false })
  equal(args.allow_doors, false, "and a caller that forbids doors is heard")
end

Harness.group("move_to queues one tagged walk and proves where it ended")
do
  local player, ctx, walk, queue = scene()
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  local started = MoveTo.start(ctx, args)
  equal(started, true, "the walk is under way, not finished")
  equal(#queue.added, 1, "exactly one action was queued")
  equal(queue.added[1].Type, "ISWalkToTimedAction", "and it is the game's own walk action")
  equal(queue.added[1].pzAgentSession, Support.SESSION, "tagged as this session's before it was added")
  ok(queue.added[1].pzAgentTag ~= nil, "with an ownership tag a panic stop can recognise")

  local evidence = Toolkit.state(ctx).evidence
  same(evidence.position_before, { x = 100, y = 200, z = 0 }, "the departure point is recorded before anything moves")
  equal(evidence.distance_before, 5, "and the gap it has to close")
  isNil(evidence.position_after, "nothing is claimed about the arrival yet")

  -- `true` is the runtime's word for "still working": ActionRuntime's
  -- normaliseOutcome accepts a table, "done", true or nil, and this suite once
  -- pinned "running" here -- a value normaliseOutcome maps to INTERNAL_ERROR,
  -- so every real walk died on its first poll while this file stayed green.
  equal(MoveTo.poll(ctx), true, "while the walk is queued the command is still working")

  finishQueue(walk, queue)
  equal(MoveTo.poll(ctx), "done", "once the character is standing there the command is done")
  same(evidence.position_after, { x = 105, y = 200, z = 0 }, "the arrival is an observation, not an assumption")
  equal(evidence.distance_after, 0, "and it closed the gap")
  equal(evidence.arrived, true, "which is what promotes this to a success")
  equal(player.state.x, 105, "the character was moved by the walk action, never by a coordinate write")
end

Harness.group("move_to that is already there queues nothing")
do
  local _, ctx, _, queue = scene({ x = 105, y = 200, z = 0 })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  -- An outcome table, not the bare "done" this group once pinned: on this path
  -- every before/after pair is identical by construction, and the runtime's
  -- verify refuses an all-unchanged bag unless the outcome itself says
  -- unchanged_is_success -- which the string cannot. The old pin held the
  -- adapter to a shape that made this very command FAILED/POSTCONDITION_FAILED
  -- at the runtime level while this file stayed green.
  local outcome = MoveTo.start(ctx, args)
  equal(type(outcome), "table", "standing on the target finishes synchronously, as an outcome table")
  equal(outcome.done, true, "which is done")
  equal(outcome.unchanged_is_success, true, "and says the unchanged readings are the postcondition holding")
  equal(#queue.added, 0, "a walk to where the character stands would be work a panic stop has to undo")
  local evidence = Toolkit.state(ctx).evidence
  equal(outcome.evidence, evidence, "the outcome carries the same evidence the runtime collects")
  equal(evidence.distance_before, 0, "the before measure is still recorded")
  equal(evidence.arrived, true, "and the after measure agrees")
end

Harness.group("a character on the storey above the target has not arrived")
do
  equal(Movement.arrived({ x = 105, y = 200, z = 1 }, { x = 105, y = 200, z = 0 }, 3.0), false,
    "no radius makes one floor into another")
  equal(Movement.arrived({ x = 105, y = 200, z = 0 }, { x = 105, y = 200, z = 0 }, 0.75), true,
    "the same square on the same floor is arrival")

  local _, ctx, walk, queue = scene({ z = 1, keepFloor = true, squares = { { 105, 200, 0 } } })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  equal(MoveTo.start(ctx, args), true, "the walk starts")
  finishQueue(walk, queue)
  local outcome, code, detail = MoveTo.poll(ctx)
  isNil(outcome, "a walk that ended one storey up is not done")
  equal(code, REASON.POSTCONDITION_FAILED, "the floor is checked separately from the distance")
  contains(detail, "floor", "and the detail says which floor the character is on")
  equal(Toolkit.state(ctx).evidence.distance_after, 0, "even though the plane distance is zero")
end

Harness.group("a wedged character is reported stuck, not waited out")
do
  local _, ctx, _, queue = scene()
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(ctx, args)
  equal(MoveTo.poll(ctx), true, "the first poll only marks where the gap stood")

  ctx.now_ms = Support.NOW + (Toolkit.DEFAULT_STALL_MS / 2)
  equal(MoveTo.poll(ctx), true, "a character that has not moved for half the window is not yet stuck")
  equal(#queue.queue.queue, 1, "the walk action is still queued the whole time, which proves nothing")

  ctx.now_ms = Support.NOW + Toolkit.DEFAULT_STALL_MS
  local outcome, code, detail = MoveTo.poll(ctx)
  isNil(outcome, "a gap that stopped closing is not progress")
  equal(code, REASON.PATH_STUCK, "which is stuck, and says so")
  contains(detail, "squares short", "naming how far short the character is")
  equal(Toolkit.state(ctx).evidence.arrived, false, "the evidence records the failure honestly")
end

Harness.group("a walk that ends short of the target is a path failure")
do
  local _, ctx, _, queue = scene()
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(ctx, args)
  Support.drainQueue(queue)
  local outcome, code, detail = MoveTo.poll(ctx)
  isNil(outcome, "an empty action queue is not an arrival")
  equal(code, REASON.PATH_NOT_FOUND, "the path did not go where the command asked")
  contains(detail, "short", "and the detail says so")
  equal(Toolkit.state(ctx).evidence.distance_after, 5, "with the gap that was left")
end

Harness.group("a stalled walk opens the closed door in its way and carries on")
do
  local shut = door({ open = false })
  local _, ctx, walk, queue = scene({ squares = { { 101, 200, 0, { shut } }, { 105, 200, 0 } } })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  equal(MoveTo.start(ctx, args), true, "the walk starts against the closed door")
  equal(stallPoll(ctx, MoveTo, Support.NOW), true, "the stall is spent opening the door, not failing the walk")
  equal(shut.state.open, true, "the door was toggled open by the game's own call")
  equal(#queue.added, 2, "and a second walk to the original target was queued")
  equal(queue.added[2].pzAgentSession, Support.SESSION, "tagged as this session's like the first")

  local evidence = Toolkit.state(ctx).evidence
  equal(evidence.doors_opened, 1, "the evidence counts the opening")
  same(evidence.doors[1], { x = 101, y = 200, z = 0, opened = true }, "and names the door's square")

  finishQueue(walk, queue)
  equal(MoveTo.poll(ctx), "done", "the re-enqueued walk ends at the target")
  equal(evidence.arrived, true, "with the arrival observed, not assumed")
end

Harness.group("a walk that drained short opens the door instead of PATH_NOT_FOUND")
do
  local shut = door({ open = false })
  local _, ctx, walk, queue = scene({ squares = { { 101, 200, 0, { shut } }, { 105, 200, 0 } } })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(ctx, args)
  Support.drainQueue(queue)
  equal(MoveTo.poll(ctx), true, "the drained queue is spent on the door, not on a path failure")
  equal(shut.state.open, true, "which is now open")
  equal(#queue.added, 2, "with the walk queued again")
  finishQueue(walk, queue)
  equal(MoveTo.poll(ctx), "done", "and the target reached")
end

Harness.group("allow_doors=false leaves the door alone and fails exactly as before")
do
  local shut = door({ open = false })
  local _, ctx, _, queue = scene({ squares = { { 101, 200, 0, { shut } }, { 105, 200, 0 } } })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0, allow_doors = false })
  MoveTo.start(ctx, args)
  local outcome, code, detail = stallPoll(ctx, MoveTo, Support.NOW)
  isNil(outcome, "the stall stays a stall")
  equal(code, REASON.PATH_STUCK, "with the exact code a doorless build reports")
  contains(detail, "squares short", "and the same detail")
  equal(shut.state.open, false, "the door was never touched")
  equal(#queue.added, 1, "and nothing extra was queued")
  isNil(Toolkit.state(ctx).evidence.doors_opened, "the evidence claims no door work it did not do")

  local _, drained, _, drainedQueue = scene({ squares = { { 101, 200, 0, { door({ open = false }) } } } })
  MoveTo.start(drained, args)
  Support.drainQueue(drainedQueue)
  outcome, code = MoveTo.poll(drained)
  isNil(outcome, "the drained-short walk stays a failure too")
  equal(code, REASON.PATH_NOT_FOUND, "with the exact code a doorless build reports")
end

Harness.group("a locked door fails the walk naming the square that needs a key")
do
  local shut = door({ open = false, locked = true })
  local _, ctx, _, queue = scene({ squares = { { 101, 200, 0, { shut } }, { 105, 200, 0 } } })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(ctx, args)
  local outcome, code, detail = stallPoll(ctx, MoveTo, Support.NOW)
  isNil(outcome, "a locked door is not opened")
  equal(code, REASON.DOOR_LOCKED, "it is the key hunt the planner has to hear about")
  contains(detail, "101", "naming the square the door stands on")
  equal(shut.state.open, false, "the door was never toggled")
  equal(#queue.added, 1, "and no second walk was queued")
  equal(Toolkit.state(ctx).evidence.arrived, false, "the evidence records where the walk ended")
end

Harness.group("a barricaded door fails the walk naming the square that needs a detour")
do
  local shut = door({ open = false, barricaded = true })
  local _, ctx = scene({ squares = { { 101, 200, 0, { shut } }, { 105, 200, 0 } } })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(ctx, args)
  local outcome, code, detail = stallPoll(ctx, MoveTo, Support.NOW)
  isNil(outcome, "a barricaded door is not opened")
  equal(code, REASON.DOOR_BARRICADED, "it is a detour, not a key hunt")
  contains(detail, "101", "naming the square")
  equal(shut.state.open, false, "the door was never toggled")
end

Harness.group("a door whose state cannot be read is left alone")
do
  -- No barricade reader: open and locked both read fine, but nil is not false,
  -- so the door cannot be proven safe to open and the walk fails as it always
  -- did -- honestly, without touching what it cannot see.
  local unreadable = door({ open = false, no_barricade_reader = true })
  local _, ctx = scene({ squares = { { 101, 200, 0, { unreadable } }, { 105, 200, 0 } } })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(ctx, args)
  local outcome, code = stallPoll(ctx, MoveTo, Support.NOW)
  isNil(outcome, "the unreadable door does not rescue the walk")
  equal(code, REASON.PATH_STUCK, "which fails with the code a doorless build reports")
  equal(unreadable.state.open, false, "and the door was never touched")
  isNil(Toolkit.state(ctx).evidence.doors, "no attempt was recorded, because none was made")
end

Harness.group("a toggle that does not take falls through with the attempt on record")
do
  local stuckDoor = door({ open = false, toggle_sticks = true })
  local _, ctx, _, queue = scene({ squares = { { 101, 200, 0, { stuckDoor } }, { 105, 200, 0 } } })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(ctx, args)
  local outcome, code = stallPoll(ctx, MoveTo, Support.NOW)
  isNil(outcome, "a door still shut after the toggle rescues nothing")
  equal(code, REASON.PATH_STUCK, "so the walk fails with its own code")
  equal(#queue.added, 1, "no second walk was queued for a door still shut")
  local evidence = Toolkit.state(ctx).evidence
  equal(evidence.doors_opened, 0, "no opening is claimed")
  same(evidence.doors[1], { x = 101, y = 200, z = 0, opened = false },
    "but the attempt is on the record, square and all")
end

Harness.group("a door behind the character is not opened for a walk leading away")
do
  -- The door rescue is the one exception this adapter grants to "the walk
  -- action is the only mutation", and the dot product is what keeps the
  -- exception narrow. Every door in the groups above stands either on the
  -- character's own square or one square east -- exactly on the goal vector --
  -- so the direction filter is never asked to reject anything, and deleting it
  -- leaves all of them green. An independent re-test confirmed there is no
  -- second lever: allow_doors, the attempt budget and the locked/barricaded
  -- gates all constrain *whether* a door may be opened, never *which*.
  --
  -- Opening a door nobody asked about is a change to the save the player did
  -- not request: it lets zombies through, changes line of sight and carries
  -- sound. It also hides the failure, because the rescue spends one of the
  -- three attempts, re-enqueues the walk and clears the stall mark, so the
  -- character polls out another full stall window instead of reporting
  -- PATH_STUCK now -- and a locked door behind them replaces an accurate
  -- PATH_STUCK with a DOOR_LOCKED naming a square irrelevant to the route.
  local behind = door({ open = false })
  local _, ctx, _, queue = scene({
    squares = { { 99, 200, 0, { behind } }, { 105, 200, 0 } },
  })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(ctx, args)
  local outcome, code, detail = stallPoll(ctx, MoveTo, Support.NOW)
  isNil(outcome, "a door behind the character rescues nothing")
  equal(code, REASON.PATH_STUCK, "the walk fails with the code a doorless build reports")
  contains(detail, "squares short", "and the same detail")
  equal(behind.state.open, false, "the door west of a walk heading east was never touched")
  equal(#queue.added, 1, "no second walk was queued off the back of it")
  isNil(Toolkit.state(ctx).evidence.doors, "and nothing about door work reached the evidence")

  -- Perpendicular is refused too: the dot product is zero, which is not
  -- greater than zero. Named separately because ">= 0" is the plausible slip.
  local beside = door({ open = false })
  local _, sideCtx, _, sideQueue = scene({
    squares = { { 100, 201, 0, { beside } }, { 105, 200, 0 } },
  })
  MoveTo.start(sideCtx, args)
  local sideOutcome, sideCode = stallPoll(sideCtx, MoveTo, Support.NOW)
  isNil(sideOutcome, "a door at right angles to the route rescues nothing either")
  equal(sideCode, REASON.PATH_STUCK, "with the same honest code")
  equal(beside.state.open, false, "and it stays shut")
  equal(#sideQueue.added, 1, "with no second walk")
end

Harness.group("a door past the square-object bound is not reached at all")
do
  -- `Toolkit.MAX_SQUARE_OBJECTS` bounds the walk over one square's object
  -- list, which the player fills by stacking. Every square in this file holds
  -- zero or one object, so `math.min` never binds here and dropping the cap is
  -- invisible: the two suites that do pin this constant pin it at other call
  -- sites, against other readers.
  --
  -- The bound is observable exactly once: a door standing past it is not found,
  -- and the walk then fails honestly instead of being rescued. That is the
  -- trade the cap makes, and it is the right way round -- a bounded read that
  -- misses a door costs one PATH_STUCK, an unbounded one runs on the game's
  -- main thread over a list nobody here controls.
  local buried = door({ open = false })
  local objects = {}
  for index = 1, Toolkit.MAX_SQUARE_OBJECTS do
    objects[index] = Support.worldObject({ name = "Crate " .. index })
  end
  objects[#objects + 1] = buried
  local _, ctx, _, queue = scene({
    squares = { { 101, 200, 0, objects }, { 105, 200, 0 } },
  })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(ctx, args)
  local outcome, code = stallPoll(ctx, MoveTo, Support.NOW)
  isNil(outcome, "the door past the bound is never reached, so nothing rescues the walk")
  equal(code, REASON.PATH_STUCK, "which fails with its own code")
  equal(buried.state.open, false, "and the buried door was not toggled")
  equal(#queue.added, 1, "with no second walk queued")
end

Harness.group("the door budget is three openings, then the walk fails honestly")
do
  local doors = {
    door({ open = false }),
    door({ open = false }),
    door({ open = false }),
    door({ open = false }),
  }
  local player, ctx, _, _ = scene({
    squares = {
      { 101, 200, 0, { doors[1] } },
      { 102, 200, 0, { doors[2] } },
      { 103, 200, 0, { doors[3] } },
      { 104, 200, 0, { doors[4] } },
      { 105, 200, 0 },
    },
  })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(ctx, args)
  local now = Support.NOW
  for step = 1, 3 do
    equal(stallPoll(ctx, MoveTo, now), true, "door " .. step .. " is opened and the walk re-enqueued")
    equal(doors[step].state.open, true, "door " .. step .. " reads open")
    -- The game walks the character one square before the next door wedges them.
    player.state.x = 100 + step
    now = now + Toolkit.DEFAULT_STALL_MS
  end
  local outcome, code = stallPoll(ctx, MoveTo, now)
  isNil(outcome, "the fourth door is past the budget")
  equal(code, REASON.PATH_STUCK, "so the walk fails with its own code rather than looping on doors")
  equal(doors[4].state.open, false, "and the fourth door stays shut")
  equal(Toolkit.state(ctx).evidence.doors_opened, 3, "the evidence counts exactly the budget")
end

Harness.group("move_near meets the same doors through the shared poll")
do
  local crate = Support.worldObject({ name = "Crate", container = Support.container({}) })
  local shut = door({ open = false })
  local _, ctx, walk, queue = scene({
    squares = { { 101, 200, 0, { shut } }, { 104, 200, 0, { crate } } },
  })
  local args = accept(MoveNear, { ref = Support.worldContainerRef(104, 200, 0, 0) })
  equal(args.allow_doors, true, "move_near declares the same switch, on by default")
  equal(MoveNear.start(ctx, args), true, "the approach starts against the closed door")
  equal(stallPoll(ctx, MoveNear, Support.NOW), true, "and spends the stall opening it")
  equal(shut.state.open, true, "the door reads open")
  finishQueue(walk, queue)
  equal(MoveNear.poll(ctx), "done", "the approach ends within reach")
  equal(Toolkit.state(ctx).evidence.doors_opened, 1, "with the opening on the evidence")
end

Harness.group("an interruption ends the walk and still says where the character got to")
do
  local _, ctx = scene({ safety = Support.takenOver() })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  local outcome, code = MoveTo.start(ctx, args)
  isNil(outcome, "a move does not start while the player has the controls")
  equal(code, REASON.USER_TAKEOVER, "and the ack says who took over")

  local _, running, walkRecord, queueRecord = scene()
  local moving = accept(MoveTo, { x = 105, y = 200, z = 0 })
  MoveTo.start(running, moving)
  walkRecord.actions[1]:perform()
  running.safety = Support.threatened()
  outcome, code = MoveTo.poll(running)
  isNil(outcome, "a horde stops the walk")
  equal(code, REASON.THREAT_INTERRUPTED, "with the reflex guard's reason")
  same(Toolkit.state(running).evidence.position_after, { x = 105, y = 200, z = 0 },
    "and the ground that was covered is still reported")
  equal(#queueRecord.queue.queue, 1, "the queue is left for PZAgent.Safety, which knows what it owns")

  MoveTo.interrupt(running)
  equal(Toolkit.state(running).evidence.arrived, true, "an interrupt closes the evidence with what it can observe")
end

Harness.group("a missing engine symbol costs one refusal naming the symbol")
do
  local _, ctx = scene()
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  Support.removeAction("ISWalkToTimedAction")
  local outcome, code, detail = MoveTo.start(ctx, args)
  isNil(outcome, "a build without the walk action cannot walk")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "which is a capability gap, not a path failure")
  contains(detail, "ISWalkToTimedAction", "and the detail names the symbol to look for in the game")

  local _, second = scene()
  Support.removeCell()
  outcome, code, detail = MoveTo.start(second, args)
  isNil(outcome, "and a build with no cell cannot find a square")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "also a capability gap")
  contains(detail, "getCell", "naming getCell")
end

Harness.group("move_to refuses what it cannot reach")
do
  local _, ctx = scene({ squares = {} })
  local args = accept(MoveTo, { x = 105, y = 200, z = 0 })
  local outcome, code = MoveTo.start(ctx, args)
  isNil(outcome, "an unloaded square is not walked to")
  equal(code, REASON.TARGET_NOT_LOADED, "it is reported as the chunk it is")

  local _, far = scene({ squares = { { 400, 200, 0 } } })
  local farArgs = accept(MoveTo, { x = 400, y = 200, z = 0 })
  outcome, code = MoveTo.start(far, farArgs)
  isNil(outcome, "a target past the waypoint cap is not one command's work")
  equal(code, REASON.TARGET_OUT_OF_RANGE, "and says so rather than walking most of the way")

  local _, rejected = scene({ queue = { addFails = true } })
  outcome, code = MoveTo.start(rejected, args)
  isNil(outcome, "a queue that refuses the action has not started a walk")
  equal(code, REASON.QUEUE_REJECTED, "which is the queue's refusal, reported as such")
end

Harness.group("move_near refuses a reference it must not act on")
do
  local code = refuse(MoveNear, {})
  equal(code, REASON.INVALID_ARGUMENT, "there is nothing to approach without a reference")

  code = refuse(MoveNear, { ref = PZ.Refs.buildZombie(Support.SESSION, "42", 0) })
  equal(code, REASON.INVALID_REF, "walking up to a zombie is not an approach this build offers")

  code = refuse(MoveNear, { ref = Support.squareRef(105, 200, 0, "11111111-2222-3333-4444-555555555555") })
  equal(code, REASON.INVALID_REF, "a reference from another session names objects that no longer exist")

  code = refuse(MoveNear, { ref = Support.squareRef(105, 200, 0), reach = 9 })
  equal(code, REASON.INVALID_ARGUMENT, "a reach wider than a room is not a reach")

  local args = accept(MoveNear, { ref = Support.squareRef(105, 200, 0) })
  equal(args.ref, Support.squareRef(105, 200, 0), "a square reference is accepted")
  isNil(args.reach, "and the reach is left to the toolkit's own default")
end

Harness.group("move_near walks to a world container and verifies the distance")
do
  local crate = Support.worldObject({ name = "Crate", container = Support.container({}) })
  local _, ctx, walk, queue = scene({ squares = { { 104, 200, 0, { crate } }, { 105, 200, 0 } } })
  local args = accept(MoveNear, { ref = Support.worldContainerRef(104, 200, 0, 0) })
  equal(MoveNear.start(ctx, args), true, "the approach is under way")
  equal(#queue.added, 1, "by one queued walk")

  local evidence = Toolkit.state(ctx).evidence
  equal(evidence.radius, Toolkit.DEFAULT_REACH, "the reach that was not given comes from the toolkit")
  equal(evidence.distance_before, 4, "the gap is measured to the container's own square")
  equal(evidence.approached_adjacent, false, "this build has no adjacent-tile finder, and says so")

  finishQueue(walk, queue)
  equal(MoveNear.poll(ctx), "done", "standing on the container's square is within reach of it")
  equal(evidence.arrived, true, "which the evidence states")
  equal(evidence.target_ref, args.ref, "against the reference that was asked for")
end

Harness.group("move_near to something already carried queues nothing")
do
  local _, ctx, _, queue = scene()
  local args = accept(MoveNear, { ref = Support.mainRef() })
  -- The same outcome-table shape the already-arrived move_to answers, for the
  -- same reason: an on-person target reads identical on both sides of every
  -- pair, and only the outcome itself can tell the runtime's verify that the
  -- unchanged readings are the postcondition holding rather than nothing
  -- having been observed.
  local outcome = MoveNear.start(ctx, args)
  equal(type(outcome), "table", "a bag on the character's back needs no walking")
  equal(outcome.done, true, "so the approach is done at once")
  equal(outcome.unchanged_is_success, true, "and the outcome says why unchanged readings are a success")
  equal(#queue.added, 0, "so nothing is queued")
  local evidence = Toolkit.state(ctx).evidence
  equal(evidence.on_person, true, "the evidence says why the distance was already zero")
  equal(evidence.distance_after, 0, "and reports it")

  local itemRef = Support.itemRef("player-main", 7)
  local carried = accept(MoveNear, { ref = itemRef })
  local second = select(2, scene())
  local carriedOutcome = MoveNear.start(second, carried)
  equal(type(carriedOutcome) == "table" and carriedOutcome.done, true,
    "an item reference resolves through its container tail")
end

Harness.group("move_near refuses a reference that names no reachable square")
do
  local _, ctx = scene()
  local vehicleRef = PZ.Refs.buildContainer(Support.SESSION, "vehicle:12:SeatFrontLeft")
  local args = accept(MoveNear, { ref = vehicleRef })
  local outcome, code, detail = MoveNear.start(ctx, args)
  isNil(outcome, "a vehicle part is not a square this build can walk to")
  equal(code, REASON.INVALID_REF, "so the reference is refused rather than guessed at")
  contains(detail, "walk to", "with a detail that says what is missing")

  local _, taken = scene({ safety = Support.takenOver() })
  outcome, code = MoveNear.start(taken, args)
  isNil(outcome, "and while the player has the controls nothing is approached")
  equal(code, REASON.USER_TAKEOVER, "the takeover outranks the bad reference")
end

Harness.finish("adapters_movement")
