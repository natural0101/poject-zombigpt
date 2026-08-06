--[[
PZAgent.CommandReader -- the mod's end of command.queue.0001.jsonl.

Invariant: a record leaves this module only after it has been proved to be a
complete, well-formed command from the session that is open right now, and no
record is ever handed out twice for execution.

Four things follow from that, and each exists because the obvious shortcut is
dangerous.

**A half-written line is not a command.** The sidecar commits a record with the
newline that ends it, but the engine's reader is line-oriented and hands back a
trailing fragment exactly as it hands back a finished line -- there is no API
here that reports whether the file ended with a newline. So completeness is
decided by decoding: a truncated JSON object does not parse, the cursor is not
advanced past it, and the next poll sees the finished line. A fragment that
stops growing is not a slow write but a corrupt record, and after a bounded
number of polls it is dropped with a diagnostic instead of blocking the stream
forever.

**A command from a session that is no longer open is refused.** References are
session-scoped: the same runtime id denotes a different object after a reload,
so executing yesterday's command against today's world is not a stale no-op, it
is the wrong action on the wrong thing.

**The lease is checked here and again before execution.** This is only the
first check. A command can sit behind a long timed action and reach the front
of the queue against a world that moved on, which is why PZAgent.ActionRuntime
checks it a second time and this file does not pretend to be sufficient.

**A redelivered command replays its stored terminal result.** Re-executing "eat
half a sandwich" because an ack was lost costs the player food that cannot be
un-eaten. The cache that makes that possible is bounded and evicts in insertion
order: replaying a result must not keep it alive forever.

The cursor is a byte offset plus the line count that produced it. The line count
is what the engine's reader can act on (it skips lines, it cannot seek); the
byte offset is what the sidecar's own reader speaks and what the diagnostics
report. Rotation is detected from the journal header's serial, and resets both:
after a rotation the file at the fixed name is a different generation, and a
cursor carried across it would skip the new generation's first records.
]]

PZAgent = PZAgent or {}

local CommandReader = {}
PZAgent.CommandReader = CommandReader

--- Lines read in one poll. The whole batch is decoded on the game thread, so
--- this is deliberately small; PZAgent.ActionRuntime asks for less again when
--- its own per-tick budget is nearly spent. The budget counts lines rather than
--- commands, so a journal header or a rotation marker spends one: the cost this
--- bounds is decoding, and those cost the same as a command.
CommandReader.MAX_RECORDS_PER_POLL = 16

--- Commands whose terminal result is remembered for replay. Insertion-ordered
--- eviction: a redelivery that keeps arriving must not keep its own entry alive
--- and push out results that have not been acknowledged yet.
CommandReader.MAX_REMEMBERED = 128

--- How many polls a trailing fragment may stay undecodable before it is treated
--- as corrupt rather than as a write in progress. At the mod's tick cadence
--- this is a fraction of a second -- far longer than a buffered write takes,
--- far shorter than a player would wait for a stuck queue.
CommandReader.MAX_PARTIAL_POLLS = 8

--- Lines the reader is willing to walk past to reach its cursor. The engine's
--- reader cannot seek, so every poll re-reads the lines it has already
--- consumed; past this the file is left alone and the reason is reported.
--- Self-healing rather than fatal: the sidecar rotates the queue journal on
--- size, and a rotation resets the cursor to zero.
CommandReader.MAX_SCAN_LINES = 4096

--- Mirrors the command schema's bounds. A value outside them is a malformed
--- command, not a command to clamp: clamping a lease would invent authority the
--- sidecar never granted.
CommandReader.MIN_LEASE_MS = 100
CommandReader.MAX_LEASE_MS = 300000
CommandReader.MAX_IDEMPOTENCY_BYTES = 128

--- What one polled record turned out to be.
CommandReader.KIND = {
  --- A command that passed every check in this file and may be dispatched.
  COMMAND = "command",
  --- A well-formed envelope that must be refused, with a reason code to ack.
  REJECTED = "rejected",
  --- A redelivery whose original terminal result is replayed verbatim.
  REPLAY = "replay",
  --- A redelivery of a command that has not finished yet: nothing to replay and
  --- nothing to execute, because the original is still in flight.
  DUPLICATE = "duplicate",
  --- A record too damaged to identify. It cannot be acked -- an ack needs a
  --- command id -- so it is reported as a diagnostic and nothing else.
  DROPPED = "dropped",
}

local KIND = CommandReader.KIND

--- Same shape Session demands of a session id, for the same reason: the id is
--- part of every reference, and a malformed one cannot have minted the refs a
--- command carries.
local UUID_PATTERN = "^%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x$"

