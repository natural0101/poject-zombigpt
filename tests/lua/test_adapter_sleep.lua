-- PZAgent.Adapters.Sleep: the one action the mod cannot cancel once it starts.
--
-- Two groups carry the file, and both are about that fact. "sleep refuses above
-- its own danger floor" asserts the bar is stricter than PZAgent.Safety's block
-- rank -- a danger level every other adapter is allowed to work through stops
-- this one, because a sleeping character cannot react and nothing here can wake
-- them. "danger is re-read on every poll" asserts the check is not spent at the
-- start: a horde that arrives while the character is still lying down ends the
-- command, and the one case where it deliberately does not is pinned too.
--
-- The entry point is called through a double that records and changes nothing.
-- The character is put to sleep by the test, never by the adapter, so "the call
-- did not raise" can never stand in for "the character slept".

local ROOT = arg[0]:match("^(.*)test_adapter_sleep%.lua$") or ""
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/adapter_support.lua")
local PZ = Harness.loadModules()
dofile(Harness.root .. "pz-mod/42/media/lua/client/PZAgent/CommandDispatcher.lua")
Support.loadModules(Harness.root, { "Sleep" })

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local Toolkit = PZ.Adapters.Toolkit
local Sleep = PZ.Adapters.Sleep
local Adapter = Sleep.Adapter
local REASON = PZ.Protocol.REASON

local BED_SQUARE = { x = 101, y = 200, z = 0 }

