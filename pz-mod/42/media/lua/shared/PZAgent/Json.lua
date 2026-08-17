--[[
PZAgent.Json -- dependency-free JSON codec for the file IPC.

Invariant: this module never emits a document it cannot read back. Anything it
is unable to represent exactly is refused with a message instead of being
approximated, because a half-valid document in the exchange directory is worse
than no document at all -- the sidecar would parse it and act on it.

Encoding is deterministic: object keys are emitted in ascending byte order and
every non-ASCII character is escaped. Two encodings of the same state are
therefore the same bytes, which is what makes a snapshot diffable and what
keeps this side byte-compatible with pz_agent_core.ipc.atomic, which serialises
with sort_keys and ensure_ascii.

Strings arrive in two shapes. A plain Lua string is a byte string, and when it
carries non-ASCII text those bytes are UTF-8 -- that is what our own modules
produce. But under Kahlua a Java string (an item display name, a room name)
answers string.byte with UTF-16 code units, not bytes: the Cyrillic letter pe
(U+043F) comes back as the single value 0x043F, and a value above 0xFF is
impossible in a byte string. The two models disagree about what 0x80..0xFF means, so every
string is classified ONCE, by one bounded scan, before any of it is encoded:
if any unit exceeds 0xFF the whole string is UTF-16 units, otherwise it is
bytes. Mixing the interpretations inside a single string would fabricate
characters that were never there.

In the byte model, a byte 0x80..0xFF that does not begin a valid UTF-8
sequence is read as a Latin-1 code point and escaped \u00XX: a Java string
whose characters all fall in U+0000..U+00FF is indistinguishable, through
byte(), from such a byte string, and refusing it was the live failure that
made a whole observation snapshot unencodable over one Russian display name.
The cost is that a genuinely corrupt byte string is also encoded as Latin-1
rather than refused; that trade is deliberate -- the fallback is per-byte,
deterministic, and states honestly which values byte() returned, and it keeps
one string we cannot interrogate further from sinking an entire snapshot.
Valid UTF-8 still encodes exactly as it always has. What is still refused,
with the offset named, is what no reader could round-trip at all: a lone or
mispaired UTF-16 surrogate, and any unit above 0xFFFF.

Decoding is a hand-written recursive-descent parser. Dynamic code evaluation is
forbidden by the project rules, and it would in any case hand the sidecar (and
anything that can write to the exchange directory) arbitrary execution inside
the game process.

Errors are returned as `nil, message`, never raised: this code runs inside game
event handlers where an uncaught error takes the whole handler down.
]]

PZAgent = PZAgent or {}

local Json = {}
PZAgent.Json = Json

--- Nesting deeper than this is a malformed or hostile document, not a state
--- snapshot: the deepest legitimate structure is observation -> inventory ->
--- items -> food, which is four levels.
local MAX_DEPTH = 64

--- Upper bound on a document handed to `decode`. The journal reader caps line
--- length separately; this is the last line of defence against a single
--- pathological file pinning the game thread.
local MAX_INPUT_BYTES = 4 * 1024 * 1024

--- A single table with more entries than this is a bug on the producing side,
--- not a state snapshot. Encoding it would stall the tick.
local MAX_TABLE_KEYS = 65536

--- Beyond 2^53 a double no longer represents consecutive integers, so an
--- integral value above it cannot be printed as an integer without lying about
--- its precision.
local MAX_SAFE_INTEGER = 9007199254740992

local floor = math.floor
local format = string.format
local byte = string.byte
local char = string.char
local sub = string.sub
local concat = table.concat
local sort = table.sort

--- JSON null. A Lua nil cannot live in a table, so a distinct sentinel is the
--- only way to round-trip an explicit null through a decoded document.
local NULL = setmetatable({}, {
  __tostring = function()
    return "null"
  end,
})
--- Published so a test derives the bound instead of copying the number, which
--- is how a cap gets raised without its guard noticing.
Json.MAX_DEPTH = MAX_DEPTH
Json.MAX_TABLE_KEYS = MAX_TABLE_KEYS