--- Top-level fields of the command envelope. Anything else in the record is
--- dropped rather than forwarded: an adapter must never see a key the wire
--- contract does not define.
local ENVELOPE_FIELDS = {
  "protocol_version",
  "schema_version",
  "session_id",
  "seq",
  "command_id",
  "idempotency_key",
  "issued_at_ms",
  "lease_ms",
  "expected_observation_seq",
  "action",
}

local Handle = {}
Handle.__index = Handle

local function isInteger(value)
  return type(value) == "number" and value == math.floor(value)
end

--- Create a reader over `options.ipc`. Returns nil plus a reason, so a caller
--- that forgot the IPC handle finds out here rather than at the first poll.
function CommandReader.new(options)
  options = options or {}
  if type(options.ipc) ~= "table" then
    return nil, "a command reader needs an Ipc handle"
  end
  return setmetatable({
    ipc = options.ipc,
    role = "command_queue",
    lines = 0,
    offset = 0,
    serial = nil,
    partial_bytes = nil,
    partial_polls = 0,
    rotations = 0,
    maxRemembered = options.maxRemembered or CommandReader.MAX_REMEMBERED,
    maxRecordsPerPoll = options.maxRecordsPerPoll or CommandReader.MAX_RECORDS_PER_POLL,
    maxPartialPolls = options.maxPartialPolls or CommandReader.MAX_PARTIAL_POLLS,
    maxScanLines = options.maxScanLines or CommandReader.MAX_SCAN_LINES,
    seen = {},
    keys = {},
    order = {},
  }, Handle)
end

--- Byte offset of the first line this reader has not consumed.
function Handle:byteOffset()
  return self.offset
end

--- Lines this reader has consumed from the current generation.
function Handle:lineOffset()
  return self.lines
end

--- Commands whose outcome is remembered for replay.
function Handle:rememberedCount()
  return #self.order
end

local function forget(self, commandId)
  local entry = self.seen[commandId]
  if entry == nil then
    return
  end
  self.seen[commandId] = nil
  if entry.key ~= nil and self.keys[entry.key] == commandId then
    self.keys[entry.key] = nil
  end
end

