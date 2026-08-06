--[[
PZAgent.Safety -- arming, panic stop and manual takeover.

Four invariants, in the order they matter.

1. **Nothing happens until armed.** A fresh state is disarmed and its mode is
   OFF. Arming is only ever the result of an explicit command, and every branch
   of `mayStart` that is not a read or a stop demands `armed`.

2. **Stop always works.** `panicStop` takes no locks, consults no queue, needs
   no session, ignores the arming state and never fails. It is deliberately the
   one function here that cannot return an error.

3. **Stop clears only what the mod queued.** The decision comes from
   PZAgent.Ownership, which will only call an entry ours on positive proof.
   When the queue also holds entries that are not ours and the game exposes no
   way to cancel a single entry, the queue is left alone and the caller is told
   so -- an uncancelled action of ours is recoverable, a deleted action of the
   player's is not.

4. **The player wins.** Any input that moves or fights sets manual takeover
   immediately; anything else needs a few events inside a short window, so a
   stray click on a UI panel does not cancel a plan. Takeover blocks every
   mutating action until it is explicitly cleared.

Everything above the "engine boundary" divider is pure: no globals, no file
access, no game objects. The functions below the divider are the only ones that
touch the game, they probe before they call, and they report what they actually
observed rather than what the API was supposed to do.
]]

PZAgent = PZAgent or {}

local Safety = {}
PZAgent.Safety = Safety

--- Input that is unmistakably character control cancels automation with no
--- debounce at all (blueprint 8.2).
Safety.IMMEDIATE_INPUTS = {
  movement = true,
  attack = true,
  aim = true,
  sprint = true,
}

--- Everything else needs this many events inside the window below before it
--- counts as a takeover, so one misplaced click does not stop the agent.
Safety.DEBOUNCE_EVENTS = 3
Safety.DEBOUNCE_WINDOW_MS = 1500

--- A sidecar that has not written a heartbeat within this long is gone. No new
--- action may start while it is (blueprint 8.4).
Safety.SIDECAR_MAX_AGE_MS = 5 * 1000

--- Danger at or above this rank suspends new mutating work; the reflex guard
--- owns the level itself.
Safety.DANGER_BLOCK_RANK = 3

--- Bound on the stop events kept in memory for the HUD and diagnostics.
Safety.STOP_HISTORY = 8

-- ---------------------------------------------------------------------------
-- pure state machine
-- ---------------------------------------------------------------------------

--- A fresh, disarmed safety state. OFF is not a default that a later step is
--- expected to change: it is the state the mod stays in until told otherwise.
function Safety.newState(options)
  options = options or {}
  local Protocol = PZAgent.Protocol
  return {
    armed = false,
    mode = Protocol.MODE.OFF,
    manual_takeover = false,
    danger_level = Protocol.DANGER.NONE,
    sidecar_last_seen_ms = nil,
    sidecar_max_age_ms = options.sidecarMaxAgeMs or Safety.SIDECAR_MAX_AGE_MS,
    debounce_events = options.debounceEvents or Safety.DEBOUNCE_EVENTS,
    debounce_window_ms = options.debounceWindowMs or Safety.DEBOUNCE_WINDOW_MS,
    danger_block_rank = options.dangerBlockRank or Safety.DANGER_BLOCK_RANK,
    takeover_count = 0,
    takeover_window_start_ms = nil,
    takeover_reason = nil,
    last_stop_ms = nil,
    last_stop_reason = nil,
    stop_count = 0,
    stops = {},
    last_error = nil,
  }
end

--- True when no sidecar heartbeat has been seen recently enough.
function Safety.sidecarStale(state, nowMs)
  if state.sidecar_last_seen_ms == nil then
    return true
  end
  return (nowMs - state.sidecar_last_seen_ms) > state.sidecar_max_age_ms
end

--- Record that a sidecar heartbeat was observed at `nowMs`.
function Safety.noteSidecarHeartbeat(state, nowMs)
  state.sidecar_last_seen_ms = nowMs
end

--- Set the reflex guard's current threat assessment.
function Safety.setDanger(state, level)
  if PZAgent.Protocol.dangerRank(level) == nil then
    return false, "unknown danger level"
  end
  state.danger_level = level
  return true
end

