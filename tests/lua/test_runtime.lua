-- End-to-end tests for PZAgent.Runtime, the layer PZAgent_Main wires up.
--
-- These run the whole path a panic stop actually takes: a request appears in
-- the exchange directory, a tick notices it, the agent disarms, only mod-owned
-- entries are cancelled, a safety event lands in the journal, and the heartbeat
-- reflects the new state.

local Harness = dofile((arg[0]:match("^(.*)test_runtime%.lua$") or "") .. "support/harness.lua")
local Mock = dofile((arg[0]:match("^(.*)test_runtime%.lua$") or "") .. "support/mock_game.lua")
local PZ = Harness.loadModules()
local Runtime = PZ.Runtime
local Safety = PZ.Safety
local Ownership = PZ.Ownership
local Json = PZ.Json
local Protocol = PZ.Protocol
local REASON = Protocol.REASON

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local NOW = 1700000000000
local SESSION = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33"

local function modAction(index)
  return {
    Type = "ISWalkToTimedAction",
    pzAgentTag = Ownership.tagFor(SESSION, "cmd-" .. index),
    pzAgentSession = SESSION,
  }
end

local function playerAction()
  return { Type = "ISReadABook", pzAgentSource = "player" }
end

--- Build an agent over a mock filesystem, with an accepted session.
local function newAgent(options)
  options = options or {}
  local fs = Mock.newFilesystem()
  local agent = {
    ipc = PZ.Ipc.new({ fileApi = fs.api, clock = function()
      return NOW
    end }),
    safety = Safety.newState(),
    session = PZ.Session.new(),
    sequence = PZ.Sequence.new(),
    player_present = false,
    player_alive = false,
  }
  if options.session ~= false then
    agent.session:offer({
      protocol_version = "1.0",
      session_id = SESSION,
      created_at_ms = NOW - 10,
      nonce = "nonce-one",
      mode = "autonomous",
    }, NOW)
  end
  return agent, fs
end

local function journalRecords(fs)
  local lines = fs:lines(PZ.Ipc.pathFor("observation_events"))
  local records = {}
  for index = 2, #lines do
    records[index - 1] = Json.decode(lines[index])
  end
  return records
end

Harness.group("a tick with no game around degrades instead of crashing")
do
  getSpecificPlayer = nil
  Mock.removeActionQueue()
  local agent, fs = newAgent()
  local document = Runtime.tick(agent, NOW)
  ok(document ~= nil, "the heartbeat is still written")
  equal(document.player_present, false, "reporting that there is no player")
  equal(agent.queue_description.ownership, Protocol.OWNERSHIP.AMBIGUOUS, "and an unreadable queue as ambiguous")
  equal(agent.queue_readable, false, "the agent knows it could not read the queue")
  ok(Ownership.blocksAutomation(agent.queue_description), "so nothing may be enqueued")
  ok(fs:read(PZ.Ipc.pathFor("game_heartbeat")) ~= nil, "and the heartbeat file exists on disk")
end

Harness.group("the sidecar is judged alive by its heartbeat changing")
do
  local agent, fs = newAgent()
  ok(Safety.sidecarStale(agent.safety, NOW), "with no sidecar heartbeat seen, the sidecar is stale")

  fs:put(PZ.Ipc.pathFor("sidecar_heartbeat"), '{"peer":"sidecar","seq":1,"session_id":"s","timestamp_ms":1}')
  ok(Runtime.readSidecarHeartbeat(agent, NOW), "a new heartbeat document counts as a sign of life")
  ok(not Safety.sidecarStale(agent.safety, NOW), "so the sidecar is no longer stale")

  ok(not Runtime.readSidecarHeartbeat(agent, NOW + 1000), "the same document again is not a fresh sign of life")
  ok(
    Safety.sidecarStale(agent.safety, NOW + Safety.SIDECAR_MAX_AGE_MS + 1),
    "so a sidecar that stopped updating goes stale on schedule"
  )

  fs:put(PZ.Ipc.pathFor("sidecar_heartbeat"), '{"peer":"sidecar","seq":2,"session_id":"s","timestamp_ms":2}')
  ok(Runtime.readSidecarHeartbeat(agent, NOW + 2000), "an updated document revives it")

  fs:put(PZ.Ipc.pathFor("sidecar_heartbeat"), "{torn")
  ok(not Runtime.readSidecarHeartbeat(agent, NOW + 3000), "a torn document is not a sign of life")

  -- The mod's own heartbeat, copied into the sidecar's file, must not be able
  -- to supervise the mod.
  local stale = NOW + Safety.SIDECAR_MAX_AGE_MS * 10
  fs:put(PZ.Ipc.pathFor("sidecar_heartbeat"), '{"peer":"game","seq":99,"session_id":"s","timestamp_ms":99}')
  ok(not Runtime.readSidecarHeartbeat(agent, stale), "a document claiming the wrong peer is not a sign of life")
  ok(Safety.sidecarStale(agent.safety, stale), "so the sidecar is still counted as gone")

  fs:put(PZ.Ipc.pathFor("sidecar_heartbeat"), '{"seq":100,"session_id":"s","timestamp_ms":100}')
  ok(not Runtime.readSidecarHeartbeat(agent, stale), "and a document claiming no peer at all is not one either")
