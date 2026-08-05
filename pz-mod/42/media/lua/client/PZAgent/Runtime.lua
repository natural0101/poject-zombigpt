--[[
PZAgent.Runtime -- the per-tick orchestration PZAgent_Main would otherwise hold.

Blueprint 11.5 requires PZAgent_Main to be wiring only, so the sequence of
"what happens on a tick" lives here, where it can be read in one place and
where the game-facing lookups are guarded rather than scattered through event
handlers.

Invariants this file is responsible for:

* A panic stop request always ends in a disarm, whether it arrived from the
  hotkey, from the sidecar's panic.stop file, or from a manual takeover -- and
  it disarms even when the action queue cannot be read at all.
* Liveness of the sidecar is decided by observing its heartbeat *change*, not
  by comparing its clock to the game's. Two processes on one machine still
  disagree about the wall clock often enough to matter, and a false "stale"
  would stop the agent while a false "fresh" would let it act with no
  supervisor.
* Nothing here decides who owns a queue entry or whether an action may run;
  those are PZAgent.Ownership and PZAgent.Safety.
]]

PZAgent = PZAgent or {}

local Runtime = {}
PZAgent.Runtime = Runtime

--- The character the agent observes. Split screen is out of scope: index 0 is
--- the only player the mod ever touches.
function Runtime.currentPlayer()
  if type(getSpecificPlayer) ~= "function" then
    return nil, "getSpecificPlayer is not available"
  end
  local ok, player = pcall(getSpecificPlayer, 0)
  if not ok then
    return nil, "getSpecificPlayer(0) failed: " .. tostring(player)
  end
  return player
end

local function isAlive(player)
  if player == nil or type(player.isDead) ~= "function" then
    return false
  end
  local ok, dead = pcall(player.isDead, player)
  if not ok then
    return false
  end
  return dead == false
end

--- Poll the sidecar heartbeat and update liveness.
---
--- The document is only ever inspected for change; nothing inside it is trusted
--- as an instruction.
function Runtime.readSidecarHeartbeat(agent, nowMs)
  local document = agent.ipc:readDocument("sidecar_heartbeat")
  if type(document) ~= "table" then
    return false
  end
  local marker = string.format(
    "%s/%s/%s",
    tostring(document.seq),
    tostring(document.timestamp_ms),
    tostring(document.session_id)
  )
  if marker ~= agent.sidecar_marker then
    agent.sidecar_marker = marker
    PZAgent.Safety.noteSidecarHeartbeat(agent.safety, nowMs)
    return true
  end
  return false
end

--- Refresh the cheap per-tick state: which player exists, what the queue holds,
--- whether the sidecar is still there.
function Runtime.refresh(agent, nowMs)
  local player, playerError = Runtime.currentPlayer()
  agent.player = player
  agent.player_present = player ~= nil
  agent.player_alive = isAlive(player)
  if player == nil and playerError ~= nil then
    agent.safety.last_error = playerError
  end

  local entries, capability, detail = PZAgent.Safety.describeQueue(player)
  agent.queue_capability = capability
  if entries == nil then
    -- The queue could not be read. It is reported as ambiguous rather than
    -- empty, so nothing treats it as safe to clear or safe to add to.
    agent.queue_description = PZAgent.Ownership.describe(nil, nil)
    agent.queue_readable = false
    if detail ~= nil then
      agent.safety.last_error = detail
    end
  else
    agent.queue_description = PZAgent.Ownership.describe(entries, agent.session:id())
    agent.queue_entries = entries
    agent.queue_readable = true
  end

  Runtime.readSidecarHeartbeat(agent, nowMs)
  return agent
end

--- Append one safety event to the observation journal. Reported, never fatal:
--- failing to log a stop must not prevent the stop.
local function recordSafetyEvent(agent, record)
  local ok, err = agent.ipc:appendRecord("observation_events", record)
  if ok == nil then
    agent.safety.last_error = err
  end
  return ok
end

--- Panic stop. Disarms first, then tries to cancel the entries the mod owns.
---
--- The order matters: the disarm is what guarantees no *further* action starts,
--- and it must not be conditional on the queue API working.
function Runtime.stop(agent, nowMs, reason)
  local player = agent.player
  if player == nil then
    player = Runtime.currentPlayer()
    agent.player = player
  end
  local entries = PZAgent.Safety.describeQueue(player)
  local sessionId = agent.session:id()
  local outcome = PZAgent.Safety.panicStop(agent.safety, entries, sessionId, nowMs, reason)
  local applied = PZAgent.Safety.applyStop(player, outcome.plan, sessionId)

  recordSafetyEvent(agent, {
    type = "safety.stop",
    timestamp_ms = nowMs,
    session_id = sessionId,
    reason_code = outcome.reason_code,
    was_armed = outcome.was_armed,
    mod_owned = outcome.plan.mod_owned,
    foreign = outcome.plan.foreign,
    cleared = applied.cleared,
    remaining = applied.remaining,
    capability = applied.capability,
    detail = applied.detail,
  })
  agent.ipc:clearPanicStop()
  outcome.applied = applied
  return outcome
end

--- Register one player input event and, when it becomes a takeover, stop.
---
--- A takeover is a stop with its own reason code: the player is now driving, so
--- the agent's queued work goes away and the agent disarms, exactly as if the
--- panic key had been pressed.
function Runtime.noteInput(agent, player, kind, nowMs)
  if player ~= nil then
    agent.player = player
  end
  if not PZAgent.Safety.noteInput(agent.safety, kind, nowMs) then
    return false
  end
  Runtime.stop(agent, nowMs, PZAgent.Protocol.REASON.USER_TAKEOVER)
  return true
end

--- One agent tick: refresh state, honour a pending stop request, heartbeat.
function Runtime.tick(agent, nowMs)
  Runtime.refresh(agent, nowMs)
  if agent.ipc:panicStopRequested() then
    Runtime.stop(agent, nowMs, PZAgent.Protocol.REASON.PANIC_STOP)
  end
  local document, err = PZAgent.Heartbeat.tick(agent, nowMs)
  if document == nil then
    agent.safety.last_error = err
  end
  return document
end

return Runtime
