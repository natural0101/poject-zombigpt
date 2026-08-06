--[[
PZAgent.Adapters.Rest -- survival.rest.

Invariant: endurance is recovered by the game's own recovery, which happens
while the character is not doing anything else. Nothing here writes the stat, and
the only thing that may promote the command to succeeded is an endurance reading
that is both higher than it was and at or above the target that was asked for.

That makes this the one adapter whose "start" legitimately queues nothing. A
standing rest *is* the absence of a queued action, and inventing a filler timed
action so the lifecycle looked more conventional would put a mod-owned entry in
the player's queue for no reason and give a panic stop something to cancel that
never needed doing. Sitting is different: it is a real action, so when a seat is
asked for it is queued, tagged and cancellable like any other.

Sitting is also optional in a way the seat request is not. If no seat was named
and this build exposes no ground-sitting action, the character rests standing and
the evidence records that it did. If a seat *was* named and no sitting action
exists, that is CAPABILITY_UNAVAILABLE naming the classes that were looked for --
the caller asked for something this build cannot do, and quietly standing there
instead would answer a different request.

Resting holds the character still and unaware, so the threat check is consulted
before the command starts and on every poll.
]]

PZAgent = PZAgent or {}
PZAgent.Adapters = PZAgent.Adapters or {}

local Rest = {}
PZAgent.Adapters.Rest = Rest

local function toolkit()
  return PZAgent.Adapters.Toolkit
end

--- Endurance runs 0..1 and full is 1. Nine tenths is "ready to run again"
--- without waiting out the long tail of the last tenth.
Rest.DEFAULT_TARGET = 0.9

--- A target under a twentieth is met by drawing breath rather than by resting,
--- and the stat does not read above 1, so a target above it could never be met.
Rest.MIN_TARGET = 0.05
Rest.MAX_TARGET = 1.0

--- Wall-clock milliseconds one rest may occupy before it is a failure rather
--- than a wait. Long, because endurance recovery is slow, but bounded.
--- The floor is one poll interval: a wait that expires before endurance has
--- been read a second time cannot observe anything.
Rest.DEFAULT_MAX_WAIT_MS = 300000
Rest.MIN_WAIT_MS = 1000
Rest.MAX_WAIT_MS = 900000

--- Endurance that has not risen for this long is not recovering: the character
--- is over-encumbered, still running, or standing in water.
Rest.STALL_MS = 30000

Rest.TIMEOUT_MS = Rest.MAX_WAIT_MS
Rest.POLL_MS = 1000

local REQUIRES = { "ISTimedActionQueue.add" }

--- Classes that put a character on a chair, best-known spelling first.
Rest.CHAIR_CLASSES = { "ISSitOnChairAction", "ISSitOnChair" }
Rest.GROUND_CLASSES = { "ISSitOnGround", "ISSitOnGroundAction" }

local REST_ARGS = { "target_endurance", "seat_ref", "allow_ground", "max_wait_ms" }

--- Argument kinds, spelled out rather than read from
--- PZAgent.CommandDispatcher.ARG, because the dispatcher is a sibling file and
--- nothing here may dereference a module at load time. A typo costs a refused
--- registration at load rather than a loosened check at dispatch.
local ARG = {
  NUMBER = "number",
  BOOLEAN = "boolean",
  REF = "ref",
}

local function restSpec(args, ctx)
  local Toolkit = toolkit()
  local checked, checkCode, checkDetail = Toolkit.checkArgs(args, REST_ARGS, {})
  if checked == nil then
    return nil, checkCode, checkDetail
  end
  local target, targetCode, targetDetail = Toolkit.readNumber(args, "target_endurance", {
    default = Rest.DEFAULT_TARGET,
    minimum = Rest.MIN_TARGET,
    maximum = Rest.MAX_TARGET,
  })
  if target == nil then
    return nil, targetCode, targetDetail
  end
  local ground, groundCode, groundDetail = Toolkit.readFlag(args, "allow_ground", false)
  if ground == nil then
    return nil, groundCode, groundDetail
  end
  local maxWait, waitCode, waitDetail = Toolkit.readCount(args, "max_wait_ms", {
    default = Rest.DEFAULT_MAX_WAIT_MS,
    minimum = Rest.MIN_WAIT_MS,
    maximum = Rest.MAX_WAIT_MS,
  })
  if maxWait == nil then
    return nil, waitCode, waitDetail
  end
  local spec = { target = target, allow_ground = ground, max_wait_ms = maxWait }
  if args.seat_ref ~= nil then
    local ref, refCode, refDetail = Toolkit.readRef(args, "seat_ref", PZAgent.Refs.KIND.SQUARE, ctx)
    if ref == nil then
      return nil, refCode, refDetail
    end
    local parsed, parseError = PZAgent.Refs.parseSquare(ref)
    if parsed == nil then
      return nil, Toolkit.reasons().INVALID_REF, parseError
    end
    spec.seat_ref = ref
    spec.seat = { x = parsed.x, y = parsed.y, z = parsed.z }
  end
  return spec