Json.null = NULL

--- Marks a table as a JSON array. Needed because an empty Lua table is both a
--- plausible `[]` and a plausible `{}`; without the marker a decode/encode
--- round trip would silently change the type of every empty collection.
local ARRAY_MT = { __name = "PZAgentJsonArray" }

--- Wrap `value` (default: a fresh table) so it always encodes as a JSON array.
function Json.array(value)
  return setmetatable(value or {}, ARRAY_MT)
end

--- True when `value` was explicitly marked as an array.
function Json.isArray(value)
  return type(value) == "table" and getmetatable(value) == ARRAY_MT
end

-- ---------------------------------------------------------------------------
-- encoding
-- ---------------------------------------------------------------------------

local ESCAPES = {
  [0x22] = "\\\"",
  [0x5C] = "\\\\",
  [0x08] = "\\b",
  [0x0C] = "\\f",
  [0x0A] = "\\n",
  [0x0D] = "\\r",
  [0x09] = "\\t",
}

--- Length of the UTF-8 sequence introduced by `first`, and the bits it carries.
local function utf8Lead(first)
  if first < 0xC0 then
    return nil
  elseif first < 0xE0 then
    return 2, first - 0xC0
  elseif first < 0xF0 then
    return 3, first - 0xE0
  elseif first < 0xF8 then
    return 4, first - 0xF0
  end
  return nil
end

--- Smallest code point each sequence length is allowed to encode. Rejecting
--- anything below it is what closes the overlong-encoding hole, where "\xC0\xAF"
--- would otherwise decode to "/" and slip past a path check upstream.
local UTF8_MIN = { [2] = 0x80, [3] = 0x800, [4] = 0x10000 }

--- Decode one UTF-8 sequence starting at `index`.
--- Returns codepoint, nextIndex or nil, message.
local function utf8Decode(text, index)
  local first = byte(text, index)
  local length, codepoint = utf8Lead(first)
  if length == nil then
    return nil, format("invalid UTF-8 lead byte 0x%02X at offset %d", first, index)
  end
  if index + length - 1 > #text then
    return nil, format("truncated UTF-8 sequence at offset %d", index)
  end
  for offset = 1, length - 1 do
    local continuation = byte(text, index + offset)
    if continuation < 0x80 or continuation > 0xBF then
      return nil, format("invalid UTF-8 continuation byte at offset %d", index + offset)
    end
    codepoint = codepoint * 64 + (continuation - 0x80)
  end
  if codepoint < UTF8_MIN[length] then
    return nil, format("overlong UTF-8 sequence at offset %d", index)
  end
  if codepoint > 0x10FFFF then
    return nil, format("UTF-8 code point above U+10FFFF at offset %d", index)
  end
  if codepoint >= 0xD800 and codepoint <= 0xDFFF then
    return nil, format("UTF-8 encodes a surrogate at offset %d", index)
  end
  return codepoint, index + length
end

--- Emit `codepoint` as one or two \uXXXX escapes.
local function escapeCodepoint(codepoint)
  if codepoint < 0x10000 then
    return format("\\u%04x", codepoint)
  end
  local rest = codepoint - 0x10000
  local high = 0xD800 + floor(rest / 0x400)
  local low = 0xDC00 + (rest % 0x400)
  return format("\\u%04x\\u%04x", high, low)
end

--- Decide which string model applies to `text` (see the header): "bytes" for
--- a byte string, "units" when any value byte() yields exceeds 0xFF, which
--- only a Java string seen through Kahlua can produce. One bounded pass over
--- the whole string, because the answer must hold for the whole string --
--- classifying per-character would mix the two interpretations.
--- Returns nil, message when a unit fits neither model.
local function classifyString(text)
  local model = "bytes"
  for index = 1, #text do
    local code = byte(text, index)
    if code > 0xFFFF then
      return nil, format("string unit 0x%X at offset %d fits neither a byte string nor UTF-16 units", code, index)
    end
    if code > 0xFF then
      model = "units"
    end
  end
  return model