end

Harness.group("a panic stop request in the exchange directory is honoured")
do
  getSpecificPlayer = function()
    return Mock.newPlayer()
  end
  Mock.installActionQueue({ modAction(1), modAction(2) })
  local agent, fs = newAgent()
  Safety.noteSidecarHeartbeat(agent.safety, NOW)
  Safety.arm(agent.safety, "AUTONOMOUS", NOW, { sessionId = SESSION, playerPresent = true })
  equal(agent.safety.armed, true, "the agent starts this case armed")

  fs:put(PZ.Ipc.pathFor("panic_stop"), '{"requested_at_ms":1}\n')
  local document = Runtime.tick(agent, NOW)

  equal(agent.safety.armed, false, "the tick disarmed the agent")
  equal(agent.safety.mode, Protocol.MODE.OFF, "and returned the mode to OFF")
  equal(document.armed, false, "the heartbeat reports the new state")
  ok(not agent.ipc:panicStopRequested(), "the request was consumed, so it does not fire again")

  local records = journalRecords(fs)
  equal(#records, 1, "one safety event was journalled")
  equal(records[1].type, "safety.stop", "of the right type")
  equal(records[1].reason_code, REASON.PANIC_STOP, "with the panic stop reason")
  equal(records[1].was_armed, true, "recording that the agent had been armed")
  equal(records[1].cleared, 2, "and that both of our entries were observed to be gone")
  equal(records[1].remaining, 0, "with nothing of ours left")
end

Harness.group("a panic stop leaves the player's queue alone")
do
  getSpecificPlayer = function()
    return Mock.newPlayer()
  end
  local queue = Mock.installActionQueue({ modAction(1), playerAction() })
  local agent, fs = newAgent()
  Safety.noteSidecarHeartbeat(agent.safety, NOW)
  Safety.arm(agent.safety, "AUTONOMOUS", NOW, { sessionId = SESSION, playerPresent = true })

  local outcome = Runtime.stop(agent, NOW, REASON.PANIC_STOP)
  equal(agent.safety.armed, false, "the agent disarmed")
  equal(outcome.plan.mod_owned, 1, "one entry was ours")
  equal(outcome.plan.foreign, 1, "one was the player's")
  equal(#queue.queue, 2, "and the queue was not touched, because clearing it would take theirs too")
  equal(outcome.applied.cleared, 0, "nothing is reported as cleared")
  equal(outcome.applied.remaining, 1, "and our entry is honestly reported as still queued")

  local records = journalRecords(fs)
  equal(records[1].foreign, 1, "the journal records that a foreign entry was present")
  contains(records[1].detail, "does not own", "with a detail explaining why the queue was left alone")
end

Harness.group("a stop works with no session, no queue and no game")
do
  getSpecificPlayer = nil
  Mock.removeActionQueue()
  local agent, fs = newAgent({ session = false })
  Safety.noteSidecarHeartbeat(agent.safety, NOW)
  agent.safety.armed = true
  local outcome = Runtime.stop(agent, NOW, REASON.PANIC_STOP)
  equal(agent.safety.armed, false, "the disarm happens regardless")
  equal(outcome.disarmed, true, "and is reported")
  equal(#outcome.plan.clear, 0, "with nothing cancelled, because nothing could be proved ours")
  isNil(agent.session:id(), "and there was never a session to prove it against")

  -- The stop happened over a queue nobody could read. The journal must not
  -- describe that the same way it describes a stop over an empty queue.
  local record = journalRecords(fs)[1]
  equal(record.queue_readable, false, "the journal records that the queue was never read")
  Harness.notEqual(record.capability, "verified", "so the outcome is not reported as verified")
  contains(record.detail, "could not be read", "and the detail says why")
end

Harness.group("an unreadable stop channel gives up authority")
do
  getSpecificPlayer = function()
    return Mock.newPlayer()
  end
  Mock.installActionQueue({})
  local agent, fs = newAgent()
  Safety.noteSidecarHeartbeat(agent.safety, NOW)
  Safety.arm(agent.safety, "AUTONOMOUS", NOW, { sessionId = SESSION, playerPresent = true })
  fs:failReadsFrom(PZ.Ipc.pathFor("panic_stop"))

  Runtime.tick(agent, NOW)
  equal(agent.safety.armed, false, "a stop request that cannot be read is not read as 'no stop'")
  contains(agent.safety.last_error, "panic.stop", "and the read failure reaches the HUD and the heartbeat")

  -- One disarm, not one per tick: a permanently broken file must not fill the
  -- journal with stop events forever.
  local afterFirst = #journalRecords(fs)
  Runtime.tick(agent, NOW + 1)
  Runtime.tick(agent, NOW + 2)
  equal(#journalRecords(fs), afterFirst, "a still-unreadable file does not stop again once disarmed")
end

Harness.group("manual input cancels automation")
do
  getSpecificPlayer = function()
    return Mock.newPlayer()
  end
  local queue = Mock.installActionQueue({ modAction(1) })
  local agent, fs = newAgent()
  Safety.noteSidecarHeartbeat(agent.safety, NOW)
  Safety.arm(agent.safety, "AUTONOMOUS", NOW, { sessionId = SESSION, playerPresent = true })

  ok(Runtime.noteInput(agent, Mock.newPlayer(), "movement", NOW), "movement is a takeover")
  equal(agent.safety.manual_takeover, true, "the takeover is recorded")
  equal(agent.safety.armed, false, "the agent disarmed itself")
  equal(#queue.queue, 0, "and its own queued action was cancelled")

  local records = journalRecords(fs)
  equal(records[1].reason_code, REASON.USER_TAKEOVER, "the journal names the takeover as the cause")

  local clicks = newAgent()
  Safety.noteSidecarHeartbeat(clicks.safety, NOW)
  Safety.arm(clicks.safety, "AUTONOMOUS", NOW, { sessionId = SESSION, playerPresent = true })
  ok(not Runtime.noteInput(clicks, nil, "ui", NOW), "a single UI click is not a takeover")
  equal(clicks.safety.armed, true, "so the agent stays armed")
end

Harness.group("a tick still heartbeats when the journal cannot be written")
do
  getSpecificPlayer = function()
    return Mock.newPlayer()
  end
  Mock.installActionQueue({})
  local agent, fs = newAgent()
  fs:failWritesTo(PZ.Ipc.pathFor("observation_events"))
  local outcome = Runtime.stop(agent, NOW, REASON.PANIC_STOP)
  equal(agent.safety.armed, false, "the stop still disarmed")
  equal(outcome.disarmed, true, "and reported the disarm")
  contains(agent.safety.last_error, "failed", "while the journal failure is recorded as the last error")

  local document = Runtime.tick(agent, NOW + 1)
  ok(document ~= nil, "the heartbeat is still written")
  equal(document.last_error ~= nil, true, "and carries the error to the sidecar")
end

Harness.group("heartbeat sequence numbers advance every tick")
do
  getSpecificPlayer = function()
    return Mock.newPlayer()
  end
  Mock.installActionQueue({})
  local agent = newAgent()
  local first = Runtime.tick(agent, NOW)
  local second = Runtime.tick(agent, NOW + 100)
  local third = Runtime.tick(agent, NOW + 200)
  equal(second.seq, first.seq + 1, "the second heartbeat follows the first")
  equal(third.seq, second.seq + 1, "and the third follows the second")
end

getSpecificPlayer = nil
Mock.removeActionQueue()
Harness.finish("test_runtime")
