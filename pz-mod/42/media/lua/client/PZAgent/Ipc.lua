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

On Windows an open can be refused by a sharing lock the other side of the
exchange -- the sidecar's poll, an antivirus scan, even this module's own
pointer read an instant earlier -- holds for microseconds. A refused open is
therefore retried a small bounded number of times inside the same call: the
game thread cannot sleep, but the lock holder is usually already closing, so
consecutive immediate reopens win the common race. A snapshot whose slot was
written but whose pointer commit was refused is carried over so the next
publish commits exactly that pointer first, boundedly.

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

--- How many times one call may try the open before reporting the refusal. On
--- Windows getFileWriter/getFileReader answer nil (or raise) while someone
--- else holds a sharing lock on the file; the holder is usually a reader
--- already closing, so an immediate reopen -- sleeping inside a game tick is
--- not an option -- wins the common race within a try or two.
--- Published so a test can derive the bound instead of keeping a copy of the
--- number, which is how a cap gets raised without its guard noticing.
Ipc.MAX_LINE_BYTES = MAX_LINE_BYTES
Ipc.MAX_DOCUMENT_BYTES = MAX_DOCUMENT_BYTES

Ipc.WRITE_OPEN_ATTEMPTS = 3
Ipc.READ_OPEN_ATTEMPTS = 3

--- How many consecutive publishSnapshot calls may carry an uncommitted
--- pointer before the pending commit is dropped and the failure reported as
--- persistent. The carry-over is one pending pointer for a bounded number of
--- ticks, never an accumulating queue.
Ipc.POINTER_COMMIT_TICK_BUDGET = 10

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
    -- The slot this handle last *committed* -- pointer write succeeded -- and
    -- a commit that wrote its slot but not yet its pointer. Both start empty:
    -- the first publish consults the pointer on disk instead.
    lastSlot = nil,
    pendingPointer = nil,
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
---
--- A refused open -- getFileWriter raising or answering nil, which is what a
--- Windows sharing lock looks like from here -- is retried up to
--- WRITE_OPEN_ATTEMPTS times before the failure is reported, with the attempt
--- count in the report so a persistent lock reads differently from a single
--- lost race.
function Handle:writeRaw(role, text, append)
  if not self:isAvailable() then
    return nil, self.fileApiError
  end
  local path, pathError = Ipc.pathFor(role)
  if path == nil then
    return nil, pathError
  end
  local writer = nil
  local raised = nil
  for _ = 1, Ipc.WRITE_OPEN_ATTEMPTS do
    local opened, result = pcall(self.fileApi.openWriter, path, append and true or false)
    if opened and result ~= nil then
      writer = result
      break
    end
    -- Remember how the *latest* attempt failed: the report below describes
    -- the state the budget ended in, not the first stumble.
    raised = (not opened) and result or nil
  end
  if writer == nil then
    if raised ~= nil then
      return nil,
        string.format("opening %s failed after %d attempts: %s", path, Ipc.WRITE_OPEN_ATTEMPTS, tostring(raised))
    end
    return nil,
      string.format("the game refused to open %s for writing (%d attempts)", path, Ipc.WRITE_OPEN_ATTEMPTS)
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
---
--- An open that raises or answers nil is retried up to READ_OPEN_ATTEMPTS
--- times -- a Windows sharing lock and an absent file are indistinguishable
--- from here, and the reopen costs less than losing the poll. Only after the
--- whole budget is a nil open treated as absence.
---
--- When the read succeeded but the reader's close raised, the lines are
--- returned together with a second value naming the leaked handle, so a
--- caller that records errors can report the leak instead of a discarded
--- pcall result hiding it.
function Handle:readLines(role, maxLines, startLine)
  if not self:isAvailable() then
    return nil, self.fileApiError
  end
  local path, pathError = Ipc.pathFor(role)
  if path == nil then
    return nil, pathError
  end
  local reader = nil
  local raised = nil
  for _ = 1, Ipc.READ_OPEN_ATTEMPTS do
    local opened, result = pcall(self.fileApi.openReader, path)
    if opened and result ~= nil then
      reader = result
      break
    end
    -- As in writeRaw: the report describes the state the budget ended in.
    raised = (not opened) and result or nil
  end
  if reader == nil then
    if raised ~= nil then
      return nil,
        string.format("opening %s failed after %d attempts: %s", path, Ipc.READ_OPEN_ATTEMPTS, tostring(raised))
    end
    -- Still nil after the whole budget: the file is absent (or held longer
    -- than any closing reader would hold it), and absent is not an error.
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
  -- The close stays inside pcall -- a raise here must not unwind the game
  -- tick -- but its failure is no longer discarded: a handle the engine could
  -- not release is exactly the sharing lock that refuses the next open.
  local closed, closeError = pcall(function()
    reader:close()
  end)
  if failure then
    return nil, failure
  end
  if not closed then
    -- The data was read in full, so the lines are returned; the leak rides
    -- along as a second value for the caller's last_error instead of
    -- replacing a successful poll with a failure.
    return lines, string.format("closing %s failed: %s", path, tostring(closeError))
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