--- Register one player input event. Returns true when it triggered a takeover.
function Safety.noteInput(state, kind, nowMs)
  if state.manual_takeover then
    return false
  end
  if Safety.IMMEDIATE_INPUTS[kind] then
    state.manual_takeover = true
    state.takeover_reason = kind
    state.takeover_count = 0
    state.takeover_window_start_ms = nil
    return true
  end
  local windowStart = state.takeover_window_start_ms
  if windowStart == nil or (nowMs - windowStart) > state.debounce_window_ms then
    state.takeover_window_start_ms = nowMs
    state.takeover_count = 1
  else
    state.takeover_count = state.takeover_count + 1
  end
  if state.takeover_count >= state.debounce_events then
    state.manual_takeover = true
    state.takeover_reason = kind
    state.takeover_count = 0
    state.takeover_window_start_ms = nil
    return true
  end
  return false
end

--- Hand control back to the agent. Only ever called for an explicit user
--- instruction: takeover does not time out on its own, because "the player
--- stopped touching the keyboard" is not consent to resume.
function Safety.clearTakeover(state)
  local was = state.manual_takeover
  state.manual_takeover = false
  state.takeover_reason = nil
  state.takeover_count = 0
  state.takeover_window_start_ms = nil
  return was
end

--- True once a command's lease has run out.
function Safety.leaseExpired(command, nowMs)
  if type(command) ~= "table" then
    return true
  end
  local issued = command.issued_at_ms
  local lease = command.lease_ms
  if type(issued) ~= "number" or type(lease) ~= "number" then
    return true
  end
  return nowMs > (issued + lease)
end

--- Arm the agent into `mode`.
---
--- Returns ok, reasonCode, detail. Every refusal names a reason code so the ack
--- the sidecar receives explains itself.
function Safety.arm(state, mode, nowMs, context)
  context = context or {}
  local Protocol = PZAgent.Protocol
  local reasons = Protocol.REASON
  local normalised = Protocol.normaliseMode(mode)
  if normalised == nil then
    return false, reasons.INVALID_ARGUMENT, "unknown session mode"
  end
  if normalised == Protocol.MODE.OFF then
    return false, reasons.INVALID_ARGUMENT, "OFF is not a mode that can be armed"
  end
  if state.manual_takeover then
    return false, reasons.USER_TAKEOVER, "the player has taken over"
  end
  if Safety.sidecarStale(state, nowMs) then
    return false, reasons.STALE_SESSION, "no live sidecar heartbeat"
  end
  if context.playerPresent == false then
    return false, reasons.PRECONDITION_FAILED, "no player character"
  end
  if context.sessionId == nil then
    return false, reasons.STALE_SESSION, "no open session"
  end
  state.mode = normalised
  state.armed = Protocol.MUTATING_MODES[normalised] == true
  state.armed_at_ms = nowMs
  return true, reasons.POSTCONDITION_MET, nil
end

--- Disarm. Always succeeds; disarming is never something to refuse.
function Safety.disarm(state, reason, nowMs)
  local Protocol = PZAgent.Protocol
  state.armed = false
  state.mode = Protocol.MODE.OFF
  state.disarmed_at_ms = nowMs
  state.disarm_reason = reason or Protocol.REASON.CANCELLED_BY_REQUEST
  return true
end

--- Whether `actionName` may start right now.
---
--- Returns ok, reasonCode, detail.
function Safety.mayStart(state, actionName, nowMs, context)
  context = context or {}
  local Protocol = PZAgent.Protocol
  local reasons = Protocol.REASON

  if not Protocol.isKnownAction(actionName) then
    return false, reasons.INVALID_ARGUMENT, "action is not in the protocol whitelist"
  end
  if Protocol.isAlwaysAllowed(actionName) then
    -- Stop, disarm and cancel bypass everything, including the lease check
    -- below: honouring a stale stop only ever removes authority, so refusing
    -- one would trade a harmless replay for a plan that keeps running.
    return true, reasons.POSTCONDITION_MET, nil
  end
  if context.command ~= nil and Safety.leaseExpired(context.command, nowMs) then
    return false, reasons.LEASE_EXPIRED, "the command's lease has run out"
  end
  if state.manual_takeover then
    return false, reasons.USER_TAKEOVER, "the player has taken over"
  end
  if Safety.sidecarStale(state, nowMs) then
    return false, reasons.STALE_SESSION, "no live sidecar heartbeat"
  end
  if context.sessionId == nil then
    return false, reasons.STALE_SESSION, "no open session"
  end
  if Protocol.isReadOnly(actionName) then
    if state.mode == Protocol.MODE.OFF then
      return false, reasons.NOT_ARMED, "the agent is off"
    end
    return true, reasons.POSTCONDITION_MET, nil
  end
  if not state.armed then
    return false, reasons.NOT_ARMED, "the agent is not armed"
  end
  if Protocol.MUTATING_MODES[state.mode] ~= true then
    return false, reasons.POLICY_DENIED, string.format("mode %s does not permit mutating actions", state.mode)
  end
  if context.playerPresent == false or context.playerAlive == false then
    return false, reasons.PRECONDITION_FAILED, "no living player character"
  end
  if PZAgent.Ownership.blocksAutomation(context.queue) then
    return false, reasons.PLAYER_BUSY_MANUAL_ACTION, "the action queue holds work the mod does not own"
  end
  local rank = Protocol.dangerRank(state.danger_level) or 0
  if rank >= state.danger_block_rank then
    return false, reasons.THREAT_INTERRUPTED, string.format("danger level is %s", state.danger_level)
  end
  return true, reasons.POSTCONDITION_MET, nil