--- The vanilla context-menu entry point, as a double that records the call and
--- changes nothing. Falling asleep is something a test does to the character
--- afterwards: an entry point that put them to sleep by itself would let the
--- adapter verify against its own call.
local function installMenu(options)
  options = options or {}
  local record = { calls = {} }
  if options.no_menu then
    _G["ISWorldObjectContextMenu"] = nil
    return record
  end
  local menu = {}
  if not options.no_permission_check then
    menu.checkCanSleep = function(_)
      if options.check_unreadable then
        return "maybe"
      end
      return options.may_sleep ~= false
    end
  end
  if not options.no_entry_point then
    menu.onSleep = function(_, object, player)
      if options.entry_fails then
        error("mock onSleep failure", 0)
      end
      record.calls[#record.calls + 1] = { object = object, player = player }
    end
  end
  _G["ISWorldObjectContextMenu"] = menu
  return record
end

--- A safety state the reflex guard has set to `level`.
local function atDanger(level)
  local safety = PZ.Safety.newState()
  safety.armed = true
  safety.mode = PZ.Protocol.MODE.ASSISTED
  PZ.Safety.setDanger(safety, level)
  return safety
end

local function scene(options)
  options = options or {}
  local stats = nil
  if not options.no_stats then
    stats = Support.stats({ fatigue = options.fatigue or 0.8 })
  end
  local player = Support.player({ x = 100, y = 200, z = 0, stats = stats, asleep = options.asleep })
  if options.no_asleep_reading then
    player.isAsleep = nil
  end
  if options.vehicle ~= nil then
    player.getVehicle = function()
      return options.vehicle
    end
  elseif not options.no_vehicle_accessor then
    player.getVehicle = function()
      return nil
    end
  end

  local bed = Support.worldObject({ name = "bed", can_sleep = true })
  local table_ = Support.worldObject({ name = "table", sprite = "furniture_tables_01" })
  Support.installCell({
    [Support.squareKey(BED_SQUARE.x, BED_SQUARE.y, BED_SQUARE.z)] = Support.square(
      BED_SQUARE.x,
      BED_SQUARE.y,
      BED_SQUARE.z,
      { options.no_bed and table_ or bed }
    ),
  })
  Support.installAction("ISWalkToTimedAction")
  local queue = Support.installQueue()
  local menu = installMenu(options)
  return {
    player = player,
    stats = stats,
    bed = bed,
    menu = menu,
    queue = queue,
    ctx = Support.context({ player = player, safety = options.safety, now_ms = options.now_ms }),
  }
end

local BED = Support.squareRef(BED_SQUARE.x, BED_SQUARE.y, BED_SQUARE.z)
local ITEM = Support.itemRef("player-main", 1)

Harness.group("sleep refuses a command it cannot act on")
do
  local s = scene()
  local function refuse(args, ctx)
    local accepted, code, detail = Adapter:validate(args, ctx or s.ctx)
    isNil(accepted, "the command is refused")
    return code, detail
  end

  ok(Adapter:validate({ bed_ref = BED }, s.ctx), "a tired character beside a bed, in no danger, may sleep")

  local code = refuse({ bed = BED })
  equal(code, REASON.INVALID_ARGUMENT, "an unknown argument is refused rather than ignored")

  code = refuse({ bed_ref = BED, hours = 0 })
  equal(code, REASON.INVALID_ARGUMENT, "no hours is not a night")

  code = refuse({ bed_ref = BED, hours = Sleep.MAX_HOURS + 1 })
  equal(code, REASON.INVALID_ARGUMENT, "and more hours than the ceiling is not either")

  code = refuse({ bed_ref = ITEM })
  equal(code, REASON.INVALID_REF, "an item is not a place to lie down")

  local detail
  code, detail = refuse({})
  equal(code, REASON.INVALID_ARGUMENT, "naming nowhere to sleep is not a command")
  contains(detail, "bed square", "and the detail says what was missing")

  code, detail = refuse({ allow_vehicle_seat = true })
  equal(code, REASON.PRECONDITION_FAILED, "permitting a vehicle seat does not put the character in one")
  contains(detail, "not in a vehicle", "with the reason it was refused")

  local nobed = scene({ no_bed = true })
  code, detail = refuse({ bed_ref = BED }, nobed.ctx)
  equal(code, REASON.PRECONDITION_FAILED, "a square with nothing to sleep on is not a bed")
  contains(detail, "nothing to sleep on", "with the reason it was refused")

  local asleep = scene({ asleep = true })
  code = refuse({ bed_ref = BED }, asleep.ctx)
  equal(code, REASON.PRECONDITION_FAILED, "a character who is already asleep is not put to sleep again")

  local refused = scene({ may_sleep = false })
  code, detail = refuse({ bed_ref = BED }, refused.ctx)
  equal(code, REASON.PRECONDITION_FAILED, "and the game's own answer is taken when it says no")
  contains(detail, "refuses sleep", "rather than being talked around")

  local taken = scene({ safety = Support.takenOver() })
  code = refuse({ bed_ref = BED }, taken.ctx)
  equal(code, REASON.USER_TAKEOVER, "a character the player is driving is not put to bed")
end

Harness.group("sleep refuses above its own danger floor, which is lower than everything else's")
do
  local calm = scene()
  ok(Adapter:validate({ bed_ref = BED }, calm.ctx), "with no danger at all the command validates")

  for _, level in ipairs({ PZ.Protocol.DANGER.LOW, PZ.Protocol.DANGER.MEDIUM, PZ.Protocol.DANGER.HIGH }) do
    local s = scene({ safety = atDanger(level) })
    local accepted, code, detail = Adapter:validate({ bed_ref = BED }, s.ctx)
    isNil(accepted, "danger " .. level .. " is refused")
    equal(code, REASON.THREAT_INTERRUPTED, "as a threat, at danger " .. level)
    contains(detail, "cannot cancel", "with the reason this action is held to a stricter bar")
  end

  -- The bar really is this adapter's own. At LOW the shared interruption check
  -- -- the one every other adapter consults -- says there is nothing to stop
  -- for, so an adapter that had only called that would have gone to sleep in
  -- front of a threat it could never wake up from.
  local low = scene({ safety = atDanger(PZ.Protocol.DANGER.LOW) })
  isNil(Toolkit.interruption(low.ctx), "the shared block rank would have let a low danger through")
  ok(
    PZ.Protocol.dangerRank(PZ.Protocol.DANGER.LOW) < PZ.Safety.DANGER_BLOCK_RANK,
    "which is what makes low danger the case worth testing"
  )
end

Harness.group("a missing entry point costs a capability, not a crash")
do
  local s = scene({ no_menu = true })
  local accepted, code, detail = Adapter:validate({ bed_ref = BED }, s.ctx)
  isNil(accepted, "with no context menu there is nothing to validate")
  equal(code, REASON.CAPABILITY_UNAVAILABLE, "the gap is a capability")
  contains(detail, "ISWorldObjectContextMenu", "naming the symbol this build did not have")

  local renamed = scene({ no_entry_point = true })
  local _, entryCode, entryDetail = Adapter:validate({ bed_ref = BED }, renamed.ctx)
  equal(entryCode, REASON.CAPABILITY_UNAVAILABLE, "a build that spells the entry point differently is the same gap")
  contains(entryDetail, "ISWorldObjectContextMenu.onSleep", "naming the member that was looked for")

  local uncheckable = scene({ no_permission_check = true })
  local _, checkCode, checkDetail = Adapter:validate({ bed_ref = BED }, uncheckable.ctx)
  equal(checkCode, REASON.CAPABILITY_UNAVAILABLE, "a build with no permission check is not a build that permits sleep")
  contains(checkDetail, "checkCanSleep", "naming the check that could not be asked")

  local unreadable = scene({ check_unreadable = true })
  local _, oddCode = Adapter:validate({ bed_ref = BED }, unreadable.ctx)
  equal(oddCode, REASON.CAPABILITY_UNAVAILABLE, "and an answer that is not a yes or a no is not an answer")

  local blind = scene({ no_stats = true })
  local _, statCode, statDetail = Adapter:validate({ bed_ref = BED }, blind.ctx)
  equal(statCode, REASON.CAPABILITY_UNAVAILABLE, "a build reporting no fatigue has no postcondition to meet")
  contains(statDetail, "PlayerStats.getFatigue", "naming the accessor that was missing")

  local unseen = scene({ no_asleep_reading = true })
  local _, asleepCode, asleepDetail = Adapter:validate({ bed_ref = BED }, unseen.ctx)
  equal(asleepCode, REASON.CAPABILITY_UNAVAILABLE, "and one that cannot say whether the character is asleep")
  contains(asleepDetail, "IsoPlayer.isAsleep", "naming that accessor too")
end

Harness.group("starting calls the entry point and nothing else")
do
  local s = scene()
  local args = { bed_ref = BED }
  ok(Adapter:validate(args, s.ctx), "the command validates")
  ok(Adapter:prepare(args, s.ctx), "and prepares")
  ok(Adapter:begin(args, s.ctx), "and starts")
  equal(#s.menu.calls, 1, "the entry point was called once")
  equal(s.menu.calls[1].object, s.bed, "against the bed that was named")
  equal(s.menu.calls[1].player, s.player, "for the character that asked")
  equal(#s.queue.added, 0, "with nothing queued, because sleep is a state and not a queue entry")
  equal(Toolkit.state(s.ctx).target_kind, "bed", "and what was slept on is recorded")

  local broken = scene({ entry_fails = true })
  ok(Adapter:validate(args, broken.ctx), "a build whose entry point raises still validates")
  ok(Adapter:prepare(args, broken.ctx), "and prepares")
  local started, code, detail = Adapter:begin(args, broken.ctx)
  isNil(started, "but the start fails")
  equal(code, REASON.QUEUE_REJECTED, "as a rejection rather than an unwound dispatcher")
  contains(detail, Sleep.ENTRY_POINT, "naming the entry point that raised")

  local seated = scene({ vehicle = { getId = function()
    return 4
  end } })
  ok(Adapter:validate({ allow_vehicle_seat = true }, seated.ctx), "a character in a vehicle may sleep in the seat")
  equal(Toolkit.state(seated.ctx).sleep.target_kind, "vehicle_seat", "and the seat is what they sleep on")
end

Harness.group("danger is re-read on every poll, not spent at the start")
do
  local s = scene()
  local args = { bed_ref = BED }
  ok(Adapter:validate(args, s.ctx), "the command starts calm")
  ok(Adapter:prepare(args, s.ctx), "and prepares")
  ok(Adapter:begin(args, s.ctx), "and starts")
  equal(Adapter:progress(args, s.ctx), "running", "a character still lying down is still working on it")

  s.ctx.safety = Support.threatened()
  local status, code = Adapter:progress(args, s.ctx)
  isNil(status, "a horde arriving before the character fell asleep ends the command")
  equal(code, REASON.THREAT_INTERRUPTED, "with the reflex guard's reason, from the poll rather than the start")

  -- The floor is stricter than the shared check only at the start. Once the
  -- command is running the poll consults PZAgent.Safety's ordinary block rank,
  -- so a low danger that arrives afterwards is not enough to end it.
  local low = scene()
  ok(Adapter:validate(args, low.ctx), "a second command starts calm")
  ok(Adapter:begin(args, low.ctx), "and runs")
  low.ctx.safety = atDanger(PZ.Protocol.DANGER.LOW)
  equal(Adapter:progress(args, low.ctx), "running", "and a low danger afterwards is below the poll's bar")

  -- And once the character is actually asleep the poll stops reporting threats
  -- entirely: the mod cannot wake them, so THREAT_INTERRUPTED would be a claim
  -- to have stopped something that is still running.
  local sleeping = scene()
  ok(Adapter:validate(args, sleeping.ctx), "a third command starts calm")
  ok(Adapter:begin(args, sleeping.ctx), "and runs")
  sleeping.player.state.asleep = true
  equal(Adapter:progress(args, sleeping.ctx), "running", "the character is seen asleep")
  sleeping.ctx.safety = Support.threatened()
  equal(Adapter:progress(args, sleeping.ctx), "running", "and a horde over a sleeping character is not an interruption")
end

Harness.group("a night that never happened is not a night")
do
  local s = scene()
  local args = { bed_ref = BED }
  ok(Adapter:validate(args, s.ctx), "the command validates")
  ok(Adapter:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  ok(Adapter:begin(args, s.ctx), "and starts")

  -- The entry point was called and the character never lay down: nothing was
  -- queued, so there is not even a drained queue to mistake for progress.
  local evidence, code, detail = Adapter:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "a character who was never seen asleep did not sleep")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command failed its postcondition")
  contains(detail, "never observed asleep", "and the detail says exactly what was not observed")

  s.ctx.now_ms = Support.NOW + Sleep.DEFAULT_MAX_WAIT_MS
  local status, timeoutCode, timeoutDetail = Adapter:progress(args, s.ctx)
  isNil(status, "and the wait runs out rather than running forever")
  equal(timeoutCode, REASON.ACTION_TIMEOUT, "as a timeout")
  contains(timeoutDetail, "never fell asleep", "naming what did not happen")
end

Harness.group("a sleep that shed no fatigue is not a sleep either")
do
  local s = scene()
  local args = { bed_ref = BED }
  ok(Adapter:validate(args, s.ctx), "the command validates")
  ok(Adapter:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  ok(Adapter:begin(args, s.ctx), "and starts")

  s.player.state.asleep = true
  equal(Adapter:progress(args, s.ctx), "running", "the character is seen asleep")
  s.player.state.asleep = false
  equal(Adapter:progress(args, s.ctx), "done", "and waking is what ends the command")

  local evidence, code, detail = Adapter:verify(before, Toolkit.observe(s.player), args, s.ctx)
  isNil(evidence, "being seen asleep is half a postcondition, not a whole one")
  equal(code, REASON.POSTCONDITION_FAILED, "so the command still fails")
  contains(detail, "nothing was slept off", "and the detail says which half was missing")
end

Harness.group("asleep and less tired is the evidence")
do
  local s = scene()
  local args = { bed_ref = BED, hours = 6 }
  ok(Adapter:validate(args, s.ctx), "the command validates")
  ok(Adapter:prepare(args, s.ctx), "and prepares")
  local before = Toolkit.observe(s.player)
  ok(Adapter:begin(args, s.ctx), "and starts")

  s.player.state.asleep = true
  equal(Adapter:progress(args, s.ctx), "running", "the character is seen asleep")
  s.stats.state.fatigue = 0.15
  s.player.state.asleep = false
  equal(Adapter:progress(args, s.ctx), "done", "and the night ends")

  local proof = Adapter:verify(before, Toolkit.observe(s.player), args, s.ctx)
  ok(proof ~= nil, "seen asleep and less tired afterwards is the evidence")
  equal(proof.kind, "fatigue_fell", "named for the reading that moved")
  equal(proof.fatigue_before, 0.8, "carrying the reading before")
  equal(proof.fatigue_after, 0.15, "and the reading after")
  equal(proof.hours_requested, 6, "with the night that was asked for")
  equal(proof.slept_on, "bed", "and what was slept on")
  equal(proof.bed_ref, BED, "naming the square it stood on")
end

Harness.group("the adapter is registered where the dispatcher looks for it")
do
  equal(PZ.Adapters.BY_NAME["survival.sleep"], Adapter, "sleep is registered under its action name")
  equal(Adapter.action, "survival.sleep", "and names itself under the key the dispatcher reads")
  ok(PZ.Protocol.isKnownAction(Adapter.action), "which is in the protocol whitelist")
  ok(type(Adapter.start) == "function", "the runtime's start() exists")
  ok(type(Adapter.poll) == "function", "and so does its poll()")
end

Harness.group("the declaration is what the dispatcher builds the args from")
do
  local checkArgs = PZ.CommandDispatcher.checkArgs
  local night = checkArgs(Adapter, {
    bed_ref = BED,
    hours = 8,
    allow_vehicle_seat = true,
    max_wait_ms = 60000,
  }, Support.SESSION)
  equal(night.bed_ref, BED, "the bed survives the rebuild")
  equal(night.hours, 8, "and so do the hours")
  equal(night.allow_vehicle_seat, true, "and the seat the caller permitted")
  equal(night.max_wait_ms, 60000, "and the wait they bounded it with")

  local _, hoursCode = checkArgs(Adapter, { bed_ref = BED, hours = Sleep.MAX_HOURS + 1 }, Support.SESSION)
  equal(hoursCode, REASON.INVALID_ARGUMENT, "a night longer than the ceiling never reaches the adapter")

  local _, fractionCode = checkArgs(Adapter, { bed_ref = BED, hours = 1.5 }, Support.SESSION)
  equal(fractionCode, REASON.INVALID_ARGUMENT, "nor does half an hour, which the entry point has no meaning for")

  local _, waitCode = checkArgs(Adapter, { bed_ref = BED, max_wait_ms = Sleep.MAX_WAIT_MS + 1 }, Support.SESSION)
  equal(waitCode, REASON.INVALID_ARGUMENT, "and a command cannot be told to wait for a character indefinitely")

  local defaulted = checkArgs(Adapter, { bed_ref = BED }, Support.SESSION)
  isNil(defaulted.allow_vehicle_seat, "the vehicle seat is absent unless it was asked for")

  local _, unknownCode = checkArgs(Adapter, { bed = BED }, Support.SESSION)
  equal(unknownCode, REASON.INVALID_ARGUMENT, "an undeclared argument is refused rather than dropped")

  local _, kindCode = checkArgs(Adapter, { bed_ref = ITEM }, Support.SESSION)
  equal(kindCode, REASON.INVALID_REF, "and an item is not a place to lie down")
end

Harness.finish("adapter_sleep")
