-- Presentation tests: PZAgent.Heartbeat.build and PZAgent.Hud.format.
--
-- Both turn internal state into something another party acts on -- the
-- sidecar's watchdog and the player's eyes -- so the assertions here are about
-- the heartbeat and the HUD telling the truth, including about what the mod
-- could not determine.

local Harness = dofile((arg[0]:match("^(.*)test_heartbeat%.lua$") or "") .. "support/harness.lua")
local Mock = dofile((arg[0]:match("^(.*)test_heartbeat%.lua$") or "") .. "support/mock_game.lua")
local PZ = Harness.loadModules()
local Heartbeat = PZ.Heartbeat
local Hud = PZ.Hud
local Safety = PZ.Safety
local Ownership = PZ.Ownership
local Protocol = PZ.Protocol

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local NOW = 1700000000000
local SESSION = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33"

local function liveSafety()
  local state = Safety.newState()
  Safety.noteSidecarHeartbeat(state, NOW)
  return state
end

Harness.group("the heartbeat carries every tier 0 field")
do
  local safety = liveSafety()
  Safety.arm(safety, "AUTONOMOUS", NOW, { sessionId = SESSION, playerPresent = true })
  Safety.setDanger(safety, Protocol.DANGER.MEDIUM)

  local document = Heartbeat.build({
    session = { session_id = SESSION, nonce = "sidecar-1", game_nonce = "g1", generation = 2 },
    safety = Safety.snapshot(safety, NOW),
    action = Ownership.describe({ { agent_tag = Ownership.tagFor(SESSION, "c"), agent_session = SESSION } }, SESSION),
    seq = 17,
    now_ms = NOW,
    player_present = true,
    player_alive = true,
    build = "42.20",
    build_verified = true,
  })

  equal(document.protocol_version, "1.1", "protocol version")
  equal(document.schema_version, "1.0", "schema version")
  equal(document.mod_version, "0.1.0", "mod version")
  equal(document.build, "42.20", "game build")
  equal(document.session_id, SESSION, "session id")
  equal(document.nonce, "g1", "the mod's own nonce, as blueprint 3.3 requires")
  equal(document.sidecar_nonce, "sidecar-1", "and the sidecar's, so the pairing is unambiguous")
  equal(document.generation, 2, "session generation")
  equal(document.seq, 17, "sequence number")
  equal(document.timestamp_ms, NOW, "timestamp")
  equal(document.player_present, true, "player present")
  equal(document.armed, true, "armed")
  equal(document.mode, "AUTONOMOUS", "mode")
  equal(document.danger_level, "medium", "danger level")
  equal(document.action.ownership, "mod", "active action ownership")
  equal(document.action.busy, true, "active action busy flag")
  equal(document.action.queued, 1, "how much is queued")
  equal(document.sidecar_stale, false, "sidecar liveness")

  ok(PZ.Json.encode(document) ~= nil, "the heartbeat is encodable, which is the point of building it")

  -- The document has exactly one consumer: pz_agent_core.session.heartbeat,
  -- whose reader refuses anything missing one of these. A heartbeat it cannot
  -- parse is a heartbeat that does not exist, and the sidecar concludes the game
  -- is gone while it is in fact running.
  equal(document.peer, "game", "the document names the peer that wrote it")
  equal(document.version, "0.1.0", "and the version of that peer's software")
  for _, field in ipairs({ "peer", "session_id", "nonce", "seq", "timestamp_ms", "version", "protocol_version" }) do
    ok(document[field] ~= nil, "the sidecar's heartbeat reader requires " .. field)
  end
  ok(document.seq == math.floor(document.seq), "seq is an integer, which that reader also insists on")
  ok(document.timestamp_ms == math.floor(document.timestamp_ms), "and so is the timestamp")
  equal(document.session_open, true, "the document says whether a session is open")
end

