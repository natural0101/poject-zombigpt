-- The real movement adapters driven through the real PZAgent.ActionRuntime.
--
-- This is the seam every other movement test steps around: the direct-call
-- suite invokes MoveTo.poll itself and checks the value, and the runtime suite
-- drives spy adapters that already speak the runtime's vocabulary. Neither
-- asks what the runtime *does* with what pollWalk actually returns -- and that
-- gap hid a defect that killed every real walk. pollWalk answered "running"
-- for a walk still under way, a value ActionRuntime's normaliseOutcome does
-- not accept (a table, "done", true or nil are its whole vocabulary), so the
-- runtime terminated the command FAILED/INTERNAL_ERROR on its first poll.
--
-- The groups below fail on that pre-fix code: the first poll of an unfinished
-- walk produced a terminal failed ack and freed the in-flight slot, so both
-- the "still in flight" and the "no terminal ack yet" assertions went red.

local ROOT = arg[0]:match("^(.*)test_movement_runtime%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Mock = dofile(ROOT .. "support/mock_game.lua")
local Command = dofile(ROOT .. "support/command_support.lua")
local Adapter = dofile(ROOT .. "support/adapter_support.lua")

local PZ = Harness.loadModules()
Command.loadModules(Harness.root)
Adapter.loadModules(Harness.root, { "Movement" })

local equal, ok, isNil = Harness.equal, Harness.ok, Harness.isNil
local same = Harness.same

local MoveTo = PZ.Adapters.Movement.MoveTo
local MoveNear = PZ.Adapters.Movement.MoveNear
local STATUS = PZ.Protocol.STATUS
local REASON = PZ.Protocol.REASON
local NOW = Command.NOW

--- A character, a loaded world, a walk class and an empty queue -- the same
--- shape the direct-call movement suite builds, minus the shortcuts: here the
--- adapter is never called by the test, only by the runtime.
local function scene(squares)
  local player = Adapter.player({ x = 100, y = 200, z = 0 })
  local byKey = {}
  for index = 1, #squares do
    local entry = squares[index]
    byKey[Adapter.squareKey(entry[1], entry[2], entry[3])] = Adapter.square(entry[1], entry[2], entry[3], entry[4])
  end
  Adapter.installCell(byKey)
  local walk = Adapter.installAction("ISWalkToTimedAction", function(_, args)
    local moved, square = args[1], args[2]
    moved.state.x = square.getX()
    moved.state.y = square.getY()
    moved.state.z = square.getZ()
  end)
  local queue = Adapter.installQueue()
  return player, walk, queue
end

--- Run every constructed walk and empty the queue, as the game would.
local function finishWalk(walk, queue)
  for index = 1, #walk.actions do
    walk.actions[index]:perform()
  end
  Adapter.drainQueue(queue)
end

local function statuses(fs)
  local records = Command.acks(fs)
  local out = {}
  for index = 1, #records do
    out[index] = records[index].status
  end
  return table.concat(out, ",")
end

Harness.group("a move_to that has to walk stays started across polls, then succeeds")
do
  local player, walk, queue = scene({ { 105, 200, 0 } })
  local agent, fs, runtime = Command.runtime(Mock, { MoveTo })
  agent.player = player
  Command.publish(fs, { Command.command({ action = "movement.move_to", args = { x = 105, y = 200, z = 0 } }) })

  runtime:tick(agent, NOW)
  ok(runtime:inFlight() ~= nil, "the admitted walk holds the in-flight slot")
  equal(statuses(fs), "accepted,accepted,started", "and its ack trail ends at started, with no terminal ack")
  equal(#queue.added, 1, "one tagged walk reached the character's queue")

  -- Two polls with the walk still unfinished: the exact window the pre-fix
  -- code could not cross -- its first poll ended the command
  -- failed/INTERNAL_ERROR ("the adapter returned running").
  runtime:tick(agent, NOW + 100)
  ok(runtime:inFlight() ~= nil, "first poll of the unfinished walk: still in flight, not failed")
  runtime:tick(agent, NOW + 200)
  ok(runtime:inFlight() ~= nil, "second poll of the unfinished walk: still in flight, not failed")
  equal(statuses(fs), "accepted,accepted,started",
    "two polls later the journal still ends at started: nothing failed, nothing finished")
  equal(#Command.terminalAcks(fs), 0, "no terminal ack was written while the character was walking")

  finishWalk(walk, queue)
  runtime:tick(agent, NOW + 300)
  isNil(runtime:inFlight(), "once the character is standing there the slot is free")
  local terminal = Command.terminalAcks(fs)
  equal(#terminal, 1, "exactly one terminal ack ends the command")
  equal(terminal[1].status, STATUS.SUCCEEDED, "and it is a success, proven by arrival")
  same(terminal[1].evidence.position_after, { x = 105, y = 200, z = 0 }, "carrying the observed arrival")
  equal(terminal[1].evidence.arrived, true, "which the evidence states outright")
  equal(player.state.x, 105, "the character was moved by the walk action, never by a coordinate write")
end

Harness.group("a move_near that has to walk stays started across polls, then succeeds")
do
  local crate = Adapter.worldObject({ name = "Crate", container = Adapter.container({}) })
  local player, walk, queue = scene({ { 104, 200, 0, { crate } } })
  local agent, fs, runtime = Command.runtime(Mock, { MoveNear })
  agent.player = player
  Command.publish(fs, {
    Command.command({ action = "movement.move_near", args = { ref = Adapter.worldContainerRef(104, 200, 0, 0) } }),
  })

  runtime:tick(agent, NOW)
  ok(runtime:inFlight() ~= nil, "the admitted approach holds the in-flight slot")
  equal(statuses(fs), "accepted,accepted,started", "and its ack trail ends at started")

  runtime:tick(agent, NOW + 100)
  ok(runtime:inFlight() ~= nil, "first poll of the unfinished approach: still in flight, not failed")
  runtime:tick(agent, NOW + 200)
  ok(runtime:inFlight() ~= nil, "second poll of the unfinished approach: still in flight, not failed")
  equal(#Command.terminalAcks(fs), 0, "no terminal ack was written while the character was walking")

  finishWalk(walk, queue)
  runtime:tick(agent, NOW + 300)
  isNil(runtime:inFlight(), "standing at the container frees the slot")
  local terminal = Command.terminalAcks(fs)
  equal(#terminal, 1, "exactly one terminal ack ends the command")
  equal(terminal[1].status, STATUS.SUCCEEDED, "and it is a success, proven by the closed distance")
  equal(terminal[1].evidence.arrived, true, "with the arrival observed, not assumed")
end

-- The two groups below pin the other half of the same seam: a move whose
-- postcondition already holds when the command arrives. Pre-fix these were
-- red the same way the audit's live probe showed: beginWalk answered "done"
-- without queueing, its three before/after pairs (position, distance, floor)
-- were necessarily identical, and ActionRuntime.verify -- unable to see an
-- unchanged_is_success the "done" string cannot carry -- ended the command
-- FAILED/POSTCONDITION_FAILED, "observed no change: all 3 before/after
-- readings match". A character told to stand where they already stand was
-- reported as having failed to stand there.

Harness.group("a move_to whose target the character already stands on succeeds at once")
do
  local player, _, queue = scene({ { 100, 200, 0 } })
  local agent, fs, runtime = Command.runtime(Mock, { MoveTo })
  agent.player = player
  Command.publish(fs, { Command.command({ action = "movement.move_to", args = { x = 100, y = 200, z = 0 } }) })

  runtime:tick(agent, NOW)
  isNil(runtime:inFlight(), "there is nothing left to drive: the postcondition held before the command")
  equal(#queue.added, 0, "and no walk was queued for a panic stop to have to undo")
  local terminal = Command.terminalAcks(fs)
  equal(#terminal, 1, "one terminal ack ends the command")
  equal(terminal[1].status, STATUS.SUCCEEDED, "and it is a success, not POSTCONDITION_FAILED")
  equal(terminal[1].reason_code, REASON.POSTCONDITION_MET, "for the postcondition that was observed")
  equal(terminal[1].evidence.arrived, true, "the truthful reading: within radius, on the target's floor")
  equal(terminal[1].evidence.distance_before, 0, "the gap was zero before the command did anything")
  equal(terminal[1].evidence.distance_after, 0, "and still is, which here is the postcondition, not a defect")
  same(terminal[1].evidence.position_after, { x = 100, y = 200, z = 0 }, "where the character was observed standing")
  equal(player.state.x, 100, "the character was never moved, least of all by a coordinate write")
end

Harness.group("a move_near to something the character carries succeeds at once")
do
  local player, _, queue = scene({})
  local agent, fs, runtime = Command.runtime(Mock, { MoveNear })
  agent.player = player
  Command.publish(fs, {
    Command.command({ action = "movement.move_near", args = { ref = Adapter.mainRef() } }),
  })

  runtime:tick(agent, NOW)
  isNil(runtime:inFlight(), "a bag on the character's back needs no walking")
  equal(#queue.added, 0, "so nothing reached the character's queue")
  local terminal = Command.terminalAcks(fs)
  equal(#terminal, 1, "one terminal ack ends the command")
  equal(terminal[1].status, STATUS.SUCCEEDED, "and it is a success")
  equal(terminal[1].reason_code, REASON.POSTCONDITION_MET, "for a postcondition that was already true")
  equal(terminal[1].evidence.on_person, true, "the evidence says why the distance was already zero")
  equal(terminal[1].evidence.distance_after, 0, "and reports it")
  equal(terminal[1].evidence.arrived, true, "as an arrival read from the world, not assumed")
end

Harness.finish("movement_runtime")