local function track(self, command)
  local existing = self.seen[command.command_id]
  if existing ~= nil then
    return existing
  end
  local entry = { key = command.idempotency_key, ack = nil }
  self.seen[command.command_id] = entry
  self.keys[command.idempotency_key] = command.command_id
  self.order[#self.order + 1] = command.command_id
  while #self.order > self.maxRemembered do
    forget(self, table.remove(self.order, 1))
  end
  return entry
end

--- Record the terminal ack of `commandId`, so a redelivery replays it.
---
--- Refuses a non-terminal status: remembering "started" as the answer to a
--- redelivery would tell the sidecar the command finished when it has not.
function Handle:remember(commandId, ack)
  if type(commandId) ~= "string" or type(ack) ~= "table" then
    return nil, "remembering a result needs a command id and an ack record"
  end
  if not PZAgent.Protocol.isTerminalStatus(ack.status) then
    return nil, string.format("status %q is not terminal", tostring(ack.status))
  end
  local entry = self.seen[commandId]
  if entry == nil then
    entry = { key = nil, ack = nil }
    self.seen[commandId] = entry
    self.order[#self.order + 1] = commandId
    while #self.order > self.maxRemembered do
      forget(self, table.remove(self.order, 1))
    end
  end
  entry.ack = ack
  return true
end

--- The remembered terminal ack of `commandId`, or nil.
function Handle:rememberedAck(commandId)
  local entry = self.seen[commandId]
  return entry and entry.ack or nil
end

-- ---------------------------------------------------------------------------
-- envelope validation
-- ---------------------------------------------------------------------------

local function rejection(record, reasonCode, detail)
  return {
    kind = KIND.REJECTED,
    command_id = record.command_id,
    action = record.action,
    seq = record.seq,
    -- Carried so the ack can be addressed to the session that issued the
    -- command even when that session is not the one the mod has open -- which
    -- is exactly the case for a STALE_SESSION refusal. The sidecar refuses an
    -- ack whose session_id is not a UUID, so a malformed one is dropped here
    -- rather than sent as an unreadable answer.
    session_id = type(record.session_id) == "string"
      and record.session_id:match(UUID_PATTERN) ~= nil
      and record.session_id
      or nil,
    reason_code = reasonCode,
    detail = detail,
  }
end

--- Enough of the envelope to address an ack at it: without a command id and an
--- action there is nobody to answer, and the record can only be dropped.
local function addressable(record)
  return type(record.command_id) == "string"
    and record.command_id:match(UUID_PATTERN) ~= nil
    and type(record.action) == "string"
    and #record.action <= 64
end

--- Copy the declared envelope fields, and nothing else.
local function normalise(record)
  local command = {}
  for index = 1, #ENVELOPE_FIELDS do
    local field = ENVELOPE_FIELDS[index]
    command[field] = record[field]
  end
  command.args = record.args
  return command
end

local function checkEnvelope(record, sessionId, nowMs)
  local Protocol = PZAgent.Protocol
  local reasons = Protocol.REASON

  if type(record.protocol_version) ~= "string" or not Protocol.isCompatible(record.protocol_version) then
    return reasons.INVALID_ARGUMENT,
      string.format(
        "protocol major of %s does not match %s",
        tostring(record.protocol_version),
        Protocol.PROTOCOL_VERSION
      )
  end
  -- The envelope carries no schema_version of its own in the 1.0 contract; when
  -- a peer adds one it is still checked, because a schema major this build does
  -- not speak changes what the fields below mean.
  if record.schema_version ~= nil then
    if type(record.schema_version) ~= "string" or not Protocol.isCompatible(record.schema_version) then
      return reasons.INVALID_ARGUMENT,
        string.format("schema major of %s is not supported", tostring(record.schema_version))
    end
  end
  if sessionId == nil then
    return reasons.STALE_SESSION, "no session is open"
  end
  if record.session_id ~= sessionId then
    return reasons.STALE_SESSION,
      string.format("command names session %s, not the open one", tostring(record.session_id))
  end
  if not isInteger(record.seq) or record.seq < 0 then
    return reasons.INVALID_ARGUMENT, "seq is missing or not a non-negative integer"
  end
  if type(record.idempotency_key) ~= "string"
    or #record.idempotency_key == 0
    or #record.idempotency_key > CommandReader.MAX_IDEMPOTENCY_BYTES
  then
    return reasons.INVALID_ARGUMENT, "idempotency_key is missing or out of bounds"
  end
  if not isInteger(record.issued_at_ms) or record.issued_at_ms < 0 then
    return reasons.INVALID_ARGUMENT, "issued_at_ms is missing or not an integer"
  end
  if not isInteger(record.lease_ms)
    or record.lease_ms < CommandReader.MIN_LEASE_MS
    or record.lease_ms > CommandReader.MAX_LEASE_MS
  then
    return reasons.INVALID_ARGUMENT, "lease_ms is missing or outside the protocol bounds"
  end
  if record.expected_observation_seq ~= nil then
    if not isInteger(record.expected_observation_seq) or record.expected_observation_seq < 0 then
      return reasons.INVALID_ARGUMENT, "expected_observation_seq is not a non-negative integer"
    end
  end
  if not Protocol.isKnownAction(record.action) then
    -- The dispatcher refuses an unknown name too. Refusing it here as well is
    -- what keeps it out of the in-flight queue: a command no adapter can serve
    -- must never occupy the one mutating slot.
    return reasons.INVALID_ARGUMENT,
      string.format("action %s is not in the protocol whitelist", tostring(record.action))
  end
  if record.args ~= nil and type(record.args) ~= "table" then
    return reasons.INVALID_ARGUMENT, "args must be an object"
  end
  if not Protocol.isAlwaysAllowed(record.action)
    and PZAgent.Safety.leaseExpired(record, nowMs)
  then
    -- Stop, disarm and cancel are exempt for the same reason Safety.mayStart
    -- exempts them: honouring a stale one only ever removes authority.
    return reasons.LEASE_EXPIRED,
      string.format(
        "lease of %s ms issued at %s had run out by %s",
        tostring(record.lease_ms),
        tostring(record.issued_at_ms),
        tostring(nowMs)
      )
  end
  return nil
end

--- Turn one decoded record into the entry PZAgent.ActionRuntime acts on.
---
--- Returns nil for a record that belongs to the transport rather than to the
--- protocol, which is the one case with nothing to report.
function Handle:classify(record, sessionId, nowMs)
  local Ipc = PZAgent.Ipc
  if type(record) ~= "table" then
    return { kind = KIND.DROPPED, detail = "record is not an object" }
  end
  if record.type == Ipc.HEADER_TYPE or record.type == Ipc.ROTATED_TYPE then
    return nil
  end
  if not addressable(record) then
    return { kind = KIND.DROPPED, detail = "record carries no usable command_id and action" }
  end
  local reasonCode, detail = checkEnvelope(record, sessionId, nowMs)
  if reasonCode ~= nil then
    return rejection(record, reasonCode, detail)
  end

  local command = normalise(record)
  command.args = command.args or {}

  local known = self.seen[command.command_id]
  if known == nil then
    local byKey = self.keys[command.idempotency_key]
    known = byKey ~= nil and self.seen[byKey] or nil
  end
  if known ~= nil then
    if known.ack ~= nil then
      return { kind = KIND.REPLAY, command = command, ack = known.ack }
    end
    return {
      kind = KIND.DUPLICATE,
      command = command,
      detail = "the original has not reached a terminal state yet",
    }
  end
  track(self, command)
  return { kind = KIND.COMMAND, command = command }
end

-- ---------------------------------------------------------------------------
-- cursor
-- ---------------------------------------------------------------------------

local function consume(self, line)
  self.lines = self.lines + 1
  -- The newline the sidecar wrote is what committed the record, so it counts
  -- towards the offset the sidecar's own reader would report.
  self.offset = self.offset + #line + 1
end

local function resetCursor(self, serial)
  self.lines = 0
  self.offset = 0
  self.serial = serial
  self.partial_bytes = nil
  self.partial_polls = 0
end

--- Notice a rotation. Returns a diagnostic when the generation changed.
---
--- The sidecar rotates by renaming the current file and starting a fresh one
--- under the same fixed name, so the only evidence available here is the header
--- serial. Records written to the retired generation after this reader last
--- polled are gone: the mod cannot read the renamed file (its name is not in
--- the fixed layout), so the loss is reported rather than papered over.
function Handle:syncGeneration()
  local lines, err = self.ipc:readLines(self.role, 1)
  if lines == nil then
    return nil, err
  end
  if lines[1] == nil then
    if self.serial ~= nil then
      resetCursor(self, nil)
      return "the command queue is empty; the cursor was reset"
    end
    return nil
  end
  local header = PZAgent.Json.decode(lines[1])
  if type(header) ~= "table" or header.type ~= PZAgent.Ipc.HEADER_TYPE then
    return nil
  end
  if type(header.serial) ~= "number" then
    return nil
  end
  if self.serial == nil then
    self.serial = header.serial
    return nil
  end
  if header.serial == self.serial then
    return nil
  end
  local previous = self.serial
  local skipped = self.lines
  resetCursor(self, header.serial)
  self.rotations = self.rotations + 1
  return string.format(
    "the command queue rotated from serial %s to %s; records after line %s of the retired generation were lost",
    tostring(previous),
    tostring(header.serial),
    tostring(skipped)
  )
end

--- Whether an undecodable trailing line should be held for another poll.
local function holdPartial(self, byteCount)
  if self.partial_bytes ~= byteCount then
    self.partial_bytes = byteCount
    self.partial_polls = 1
    return true
  end
  self.partial_polls = self.partial_polls + 1
  return self.partial_polls <= self.maxPartialPolls
end

--- Read the next batch of records.
---
--- Returns a list of entries (see CommandReader.KIND), an optional diagnostic
--- string, and how many lines the batch consumed -- or nil plus a reason when
--- the queue could not be read at all. An empty list is the ordinary case: no
--- new records.
---
--- The line count is what the caller spends its budget on, and it is not the
--- number of entries: a transport record produces no entry and a held-back
--- fragment produces neither, yet both cost a decode.
function Handle:poll(sessionId, nowMs, limit)
  local budget = limit or self.maxRecordsPerPoll
  if budget > self.maxRecordsPerPoll then
    budget = self.maxRecordsPerPoll
  end
  if budget <= 0 then
    return {}
  end
  local diagnostic, syncError = self:syncGeneration()
  if syncError ~= nil then
    return nil, syncError
  end
  if self.lines + budget > self.maxScanLines then
    return nil,
      string.format(
        "the command queue cursor is at line %s, past the %s line scan budget; waiting for the sidecar to rotate it",
        tostring(self.lines),
        tostring(self.maxScanLines)
      )
  end
  local lines, readError = self.ipc:readLines(self.role, budget, self.lines)
  if lines == nil then
    return nil, readError
  end
  local entries = {}
  local consumed = 0
  for index = 1, #lines do
    local line = lines[index]
    local record = PZAgent.Json.decode(line)
    consumed = consumed + 1
    if record == nil then
      if index == #lines and holdPartial(self, #line) then
        -- Incomplete rather than corrupt, as far as anything here can tell.
        -- The cursor stays put and the next poll sees the finished line.
        consumed = consumed - 1
        break
      end
      consume(self, line)
      entries[#entries + 1] = {
        kind = KIND.DROPPED,
        detail = string.format("record of %s bytes did not decode as JSON", tostring(#line)),
      }
      self.partial_bytes = nil
      self.partial_polls = 0
    else
      consume(self, line)
      self.partial_bytes = nil
      self.partial_polls = 0
      local entry = self:classify(record, sessionId, nowMs)
      if entry ~= nil then
        entries[#entries + 1] = entry
      end
    end
  end
  return entries, diagnostic, consumed
end

return CommandReader
