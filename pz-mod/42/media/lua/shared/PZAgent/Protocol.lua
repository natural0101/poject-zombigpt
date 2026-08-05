--[[
PZAgent.Protocol -- the closed vocabularies of the wire protocol, restated for
the mod side.

Invariant: every constant here mirrors
packages/pz_agent_core/src/pz_agent_core/protocol/ and version.py exactly. The
mod is the side that has to refuse an unknown command *before* dispatch, so it
cannot delegate the question "is this a real action name?" to the sidecar that
sent it. Duplication is the price of that check; drift is caught by the version
gate and by tests/lua/test_protocol.lua.

Nothing here has behaviour beyond lookup and version comparison. Anything that
decides what may run belongs in PZAgent.Safety.
]]

PZAgent = PZAgent or {}

local Protocol = {}
PZAgent.Protocol = Protocol

--- Mirrors pz_agent_core.version.
Protocol.PROTOCOL_VERSION = "1.0"
Protocol.SCHEMA_VERSION = "1.0"
Protocol.MOD_VERSION = "0.1.0"
Protocol.TARGET_BUILD = "42.20"
Protocol.SUPPORTED_BUILDS = { "42.20" }

--- Every command the sidecar may send, in the order ActionName declares them.
--- The mod rejects anything outside this list before it reaches an adapter.
Protocol.ACTION_NAMES = {
  "session.arm",
  "session.disarm",
  "safety.stop",
  "action.wait",
  "movement.move_to",
  "movement.move_near",
  "inventory.transfer",
  "inventory.ensure_main",
  "inventory.equip",
  "inventory.unequip",
  "consume.eat",
  "consume.drink",
  "literature.read",
  "world.inspect",
  "plan.cancel",
}

local function toSet(list)
  local set = {}
  for index = 1, #list do
    set[list[index]] = true
  end
  return set
end

Protocol.ACTIONS = toSet(Protocol.ACTION_NAMES)

--- Actions that only read state: allowed in OBSERVE and without arming.
Protocol.READ_ONLY_ACTIONS = toSet({ "world.inspect", "action.wait" })

--- Actions that bypass the arming check entirely -- stopping must always work.
Protocol.ALWAYS_ALLOWED_ACTIONS = toSet({ "safety.stop", "session.disarm", "plan.cancel" })

Protocol.STATUS = {
  RECEIVED = "received",
  ACCEPTED = "accepted",
  REJECTED = "rejected",
  STARTED = "started",
  PROGRESS = "progress",
  SUCCEEDED = "succeeded",
  FAILED = "failed",
  CANCELLED = "cancelled",
  LOST = "lost",
}

Protocol.TERMINAL_STATUSES = toSet({
  Protocol.STATUS.SUCCEEDED,
  Protocol.STATUS.FAILED,
  Protocol.STATUS.CANCELLED,
  Protocol.STATUS.REJECTED,
  Protocol.STATUS.LOST,
})

Protocol.MODE = {
  OFF = "OFF",
  OBSERVE = "OBSERVE",
  ASSISTED = "ASSISTED",
  AUTONOMOUS = "AUTONOMOUS",
  REFLEX_ONLY = "REFLEX_ONLY",
  EXPERIMENTAL_INPUT = "EXPERIMENTAL_INPUT",
}

--- Modes in which mutating commands are accepted at all.
Protocol.MUTATING_MODES = toSet({
  Protocol.MODE.ASSISTED,
  Protocol.MODE.AUTONOMOUS,
  Protocol.MODE.EXPERIMENTAL_INPUT,
})

--- Which side of the exchange wrote a document. Mirrors
--- pz_agent_core.session.heartbeat.Peer, which refuses a heartbeat file whose
--- `peer` does not match the file it was found in.
Protocol.PEER = {
  GAME = "game",
  SIDECAR = "sidecar",
}

Protocol.DANGER = {
  NONE = "none",
  LOW = "low",
  MEDIUM = "medium",
  HIGH = "high",
  CRITICAL = "critical",
}

Protocol.DANGER_RANK = {
  [Protocol.DANGER.NONE] = 0,
  [Protocol.DANGER.LOW] = 1,
  [Protocol.DANGER.MEDIUM] = 2,
  [Protocol.DANGER.HIGH] = 3,
  [Protocol.DANGER.CRITICAL] = 4,
}

Protocol.OWNERSHIP = {
  NONE = "none",
  MOD = "mod",
  MANUAL = "manual",
  AMBIGUOUS = "ambiguous",
}

Protocol.CAPABILITY = {
  VERIFIED = "verified",
  AVAILABLE_UNVERIFIED = "available_unverified",
  EXPERIMENTAL = "experimental",
  UNSUPPORTED = "unsupported",
  DISABLED_BY_POLICY = "disabled_by_policy",
}

