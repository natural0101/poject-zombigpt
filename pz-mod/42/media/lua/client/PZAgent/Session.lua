--[[
PZAgent.Session -- the handshake of blueprint 3.3.

Invariant: a session document is accepted only when every one of the blueprint's
conditions holds -- well-formed session id, a protocol major this build speaks,
a timestamp that is not stale, a nonce that differs from the previous session's,
and a live sidecar. Accepting one has exactly one effect: the mod knows which
session id to stamp on references and journal records. It never arms. Arming is
a separate, explicit command (blueprint 8.1), and a handshake that could arm
would turn "the sidecar restarted" into "the agent started playing".

The nonce check is what stops a replay. The session document is an ordinary file
in a directory the player can edit and any local process can write; re-presenting
yesterday's document with yesterday's session id would otherwise resurrect a
session whose references point at objects that no longer exist.

`evaluate` is pure -- no globals, no file access -- so every rejection branch is
testable outside the game.
]]

PZAgent = PZAgent or {}

local Session = {}
PZAgent.Session = Session

--- Session documents older than this are refused. Generous enough to survive a
--- slow game load, short enough that a document left on disk from a previous
--- play session is never mistaken for a live offer.
Session.MAX_AGE_MS = 60 * 1000

--- Tolerance for a sidecar clock running ahead of the game's. Beyond it the
--- timestamp is not skew, it is wrong.
Session.MAX_SKEW_MS = 5 * 1000

--- Bounds on the observation rate the sidecar may ask for, in Hz. The upper
--- bound is the real constraint: the observation tick runs on the game thread.
Session.MIN_OBSERVATION_HZ = 1
Session.MAX_OBSERVATION_HZ = 10
Session.DEFAULT_OBSERVATION_HZ = 4

local UUID_PATTERN = "^%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x$"
local NONCE_PATTERN = "^[A-Za-z0-9_%.%-]+$"
local MAX_NONCE_BYTES = 64

local Handle = {}
Handle.__index = Handle

local function reject(reasonCode, detail)
  return { accepted = false, reason_code = reasonCode, detail = detail }
end

local function isInteger(value)
  return type(value) == "number" and value == math.floor(value)
end

