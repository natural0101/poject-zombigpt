--[[
PZAgent.Refs -- stable, session-scoped references to game objects.

Invariant: the strings this module builds and parses are byte-identical to the
ones produced by pz_agent_core.protocol.refs. The two sides never exchange a
Lua table or a Java handle, only these strings, so a disagreement about where
the delimiters fall does not fail loudly -- it silently resolves to a different
object, which is exactly the class of bug the whole reference scheme exists to
prevent.

The subtle part is the item reference. Its container tail itself contains
colons ("world:10726:9455:0:3:1"), so an item reference must be split from the
ends -- last colon for the generation, next-to-last for the runtime id, and
everything between the session and those two is the tail, verbatim. A naive
left-to-right split produces a reference that looks plausible and points at the
wrong container.

Every parser returns `table` or `nil, message`. Nothing raises: a malformed
reference is a routine event on a boundary that accepts input from outside the
game.
]]

PZAgent = PZAgent or {}

local Refs = {}
PZAgent.Refs = Refs

Refs.SEPARATOR = ":"

Refs.KIND = {
  ITEM = "item",
  CONTAINER = "container",
  SQUARE = "square",
  ZOMBIE = "zombie",
  WOUND = "wound",
  OBJECT = "object",
}

local KIND_SET = {}
for _, kind in pairs(Refs.KIND) do
  KIND_SET[kind] = true
end

--- No legitimate reference comes close to this. The bound keeps the scanning
--- loops below finite on input that arrived from the exchange directory.
local MAX_REF_BYTES = 1024

--- Container and item segments use a restricted alphabet so a reference can
--- never smuggle a path separator or a delimiter. Mirrors _SAFE_SEGMENT.
local SEGMENT_PATTERN = "^[A-Za-z0-9_%.%-]+$"

--- Mirrors _SESSION_SEGMENT: UUIDs, and nothing that could contain a colon.
local SESSION_PATTERN = "^[A-Za-z0-9%-]+$"
local MAX_SESSION_BYTES = 64

local function isSegment(value)
  return type(value) == "string" and value:match(SEGMENT_PATTERN) ~= nil
end

local function isSession(value)
  return type(value) == "string"
    and #value >= 1
    and #value <= MAX_SESSION_BYTES
    and value:match(SESSION_PATTERN) ~= nil
end

Refs.isSessionSegment = isSession
Refs.isSafeSegment = isSegment

--- Split at the first separator. Mirrors Python's str.partition: when there is
--- no separator the whole string is the head and the tail is empty.
local function partition(text)
  local index = text:find(Refs.SEPARATOR, 1, true)
  if index == nil then
    return text, false, ""
  end
  return text:sub(1, index - 1), true, text:sub(index + 1)
end

--- Split at the last separator. Mirrors Python's str.rpartition: when there is
--- no separator the head is empty and the whole string is the tail.
local function rpartition(text)
  local last = nil
  local cursor = 1
  while true do
    local index = text:find(Refs.SEPARATOR, cursor, true)
    if index == nil then
      break
    end
    last = index
    cursor = index + 1
  end
  if last == nil then
    return "", false, text
  end
  return text:sub(1, last - 1), true, text:sub(last + 1)
end

--- Accept only what SquareRef/ZombieRef would have written: an optionally
--- signed run of ASCII digits. Deliberately stricter than Python's int(),
--- which also accepts surrounding whitespace and underscore separators --
--- neither can be produced by the writer, so both mean tampering here.
local function toInteger(text)
  if type(text) ~= "string" or text:match("^[-+]?%d+$") == nil then
    return nil
  end
  return tonumber(text)
end

local function isCount(text)
  return type(text) == "string" and text:match("^%d+$") ~= nil
end