end

Rest.restSpec = restSpec

--- The first object on `square` that a character could sit on.
---
--- Read off the object rather than matched by name where the build allows it:
--- `isChair`/`isSofa` are the questions the game itself asks, and the sprite
--- name is only consulted when neither accessor exists.
function Rest.seatOn(square)
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
    if Toolkit.readBooleanOf(object, { "isChair", "isSofa" }) == true then
      return object
    end
    local okSprite, sprite = Toolkit.call(object, "getSprite")
    if okSprite and sprite ~= nil then
      local name = Toolkit.readStringOf(sprite, { "getName" })
      if type(name) == "string" then
        local lowered = name:lower()
        if lowered:find("chair", 1, true) ~= nil or lowered:find("sofa", 1, true) ~= nil then
          return object
        end
      end
    end
  end
  return nil
end

local RestAdapter = nil

local function restValidate(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local required, requiredCode, requiredDetail = Toolkit.requireSymbols(RestAdapter.requires)
  if required == nil then
    return nil, requiredCode, requiredDetail
  end
  local spec, code, detail = restSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local snapshot = Toolkit.snapshot(ctx)
  local endurance = Toolkit.stat(snapshot, "endurance")
  if endurance == nil then
    -- Without a reading there is no postcondition this command could ever meet.
    return Toolkit.unavailable("PlayerStats.getEndurance")
  end
  if endurance >= spec.target then
    return nil,
      reasons.PRECONDITION_FAILED,
      string.format("endurance is already %.2f, at or above the %.2f asked for", endurance, spec.target)
  end
  local threatCode, threatDetail = Toolkit.interruption(ctx)
  if threatCode ~= nil then
    return nil, threatCode, threatDetail
  end
  spec.endurance_at_start = endurance
  Toolkit.state(ctx).rest = spec
  return true
end

--- Walk to the seat, if one was named, and sit down.
local function restPrepare(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = restSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local state = Toolkit.state(ctx)
  if spec.seat ~= nil then
    local approach, approachCode, approachDetail = Toolkit.approach(ctx, spec.seat)
    if approach == nil then
      return nil, approachCode, approachDetail
    end
    local square, missing = Toolkit.gridSquare(spec.seat.x, spec.seat.y, spec.seat.z)
    if square == nil then
      if missing == "IsoCell.getGridSquare" then
        return nil, reasons.TARGET_NOT_LOADED, "the square the seat stands on is not loaded"
      end
      return Toolkit.unavailable(missing)
    end
    local seat, seatMissing = Rest.seatOn(square)
    if seat == nil then
      if seatMissing ~= nil then
        return Toolkit.unavailable(seatMissing)
      end
      return nil, reasons.PRECONDITION_FAILED, "there is nothing to sit on at that square"
    end
    local action, actionCode, actionDetail = Toolkit.constructFirst(Rest.CHAIR_CLASSES, ctx.player, seat)
    if action == nil then
      return nil, actionCode, actionDetail
    end
    local queued, queueCode, queueDetail = Toolkit.enqueue(ctx, action)
    if queued == nil then
      return nil, queueCode, queueDetail
    end
    state.seated = "chair"
    return true
  end
  if spec.allow_ground then
    local action, actionCode = Toolkit.constructFirst(Rest.GROUND_CLASSES, ctx.player)
    if action == nil then
      -- No seat was named, so the ground was a preference rather than the
      -- request. Standing still recovers endurance too, and the evidence says
      -- which of the two actually happened.
      state.seated = "standing"
      state.ground_unavailable = actionCode
      return true
    end
    local queued, queueCode, queueDetail = Toolkit.enqueue(ctx, action)
    if queued == nil then
      return nil, queueCode, queueDetail
    end
    state.seated = "ground"
    return true
  end
  state.seated = "standing"
  return true
end

--- Rest queues nothing of its own: recovering is what happens while the
--- character does nothing. The clock the timeout is measured from starts here.
local function restStart(_, args, ctx)
  local Toolkit = toolkit()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = restSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local state = Toolkit.state(ctx)
  state.started_ms = ctx.now_ms
  if state.seated == nil then
    state.seated = "standing"
  end
  return true
end

local function restProgress(_, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local code, detail = Toolkit.interruption(ctx)
  if code ~= nil then
    return nil, code, detail
  end
  local spec, specCode, specDetail = restSpec(args, ctx)
  if spec == nil then
    return nil, specCode, specDetail
  end
  local snapshot = Toolkit.snapshot(ctx)
  local endurance = Toolkit.stat(snapshot, "endurance")
  if endurance == nil then
    return Toolkit.unavailable("PlayerStats.getEndurance")
  end
  if endurance >= spec.target then
    return "done"
  end
  local state = Toolkit.state(ctx)
  local started = state.started_ms or ctx.now_ms
  if (ctx.now_ms - started) >= spec.max_wait_ms then
    return nil,
      reasons.ACTION_TIMEOUT,
      string.format("endurance reached %.2f of the %.2f asked for before the wait ran out", endurance, spec.target)
  end
  local movement, elapsed = Toolkit.trackProgress(ctx, "endurance", endurance, {
    direction = "up",
    epsilon = 0.005,
    stall_ms = Rest.STALL_MS,
  })
  if movement == "stalled" then
    return nil,
      reasons.NO_PROGRESS,
      string.format("endurance has not risen for %d ms; something is keeping the character from resting", elapsed)
  end
  return "running"
end

local function restVerify(_, before, after, args, ctx)
  local Toolkit = toolkit()
  local reasons = Toolkit.reasons()
  local spec, code, detail = restSpec(args, ctx)
  if spec == nil then
    return nil, code, detail
  end
  local was = Toolkit.stat(before, "endurance")
  local now = Toolkit.stat(after, "endurance")
  if now == nil then
    return Toolkit.unavailable("PlayerStats.getEndurance")
  end
  if was ~= nil and now <= was then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format("endurance went from %.3f to %.3f, so nothing was recovered", was, now)
  end
  if now < spec.target then
    return nil,
      reasons.POSTCONDITION_FAILED,
      string.format("endurance recovered to %.3f, short of the %.3f asked for", now, spec.target)
  end
  return {
    kind = "endurance_recovered",
    endurance_before = was,
    endurance_after = now,
    target_endurance = spec.target,
    posture = Toolkit.state(ctx).seated,
    seat_ref = spec.seat_ref,
  }
end

RestAdapter = toolkit().declare({
  name = "survival.rest",
  capability = toolkit().CAPABILITY.SURVIVAL_REST,
  requires = REQUIRES,
  timeout_ms = Rest.TIMEOUT_MS,
  poll_interval_ms = Rest.POLL_MS,
  args = {
    -- The bounds restSpec reads, from the same constants, so the declaration
    -- and the check cannot drift apart.
    target_endurance = { type = ARG.NUMBER, min = Rest.MIN_TARGET, max = Rest.MAX_TARGET },
    seat_ref = { type = ARG.REF, kinds = { square = true } },
    allow_ground = { type = ARG.BOOLEAN },
    -- Whole milliseconds, with a ceiling: this is the clock that ends the
    -- command, and an unbounded one is a character sitting still until
    -- something else moves them.
    max_wait_ms = { type = ARG.NUMBER, integer = true, min = Rest.MIN_WAIT_MS, max = Rest.MAX_WAIT_MS },
  },
  validate = restValidate,
  prepare = restPrepare,
  begin = restStart,
  progress = restProgress,
  verify = restVerify,
})

Rest.Adapter = RestAdapter
toolkit().register(RestAdapter)

return Rest