end

--- Quote and escape a string in whichever model classifyString assigns it.
--- Returns nil, message only for what no reader could round-trip: a lone or
--- mispaired surrogate, or a unit above 0xFFFF. U+FFFD substitution is not an
--- option there, because it would silently alter game data.
local function encodeString(text)
  local model, modelErr = classifyString(text)
  if model == nil then
    return nil, modelErr
  end
  local pieces = { "\"" }
  local count = 1
  local index = 1
  local length = #text
  while index <= length do
    local code = byte(text, index)
    local piece
    if ESCAPES[code] then
      piece = ESCAPES[code]
      index = index + 1
    elseif code < 0x20 then
      piece = format("\\u%04x", code)
      index = index + 1
    elseif code < 0x80 then
      piece = char(code)
      index = index + 1
    elseif model == "bytes" then
      local codepoint, nextIndex = utf8Decode(text, index)
      if codepoint == nil then
        -- Not UTF-8 at this position, so the Latin-1 reading of the single
        -- byte is the only deterministic one left; the header documents why
        -- this is a fallback and not a refusal. The discarded value in
        -- nextIndex is the UTF-8 diagnostic, which no longer signals an error.
        piece = escapeCodepoint(code)
        index = index + 1
      else
        piece = escapeCodepoint(codepoint)
        index = nextIndex
      end
    elseif code >= 0xD800 and code <= 0xDBFF then
      local low = index < length and byte(text, index + 1)
      if not low or low < 0xDC00 or low > 0xDFFF then
        return nil, format("lone or mispaired high surrogate 0x%04X at offset %d", code, index)
      end
      piece = escapeCodepoint(0x10000 + (code - 0xD800) * 0x400 + (low - 0xDC00))
      index = index + 2
    elseif code >= 0xDC00 and code <= 0xDFFF then
      return nil, format("lone low surrogate 0x%04X at offset %d", code, index)
    else
      piece = escapeCodepoint(code)
      index = index + 1
    end
    count = count + 1
    pieces[count] = piece
  end
  pieces[count + 1] = "\""
  return concat(pieces)
end

--- Format a number so that reading it back yields the identical value.
local function encodeNumber(value)
  if value ~= value then
    return nil, "NaN cannot be represented in JSON"
  end
  if value == math.huge or value == -math.huge then
    return nil, "infinity cannot be represented in JSON"
  end
  if value == 0 then
    -- Normalises negative zero, which JSON permits but which would make two
    -- encodings of the same state differ.
    return "0"
  end
  if value == floor(value) and value >= -MAX_SAFE_INTEGER and value <= MAX_SAFE_INTEGER then
    return format("%.0f", value)
  end
  local shortest = format("%.14g", value)
  if tonumber(shortest) ~= value then
    shortest = format("%.17g", value)
  end
  return shortest
end

--- Decide whether a table is an array or an object.
--- Returns "array"/"object" or nil, message when the shape has no JSON meaning.
local function classifyTable(value)
  local marked = getmetatable(value) == ARRAY_MT
  local count = 0
  local highest = 0
  local hasStringKey = false
  for key in pairs(value) do
    count = count + 1
    if count > MAX_TABLE_KEYS then
      return nil, format("table has more than %d entries", MAX_TABLE_KEYS)
    end
    local keyType = type(key)
    if keyType == "number" then
      if key ~= floor(key) or key < 1 then
        return nil, "array index must be a positive integer"
      end
      if key > highest then
        highest = key
      end
    elseif keyType == "string" then
      hasStringKey = true
    else
      return nil, format("table key of type %s cannot be encoded", keyType)
    end
  end
  if hasStringKey and (highest > 0 or marked) then
    return nil, "table mixes string and array keys"
  end
  if count ~= highest then
    -- A hole would be silently truncated by `#`, so the array is refused. This
    -- also catches an empty unmarked table, which falls through to "object".
    if highest > 0 or marked then
      return nil, "array table has holes"
    end
    return "object"
  end
  if count == 0 then
    -- An unmarked empty table is an object; Json.array() is how a caller says
    -- otherwise.
    return marked and "array" or "object"
  end
  if hasStringKey then
    return "object"
  end
  return "array"
