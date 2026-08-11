-- Handshake tests for PZAgent.Session (blueprint 3.3).
--
-- Every rejection branch is exercised, because the accept path is the one that
-- gives an outside process a session id the mod will stamp on references and
-- journal records.

local Harness = dofile((arg[0]:match("^(.*)test_session%.lua$") or "") .. "support/harness.lua")
local PZ = Harness.loadModules()
local Session = PZ.Session
local REASON = PZ.Protocol.REASON

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

local NOW = 1700000000000

local function request(overrides)
  local base = {
    protocol_version = "1.0",
    session_id = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33",
    created_at_ms = NOW - 100,
    sidecar_version = "0.1.0",
    requested_observation_hz = 4,
    mode = "observe",
    nonce = "nonce-one",
  }
  for key, value in pairs(overrides or {}) do
    if value == "\0remove" then
      base[key] = nil
    else
      base[key] = value
    end
  end
  return base
end

local REMOVE = "\0remove"

Harness.group("a well-formed offer is accepted")
do
  local decision = Session.evaluate(request(), nil, NOW)
  ok(decision.accepted, "the offer is accepted")
  equal(decision.session.session_id, "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33", "session id is carried through")
  equal(decision.session.nonce, "nonce-one", "the sidecar nonce is remembered")
  equal(decision.session.mode, "OBSERVE", "the blueprint's lowercase mode normalises to the enum spelling")
  equal(decision.session.observation_hz, 4, "the requested rate is carried through")
  equal(decision.session.accepted_at_ms, NOW, "acceptance is timestamped")

  -- The single most important assertion in this file.
  equal(decision.session.armed, false, "accepting a session never arms the agent")
end

Harness.group("the protocol major must match")
do
  local rejected = Session.evaluate(request({ protocol_version = "2.0" }), nil, NOW)
  ok(not rejected.accepted, "a different major is rejected")
  equal(rejected.reason_code, REASON.INVALID_ARGUMENT, "with INVALID_ARGUMENT")
  contains(rejected.detail, "protocol major", "and a detail naming the problem")

  ok(Session.evaluate(request({ protocol_version = "1.9" }), nil, NOW).accepted, "a higher minor is accepted")
  ok(not Session.evaluate(request({ protocol_version = "0.9" }), nil, NOW).accepted, "a lower major is rejected")
  ok(not Session.evaluate(request({ protocol_version = "1" }), nil, NOW).accepted, "a malformed version is rejected")
  ok(not Session.evaluate(request({ protocol_version = REMOVE }), nil, NOW).accepted, "a missing version is rejected")
  ok(not Session.evaluate(request({ protocol_version = 1.0 }), nil, NOW).accepted, "a numeric version is rejected")
end