Harness.group("the heartbeat never overstates what the mod knows")
do
  local unverified = Heartbeat.build({ safety = {}, now_ms = NOW })
  equal(unverified.build, Protocol.TARGET_BUILD, "with no probe result the target build is the fallback")
  equal(unverified.build_verified, false, "and the fallback is flagged as unverified")
  equal(unverified.armed, false, "an absent safety block reads as disarmed")
  equal(unverified.mode, Protocol.MODE.OFF, "and as OFF")
  equal(unverified.player_present, false, "and as no player")
  equal(unverified.sidecar_stale, true, "and as no sidecar, which is the safe reading of 'unknown'")
  equal(unverified.action.ownership, Protocol.OWNERSHIP.NONE, "with no action reported")
  isNil(unverified.session_id, "and no session id invented")
  isNil(unverified.nonce, "nor a nonce")
  equal(unverified.session_open, false, "the absence of a session is stated rather than left to inference")
  equal(unverified.peer, "game", "while the peer is known regardless of any session")

  local wrongBuild = Heartbeat.build({ safety = {}, now_ms = NOW, build = "41.78", build_verified = true })
  equal(wrongBuild.build_supported, false, "a build outside the supported set is reported, not accepted quietly")
  equal(wrongBuild.build, "41.78", "and the real value is passed through")

  local errored = Heartbeat.build({ safety = {}, now_ms = NOW, last_error = "queue unreadable" })
  equal(errored.last_error, "queue unreadable", "the last error travels with the heartbeat")
end

Harness.group("the build probe reports its own absence")
do
  local detected, reason = Heartbeat.detectBuild()
  isNil(detected, "outside the game there is no version to read")
  contains(reason, "getCore", "and the reason names the missing global")
end

Harness.group("the HUD shows what the player needs to decide with")
do
  local safety = liveSafety()
  local lines = Hud.format({ safety = Safety.snapshot(safety, NOW), session = nil })
  contains(lines[1].text, "OFF", "a disarmed agent shows its mode")
  contains(lines[1].text, "disarmed", "and says it is disarmed")
  contains(lines[2].text, "none", "with no session")

  Safety.arm(safety, "AUTONOMOUS", NOW, { sessionId = SESSION, playerPresent = true })
  local armed = Hud.format({ safety = Safety.snapshot(safety, NOW), session = { session_id = SESSION } })
  contains(armed[1].text, "ARMED", "an armed agent says so first")
  contains(armed[1].text, "AUTONOMOUS", "along with the mode")
  contains(armed[2].text, "3f2b9c1e", "and an abbreviated session id")
  ok(#armed[2].text < 40, "the session line is bounded, since it renders text from outside the game")

  Safety.noteInput(safety, "movement", NOW)
  Safety.setDanger(safety, Protocol.DANGER.HIGH)
  local flagged = Hud.format({
    safety = Safety.snapshot(safety, NOW + Safety.SIDECAR_MAX_AGE_MS + 1),
    session = { session_id = SESSION },
    last_error = string.rep("a very long error message ", 20),
  })
  local flags = flagged[3].text
  contains(flags, "MANUAL", "a takeover is flagged")
  contains(flags, "NO SIDECAR", "a dead sidecar is flagged")
  contains(flags, "DANGER HIGH", "the danger level is flagged")
  ok(#flagged[4].text <= 48, "a long error is truncated rather than drawn off-screen")
  contains(flagged[4].text, "...", "and the truncation is visible")

  local bare = Hud.format(nil)
  contains(bare[1].text, "OFF", "with no state at all the HUD still renders, showing OFF")
  contains(bare[2].text, "none", "and no session")
  contains(bare[3].text, "NO SIDECAR", "and reads an unknown sidecar state as a missing one")
end

Harness.group("the HUD refuses to build without the UI class it needs")
do
  ok(not Hud.isSupported(), "outside the game the UI base class is absent")
  local panel, reason = Hud.create(function()
    return {}
  end)
  isNil(panel, "so no panel is created")
  contains(reason, "ISPanel", "and the reason names what is missing")
  ok(not Hud.destroy(nil), "destroying a panel that was never created is a no-op")
  contains(select(2, Hud.destroy(nil)), "no HUD panel", "which says so rather than reading as a removal")
  local stubborn = { removeFromUIManager = function()
    error("mock UI failure", 0)
  end }
  local removed, removeError = Hud.destroy(stubborn)
  ok(not removed, "a panel that refuses to be removed is not reported as removed")
  contains(removeError, "failed", "and the failure carries its reason")

  ISPanel = { derive = function() return {} end }
  isNil(Hud.create("not a function"), "a HUD without a state provider is refused")
  ISPanel = nil
  Mock.removeActionQueue()
end

Harness.finish("test_heartbeat")
