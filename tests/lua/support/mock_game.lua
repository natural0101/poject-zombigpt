--[[
Mocks for the engine surface the client modules touch.

Only two things are mocked, because only two things are used: the file
writer/reader pair behind PZAgent.Ipc, and the timed-action queue behind
PZAgent.Safety's engine boundary. Everything else in the mod is pure and needs
no stand-in.

The filesystem mock records the order in which files were written. That is what
lets a test assert the snapshot pointer is written *after* its slot -- the
ordering is the entire correctness argument for the two-slot scheme, and it is
invisible to a test that only inspects final contents.
]]

local Mock = {}

-- ---------------------------------------------------------------------------
-- filesystem
-- ---------------------------------------------------------------------------

local Filesystem = {}
Filesystem.__index = Filesystem

--- Create an in-memory filesystem plus the fileApi table PZAgent.Ipc accepts.
function Mock.newFilesystem()
  local self = setmetatable({
    files = {},
    order = {},
    writes = 0,
    failWrites = {},
    failReads = {},
    readGrace = {},
  }, Filesystem)
  self.api = {
    openWriter = function(name, append)
      return self:openWriter(name, append)
    end,
    openReader = function(name)
      return self:openReader(name)
    end,
  }
  return self
end

--- Make every write to `name` raise, to exercise the reporting branches.
function Filesystem:failWritesTo(name)
  self.failWrites[name] = true
end

--- Make reads of `name` raise. `afterOpens` lets the first N opens succeed,
--- which is how a test reaches code that reads one file twice and must cope
--- with the second read failing.
function Filesystem:failReadsFrom(name, afterOpens)
  self.failReads[name] = true
  self.readGrace[name] = afterOpens or 0
end

function Filesystem:openWriter(name, append)
  if self.failWrites[name] then
    error("mock write failure for " .. name, 0)
  end
  if not append then
    self.files[name] = ""
  elseif self.files[name] == nil then
    self.files[name] = ""
  end
  local fs = self
  return {
    write = function(_, text)
      fs.files[name] = fs.files[name] .. text
      fs.writes = fs.writes + 1
      fs.order[#fs.order + 1] = name
    end,
    close = function() end,
  }
end

function Filesystem:openReader(name)
  if self.failReads[name] then
    local grace = self.readGrace[name] or 0
    if grace > 0 then
      self.readGrace[name] = grace - 1
    else
      error("mock read failure for " .. name, 0)
    end
  end
  local content = self.files[name]
  if content == nil then
    return nil
  end
  local lines = {}
  local count = 0
  for line in (content .. "\n"):gmatch("([^\n]*)\n") do
    count = count + 1
    lines[count] = line
  end
  -- A trailing newline produces one empty final entry; the game's reader does
  -- not report it either.
  if count > 0 and lines[count] == "" then
    lines[count] = nil
  end
  local cursor = 0
  return {
    readLine = function()
      cursor = cursor + 1
      return lines[cursor]
    end,
    close = function() end,
  }
end

--- Contents of `name`, or nil.
function Filesystem:read(name)
  return self.files[name]
end

--- Set the contents of `name` directly, standing in for the sidecar.
function Filesystem:put(name, content)
  self.files[name] = content
end

--- Index of the last write to `name` in the global write order, or nil.
function Filesystem:lastWriteIndex(name)
  local found = nil
  for index = 1, #self.order do
    if self.order[index] == name then
      found = index
    end
  end
  return found
end

--- Lines of `name`, decoded from the recorded content.
function Filesystem:lines(name)
  local content = self.files[name]
  if content == nil then
    return {}
  end
  local lines = {}
  local count = 0
  for line in content:gmatch("([^\n]+)") do
    count = count + 1
    lines[count] = line
  end
  return lines
end

-- ---------------------------------------------------------------------------
-- timed action queue
-- ---------------------------------------------------------------------------

--- Install a fake ISTimedActionQueue global holding `entries`.
---
--- `options.noClear` drops the clear function, which is how the "the game does
--- not expose a cancellation we can use" branch is reached.
function Mock.installActionQueue(entries, options)
  options = options or {}
  local queue = { queue = entries or {} }
  local api = {
    getTimedActionQueue = function()
      return queue
    end,
  }
  if not options.noClear then
    api.clear = function()
      if options.clearFails then
        error("mock clear failure", 0)
      end
      queue.queue = {}
    end
  end
  ISTimedActionQueue = api
  return queue
end

--- Remove the fake queue, restoring the "this build has no such API" state.
function Mock.removeActionQueue()
  ISTimedActionQueue = nil
end

--- A player stand-in. `alive` defaults to true.
function Mock.newPlayer(alive)
  return {
    isDead = function()
      return alive == false
    end,
  }
end

return Mock