Harness.group("a nonce may never be reused")
do
  local previous = { session_id = "11111111-1111-1111-1111-111111111111", nonce = "nonce-one" }
  local rejected = Session.evaluate(request(), previous, NOW)
  ok(not rejected.accepted, "an offer repeating the previous nonce is rejected")
  equal(rejected.reason_code, REASON.STALE_SESSION, "with STALE_SESSION, because it is a replay")
  contains(rejected.detail, "nonce", "and a detail naming the nonce")

  local accepted = Session.evaluate(request({ nonce = "nonce-two" }), previous, NOW)
  ok(accepted.accepted, "a fresh nonce is accepted")

  local sameId = { session_id = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33", nonce = "older" }
  local reopened = Session.evaluate(request({ nonce = "nonce-three" }), sameId, NOW)
  ok(not reopened.accepted, "reopening a session id that is still open is rejected")
  equal(reopened.reason_code, REASON.STALE_SESSION, "with STALE_SESSION")

  sameId.terminated = true
  ok(
    Session.evaluate(request({ nonce = "nonce-four" }), sameId, NOW).accepted,
    "the same id may return once the previous session was terminated"
  )
end

Harness.group("malformed fields are rejected")
do
  local cases = {
    { field = "session_id", value = "not-a-uuid" },
    { field = "session_id", value = REMOVE },
    { field = "session_id", value = "3f2b9c1e0a4d4c7b9e218b6d5f0a1c33" },
    { field = "nonce", value = REMOVE },
    { field = "nonce", value = "" },
    { field = "nonce", value = "has spaces" },
    { field = "nonce", value = string.rep("n", 65) },
    { field = "nonce", value = 12345 },
    { field = "created_at_ms", value = REMOVE },
    { field = "created_at_ms", value = -1 },
    { field = "created_at_ms", value = 1.5 },
    { field = "created_at_ms", value = "1700000000000" },
    { field = "mode", value = REMOVE },
    { field = "mode", value = "godmode" },
    { field = "requested_observation_hz", value = 0 },
    { field = "requested_observation_hz", value = 60 },
    { field = "requested_observation_hz", value = "four" },
  }
  for index = 1, #cases do
    local case = cases[index]
    local decision = Session.evaluate(request({ [case.field] = case.value }), nil, NOW)
    ok(
      not decision.accepted and decision.reason_code == REASON.INVALID_ARGUMENT,
      string.format("%s = %s is rejected as INVALID_ARGUMENT", case.field, tostring(case.value))
    )
  end

  ok(not Session.evaluate("not a table", nil, NOW).accepted, "a non-table offer is rejected")
  ok(not Session.evaluate(nil, nil, NOW).accepted, "a nil offer is rejected")
  equal(
    Session.evaluate(request(), nil, nil).reason_code,
    REASON.INTERNAL_ERROR,
    "no usable clock is an internal error, not a rejection of the sidecar"
  )

  -- The rate is optional; the default applies when it is absent.
  local defaulted = Session.evaluate(request({ requested_observation_hz = REMOVE }), nil, NOW)
  ok(defaulted.accepted, "an absent rate is accepted")
  equal(defaulted.session.observation_hz, Session.DEFAULT_OBSERVATION_HZ, "and takes the default")
end

Harness.group("timestamps must be current")
do
  local stale = Session.evaluate(request({ created_at_ms = NOW - Session.MAX_AGE_MS - 1 }), nil, NOW)
  ok(not stale.accepted, "a document older than the limit is rejected")
  equal(stale.reason_code, REASON.STALE_SESSION, "with STALE_SESSION")
  contains(stale.detail, "old", "and a detail about its age")

  ok(
    Session.evaluate(request({ created_at_ms = NOW - Session.MAX_AGE_MS + 1 }), nil, NOW).accepted,
    "a document just inside the limit is accepted"
  )

  local future = Session.evaluate(request({ created_at_ms = NOW + Session.MAX_SKEW_MS + 1 }), nil, NOW)
  ok(not future.accepted, "a document from the future is rejected")
  equal(future.reason_code, REASON.STALE_SESSION, "with STALE_SESSION")

  ok(
    Session.evaluate(request({ created_at_ms = NOW + 1000 }), nil, NOW).accepted,
    "a small forward skew is tolerated"
  )

  local shortWindow = Session.evaluate(request(), nil, NOW, { maxAgeMs = 10 })
  ok(not shortWindow.accepted, "the age limit is configurable")
end

Harness.group("a dead sidecar cannot open a session")
do
  local decision = Session.evaluate(request(), nil, NOW, { sidecarFresh = false })
  ok(not decision.accepted, "with no live sidecar heartbeat the offer is rejected")
  equal(decision.reason_code, REASON.STALE_SESSION, "with STALE_SESSION")
  ok(Session.evaluate(request(), nil, NOW, { sidecarFresh = true }).accepted, "a live sidecar is accepted")
end

Harness.group("the session holder tracks state")
do
  local holder = Session.new()
  isNil(holder:current(), "a fresh holder has no session")
  isNil(holder:id(), "and no session id")

  local first = holder:offer(request(), NOW, { randomFn = function() return 1 end })
  ok(first.accepted, "the first offer is accepted")
  equal(holder:id(), "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33", "the holder reports the open session id")
  equal(holder:current().generation, 1, "the first session is generation one")
  ok(holder:current().game_nonce ~= nil, "the mod minted its own nonce for the heartbeat reply")
  equal(holder:current().armed, false, "the held session is not armed")

  local replay = holder:offer(request(), NOW, {})
  ok(not replay.accepted, "replaying the same document is rejected")
  equal(holder:current().generation, 1, "a rejected offer leaves the open session untouched")

  local second = holder:offer(
    request({ session_id = "11111111-2222-3333-4444-555555555555", nonce = "nonce-two" }),
    NOW + 1,
    { randomFn = function() return 2 end }
  )
  ok(second.accepted, "a genuinely new session is accepted")
  equal(holder:current().generation, 2, "the generation advances")
  ok(holder.previous.terminated, "the displaced session is marked terminated")
  Harness.notEqual(holder:current().game_nonce, first.session.game_nonce, "each session gets its own mod nonce")

  ok(holder:terminate("SESSION_TERMINATED"), "terminate closes the open session")
  isNil(holder:current(), "there is no open session afterwards")
  ok(not holder:terminate(), "terminating twice reports that there was nothing to close")
end

Harness.group("a session this run already closed cannot be reopened by its own document")
do
  -- The one-deep `previous` memory is defeated by a single intervening
  -- session: after A, B, the file on disk can be rewritten with A's document
  -- and A is neither the open session nor the remembered previous one. A is
  -- dead -- the sidecar moved on -- and reopening it would make the mod accept
  -- commands and resolve references stamped with a session nobody is running.
  local SECOND = "11111111-2222-3333-4444-555555555555"
  local holder = Session.new()
  local a = request({ nonce = "nonce-one" })
  ok(holder:offer(a, NOW).accepted, "the first session opens")
  ok(holder:offer(request({ session_id = SECOND, nonce = "nonce-two" }), NOW).accepted, "the second replaces it")

  local replay = holder:offer(request({ nonce = "nonce-one" }), NOW)
  ok(not replay.accepted, "re-presenting the first session's document is refused")
  equal(replay.reason_code, REASON.STALE_SESSION, "as a stale session")
  equal(holder:id(), SECOND, "and the open session is untouched")

  local recycled = holder:offer(request({ nonce = "nonce-three" }), NOW)
  ok(not recycled.accepted, "a session id this run already used is refused even under a fresh nonce")
  equal(recycled.reason_code, REASON.STALE_SESSION, "as a stale session")

  local reNonced = holder:offer(
    request({ session_id = "99999999-8888-7777-6666-555555555555", nonce = "nonce-two" }),
    NOW
  )
  ok(not reNonced.accepted, "and a nonce this run already accepted is refused under a fresh session id")
  equal(holder:id(), SECOND, "the open session survives every one of them")

  local third = holder:offer(
    request({ session_id = "99999999-8888-7777-6666-555555555555", nonce = "nonce-four" }),
    NOW
  )
  ok(third.accepted, "a genuinely new session is still accepted")
end

Harness.group("mod nonces are deterministic given their inputs")
do
  local a = Session.makeNonce(NOW, 1, function() return 7 end)
  local b = Session.makeNonce(NOW, 1, function() return 7 end)
  local c = Session.makeNonce(NOW, 2, function() return 7 end)
  equal(a, b, "the same inputs give the same nonce")
  Harness.notEqual(a, c, "a different counter gives a different nonce")
  ok(a:match("^[A-Za-z0-9_%.%-]+$") ~= nil, "the nonce uses the safe alphabet a reference segment allows")
end

Harness.finish("test_session")
