--[[
PZAgent.Adapters.Sleep -- survival.sleep.

Invariant: the character is put to sleep by the game's own sleep entry point and
by nothing else. `setAsleep` and the fatigue stat are never written here. The
postcondition is two observations: the character was seen asleep, and fatigue is
lower afterwards than it was before.

Two honest limitations, both of which shape the code rather than being footnotes
to it.

**Sleep is not a queue entry.** It is a character state the game drives to its
own end. There is therefore nothing for PZAgent.Ownership to tag and nothing for
a panic stop to cancel: once the character is asleep, the mod cannot wake them.
That is why this adapter refuses to start unless the reflex guard reports no
danger at all -- a stricter bar than the usual block rank, because every other
action can be cancelled and this one cannot.

**The entry point is a guess.** Vanilla drives sleeping from a context-menu
callback rather than from a timed action class, and there is no Build 42.20 here
to check the name or the argument order against. So it is called through
Toolkit, a build that spells it differently costs a CAPABILITY_UNAVAILABLE
naming the symbol, and a build that accepts the call but does nothing fails on
the postcondition below instead of being reported as a night's sleep.

Sleeping in a vehicle seat is the same call with the vehicle as the target,
which is the same guess again; `allow_vehicle_seat` exists so that guess is only
ever made when the caller asked for it.
]]

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Sleep = {}
PZAgent.Adapters.Sleep = Sleep

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

Sleep.DEFAULT_HOURS = 8
Sleep.MIN_HOURS = 1
Sleep.MAX_HOURS = 16

--- Wall-clock milliseconds. Sleeping runs at accelerated game time, so this is
--- a bound on the command rather than on the night. The floor is one poll
--- interval, below which the wait ends before the character can be seen asleep.
Sleep.DEFAULT_MAX_WAIT_MS = 600000
Sleep.MIN_WAIT_MS = 1000
Sleep.MAX_WAIT_MS = 1800000

Sleep.TIMEOUT_MS = Sleep.MAX_WAIT_MS
Sleep.POLL_MS = 1000

--- The vanilla entry point, and the check that goes with it. Both are looked up
--- rather than called directly, so a rename is a reported gap.
Sleep.ENTRY_POINT = "ISWorldObjectContextMenu.onSleep"
Sleep.PERMISSION_CHECK = "checkCanSleep"

local REQUIRES = { "ISWorldObjectContextMenu", "ISWorldObjectContextMenu.onSleep" }

local SLEEP_ARGS = { "bed_ref", "hours", "allow_vehicle_seat", "max_wait_ms" }

--- Argument kinds, spelled out rather than read from
--- PZAgent.CommandDispatcher.ARG, because the dispatcher is a sibling file and
--- nothing here may dereference a module at load time. A typo costs a refused
--- registration at load rather than a loosened check at dispatch.
local ARG = {
  NUMBER = "number",
  BOOLEAN = "boolean",
  REF = "ref",
}

--- The first object on `square` a character could sleep on.
function Sleep.bedOn(square)
  local Toolkit = toolkit()
  local ok, objects = Toolkit.call(square, "getObjects")
  if not ok or objects == nil then
    return nil, "IsoGridSquare.getObjects"
  end
  local size = Toolkit.listSize(objects)
  if size == nil then
    return nil, "IsoGridSquare.getObjects().size"
  end
  local scanned = math.min(size, Toolkit.MAX_SQUARE_OBJECTS)
  for index = 0, scanned - 1 do
    local object = Toolkit.listGet(objects, index)
    if Toolkit.readBooleanOf(object, { "isBed" }) == true then
      return object
    end
    local okSprite, sprite = Toolkit.call(object, "getSprite")
    if okSprite and sprite ~= nil then
      local name = Toolkit.readStringOf(sprite, { "getName" })
      if type(name) == "string" and name:lower():find("bed", 1, true) ~= nil then
        return object
      end
    end
  end
  return nil
end

--- Does the game itself say this character may sleep right now?
---
--- Returns true, false, or nil plus the symbol that was missing. A build with no
--- check is not a build where sleeping is permitted; it is a build where the
--- question cannot be asked, and the two must not collapse.
function Sleep.maySleep(player)
  local Toolkit = toolkit()
  local menu = Toolkit.globalNamed("ISWorldObjectContextMenu")
  if type(menu) ~= "table" then
    return nil, "ISWorldObjectContextMenu"
  end
  local check = Toolkit.method(menu, Sleep.PERMISSION_CHECK)
  if check == nil then
    return nil, "ISWorldObjectContextMenu." .. Sleep.PERMISSION_CHECK
  end
  local ok, allowed = pcall(check, player)
  if not ok or type(allowed) ~= "boolean" then
    return nil, "ISWorldObjectContextMenu." .. Sleep.PERMISSION_CHECK
  end
  return allowed
end

