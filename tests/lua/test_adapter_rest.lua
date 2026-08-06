-- PZAgent.Adapters.Rest: recovering endurance without queueing anything.
--
-- The group that matters is "a rest that recovered nothing is not a rest": this
-- is the one adapter whose start legitimately queues no action, so there is no
-- queue entry to mistake for progress and nothing but the endurance reading can
-- promote the command. The seat is the other half -- asking for a chair this
-- build cannot construct is a capability gap naming the classes, while merely
-- preferring the ground is not.

local ROOT = arg[0]:match("^(.*)test_adapter_rest%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
Support.loadModules(Harness.root, { "Rest" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local Toolkit = PZ.Adapters.Toolkit
local Rest = PZ.Adapters.Rest
local Adapter = Rest.Adapter
local REASON = PZ.Protocol.REASON

local SEAT_SQUARE = { x = 101, y = 200, z = 0 }

local function scene(options)
  options = options or {}
  local stats = nil
  if not options.no_stats then
    stats = Support.stats({ endurance = options.endurance or 0.4 })
  end
  local player = Support.player({ x = 100, y = 200, z = 0, stats = stats })

  local chair = Support.worldObject({ name = "chair", sprite = "furniture_seating_indoor_chair_01" })
  local bare = Support.worldObject({ name = "rug", sprite = "floors_rugs_01" })
  Support.installCell({
    [Support.squareKey(SEAT_SQUARE.x, SEAT_SQUARE.y, SEAT_SQUARE.z)] = Support.square(
      SEAT_SQUARE.x,
      SEAT_SQUARE.y,
      SEAT_SQUARE.z,
      { options.no_seat and bare or chair }
    ),
  })
  Support.installAction("ISWalkToTimedAction")
  if not options.no_chair_class then
    Support.installAction("ISSitOnChairAction")
  else
    Support.removeAction("ISSitOnChairAction")
    Support.removeAction("ISSitOnChair")
  end
  if options.ground then
    Support.installAction("ISSitOnGround")
  else
    Support.removeAction("ISSitOnGround")
    Support.removeAction("ISSitOnGroundAction")
  end
  local queue = Support.installQueue()
  return {
    player = player,
    stats = stats,
    ctx = Support.context({ player = player, safety = options.safety, now_ms = options.now_ms }),
    queue = queue,
  }
end

local SEAT = Support.squareRef(SEAT_SQUARE.x, SEAT_SQUARE.y, SEAT_SQUARE.z)
local ITEM = Support.itemRef("player-main", 1)

Harness.group("rest refuses a command it cannot act on")
do
  local s = scene()
  local function refuse(args, ctx)
    local accepted, code, detail = Adapter:validate(args, ctx or s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  local code = refuse({ endurance = 0.9 })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  code = refuse({ target_endurance = 0 })
  equal(code, REASON.INVALID_ARGUMENT, "a target nothing could fall short of is not a target")

  code = refuse({ target_endurance = 1.5 })
  equal(code, REASON.INVALID_ARGUMENT, "and one the stat cannot reach is not either")

  code = refuse({ max_wait_ms = 10 })
  equal(code, REASON.INVALID_ARGUMENT, "a wait shorter than a poll cannot observe anything")

  code = refuse({ max_wait_ms = 1500.5 })
  equal(code, REASON.INVALID_ARGUMENT, "and milliseconds come whole")

  code = refuse({ seat_ref = ITEM })
  equal(code, REASON.INVALID_REF, "an item is not a place to sit")

  local detail
  local rested = scene({ endurance = 0.95 })
  code, detail = refuse({}, rested.ctx)
  equal(code, REASON.PRECONDITION_FAILED, "a character who is already rested has nothing to recover")
  contains(detail, "already", "and the detail says so")

  local threatened = scene({ safety = Support.threatened() })
  code = refuse({}, threatened.ctx)
  equal(code, REASON.THREAT_INTERRUPTED, "and resting still and unaware does not start into a horde")
end

Harness.group("a build that reports no endurance costs a capability, not a guess")
do
  local blind = scene({ no_stats = true })
  local accepted, code, detail = Adapter:validate({}, blind.ctx)
  isNil(accepted, "without a reading there is no postcondition this command could meet")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "PlayerStats.getEndurance", "naming the accessor this build did not have")

  local queueless = scene()
  Support.removeQueue()
  local _, queueCode, queueDetail = Adapter:validate({}, queueless.ctx)
  equal(queueCode, REASON.CAPABILITY_UNAVAILABLE, "and a build with no action queue is the same kind of gap")
  contains(queueDetail, "ISTimedActionQueue.add", "naming the symbol it could not reach")
end

Harness.group("a seat that was asked for is a seat that must exist")
do
  local s = scene({ no_chair_class = true })
  local args = { seat_ref = SEAT }
  ok(Adapter:validate(args, s.ctx), "the command validates")
  local prepared, code, detail = Adapter:prepare(args, s.ctx)
  isNil(prepared, "a build with no sitting action cannot honour a seat that was named")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "which is a capability gap")
  contains(detail, "ISSitOnChairAction", "naming the classes that were looked for")

  local empty = scene({ no_seat = true })
  local _, seatCode, seatDetail = Adapter:prepare(args, empty.ctx)
  equal(seatCode, REASON.PRECONDITION_FAILED, "and a square with nothing to sit on is not a seat")
  contains(seatDetail, "nothing to sit on", "with the reason it was refused")

  local seated = scene()
  ok(Adapter:validate(args, seated.ctx), "with the class present the command validates")
  ok(Adapter:prepare(args, seated.ctx), "and prepares")
  equal(#seated.queue.added, 1, "one action was queued")
  equal(seated.queue.added[1].Type, "ISSitOnChairAction", "and it is the sit the caller asked for")
  equal(Toolkit.state(seated.ctx).seated, "chair", "the posture is recorded from what was queued")
end

Harness.group("the ground is a preference, and standing is the fallback")
do
  local s = scene()
  ok(Adapter:validate({ allow_ground = true }, s.ctx), "the command validates")
  ok(Adapter:prepare({ allow_ground = true }, s.ctx), "and prepares on a build with no ground action")
  equal(Toolkit.state(s.ctx).seated, "standing", "the character rests standing instead")
  equal(#s.queue.added, 0, "with nothing queued, because nothing was asked for that could not be done")

  local ground = scene({ ground = true })
  ok(Adapter:validate({ allow_ground = true }, ground.ctx), "with a ground action the command validates")
  ok(Adapter:prepare({ allow_ground = true }, ground.ctx), "and prepares")
  equal(ground.queue.added[1].Type, "ISSitOnGround", "the sit is queued and tagged like any other action")
  equal(Toolkit.state(ground.ctx).seated, "ground", "and the posture says where the character is")
end

Harness.group("rest queues nothing of its own")
do
  local s = scene()
  ok(Adapter:validate({}, s.ctx), "the command validates")
  ok(Adapter:prepare({}, s.ctx), "and prepares")
  ok(Adapter:begin({}, s.ctx), "and starts")
  equal(#s.queue.added, 0, "standing still is the absence of an action, not a filler one")
  equal(Toolkit.state(s.ctx).started_ms, Support.NOW, "the clock the wait is measured against starts here")
end

Harness.group("progress is read off endurance, not off the queue")
do
  local s = scene()
  ok(Adapter:validate({}, s.ctx), "the command validates")
  ok(Adapter:begin({}, s.ctx), "and starts")
  equal(Adapter:progress({}, s.ctx), "running", "an unrecovered character is still resting")

  s.stats.state.endurance = 0.95
  equal(Adapter:progress({}, s.ctx), "done", "endurance reaching the target is what ends the command")

  local threatened = scene()
  ok(Adapter:validate({}, threatened.ctx), "a second rest starts calm")
  ok(Adapter:begin({}, threatened.ctx), "and runs")
  threatened.ctx.safety = Support.threatened()
  local status, code = Adapter:progress({}, threatened.ctx)
  isNil(status, "a horde arriving mid-rest ends it")
  equal(code, REASON.THREAT_INTERRUPTED, "with the reflex guard's reason, on the poll rather than only at the start")
end

Harness.group("endurance that stops rising is not a rest that is still working")
do
  local s = scene()
  ok(Adapter:validate({}, s.ctx), "the command validates")
  ok(Adapter:begin({}, s.ctx), "and starts")
  equal(Adapter:progress({}, s.ctx), "running", "the first poll marks where endurance stood")

  s.ctx.now_ms = Support.NOW + Rest.STALL_MS
  local status, code, detail = Adapter:progress({}, s.ctx)
  isNil(status, "endurance that has not risen since is not recovering")
  equal(code, REASON.NO_PROGRESS, "which is its own reason, not a timeout")
  contains(detail, "has not risen", "and the detail says what was watched")

  local waited = scene()
  ok(Adapter:validate({ max_wait_ms = 2000 }, waited.ctx), "a short wait validates")
  ok(Adapter:begin({ max_wait_ms = 2000 }, waited.ctx), "and starts")
  waited.ctx.now_ms = Support.NOW + 2000
  local timedOut, timeoutCode = Adapter:progress({ max_wait_ms = 2000 }, waited.ctx)
  isNil(timedOut, "a wait that ran out ends the command")
  equal(timeoutCode, REASON.ACTION_TIMEOUT, "as a timeout, which is a different fact from a stall")
end

Harness.group("a rest that recovered nothing is not a rest")
do
  local s = scene()
  local args = {}
  ok(Adapter:validate(args, s.ctx), "the command validates")
  ok(Adapter:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  ok(Adapter:begin(args, s.ctx), "and starts")

  -- Time passes and the character recovers nothing: there is no queue entry to
  -- have drained here, so an adapter that believed its own start would report a
  -- rest that never happened.
  local evidence, code, detail = Adapter:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "endurance that did not move proves nothing")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed its postcondition")
  contains(detail, "nothing was recovered", "and the detail says what was not observed")

  s.stats.state.endurance = 0.5
  local short, shortCode, shortDetail = Adapter:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(short, "and endurance that rose but fell short is not the rest that was asked for")
  equal(shortCode, REASON.POSTCONDITION_FAILED, "which is the same refusal")
  contains(shortDetail, "short of", "with a detail that says how far")
end

Harness.group("endurance both higher and at the target is the evidence")
do
  local s = scene()
  local args = { seat_ref = SEAT }
  ok(Adapter:validate(args, s.ctx), "the command validates")
  ok(Adapter:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  ok(Adapter:begin(args, s.ctx), "and starts")

  s.stats.state.endurance = 0.93
  local proof = Adapter:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "endurance above the target and above where it started is the evidence")
  equal(proof.kind, "endurance_recovered", "named for what was observed")
  equal(proof.endurance_before, 0.4, "carrying the reading before")
  equal(proof.endurance_after, 0.93, "and the reading after")
  equal(proof.target_endurance, Rest.DEFAULT_TARGET, "with the target it was measured against")
  equal(proof.posture, "chair", "and where the character rested")
  equal(proof.seat_ref, SEAT, "naming the seat that was asked for")
end

Harness.group("the adapter is registered where the dispatcher looks for it")
do
  equal(PZ.Adapters.BY_NAME["survival.rest"], Adapter, "rest is registered under its action name")
  equal(Adapter.action, "survival.rest", "and names itself under the key the dispatcher reads")
  ok(PZ.Protocol.isKnownAction(Adapter.action), "which is in the protocol whitelist")
  ok(type(Adapter.start) == "function", "the runtime's start() exists")
  ok(type(Adapter.poll) == "function", "and so does its poll()")
end

Harness.group("the declaration is what the dispatcher builds the args from")
do
  local checkArgs = PZ.CommandDispatcher.checkArgs
  local rest = checkArgs(Adapter, {
    target_endurance = 0.8,
    seat_ref = SEAT,
    allow_ground = true,
    max_wait_ms = 60000,
  }, Support.SESSION)
  equal(rest.target_endurance, 0.8, "the target survives the rebuild")
  equal(rest.seat_ref, SEAT, "and so does the seat")
  equal(rest.allow_ground, true, "and the ground the caller permitted")
  equal(rest.max_wait_ms, 60000, "and the wait they bounded it with")

  local _, highCode = checkArgs(Adapter, { target_endurance = 1.5 }, Support.SESSION)
  equal(highCode, REASON.INVALID_ARGUMENT, "a target above the stat's ceiling never reaches the adapter")

  local _, lowCode = checkArgs(Adapter, { target_endurance = 0.001 }, Support.SESSION)
  equal(lowCode, REASON.INVALID_ARGUMENT, "nor does one below the floor")

  local _, waitCode = checkArgs(Adapter, { max_wait_ms = Rest.MAX_WAIT_MS + 1 }, Support.SESSION)
  equal(waitCode, REASON.INVALID_ARGUMENT, "and a character cannot be told to sit until something else moves them")

  local _, flagCode = checkArgs(Adapter, { allow_ground = "yes" }, Support.SESSION)
  equal(flagCode, REASON.INVALID_ARGUMENT, "a flag is a boolean, not a word that looks like one")

  local _, unknownCode = checkArgs(Adapter, { endurance = 0.9 }, Support.SESSION)
  equal(unknownCode, REASON.INVALID_ARGUMENT, "an undeclared argument is refused rather than dropped")

  local _, kindCode = checkArgs(Adapter, { seat_ref = ITEM }, Support.SESSION)
  equal(kindCode, REASON.INVALID_REF, "and an item is not a place to sit")
end

Harness.finish("adapter_rest")