end

local encodeValue

local function encodeArray(value, depth, seen)
  local pieces = {}
  for index = 1, #value do
    local encoded, err = encodeValue(value[index], depth + 1, seen)
    if encoded == nil then
      return nil, format("[%d]: %s", index, err)
    end
    pieces[index] = encoded
  end
  return "[" .. concat(pieces, ",") .. "]"
end

local function encodeObject(value, depth, seen)
  local keys = {}
  local count = 0
  for key in pairs(value) do
    count = count + 1
    keys[count] = key
  end
  sort(keys)
  local pieces = {}
  for index = 1, count do
    local key = keys[index]
    local encodedKey, keyErr = encodeString(key)
    if encodedKey == nil then
      return nil, format("key %q: %s", key, keyErr)
    end
    local encoded, err = encodeValue(value[key], depth + 1, seen)
    if encoded == nil then
      return nil, format("%s: %s", key, err)
    end
    pieces[index] = encodedKey .. ":" .. encoded
  end
  return "{" .. concat(pieces, ",") .. "}"
end

encodeValue = function(value, depth, seen)
  if depth > MAX_DEPTH then
    return nil, format("nesting deeper than %d levels", MAX_DEPTH)
  end
  local valueType = type(value)
  if value == NULL then
    return "null"
  elseif valueType == "boolean" then
    return value and "true" or "false"
  elseif valueType == "number" then
    return encodeNumber(value)
  elseif valueType == "string" then
    return encodeString(value)
  elseif valueType == "table" then
    if seen[value] then
      return nil, "table contains a cycle"
    end
    seen[value] = true
    local shape, shapeErr = classifyTable(value)
    local encoded, err
    if shape == nil then
      encoded, err = nil, shapeErr
    elseif shape == "array" then
      encoded, err = encodeArray(value, depth, seen)
    else
      encoded, err = encodeObject(value, depth, seen)
    end
    seen[value] = nil
    return encoded, err
  elseif valueType == "nil" then
    return nil, "nil cannot be encoded; use PZAgent.Json.null for an explicit null"
  end
  return nil, format("values of type %s cannot be encoded", valueType)
end

--- Encode `value` as a JSON document.
--- Returns the text, or nil plus a message describing what was unrepresentable.
function Json.encode(value)
  return encodeValue(value, 1, {})
end

-- ---------------------------------------------------------------------------
-- decoding
-- ---------------------------------------------------------------------------

local WHITESPACE = { [0x20] = true, [0x09] = true, [0x0A] = true, [0x0D] = true }

local function skipWhitespace(text, index)
  local length = #text
  while index <= length and WHITESPACE[byte(text, index)] do
    index = index + 1
  end
  return index
end

--- Encode a code point as UTF-8 bytes, for \u escapes seen while decoding.
local function utf8Encode(codepoint)
  if codepoint < 0x80 then
    return char(codepoint)
  elseif codepoint < 0x800 then
    return char(0xC0 + floor(codepoint / 0x40), 0x80 + (codepoint % 0x40))
  elseif codepoint < 0x10000 then
    return char(
      0xE0 + floor(codepoint / 0x1000),
      0x80 + (floor(codepoint / 0x40) % 0x40),
      0x80 + (codepoint % 0x40)
    )
  end
  return char(
    0xF0 + floor(codepoint / 0x40000),
    0x80 + (floor(codepoint / 0x1000) % 0x40),
    0x80 + (floor(codepoint / 0x40) % 0x40),
    0x80 + (codepoint % 0x40)
  )