end

--- Panic stop.
---
--- Disarms unconditionally and returns the plan naming the queue entries that
--- may be cancelled. This function cannot fail: it does not touch the game, it
--- does not need a session, and it does not care whether the agent was armed.
--- Applying the plan to the game is `Safety.applyStop`, which can fail, and
--- whose failure never undoes the disarm.
function Safety.panicStop(state, queueEntries, sessionId, nowMs, reason)
  local Protocol = PZAgent.Protocol
  local plan = PZAgent.Ownership.panicPlan(queueEntries, sessionId)
  local wasArmed = state.armed
  Safety.disarm(state, reason or Protocol.REASON.PANIC_STOP, nowMs)
  state.last_stop_ms = nowMs
  state.last_stop_reason = reason or Protocol.REASON.PANIC_STOP
  state.stop_count = state.stop_count + 1
  local history = state.stops
  -- What the plan *proposes*, not what was cancelled: applying the plan is a
  -- separate step that can fail, and this record is written before it runs.
  history[#history + 1] = {
    at_ms = nowMs,
    reason = state.last_stop_reason,
    mod_owned = plan.mod_owned,
    queue_readable = plan.readable == true,
  }
  while #history > Safety.STOP_HISTORY do
    table.remove(history, 1)
  end
  return {
    plan = plan,
    was_armed = wasArmed,
    disarmed = true,
    at_ms = nowMs,
    reason_code = state.last_stop_reason,
  }
end

--- The safety block of an observation or heartbeat.
function Safety.snapshot(state, nowMs)
  return {
    armed = state.armed,
    mode = state.mode,
    danger_level = state.danger_level,
    manual_takeover = state.manual_takeover,
    sidecar_stale = Safety.sidecarStale(state, nowMs),
  }
end

-- ---------------------------------------------------------------------------
-- engine boundary
--
-- Below this line the game is touched. Every call is guarded, every capability
-- is probed before use, and nothing reports success it did not observe.
-- ---------------------------------------------------------------------------

local function queueObject(player)
  if type(ISTimedActionQueue) ~= "table" or type(ISTimedActionQueue.getTimedActionQueue) ~= "function" then
    return nil, "ISTimedActionQueue.getTimedActionQueue is not available in this build"
  end
  local ok, queue = pcall(ISTimedActionQueue.getTimedActionQueue, player)
  if not ok then
    return nil, "reading the action queue failed: " .. tostring(queue)
  end
  if queue == nil or type(queue.queue) ~= "table" then
    return nil, "the action queue has no readable entry list"
  end
  return queue
end

