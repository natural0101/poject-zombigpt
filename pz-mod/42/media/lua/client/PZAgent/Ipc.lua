--[[
PZAgent.Ipc -- file exchange with the sidecar, over the fixed layout of
blueprint 3.2 (%USERPROFILE%\Zomboid\Lua\pz_agent\).

Two invariants.

**A command can never name a file.** Every filename is a constant in the FILES
table and the only thing a caller may supply is a role key, which is looked up
in that table. There is no code path from a decoded command to a path
fragment, so no amount of malformed input reaches an unintended file.

**A reader never sees a half-written document.** Journals are appended one
complete line at a time, and the newline is what commits a record. Full
snapshots use the two-slot scheme of blueprint 3.5: the slot that is *not*
currently published is rewritten, and only then is the pointer rewritten. The
pointer is the commit point, so a reader either follows the old slot or the new
one and never parses a file that is being written.

The game's file API is reached through an injected table so this module can be
exercised outside the game. The default resolves the engine globals lazily --
loading this file on a machine with no game must not fail.

`pcall` is used exactly where blueprint 13.2 permits it: around the calls that
cross into the engine. Failures are returned, never swallowed.
]]

PZAgent = PZAgent or {}

local Ipc = {}
PZAgent.Ipc = Ipc

--- Exchange directory, relative to the game's Lua directory.
Ipc.ROOT = "pz_agent"

--- Every file the mod may touch. Mirrors pz_agent_core.ipc.layout.
Ipc.FILES = {
  session = "session.json",
  capabilities = "capabilities.json",
  snapshot_a = "observation.snapshot.a.json",
  snapshot_b = "observation.snapshot.b.json",
  snapshot_pointer = "observation.snapshot.pointer",
  observation_events = "observation.events.0001.jsonl",
  command_queue = "command.queue.0001.jsonl",
  command_ack = "command.ack.0001.jsonl",
  game_heartbeat = "heartbeat.game.json",
  sidecar_heartbeat = "heartbeat.sidecar.json",
  panic_stop = "panic.stop",
}

--- Journal record types reserved by the transport itself. A caller that tried
--- to append one could forge a rotation and make the reader believe records it
--- never received had been dropped on purpose.
Ipc.HEADER_TYPE = "journal.header"
Ipc.ROTATED_TYPE = "journal.rotated"

--- Matches pz_agent_core.ipc.journal.MAX_LINE_BYTES: a longer line is refused
--- by the reader, so writing it would only produce an unreadable record.
local MAX_LINE_BYTES = 256 * 1024

--- Matches pz_agent_core.ipc.atomic.MAX_DOCUMENT_BYTES' order of magnitude and
--- bounds a single read of a whole document into memory on the game thread.
local MAX_DOCUMENT_BYTES = 4 * 1024 * 1024

--- Journals grow without an atomic rename to rotate them, so the mod bounds
--- them itself: at the cap the file is restarted with a fresh header whose
--- serial is one higher. The sidecar's reader sees the serial jump and reports
--- lost records, which is honest -- silently growing the file until the disk
--- fills is not.
local DEFAULT_MAX_JOURNAL_BYTES = 1024 * 1024

--- Upper bound on lines pulled out of a journal in one poll, so a backlog
--- cannot stall a game tick.
local DEFAULT_MAX_LINES = 256

local Handle = {}
Handle.__index = Handle

--- Resolve the engine's file API. Returns nil plus a reason when the globals
--- are absent, which is what makes this module loadable outside the game.
function Ipc.defaultFileApi()
  if type(getFileWriter) ~= "function" or type(getFileReader) ~= "function" then
    return nil, "the game's file API (getFileWriter/getFileReader) is not available"
  end
  return {
    openWriter = function(name, append)
      return getFileWriter(name, true, append)
    end,
    openReader = function(name)
      return getFileReader(name, false)
    end,
  }
end

--- Path of the file `role` names.
---
--- The lookup is the security boundary: an unknown role is an error, never a
--- filename.
function Ipc.pathFor(role)
  if type(role) ~= "string" then
    return nil, "file role must be a string"
  end
  local name = Ipc.FILES[role]
  if name == nil then
    return nil, string.format("unknown file role %q", role)
  end
  return Ipc.ROOT .. "/" .. name
end

--- Create an IPC handle. `options.fileApi` overrides the engine file API.
function Ipc.new(options)
  options = options or {}
  local fileApi = options.fileApi
  local apiError = nil
  if type(fileApi) ~= "table" then
    if fileApi ~= nil then
      fileApi, apiError = nil, "the injected file API must be a table"
    else
      fileApi, apiError = Ipc.defaultFileApi()
    end
  end
  local self = setmetatable({
    fileApi = fileApi,
    fileApiError = apiError,
    maxJournalBytes = options.maxJournalBytes or DEFAULT_MAX_JOURNAL_BYTES,
    maxLines = options.maxLines or DEFAULT_MAX_LINES,
    clock = options.clock,
    journals = {},
    lastSlot = nil,
  }, Handle)
  return self