--- Decide whether an incoming session document may be accepted.
---
--- `previous` is the last session this mod accepted (or nil), `nowMs` the
--- current wall clock in milliseconds, and `options.sidecarFresh` whether a
--- sidecar heartbeat has been seen recently.
---
--- Returns a decision table: `accepted`, plus either `session` or
--- `reason_code`/`detail`.
function Session.evaluate(request, previous, nowMs, options)
  options = options or {}
  local Protocol = PZAgent.Protocol
  local reasons = Protocol.REASON

  if type(request) ~= "table" then
    return reject(reasons.INVALID_ARGUMENT, "session document is not an object")
  end
  if not isInteger(nowMs) then
    return reject(reasons.INTERNAL_ERROR, "the mod has no usable clock")
  end

  if type(request.protocol_version) ~= "string" then
    return reject(reasons.INVALID_ARGUMENT, "protocol_version is missing")
  end
  if not Protocol.isCompatible(request.protocol_version) then
    return reject(
      reasons.INVALID_ARGUMENT,
      string.format(
        "protocol major of %q does not match %q",
        request.protocol_version,
        Protocol.PROTOCOL_VERSION
      )
    )
  end

  if type(request.session_id) ~= "string" or request.session_id:match(UUID_PATTERN) == nil then
    return reject(reasons.INVALID_ARGUMENT, "session_id is not a UUID")
  end

  if type(request.nonce) ~= "string"
    or #request.nonce == 0
    or #request.nonce > MAX_NONCE_BYTES
    or request.nonce:match(NONCE_PATTERN) == nil
  then
    return reject(reasons.INVALID_ARGUMENT, "nonce is missing or malformed")
  end

  if not isInteger(request.created_at_ms) or request.created_at_ms < 0 then
    return reject(reasons.INVALID_ARGUMENT, "created_at_ms is missing or not an integer")
  end

  local mode = Protocol.normaliseMode(request.mode)
  if mode == nil then
    return reject(reasons.INVALID_ARGUMENT, "mode is missing or not a known session mode")
  end

  local hz = request.requested_observation_hz
  if hz == nil then
    hz = Session.DEFAULT_OBSERVATION_HZ
  end
  if type(hz) ~= "number" or hz < Session.MIN_OBSERVATION_HZ or hz > Session.MAX_OBSERVATION_HZ then
    return reject(
      reasons.INVALID_ARGUMENT,
      string.format(
        "requested_observation_hz must be within %d..%d",
        Session.MIN_OBSERVATION_HZ,
        Session.MAX_OBSERVATION_HZ
      )
    )
  end

  local age = nowMs - request.created_at_ms
  local maxAge = options.maxAgeMs or Session.MAX_AGE_MS
  if age > maxAge then
    return reject(
      reasons.STALE_SESSION,
      string.format("session document is %d ms old, limit is %d ms", age, maxAge)
    )
  end
  if age < -(options.maxSkewMs or Session.MAX_SKEW_MS) then
    return reject(reasons.STALE_SESSION, "session document is timestamped in the future")
  end

  if type(previous) == "table" then
    if previous.nonce ~= nil and previous.nonce == request.nonce then
      -- Same nonce means the same document. Either the sidecar failed to mint a
      -- new one or something is replaying an old file; neither may open a
      -- session.
      return reject(reasons.STALE_SESSION, "nonce is identical to the previous session's")
    end
    if previous.session_id == request.session_id and previous.terminated ~= true then
      return reject(reasons.STALE_SESSION, "this session id is already open")
    end
  end

  if options.sidecarFresh == false then
    return reject(reasons.STALE_SESSION, "no live sidecar heartbeat")
  end

  return {
    accepted = true,
    reason_code = reasons.POSTCONDITION_MET,
    session = {
      session_id = request.session_id,
      nonce = request.nonce,
      sidecar_version = type(request.sidecar_version) == "string" and request.sidecar_version or nil,
      protocol_version = request.protocol_version,
      mode = mode,
      observation_hz = hz,
      created_at_ms = request.created_at_ms,
      accepted_at_ms = nowMs,
      -- Accepting a session never arms. Blueprint 8.1 makes arming an explicit,
      -- separate step.
      armed = false,
      generation = 0,
    },
  }
end

--- Mint the mod's own nonce for the heartbeat reply.
---
--- Not a security token: it exists so the sidecar can tell one game session from
--- the next after a restart. `randomFn` is injectable so the value is
--- deterministic under test.
function Session.makeNonce(nowMs, counter, randomFn)
  local salt = 0
  if randomFn then
    salt = randomFn()
  elseif math.random then
    salt = math.random(0, 0xFFFFFF)
  end
  return string.format("g%x-%x-%x", math.floor(nowMs or 0), math.floor(counter or 0), math.floor(salt))
end

--- Create the session holder the rest of the mod reads from.
function Session.new()
  return setmetatable({ session = nil, previous = nil, generation = 0, nonceCounter = 0 }, Handle)
end

--- The open session, or nil.
function Handle:current()
  return self.session
end

--- The id of the open session, or nil. Convenience for reference building.
function Handle:id()
  return self.session and self.session.session_id or nil
end

--- Offer a session document. On acceptance the previous session is closed and
--- the new one becomes current; on rejection nothing changes.
function Handle:offer(request, nowMs, options)
  local baseline = self.session or self.previous
  local decision = Session.evaluate(request, baseline, nowMs, options)
  if not decision.accepted then
    return decision
  end
  if self.session ~= nil then
    self.previous = self.session
    self.previous.terminated = true
  end
  self.generation = self.generation + 1
  self.nonceCounter = self.nonceCounter + 1
  decision.session.generation = self.generation
  decision.session.game_nonce = Session.makeNonce(nowMs, self.nonceCounter, options and options.randomFn)
  self.session = decision.session
  return decision
end

--- Close the open session. Its nonce stays remembered, so the same document
--- cannot reopen it.
function Handle:terminate(reason)
  if self.session == nil then
    return false
  end
  self.session.terminated = true
  self.session.terminated_reason = reason or PZAgent.Protocol.REASON.SESSION_TERMINATED
  self.previous = self.session
  self.session = nil
  return true
end

return Session