end

local function readHex4(text, index)
  local digits = sub(text, index, index + 3)
  if #digits < 4 or digits:match("^%x%x%x%x$") == nil then
    return nil, format("malformed \\u escape at offset %d", index)
  end
  return tonumber(digits, 16), index + 4
end

local STRING_ESCAPES = {
  ["\""] = "\"",
  ["\\"] = "\\",
  ["/"] = "/",
  ["b"] = "\b",
  ["f"] = "\f",
  ["n"] = "\n",
  ["r"] = "\r",
  ["t"] = "\t",
}

local function decodeString(text, index)
  -- index points at the opening quote.
  index = index + 1
  local pieces = {}
  local count = 0
  local length = #text
  while true do
    if index > length then
      return nil, "unterminated string"
    end
    local code = byte(text, index)
    if code == 0x22 then
      return concat(pieces), index + 1
    elseif code < 0x20 then
      return nil, format("unescaped control character 0x%02X in string at offset %d", code, index)
    elseif code == 0x5C then
      local escape = sub(text, index + 1, index + 1)
      local simple = STRING_ESCAPES[escape]
      if simple then
        count = count + 1
        pieces[count] = simple
        index = index + 2
      elseif escape == "u" then
        local codepoint, nextIndex = readHex4(text, index + 2)
        if codepoint == nil then
          return nil, nextIndex
        end
        index = nextIndex
        if codepoint >= 0xD800 and codepoint <= 0xDBFF then
          if sub(text, index, index + 1) ~= "\\u" then
            return nil, format("lone high surrogate at offset %d", index)
          end
          local low, afterLow = readHex4(text, index + 2)
          if low == nil then
            return nil, afterLow
          end
          if low < 0xDC00 or low > 0xDFFF then
            return nil, format("high surrogate not followed by a low one at offset %d", index)
          end
          codepoint = 0x10000 + (codepoint - 0xD800) * 0x400 + (low - 0xDC00)
          index = afterLow
        elseif codepoint >= 0xDC00 and codepoint <= 0xDFFF then
          return nil, format("lone low surrogate at offset %d", index)
        end
        count = count + 1
        pieces[count] = utf8Encode(codepoint)
      else
        return nil, format("unknown escape \\%s at offset %d", escape, index + 1)
      end
    else
      count = count + 1
      pieces[count] = char(code)
      index = index + 1
    end
  end
end

local DIGITS = {}
for digit = 0x30, 0x39 do
  DIGITS[digit] = true
end

--- Parse a JSON number by the grammar, so "01", "+1", ".5", "1." and "1e" are
--- all refused rather than silently accepted by tonumber.
local function decodeNumber(text, index)
  local start = index
  local length = #text
  if byte(text, index) == 0x2D then
    index = index + 1
  end
  local intStart = index
  if byte(text, index) == 0x30 then
    index = index + 1
  else
    while index <= length and DIGITS[byte(text, index)] do
      index = index + 1
    end
  end
  if index == intStart then
    return nil, format("number has no integer part at offset %d", start)
  end
  if byte(text, index) == 0x2E then
    index = index + 1
    local fracStart = index
    while index <= length and DIGITS[byte(text, index)] do
      index = index + 1
    end
    if index == fracStart then
      return nil, format("number has no digits after the decimal point at offset %d", start)
    end
  end
  local exponent = byte(text, index)
  if exponent == 0x65 or exponent == 0x45 then
    index = index + 1
    local sign = byte(text, index)
    if sign == 0x2B or sign == 0x2D then
      index = index + 1
    end
    local expStart = index
    while index <= length and DIGITS[byte(text, index)] do
      index = index + 1
    end
    if index == expStart then
      return nil, format("number has no digits in its exponent at offset %d", start)
    end
  end
  local literal = sub(text, start, index - 1)
  local value = tonumber(literal)
  if value == nil then
    return nil, format("unparsable number %q at offset %d", literal, start)
  end
  return value, index