local function sleepSpec(args, ctx)
  local Toolkit = toolkit()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, SLEEP_ARGS, {})
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local hours, hoursCode, hoursDetail = Toolkit.readCount(args, "hours", {
    default = Sleep.DEFAULT_HOURS,
    minimum = Sleep.MIN_HOURS,
    maximum = Sleep.MAX_HOURS,
  })
  if hours == nil then
    return nil, hoursCode, hoursDetail
  end
  local vehicle, vehicleCode, vehicleDetail = Toolkit.readFlag(args, "allow_vehicle_seat", false)
  if vehicle == nil then
    return nil, vehicleCode, vehicleDetail
  end
  local maxWait, waitCode, waitDetail = Toolkit.readCount(args, "max_wait_ms", {
    default = Sleep.DEFAULT_MAX_WAIT_MS,
    minimum = Sleep.MIN_WAIT_MS,
    maximum = Sleep.MAX_WAIT_MS,
  })
  if maxWait == nil then
    return nil, waitCode, waitDetail
  end
  local spec = { hours = hours, allow_vehicle_seat = vehicle, max_wait_ms = maxWait }
  if args.bed_ref ~= nil then
    local ref, refCode, refDetail = Toolkit.readRef(args, "bed_ref", PZAgent.Refs.KIND.SQUARE, ctx)
    if ref == nil then
      return nil, refCode, refDetail
    end
    local parsed, parseError = PZAgent.Refs.parseSquare(ref)
    if parsed == nil then
      return nil, Toolkit.reasons().INVALID_REF, parseError
    end
    spec.bed_ref = ref
    spec.bed = { x = parsed.x, y = parsed.y, z = parsed.z }
  end
  return spec
end

Sleep.sleepSpec = sleepSpec

--- The thing the character will sleep on: a bed on the named square, or the
--- vehicle they are sitting in when that was permitted.
local function sleepTarget(ctx, spec)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  if spec.bed ~= nil then
    local square, missing = Toolkit.gridSquare(spec.bed.x, spec.bed.y, spec.bed.z)
    if square == nil then
      if missing == "IsoCell.getGridSquare" then
        return nil, reasons.TARGET_NOT_LOADED, "the square the bed stands on is not loaded"
      end
      return Toolkit.unavailable(missing)
    end
    local bed, bedMissing = Sleep.bedOn(square)
    if bed == nil then
      if bedMissing ~= nil then
        return Toolkit.unavailable(bedMissing)
      end
      return nil, reasons.PRECONDITION_FAILED, "there is nothing to sleep on at that square"
    end
    return { object = bed, kind = "bed", point = spec.bed }
  end
  if not spec.allow_vehicle_seat then
    return nil, reasons.INVALID_ARGUMENT, "name a bed square, or permit a vehicle seat"
  end
  local ok, vehicle = Toolkit.call(ctx.player, "getVehicle")
  if not ok then
    return Toolkit.unavailable("IsoPlayer.getVehicle")
  end
  if vehicle == nil then
    return nil, reasons.PRECONDITION_FAILED, "the character is not in a vehicle seat"
  end
  return { object = vehicle, kind = "vehicle_seat" }
end

Sleep.sleepTarget = sleepTarget

local SleepAdapter = nil