--- Stable reason codes. Append-only: renaming one is a protocol major bump.
Protocol.REASON = {
  POSTCONDITION_MET = "POSTCONDITION_MET",

  NOT_ARMED = "NOT_ARMED",
  STALE_SESSION = "STALE_SESSION",
  LEASE_EXPIRED = "LEASE_EXPIRED",
  SEQ_CONFLICT = "SEQ_CONFLICT",
  GAME_DISCONNECTED = "GAME_DISCONNECTED",
  SAVE_CHANGED = "SAVE_CHANGED",
  SESSION_TERMINATED = "SESSION_TERMINATED",

  CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE",
  INVALID_REF = "INVALID_REF",
  INVALID_ARGUMENT = "INVALID_ARGUMENT",
  PRECONDITION_FAILED = "PRECONDITION_FAILED",
  QUEUE_REJECTED = "QUEUE_REJECTED",
  POLICY_DENIED = "POLICY_DENIED",

  PLAYER_BUSY_MANUAL_ACTION = "PLAYER_BUSY_MANUAL_ACTION",
  USER_TAKEOVER = "USER_TAKEOVER",
  THREAT_INTERRUPTED = "THREAT_INTERRUPTED",
  PANIC_STOP = "PANIC_STOP",
  CANCELLED_BY_REQUEST = "CANCELLED_BY_REQUEST",

  PATH_NOT_FOUND = "PATH_NOT_FOUND",
  PATH_STUCK = "PATH_STUCK",
  TARGET_OUT_OF_RANGE = "TARGET_OUT_OF_RANGE",
  TARGET_NOT_LOADED = "TARGET_NOT_LOADED",

  ACTION_TIMEOUT = "ACTION_TIMEOUT",
  POSTCONDITION_FAILED = "POSTCONDITION_FAILED",
  NO_PROGRESS = "NO_PROGRESS",

  NO_SAFE_FOOD = "NO_SAFE_FOOD",
  NO_SAFE_DRINK = "NO_SAFE_DRINK",
  NO_SUITABLE_LITERATURE = "NO_SUITABLE_LITERATURE",
  CONTAINER_FULL = "CONTAINER_FULL",
  RESOURCE_RESERVED = "RESOURCE_RESERVED",

  INTERNAL_ERROR = "INTERNAL_ERROR",
}

--- True when `name` is a command this build knows how to dispatch.
function Protocol.isKnownAction(name)
  return type(name) == "string" and Protocol.ACTIONS[name] == true
end

--- True when `name` needs neither arming nor a mutating mode.
function Protocol.isAlwaysAllowed(name)
  return type(name) == "string" and Protocol.ALWAYS_ALLOWED_ACTIONS[name] == true
end

--- True when `name` only reads state.
function Protocol.isReadOnly(name)
  return type(name) == "string" and Protocol.READ_ONLY_ACTIONS[name] == true
end

--- True when `status` ends a command's lifecycle.
function Protocol.isTerminalStatus(status)
  return type(status) == "string" and Protocol.TERMINAL_STATUSES[status] == true
end

--- Major component of a MAJOR.MINOR version string, or nil when malformed.
function Protocol.major(version)
  if type(version) ~= "string" then
    return nil
  end
  local head = version:match("^(%d+)%.%d+$")
  if head == nil then
    return nil
  end
  return tonumber(head)
end

--- Compatibility is major-only: minor versions are additive by contract, so a
--- peer on 1.7 talks to this 1.0 while 2.0 is refused.
function Protocol.isCompatible(remoteVersion)
  local remote = Protocol.major(remoteVersion)
  return remote ~= nil and remote == Protocol.major(Protocol.PROTOCOL_VERSION)
end

--- True when `build` is one this release was designed against. A build outside
--- the set is reported, not silently accepted as equivalent.
function Protocol.isSupportedBuild(build)
  for index = 1, #Protocol.SUPPORTED_BUILDS do
    if Protocol.SUPPORTED_BUILDS[index] == build then
      return true
    end
  end
  return false
end

--- Normalise a mode name to the enum spelling, or nil when it is not a mode.
--- The blueprint's session.json example writes "observe" while the protocol
--- enum spells it "OBSERVE"; accepting both here means a lowercase handshake
--- is understood instead of being rejected as malformed.
function Protocol.normaliseMode(mode)
  if type(mode) ~= "string" then
    return nil
  end
  local upper = mode:upper()
  if Protocol.MODE[upper] == upper then
    return upper
  end
  return nil
end

--- Rank of a danger level for comparisons, or nil when unknown.
function Protocol.dangerRank(level)
  if type(level) ~= "string" then
    return nil
  end
  return Protocol.DANGER_RANK[level]
end

return Protocol