local function checkRaw(raw)
  if type(raw) ~= "string" then
    return "reference must be a string, got " .. type(raw)
  end
  if #raw == 0 then
    return "reference is empty"
  end
  if #raw > MAX_REF_BYTES then
    return string.format("reference of %d bytes exceeds the %d byte limit", #raw, MAX_REF_BYTES)
  end
  return nil
end

-- ---------------------------------------------------------------------------
-- container references
-- ---------------------------------------------------------------------------

--- container:<session>:<tail>
function Refs.buildContainer(sessionId, tail)
  if not isSession(sessionId) then
    return nil, "invalid session segment"
  end
  if type(tail) ~= "string" or #tail == 0 then
    return nil, "container tail is empty"
  end
  for part in (tail .. Refs.SEPARATOR):gmatch("([^:]*)" .. Refs.SEPARATOR) do
    if not isSegment(part) then
      return nil, string.format("invalid container tail segment: %q", part)
    end
  end
  return Refs.KIND.CONTAINER .. Refs.SEPARATOR .. sessionId .. Refs.SEPARATOR .. tail
end

function Refs.playerMainContainer(sessionId)
  return Refs.buildContainer(sessionId, "player-main")
end

function Refs.wornContainer(sessionId, slot, itemRuntimeId)
  if not isSegment(slot) then
    return nil, "invalid slot segment"
  end
  if not isSegment(itemRuntimeId) then
    return nil, "invalid runtime id segment"
  end
  return Refs.buildContainer(sessionId, "worn" .. Refs.SEPARATOR .. slot .. Refs.SEPARATOR .. itemRuntimeId)
end

function Refs.carriedContainer(sessionId, itemRuntimeId)
  if not isSegment(itemRuntimeId) then
    return nil, "invalid runtime id segment"
  end
  return Refs.buildContainer(sessionId, "carried" .. Refs.SEPARATOR .. itemRuntimeId)
end

--- container:<session>:world:<x>:<y>:<z>:<object-index>:<container-index>
function Refs.worldContainer(sessionId, x, y, z, objectIndex, containerIndex)
  local parts = { "world" }
  local coordinates = { x, y, z, objectIndex, containerIndex }
  for index = 1, 5 do
    local value = coordinates[index]
    if type(value) ~= "number" or value ~= math.floor(value) then
      return nil, "world container coordinates must be integers"
    end
    parts[index + 1] = string.format("%.0f", value)
  end
  return Refs.buildContainer(sessionId, table.concat(parts, Refs.SEPARATOR))
end

function Refs.parseContainer(raw)
  local rawError = checkRaw(raw)
  if rawError then
    return nil, rawError
  end
  local kind, _, rest = partition(raw)
  if kind ~= Refs.KIND.CONTAINER then
    return nil, "not a container ref"
  end
  local session, _, tail = partition(rest)
  if #tail == 0 then
    return nil, "container ref missing tail"
  end
  if not isSession(session) then
    return nil, "invalid session segment"
  end
  for part in (tail .. Refs.SEPARATOR):gmatch("([^:]*)" .. Refs.SEPARATOR) do
    if not isSegment(part) then
      return nil, string.format("invalid container tail segment: %q", part)
    end
  end
  return {
    kind = Refs.KIND.CONTAINER,
    session_id = session,
    tail = tail,
  }
end

function Refs.isPlayerMainTail(tail)
  return tail == "player-main"
end

--- True for the main inventory and anything worn or carried -- the containers
--- that move with the player and so need no travel to reach.
function Refs.isOnPersonTail(tail)
  if type(tail) ~= "string" then
    return false
  end
  return tail == "player-main" or tail:sub(1, 5) == "worn:" or tail:sub(1, 8) == "carried:"
end

function Refs.isWorldTail(tail)
  return type(tail) == "string" and tail:sub(1, 6) == "world:"
end

-- ---------------------------------------------------------------------------
-- item references
-- ---------------------------------------------------------------------------

--- item:<session>:<container-tail>:<runtime-id>:<generation>
function Refs.buildItem(sessionId, containerTail, runtimeId, generation)
  if not isSession(sessionId) then
    return nil, "invalid session segment"
  end
  if type(containerTail) ~= "string" or #containerTail == 0 then
    return nil, "container tail is empty"
  end
  if not isSegment(runtimeId) then
    return nil, "invalid runtime id segment"
  end
  if type(generation) ~= "number" or generation ~= math.floor(generation) or generation < 0 then
    return nil, "generation must be a non-negative integer"
  end
  return table.concat({
    Refs.KIND.ITEM,
    sessionId,
    containerTail,
    runtimeId,
    string.format("%.0f", generation),
  }, Refs.SEPARATOR)
end

--- Parse an item reference from the ends, never left to right.
---
--- The container tail is returned verbatim and is not re-validated segment by
--- segment: ItemRef.parse on the Python side does not validate it either, and
--- a mod that were stricter would reject a reference the sidecar considers
--- well-formed. Callers that need the tail checked run it through
--- parseContainer or containerRefOf.
function Refs.parseItem(raw)
  local rawError = checkRaw(raw)
  if rawError then
    return nil, rawError
  end
  local kind, _, rest = partition(raw)
  if kind ~= Refs.KIND.ITEM then
    return nil, "not an item ref"
  end
  local session, _, remainder = partition(rest)
  local head, hasGeneration, generation = rpartition(remainder)
  if not hasGeneration then
    return nil, "item ref missing generation"
  end
  local containerTail, hasRuntimeId, runtimeId = rpartition(head)
  if not hasRuntimeId then
    return nil, "item ref missing runtime id"
  end
  if not isCount(generation) then
    return nil, "item ref generation must be numeric"
  end
  if not isSession(session) then
    return nil, "invalid session segment"
  end
  if not isSegment(runtimeId) then
    return nil, "invalid runtime id segment"
  end
  return {
    kind = Refs.KIND.ITEM,
    session_id = session,
    container_tail = containerTail,
    runtime_id = runtimeId,
    generation = tonumber(generation),
  }
end

--- Reconstruct the owning container reference of a parsed item.
function Refs.containerRefOf(item)
  if type(item) ~= "table" then
    return nil, "expected a parsed item ref"
  end
  return Refs.buildContainer(item.session_id, item.container_tail)
end

-- ---------------------------------------------------------------------------
-- square and zombie references
-- ---------------------------------------------------------------------------

--- square:<session>:<x>:<y>:<z>
function Refs.buildSquare(sessionId, x, y, z)
  if not isSession(sessionId) then
    return nil, "invalid session segment"
  end
  local parts = { Refs.KIND.SQUARE, sessionId }
  local coordinates = { x, y, z }
  for index = 1, 3 do
    local value = coordinates[index]
    if type(value) ~= "number" or value ~= math.floor(value) then
      return nil, "square coordinates must be integers"
    end
    parts[index + 2] = string.format("%.0f", value)
  end
  return table.concat(parts, Refs.SEPARATOR)
end

function Refs.parseSquare(raw)
  local rawError = checkRaw(raw)
  if rawError then
    return nil, rawError
  end
  local parts = {}
  local count = 0
  for part in (raw .. Refs.SEPARATOR):gmatch("([^:]*)" .. Refs.SEPARATOR) do
    count = count + 1
    if count > 5 then
      return nil, "not a square ref"
    end
    parts[count] = part
  end
  if count ~= 5 or parts[1] ~= Refs.KIND.SQUARE then
    return nil, "not a square ref"
  end
  if not isSession(parts[2]) then
    return nil, "invalid session segment"
  end
  local x, y, z = toInteger(parts[3]), toInteger(parts[4]), toInteger(parts[5])
  if x == nil or y == nil or z == nil then
    return nil, "square ref has non-integer coordinate"
  end
  return { kind = Refs.KIND.SQUARE, session_id = parts[2], x = x, y = y, z = z }
end

--- Grid distance ignoring the floor; movement checks compare floors separately.
function Refs.chebyshevDistance(first, second)
  return math.max(math.abs(first.x - second.x), math.abs(first.y - second.y))
end

--- zombie:<session>:<runtime-id>:<generation>
function Refs.buildZombie(sessionId, runtimeId, generation)
  if not isSession(sessionId) then
    return nil, "invalid session segment"
  end
  if not isSegment(runtimeId) then
    return nil, "invalid runtime id segment"
  end
  if type(generation) ~= "number" or generation ~= math.floor(generation) or generation < 0 then
    return nil, "generation must be a non-negative integer"
  end
  return table.concat({
    Refs.KIND.ZOMBIE,
    sessionId,
    runtimeId,
    string.format("%.0f", generation),
  }, Refs.SEPARATOR)
end

function Refs.parseZombie(raw)
  local rawError = checkRaw(raw)
  if rawError then
    return nil, rawError
  end
  local parts = {}
  local count = 0
  for part in (raw .. Refs.SEPARATOR):gmatch("([^:]*)" .. Refs.SEPARATOR) do
    count = count + 1
    if count > 4 then
      return nil, "not a zombie ref"
    end
    parts[count] = part
  end
  if count ~= 4 or parts[1] ~= Refs.KIND.ZOMBIE then
    return nil, "not a zombie ref"
  end
  if not isCount(parts[4]) then
    return nil, "zombie ref generation must be numeric"
  end
  if not isSession(parts[2]) then
    return nil, "invalid session segment"
  end
  if not isSegment(parts[3]) then
    return nil, "invalid runtime id segment"
  end
  return {
    kind = Refs.KIND.ZOMBIE,
    session_id = parts[2],
    runtime_id = parts[3],
    generation = tonumber(parts[4]),
  }
end

-- ---------------------------------------------------------------------------
-- generic inspection
-- ---------------------------------------------------------------------------

--- Classify a reference without fully parsing it.
function Refs.kindOf(raw)
  if type(raw) ~= "string" then
    return nil, "reference must be a string"
  end
  local kind = partition(raw)
  if not KIND_SET[kind] then
    return nil, "unknown ref kind"
  end
  return kind
end

--- True when `raw` was minted by `sessionId`.
---
--- A reference from an earlier session is not merely stale: its runtime ids may
--- now denote entirely different objects, so a false here is INVALID_REF and
--- never a reason to retry.
function Refs.belongsToSession(raw, sessionId)
  if type(raw) ~= "string" or type(sessionId) ~= "string" then
    return false
  end
  local kind, _, rest = partition(raw)
  if not KIND_SET[kind] then
    return false
  end
  local candidate = partition(rest)
  return candidate == sessionId
end

--- Parse any reference kind this module understands.
function Refs.parse(raw)
  local kind, kindError = Refs.kindOf(raw)
  if kind == nil then
    return nil, kindError
  end
  if kind == Refs.KIND.ITEM then
    return Refs.parseItem(raw)
  elseif kind == Refs.KIND.CONTAINER then
    return Refs.parseContainer(raw)
  elseif kind == Refs.KIND.SQUARE then
    return Refs.parseSquare(raw)
  elseif kind == Refs.KIND.ZOMBIE then
    return Refs.parseZombie(raw)
  end
  return nil, string.format("no parser for ref kind %q", kind)
end

return Refs