--- Describe the character's action queue as the plain table PZAgent.Ownership
--- consumes.
---
--- Returns entries, capabilityState, detail. When the queue cannot be read the
--- entries are nil and the capability is `unsupported` -- callers must treat
--- that as "the queue is not mine to touch", never as "the queue is empty".
function Safety.describeQueue(player)
  local Protocol = PZAgent.Protocol
  if player == nil then
    return nil, Protocol.CAPABILITY.UNSUPPORTED, "no player character"
  end
  local queue, detail = queueObject(player)
  if queue == nil then
    return nil, Protocol.CAPABILITY.UNSUPPORTED, detail
  end
  local entries = {}
  local count = 0
  local limit = PZAgent.Ownership.MAX_ENTRIES
  local present = #queue.queue
  local scanned = math.min(present, limit)
  for index = 1, scanned do
    local action = queue.queue[index]
    local entry = { index = index }
    if type(action) == "table" then
      entry.action_type = type(action.Type) == "string" and action.Type or nil
      entry.agent_tag = type(action.pzAgentTag) == "string" and action.pzAgentTag or nil
      entry.agent_session = type(action.pzAgentSession) == "string" and action.pzAgentSession or nil
      entry.source = type(action.pzAgentSource) == "string" and action.pzAgentSource or nil
    end
    count = count + 1
    entries[count] = entry
  end
  if present > scanned then
    -- Walking an arbitrarily long queue on the game thread is not an option, so
    -- the read is bounded -- but the entries left behind are declared, never
    -- dropped in silence. PZAgent.Ownership counts them as foreign, which is
    -- what stops a wholesale clear from reaching an entry nobody looked at.
    entries.truncated = true
    entries.dropped = present - scanned
  end
  -- The API answered and the entries were read, but no probe has confirmed on a
  -- live build that `queue` lists exactly the actions the character will run.
  return entries, Protocol.CAPABILITY.AVAILABLE_UNVERIFIED, nil
end

--- Cancel the entries a panic stop plan names.
---
--- Returns a result table: `cleared` (entries the mod observed to be gone),
--- `remaining` (mod-owned entries still queued), `capability` and `detail`.
--- Success is never assumed: the queue is re-read afterwards and the counts
--- come from that second read.
function Safety.applyStop(player, plan, sessionId)
  local Protocol = PZAgent.Protocol
  local result = {
    cleared = 0,
    remaining = 0,
    attempted = 0,
    capability = Protocol.CAPABILITY.UNSUPPORTED,
    detail = nil,
  }
  if type(plan) ~= "table" then
    result.detail = "no stop plan was produced"
    return result
  end
  if plan.readable ~= true then
    -- A plan built from a queue that could not be read counts zero mod-owned
    -- entries, exactly as an observed-empty queue does. Reporting that as
    -- "verified: nothing of mine was queued" would be a postcondition claimed
    -- without an observation, which is the one thing this file may not do.
    result.detail = "the action queue could not be read, so nothing was cancelled"
    return result
  end
  if plan.mod_owned == 0 then
    result.capability = Protocol.CAPABILITY.VERIFIED
    result.detail = "nothing the mod owns is queued"
    return result
  end
  if plan.foreign > 0 or plan.truncated == true then
    -- Blueprint 8.12: under any uncertainty, do not clear the player's queue.
    -- The whole-queue clear is the only cancellation the game is known to
    -- expose, and it would take the player's entries with ours.
    result.remaining = plan.mod_owned
    result.capability = Protocol.CAPABILITY.UNSUPPORTED
    if plan.truncated == true then
      result.detail = "the queue is longer than the mod can inspect; it was left untouched"
    else
      result.detail = "the queue also holds entries the mod does not own; it was left untouched"
    end
    return result
  end
  local queue, detail = queueObject(player)
  if queue == nil then
    result.remaining = plan.mod_owned
    result.detail = detail
    return result
  end
  if type(ISTimedActionQueue.clear) ~= "function" then
    result.remaining = plan.mod_owned
    result.detail = "ISTimedActionQueue.clear is not available in this build"
    return result
  end
  result.attempted = plan.mod_owned
  local ok, err = pcall(ISTimedActionQueue.clear, player)
  if not ok then
    result.remaining = plan.mod_owned
    result.detail = "clearing the action queue failed: " .. tostring(err)
    return result
  end
  local after = Safety.describeQueue(player)
  if after == nil then
    -- The clear may well have worked, but it was not observed. Reporting it as
    -- cleared would be exactly the fabricated success this project forbids.
    result.remaining = plan.mod_owned
    result.capability = Protocol.CAPABILITY.AVAILABLE_UNVERIFIED
    result.detail = "the queue could not be re-read, so the clear is unverified"
    return result
  end
  local description = PZAgent.Ownership.describe(after, sessionId)
  result.remaining = description.mod_owned
  result.cleared = plan.mod_owned - description.mod_owned
  if result.cleared < 0 then
    result.cleared = 0
  end
  result.capability = Protocol.CAPABILITY.AVAILABLE_UNVERIFIED
  return result
end

return Safety