end

--- True when the engine file API was resolved. Every operation reports the
--- reason it was not rather than pretending the write happened.
function Handle:isAvailable()
  return type(self.fileApi) == "table"
end

function Handle:nowMs()
  if self.clock then
    return self.clock()
  end
  if type(getTimestampMs) == "function" then
    return getTimestampMs()
  end
  return 0
end

local function encodeLine(record)
  local Json = PZAgent.Json
  local text, err = Json.encode(record)
  if text == nil then
    return nil, err
  end
  if #text + 1 > MAX_LINE_BYTES then
    return nil, string.format("record of %d bytes exceeds the %d byte line limit", #text + 1, MAX_LINE_BYTES)
  end
  return text .. "\n"
end

--- Write `text` to the file `role` names. `append` selects append mode.
function Handle:writeRaw(role, text, append)
  if not self:isAvailable() then
    return nil, self.fileApiError
  end
  local path, pathError = Ipc.pathFor(role)
  if path == nil then
    return nil, pathError
  end
  local opened, writer = pcall(self.fileApi.openWriter, path, append and true or false)
  if not opened then
    return nil, string.format("opening %s failed: %s", path, tostring(writer))
  end
  if writer == nil then
    return nil, string.format("the game refused to open %s for writing", path)
  end
  local written, writeError = pcall(function()
    writer:write(text)
  end)
  local closed, closeError = pcall(function()
    writer:close()
  end)
  if not written then
    return nil, string.format("writing %s failed: %s", path, tostring(writeError))
  end
  if not closed then
    return nil, string.format("closing %s failed: %s", path, tostring(closeError))
  end
  return #text
end

--- Read up to `maxLines` lines from the file `role` names.
--- Returns a table of lines (possibly empty) or nil plus a reason. A missing
--- file is not an error: it is an empty stream that may appear later.
function Handle:readLines(role, maxLines, startLine)
  if not self:isAvailable() then
    return nil, self.fileApiError
  end
  local path, pathError = Ipc.pathFor(role)
  if path == nil then
    return nil, pathError
  end
  local opened, reader = pcall(self.fileApi.openReader, path)
  if not opened then
    return nil, string.format("opening %s failed: %s", path, tostring(reader))
  end
  if reader == nil then
    return {}
  end
  local limit = maxLines or self.maxLines
  local skip = startLine or 0
  local lines = {}
  local count = 0
  local bytes = 0
  local failure = nil
  local seen = 0
  while count < limit do
    local ok, line = pcall(function()
      return reader:readLine()
    end)
    if not ok then
      failure = string.format("reading %s failed: %s", path, tostring(line))
      break
    end
    if line == nil then
      break
    end
    seen = seen + 1
    bytes = bytes + #line + 1
    if bytes > MAX_DOCUMENT_BYTES then
      failure = string.format("%s exceeds the %d byte read limit", path, MAX_DOCUMENT_BYTES)
      break
    end
    if seen > skip then
      count = count + 1
      lines[count] = line
    end
  end
  pcall(function()
    reader:close()
  end)
  if failure then
    return nil, failure
  end
  return lines
end

--- Replace the file `role` names with `document`, encoded as JSON.
function Handle:writeDocument(role, document)
  local text, err = PZAgent.Json.encode(document)
  if text == nil then
    return nil, err
  end
  if #text > MAX_DOCUMENT_BYTES then
    return nil, string.format("document of %d bytes exceeds the %d byte limit", #text, MAX_DOCUMENT_BYTES)
  end
  return self:writeRaw(role, text, false)
end

--- Read and decode the whole document in the file `role` names.
--- Returns nil plus a reason when the file is absent, unreadable or not a
--- complete JSON document -- the caller must not act on a partial read.
function Handle:readDocument(role)
  local lines, err = self:readLines(role, math.huge)
  if lines == nil then
    return nil, err
  end
  if #lines == 0 then
    return nil, "document is empty or absent"
  end
  return PZAgent.Json.decode(table.concat(lines, "\n"))
end

-- ---------------------------------------------------------------------------
-- journals
-- ---------------------------------------------------------------------------

local function journalState(self, role)
  local state = self.journals[role]
  if state ~= nil then
    return state
  end
  state = { serial = 0, bytes = 0, started = false }
  self.journals[role] = state
  -- Resume the serial of a file an earlier game session left behind, so a
  -- reader that survived the restart is not told the stream rewound.
  local lines = self:readLines(role, 1)
  if lines ~= nil and lines[1] ~= nil then
    local header = PZAgent.Json.decode(lines[1])
    if type(header) == "table" and header.type == Ipc.HEADER_TYPE and type(header.serial) == "number" then
      state.serial = header.serial
      state.started = true
    end
  end
  return state
end

local function writeHeader(self, role, state)
  local line, err = encodeLine({
    type = Ipc.HEADER_TYPE,
    serial = state.serial,
    created_at_ms = self:nowMs(),
  })
  if line == nil then
    return nil, err
  end
  local written, writeError = self:writeRaw(role, line, state.started)
  if written == nil then
    return nil, writeError
  end
  state.bytes = written
  state.started = true
  return written
end

--- Append one record to a journal, as a single complete line.
---
--- When the file reaches the size cap it is restarted with the next serial:
--- without an atomic rename there is no way to keep the old generation, so the
--- reader is told records were lost instead of being left to guess.
function Handle:appendRecord(role, record)
  if type(record) == "table" and (record.type == Ipc.HEADER_TYPE or record.type == Ipc.ROTATED_TYPE) then
    return nil, string.format("record type %q is reserved for the journal itself", record.type)
  end
  local line, encodeError = encodeLine(record)
  if line == nil then
    return nil, encodeError
  end
  local state = journalState(self, role)
  if not state.started then
    local ok, headerError = writeHeader(self, role, state)
    if ok == nil then
      return nil, headerError
    end
  elseif state.bytes > 0 and state.bytes + #line > self.maxJournalBytes then
    local marker = encodeLine({
      type = Ipc.ROTATED_TYPE,
      serial = state.serial,
      next_serial = state.serial + 1,
      rotated_at_ms = self:nowMs(),
    })
    if marker ~= nil then
      self:writeRaw(role, marker, true)
    end
    state.serial = state.serial + 1
    state.started = false
    local ok, headerError = writeHeader(self, role, state)
    if ok == nil then
      return nil, headerError
    end
  end
  local written, writeError = self:writeRaw(role, line, true)
  if written == nil then
    return nil, writeError
  end
  state.bytes = state.bytes + written
  return state.bytes
end

--- Bytes written to `role`'s journal since this handle first touched it.
function Handle:journalBytes(role)
  local state = self.journals[role]
  return state and state.bytes or 0
end

-- ---------------------------------------------------------------------------
-- snapshots
-- ---------------------------------------------------------------------------

local SLOT_ROLE = { a = "snapshot_a", b = "snapshot_b" }

--- The slot the pointer currently names, or nil when the pointer is unusable.
function Handle:currentSlot()
  local pointer = self:readDocument("snapshot_pointer")
  if type(pointer) ~= "table" then
    return nil
  end
  if pointer.slot ~= "a" and pointer.slot ~= "b" then
    return nil
  end
  return pointer.slot
end

--- Publish a full snapshot. Returns the slot it landed in.
---
--- The slot is chosen from the pointer on disk rather than from memory, so a
--- mod that reloaded mid-session never overwrites the slot a reader is about to
--- follow.
function Handle:publishSnapshot(document)
  if type(document) ~= "table" then
    return nil, "a snapshot must be a table"
  end
  local seq = document.seq
  if type(seq) ~= "number" or seq ~= math.floor(seq) or seq < 0 then
    return nil, "a snapshot requires a non-negative integer 'seq'"
  end
  local current = self:currentSlot()
  local slot = (current == "a") and "b" or "a"
  local written, writeError = self:writeDocument(SLOT_ROLE[slot], document)
  if written == nil then
    return nil, writeError
  end
  -- The pointer is written last and is the commit point.
  local pointed, pointerError = self:writeDocument("snapshot_pointer", {
    slot = slot,
    seq = seq,
    written_at_ms = self:nowMs(),
  })
  if pointed == nil then
    return nil, pointerError
  end
  self.lastSlot = slot
  return slot
end

-- ---------------------------------------------------------------------------
-- panic stop file
-- ---------------------------------------------------------------------------

--- True when the sidecar (or a previous run) left a panic stop request.
---
--- The file is treated as a flag, not as data: its contents are never parsed
--- into an instruction. Any non-empty content means "stop", which is the only
--- reading that fails safe.
function Handle:panicStopRequested()
  local lines, err = self:readLines("panic_stop", 4)
  if lines == nil then
    return false, err
  end
  for index = 1, #lines do
    if #lines[index] > 0 then
      return true
    end
  end
  return false
end

--- Clear the panic stop request, after the stop has actually been applied.
function Handle:clearPanicStop()
  return self:writeRaw("panic_stop", "", false)
end

--- Raise a panic stop request that the sidecar can observe.
function Handle:requestPanicStop(reason)
  local text, err = PZAgent.Json.encode({
    requested_at_ms = self:nowMs(),
    source = "mod",
    reason = reason or PZAgent.Protocol.REASON.PANIC_STOP,
  })
  if text == nil then
    return nil, err
  end
  return self:writeRaw("panic_stop", text .. "\n", false)
end

return Ipc