--- Size of the journal already on disk, or nil when it could not be measured.
---
--- The engine exposes no stat call, so the file is measured by reading it. That
--- is the only way the size cap can hold across a mod reload: a handle that
--- started counting at zero would let the file grow by another whole cap every
--- time the mod is reloaded, which is unbounded growth on a long-running game.
--- The read is itself bounded -- `readLines` refuses past MAX_DOCUMENT_BYTES --
--- and a file too large to measure is over any cap this module would set, which
--- is why nil is treated as "rotate now" by the caller.
local function measureJournal(self, role)
  local lines, err = self:readLines(role, math.huge)
  if lines == nil then
    return nil, err
  end
  local bytes = 0
  for index = 1, #lines do
    bytes = bytes + #lines[index] + 1
  end
  return bytes
end

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
      -- Adopt the size too, not just the serial: the cap is on the file, not on
      -- what this handle happens to have appended to it.
      state.bytes = measureJournal(self, role) or self.maxJournalBytes
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
--- The slot alternates from the slot this handle last *committed* -- pointer
--- write succeeded -- held in memory. Reading the pointer back from disk on
--- every publish, microseconds before truncate-writing it, is how the mod
--- collided with its own sharing lock on Windows (and with the sidecar's
--- poll). Only the first publish after a load reads the pointer from disk: a
--- mod that reloaded mid-session must not overwrite the slot a reader of the
--- previous incarnation is about to follow.
---
--- When the slot write succeeds but the pointer write is refused, the disk
--- pointer still names the old slot, so the commit is carried over: the next
--- publish retries exactly that pointer first, then writes its own snapshot
--- to the other slot as usual. The carry-over exists only for a slot whose
--- content is complete, so the pointer can never name a slot whose write
--- failed. After POINTER_COMMIT_TICK_BUDGET consecutive publishes without a
--- committed pointer the pending state is dropped and the failure reported as
--- persistent.
function Handle:publishSnapshot(document)
  if type(document) ~= "table" then
    return nil, "a snapshot must be a table"
  end
  local seq = document.seq
  if type(seq) ~= "number" or seq ~= math.floor(seq) or seq < 0 then
    return nil, "a snapshot requires a non-negative integer 'seq'"
  end
  local pending = self.pendingPointer
  if pending ~= nil then
    local pointed, pointerError = self:writeDocument("snapshot_pointer", {
      slot = pending.slot,
      seq = pending.seq,
      written_at_ms = pending.written_at_ms,
    })
    if pointed == nil then
      pending.attempts = pending.attempts + 1
      if pending.attempts >= Ipc.POINTER_COMMIT_TICK_BUDGET then
        self.pendingPointer = nil
        return nil,
          string.format(
            "the snapshot pointer could not be committed for %d consecutive publishes"
              .. " (last failure: %s); the pending commit for slot %q is dropped",
            Ipc.POINTER_COMMIT_TICK_BUDGET,
            tostring(pointerError),
            pending.slot
          )
      end
      return nil, pointerError
    end
    -- The carried-over commit landed: the pointer now names the pending slot
    -- and the alternation continues from it, sending this call's document to
    -- the other slot.
    self.pendingPointer = nil
    self.lastSlot = pending.slot
  end
  local current = self.lastSlot
  if current == nil then
    -- First publish since this handle was created: only the pointer on disk
    -- knows which slot a reader may be following.
    current = self:currentSlot()
  end
  local slot = (current == "a") and "b" or "a"
  local written, writeError = self:writeDocument(SLOT_ROLE[slot], document)
  if written == nil then
    return nil, writeError
  end
  -- The pointer is written last and is the commit point.
  local writtenAt = self:nowMs()
  local pointed, pointerError = self:writeDocument("snapshot_pointer", {
    slot = slot,
    seq = seq,
    written_at_ms = writtenAt,
  })
  if pointed == nil then
    -- The slot holds a complete document but the disk pointer still names the
    -- old one. lastSlot is deliberately not advanced: a reader may be
    -- following the old slot as current, and the next publish must not treat
    -- the uncommitted slot as published. The commit is carried over instead.
    self.pendingPointer = { slot = slot, seq = seq, written_at_ms = writtenAt, attempts = 1 }
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
