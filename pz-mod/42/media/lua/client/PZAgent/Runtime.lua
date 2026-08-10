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
* A session offer on disk is read once per tick and decided at most once per
  document. A rejected or unreadable offer never touches the session that is
  already open: the offer file lives in a directory the player can edit and
  any local process can write, so a bad document there must cost nothing but
  a recorded refusal.
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
  if document.peer ~= PZAgent.Protocol.PEER.SIDECAR then
    -- A document in the sidecar's file that does not claim to be the sidecar's
    -- is not evidence a sidecar is running -- a copy of the mod's own heartbeat
    -- would otherwise supervise the mod. The sidecar refuses the mirror image of
    -- this, so the two sides agree about what liveness means.
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

--- The exact text Ipc.readDocument returns for a file that is missing or
--- empty. Matched so that an exchange directory with no session.json -- the
--- ordinary state of a game started before the sidecar -- is not reported as
--- an error on every tick. Coupled to Ipc by wording; test_runtime covers the
--- pairing, so a reworded Ipc fails a test instead of silently turning
--- "no offer yet" into a permanent HUD error.
local NO_DOCUMENT = "document is empty or absent"

--- Poll the sidecar's session offer (session.json) and, when it holds a
--- document not yet decided on, offer it to the session holder.
---
--- The mirror of readSidecarHeartbeat: one bounded read per tick, and nothing
--- inside the document is trusted until Session.evaluate has accepted it.
--- Every refusal -- unreadable file, malformed JSON, rejected offer -- leaves
--- the current session exactly as it was and is reported through
--- safety.last_error, the same single overwritten slot every other per-tick
--- failure uses, so a permanently broken file occupies one field forever
--- instead of filling a journal.
---
--- Returns true plus the decision when a session was accepted; false
--- otherwise (with the decision as the second value when one was evaluated).
function Runtime.readSession(agent, nowMs)
  local document, readError = agent.ipc:readDocument("session")
  if document == nil then
    if readError ~= nil and readError ~= NO_DOCUMENT then
      agent.safety.last_error = "session offer unreadable: " .. tostring(readError)
    end
    return false
  end
  if type(document) ~= "table" then
    agent.safety.last_error = "session offer unreadable: the document is not an object"
    return false
  end
  local nonce = document.nonce
  if type(nonce) == "string" and nonce == agent.session_offer_marker then
    -- Already decided. The decided offer staying on disk is the steady state
    -- of the exchange directory, not a fresh attempt, so it is skipped without
    -- re-evaluating and without recording another refusal.
    return false
  end
  local sidecarFresh = not PZAgent.Safety.sidecarStale(agent.safety, nowMs)
  local decision = agent.session:offer(document, nowMs, { sidecarFresh = sidecarFresh })
  if decision.accepted then
    agent.session_offer_marker = nonce
    return true, decision
  end
  if sidecarFresh and type(nonce) == "string" then
    -- Rejected with a live sidecar watching: re-presenting the identical
    -- document cannot change the answer, so its nonce is remembered and it is
    -- refused exactly once. A rejection with the sidecar gone is different --
    -- the same offer may become acceptable the moment the sidecar's heartbeat
    -- appears, so it stays eligible for the next tick.
    agent.session_offer_marker = nonce
  end
  agent.safety.last_error = string.format(
    "session offer refused: %s (%s)",
    tostring(decision.reason_code),
    tostring(decision.detail)
  )
  return false, decision
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
    -- empty, so nothing treats it as safe to clear or safe to add to. The
    -- entries from the last successful read are dropped with it: a description
    -- of the queue as it was some ticks ago is not an observation of the queue
    -- as it is.
    agent.queue_description = PZAgent.Ownership.describe(nil, nil)
    agent.queue_entries = nil
    agent.queue_readable = false
    if detail ~= nil then
      agent.safety.last_error = detail
    end
  else
    agent.queue_description = PZAgent.Ownership.describe(entries, agent.session:id())
    agent.queue_entries = entries
    agent.queue_readable = true
  end

  -- Order matters: the heartbeat read first, so an offer written in the same
  -- inter-tick window as the sidecar's first heartbeat is judged against the
  -- freshest liveness observation instead of last tick's.
  Runtime.readSidecarHeartbeat(agent, nowMs)
  Runtime.readSession(agent, nowMs)
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
  local entries, _, queueDetail = PZAgent.Safety.describeQueue(player)
  if entries == nil and queueDetail ~= nil then
    agent.safety.last_error = queueDetail
  end
  local sessionId = agent.session:id()
  local outcome = PZAgent.Safety.panicStop(agent.safety, entries, sessionId, nowMs, reason)
  local applied = PZAgent.Safety.applyStop(player, outcome.plan, sessionId)

  recordSafetyEvent(agent, {
    type = "safety.stop",
    timestamp_ms = nowMs,
    session_id = sessionId,
    reason_code = outcome.reason_code,
    was_armed = outcome.was_armed,
    -- A stop over a queue nobody could read reports zero of everything; without
    -- this flag that record is indistinguishable from a stop over an empty
    -- queue, and the sidecar would draw the opposite conclusion.
    queue_readable = outcome.plan.readable == true,
    queue_truncated = outcome.plan.truncated == true,
    mod_owned = outcome.plan.mod_owned,
    foreign = outcome.plan.foreign,
    cleared = applied.cleared,
    remaining = applied.remaining,
    capability = applied.capability,
    detail = applied.detail,
  })
  local cleared, clearError = agent.ipc:clearPanicStop()
  if cleared == nil then
    -- The stop happened; the request file outliving it only means the next tick
    -- stops again. Still reported: a request that cannot be cleared is a disk
    -- problem the player needs to see on the HUD.
    agent.safety.last_error = clearError
  end
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
  local requested, requestError = agent.ipc:panicStopRequested()
  if requestError ~= nil then
    -- The stop channel is unreadable. That is not "no stop was requested": the
    -- mod cannot tell, so it says so and gives up whatever authority it still
    -- has, which is the only harmless direction to be wrong in. Gated on being
    -- armed so a permanently unreadable file disarms once instead of writing a
    -- stop event on every tick forever.
    agent.safety.last_error = requestError
    requested = agent.safety.armed == true
  end
  if requested then
    Runtime.stop(agent, nowMs, PZAgent.Protocol.REASON.PANIC_STOP)
  end
  local document, err = PZAgent.Heartbeat.tick(agent, nowMs)
  if document == nil then
    agent.safety.last_error = err
  end
  return document
end

return Runtime