end

local decodeValue

local function decodeArray(text, index, depth)
  index = skipWhitespace(text, index + 1)
  local result = Json.array({})
  if sub(text, index, index) == "]" then
    return result, index + 1
  end
  local count = 0
  while true do
    local value, nextIndex = decodeValue(text, index, depth + 1)
    if value == nil then
      return nil, nextIndex
    end
    count = count + 1
    result[count] = value
    index = skipWhitespace(text, nextIndex)
    local delimiter = sub(text, index, index)
    if delimiter == "," then
      index = skipWhitespace(text, index + 1)
    elseif delimiter == "]" then
      return result, index + 1
    else
      return nil, format("expected ',' or ']' at offset %d", index)
    end
  end
end

local function decodeObject(text, index, depth)
  index = skipWhitespace(text, index + 1)
  local result = {}
  if sub(text, index, index) == "}" then
    return result, index + 1
  end
  while true do
    if sub(text, index, index) ~= "\"" then
      return nil, format("expected a string key at offset %d", index)
    end
    local key, afterKey = decodeString(text, index)
    if key == nil then
      return nil, afterKey
    end
    if result[key] ~= nil then
      -- Duplicate keys let a producer show one value to a validator and another
      -- to the consumer, depending on which wins. Refuse the document.
      return nil, format("duplicate object key %q", key)
    end
    index = skipWhitespace(text, afterKey)
    if sub(text, index, index) ~= ":" then
      return nil, format("expected ':' after object key at offset %d", index)
    end
    local value, afterValue = decodeValue(text, skipWhitespace(text, index + 1), depth + 1)
    if value == nil then
      return nil, afterValue
    end
    result[key] = value
    index = skipWhitespace(text, afterValue)
    local delimiter = sub(text, index, index)
    if delimiter == "," then
      index = skipWhitespace(text, index + 1)
    elseif delimiter == "}" then
      return result, index + 1
    else
      return nil, format("expected ',' or '}' at offset %d", index)
    end
  end
end

decodeValue = function(text, index, depth)
  if depth > MAX_DEPTH then
    return nil, format("nesting deeper than %d levels", MAX_DEPTH)
  end
  if index > #text then
    return nil, "unexpected end of document"
  end
  local code = byte(text, index)
  if code == 0x7B then
    return decodeObject(text, index, depth)
  elseif code == 0x5B then
    return decodeArray(text, index, depth)
  elseif code == 0x22 then
    return decodeString(text, index)
  elseif code == 0x2D or DIGITS[code] then
    return decodeNumber(text, index)
  elseif sub(text, index, index + 3) == "true" then
    return true, index + 4
  elseif sub(text, index, index + 4) == "false" then
    return false, index + 5
  elseif sub(text, index, index + 3) == "null" then
    return NULL, index + 4
  end
  return nil, format("unexpected character %q at offset %d", sub(text, index, index), index)
end

--- Decode a complete JSON document.
--- Returns the value, or nil plus a message. `false` and `null` decode to
--- `false` and `PZAgent.Json.null`, so a nil return always means failure.
function Json.decode(text)
  if type(text) ~= "string" then
    return nil, format("expected a string to decode, got %s", type(text))
  end
  if #text > MAX_INPUT_BYTES then
    return nil, format("document of %d bytes exceeds the %d byte limit", #text, MAX_INPUT_BYTES)
  end
  local index = skipWhitespace(text, 1)
  local value, nextIndex = decodeValue(text, index, 1)
  if value == nil then
    return nil, nextIndex
  end
  local trailing = skipWhitespace(text, nextIndex)
  if trailing <= #text then
    return nil, format("trailing content at offset %d", trailing)
  end
  return value
end

return Json