local function sleepValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(SleepAdapter.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = sleepSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local snapshot = Toolkit.snapshot(ctx)
  if Toolkit.stat(snapshot, "fatigue") == nil then
    return Toolkit.unavailable("PlayerStats.getFatigue")
  end
  if type(snapshot.asleep) ~= "boolean" then
    -- Without this reading the "was seen asleep" half of the postcondition can
    -- never be observed, and a night that did nothing would look like a night.
    return Toolkit.unavailable("IsoPlayer.isAsleep")
  end
  if snapshot.asleep then
    return nil, reasons.PRECONDITION_FAILED, "the character is already asleep"
  end
  -- Stricter than Safety's usual block rank on purpose: sleeping cannot be
  -- cancelled once it has begun, so "not dangerous enough to stop other work"
  -- is not a good enough answer here.
  local danger = ctx.safety ~= nil and ctx.safety.danger_level or PZAgent.Protocol.DANGER.NONE
  if (PZAgent.Protocol.dangerRank(danger) or 0) > 0 then
    return nil,
      reasons.THREAT_INTERRUPTED,
      string.format("danger is %s, and sleep is the one action the mod cannot cancel", tostring(danger))
  end
  local interruptCode, interruptDetail = Toolkit.interruption(ctx)
  if interruptCode ~= nil then
    return nil, interruptCode, interruptDetail
  end
  local allowed, missing = Sleep.maySleep(ctx.player)
  if allowed == nil then
    return Toolkit.unavailable(missing)
  end
  if not allowed then
    return nil, reasons.PRECONDITION_FAILED, "the game refuses sleep for this character right now"
  end
  local target, targetCode, targetDetail = sleepTarget(ctx, spec)
  if target == nil then
    return nil, targetCode, targetDetail
  end
  spec.target_kind = target.kind
  spec.fatigue_at_start = Toolkit.stat(snapshot, "fatigue")
  Toolkit.state(ctx).sleep = spec
  return true
end

local function sleepPrepare(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = sleepSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local target, targetCode, targetDetail = sleepTarget(ctx, spec)
  if target == nil then
    return nil, targetCode, targetDetail
  end
  local approach, approachCode, approachDetail = Toolkit.approach(ctx, target.point)
  if approach == nil then
    return nil, approachCode, approachDetail
  end
  Toolkit.state(ctx).walking_to_bed = approach == "walking"
  return true
end

local function sleepStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = sleepSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local target, targetCode, targetDetail = sleepTarget(ctx, spec)
  if target == nil then
    return nil, targetCode, targetDetail
  end
  local menu = Toolkit.globalNamed("ISWorldObjectContextMenu")
  if type(menu) ~= "table" then
    return Toolkit.unavailable("ISWorldObjectContextMenu")
  end
  local onSleep = Toolkit.method(menu, "onSleep")
  if onSleep == nil then
    return Toolkit.unavailable(Sleep.ENTRY_POINT)
  end
  -- Argument order as vanilla's context menu documents it. Unverifiable here;
  -- a build that orders them differently produces a character who does not fall
  -- asleep, and the postcondition below is what refuses to call that a success.
  local ok, err = pcall(onSleep, nil, target.object, ctx.player)
  if not ok then
    return nil, Toolkit.reasons().QUEUE_REJECTED, Sleep.ENTRY_POINT .. " failed: " .. tostring(err)
  end
  local state = Toolkit.state(ctx)
  state.started_ms = ctx.now_ms
  state.target_kind = target.kind
  return true
end

local function sleepProgress(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec, specCode, specDetail = sleepSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local state = Toolkit.state(ctx)
  local snapshot = Toolkit.snapshot(ctx)
  if snapshot.asleep == true then
    state.asleep_observed = true
    -- No interruption check while asleep: there is nothing this adapter could
    -- do about it, and reporting THREAT_INTERRUPTED for a state it cannot end
    -- would be a claim to have stopped something that is still running.
    return "running"
  end
  if state.asleep_observed == true then
    return "done"
  end
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local started = state.started_ms or ctx.now_ms
  if (ctx.now_ms - started) >= spec.max_wait_ms then
    return nil, reasons.ACTION_TIMEOUT, "the character never fell asleep"
  end
  return "running"
end

local function sleepVerify(_, before, after, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec, code, detail = sleepSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local state = Toolkit.state(ctx)
  local seen = state.asleep_observed == true
    or (type(before) == "table" and before.asleep == true)
    or (type(after) == "table" and after.asleep == true)
  if not seen then
    return nil, reasons.POSTCONDITION_FAILED, "the character was never observed asleep"
  end
  local was = Toolkit.stat(before, "fatigue")
  local now = Toolkit.stat(after, "fatigue")
  if now == nil then
    return Toolkit.unavailable("PlayerStats.getFatigue")
  end
  if was == nil then
    return nil, reasons.POSTCONDITION_FAILED, "there is no fatigue reading from before the sleep to compare against"
  end
  if now >= was then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format("fatigue went from %.3f to %.3f, so nothing was slept off", was, now)
  end
  return {
    kind = "fatigue_fell",
    fatigue_before = was,
    fatigue_after = now,
    hours_requested = spec.hours,
    slept_on = state.target_kind or spec.target_kind,
    bed_ref = spec.bed_ref,
  }
end

SleepAdapter = toolkit().declare({
  name = "survival.sleep",
  -- No probe declares a sleep capability, and §12.4 does not list one. The
  -- symbols in `requires` plus the guarded entry point are the whole gate.
  capability = nil,
  requires = REQUIRES,
  timeout_ms = Sleep.TIMEOUT_MS,
  poll_interval_ms = Sleep.POLL_MS,
  args = {
    bed_ref = { type = ARG.REF, kinds = { square = true } },
    -- The bounds sleepSpec reads, from the same constants, so the declaration
    -- and the check cannot drift apart. Whole hours: the entry point takes a
    -- night, not a fraction of one.
    hours = { type = ARG.NUMBER, integer = true, min = Sleep.MIN_HOURS, max = Sleep.MAX_HOURS },
    -- Off by default, because sleeping in a seat is the same unverified entry
    -- point aimed at a vehicle, and that guess is only made when it was asked
    -- for.
    allow_vehicle_seat = { type = ARG.BOOLEAN },
    max_wait_ms = { type = ARG.NUMBER, integer = true, min = Sleep.MIN_WAIT_MS, max = Sleep.MAX_WAIT_MS },
  },
  validate = sleepValidate,
  prepare = sleepPrepare,
  begin = sleepStart,
  progress = sleepProgress,
  verify = sleepVerify,
})

Sleep.Adapter = SleepAdapter
toolkit().register(SleepAdapter)

return Sleep
