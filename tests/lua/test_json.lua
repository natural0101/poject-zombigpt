-- Codec tests for PZAgent.Json: round trips, escaping, and every branch that
-- must refuse a value rather than emit a document nobody can read back.

local Harness = dofile((arg[0]:match("^(.*)test_json%.lua$") or "") .. "support/harness.lua")
local PZ = Harness.loadModules()
local Json = PZ.Json

local equal, ok, isNil, contains = Harness.equal, Harness.ok, Harness.isNil, Harness.contains

Harness.group("deterministic encoding")
do
  -- Keys are emitted in byte order regardless of the order pairs() yields, so
  -- two encodings of the same state are the same bytes.
  local first = Json.encode({ zulu = 1, alpha = 2, mike = 3 })
  local second = Json.encode({ mike = 3, alpha = 2, zulu = 1 })
  equal(first, '{"alpha":2,"mike":3,"zulu":1}', "object keys sort ascending")
  equal(first, second, "insertion order does not change the encoding")
  equal(Json.encode({}), "{}", "an unmarked empty table is an object")
  equal(Json.encode(Json.array({})), "[]", "a marked empty table is an array")
  equal(Json.encode({ 1, 2, 3 }), "[1,2,3]", "a dense integer-keyed table is an array")
end

Harness.group("numbers")
do
  equal(Json.encode(0), "0", "zero")
  equal(Json.encode(-0.0), "0", "negative zero normalises, so encodings do not differ")
  equal(Json.encode(42), "42", "an integral value has no decimal point")
  equal(Json.encode(-7), "-7", "negative integer")
  equal(Json.encode(1.5), "1.5", "a fraction round-trips")
  equal(tonumber(Json.encode(0.1 + 0.2)), 0.1 + 0.2, "a value with no short form still round-trips")
  isNil(Json.encode(0 / 0), "NaN is refused")
  contains(select(2, Json.encode(0 / 0)), "NaN", "the NaN refusal says so")
  isNil(Json.encode(math.huge), "positive infinity is refused")
  isNil(Json.encode(-math.huge), "negative infinity is refused")
end

Harness.group("string escaping")
do
  equal(Json.encode("plain"), '"plain"', "an ASCII string is emitted as-is")
  equal(Json.encode('he said "hi"'), '"he said \\"hi\\""', "quotes are escaped")
  equal(Json.encode("back\\slash"), '"back\\\\slash"', "backslashes are escaped")
  equal(Json.encode("a\tb\nc\rd"), '"a\\tb\\nc\\rd"', "the named control escapes are used")
  equal(Json.encode("\8\12"), '"\\b\\f"', "backspace and form feed are escaped")
  equal(Json.encode("\1\31"), '"\\u0001\\u001f"', "other control characters become \\u escapes")
  equal(Json.encode("\0stop"), '"\\u0000stop"', "a NUL byte does not truncate the string")

  -- Non-ASCII is escaped rather than passed through, matching the sidecar's
  -- ensure_ascii serialisation, so the file survives a reader that opens it in
  -- the system code page.
  equal(Json.encode("\208\148\208\176"), '"\\u0414\\u0430"', "Cyrillic becomes \\u escapes")
  equal(Json.encode("\226\152\131"), '"\\u2603"', "a BMP symbol becomes one escape")
  equal(Json.encode("\240\159\146\169"), '"\\ud83d\\udca9"', "an astral code point becomes a surrogate pair")

  -- A byte that does not begin valid UTF-8 no longer refuses the string: it
  -- falls back to the Latin-1 reading, because through Kahlua's byte() a Java
  -- string of U+0080..U+00FF characters is indistinguishable from such a byte
  -- string (the live Build 42.20.2 failure). The fallback is per-byte and
  -- deterministic, and it never re-opens the overlong hole -- "\192\175"
  -- becomes À¯, which is not "/".
  equal(Json.encode("\255\254"), '"\\u00ff\\u00fe"', "invalid UTF-8 falls back to Latin-1 per byte")
  equal(Json.encode("\192\175"), '"\\u00c0\\u00af"', "an overlong encoding of '/' never decodes to '/'")
  equal(Json.encode("\237\160\128"), '"\\u00ed\\u00a0\\u0080"', "a UTF-8 encoded surrogate is read as Latin-1 bytes")
  equal(Json.encode("\226\152"), '"\\u00e2\\u0098"', "a truncated sequence falls back to Latin-1")
  equal(Json.encode("caf\233"), '"caf\\u00e9"', "a Latin-1 byte string encodes its accents as \\u00XX")
  equal(Json.encode("\128"), '"\\u0080"', "even an implausible Latin-1 byte encodes deterministically")
end

Harness.group("unrepresentable values")
do
  isNil(Json.encode(print), "a function cannot be encoded")
  isNil(Json.encode(nil), "a bare nil cannot be encoded")
  contains(select(2, Json.encode(nil)), "Json.null", "the nil refusal points at the null sentinel")
  isNil(Json.encode({ [1] = "a", [3] = "c" }), "an array with a hole is refused")
  isNil(Json.encode({ [1] = "a", key = "b" }), "a table mixing array and object keys is refused")
  isNil(Json.encode({ [0] = "zero" }), "a zero index is not an array index")
  isNil(Json.encode({ [1.5] = "half" }), "a fractional key is refused")
  isNil(Json.encode({ [true] = 1 }), "a boolean key is refused")
  isNil(Json.encode(Json.array({ [1] = "a", [3] = "c" })), "a marked array with a hole is still refused")

  local cyclic = {}
  cyclic.self = cyclic
  isNil(Json.encode(cyclic), "a cycle is refused instead of recursing forever")
  contains(select(2, Json.encode(cyclic)), "cycle", "the cycle refusal says so")

  local deep = {}
  local cursor = deep
  for _ = 1, 80 do
    cursor.next = {}
    cursor = cursor.next
  end
  isNil(Json.encode(deep), "nesting past the depth bound is refused")
end

Harness.group("null")
do
  equal(Json.encode(Json.null), "null", "the sentinel encodes as null")
  equal(Json.encode({ a = Json.null }), '{"a":null}', "an explicit null survives as a key")
  equal(Json.decode("null"), Json.null, "null decodes to the sentinel")
  ok(Json.decode('{"a":null}').a == Json.null, "a decoded null is the sentinel, not a missing key")
end

Harness.group("round trip")
do
  local original = {
    schema_version = "1.0",
    seq = 12,
    armed = false,
    ratio = 0.25,
    empty_object = {},
    empty_array = Json.array({}),
    items = { { ref = "item:x", weight = 1.5 }, { ref = "item:y", weight = 2 } },
    text = "\208\148\208\176 \240\159\146\169 \"quoted\"\n",
    nothing = Json.null,
  }
  local encoded = Json.encode(original)
  ok(encoded ~= nil, "the sample document encodes")
  local decoded, err = Json.decode(encoded)
  ok(decoded ~= nil, "the encoding decodes again: " .. tostring(err))
  equal(Json.encode(decoded), encoded, "re-encoding a decoded document is byte-identical")
  equal(decoded.text, original.text, "escaped text comes back byte-identical")
  ok(Json.isArray(decoded.empty_array), "an empty array stays an array through a round trip")
  ok(not Json.isArray(decoded.empty_object), "an empty object stays an object through a round trip")
end

Harness.group("decoder strictness")
do
  isNil(Json.decode("{"), "a truncated object is refused")
  isNil(Json.decode("[1,2"), "a truncated array is refused")
  isNil(Json.decode('"unterminated'), "an unterminated string is refused")
  isNil(Json.decode("[1,2]trailing"), "trailing content is refused")
  isNil(Json.decode("{'a':1}"), "single quotes are not JSON")
  isNil(Json.decode('{"a":1,}'), "a trailing comma is refused")
  isNil(Json.decode('{a:1}'), "an unquoted key is refused")
  isNil(Json.decode("01"), "a leading zero is refused")
  isNil(Json.decode("+1"), "a leading plus is refused")
  isNil(Json.decode(".5"), "a bare fraction is refused")
  isNil(Json.decode("1."), "a trailing decimal point is refused")
  isNil(Json.decode("1e"), "an empty exponent is refused")
  isNil(Json.decode('"raw\1control"'), "a raw control character inside a string is refused")
  isNil(Json.decode('"\\q"'), "an unknown escape is refused")
  isNil(Json.decode('"\\u00"'), "a short \\u escape is refused")
  isNil(Json.decode('"\\ud83d"'), "a lone high surrogate is refused")
  isNil(Json.decode('"\\udca9"'), "a lone low surrogate is refused")
  isNil(Json.decode('"\\ud83dx"'), "a high surrogate not followed by a low one is refused")
  isNil(Json.decode(""), "an empty document is refused")
  isNil(Json.decode(42), "a non-string input is refused")

  -- Duplicate keys let a producer show one value to a validator and another to
  -- the consumer, depending on which one wins.
  isNil(Json.decode('{"a":1,"a":2}'), "a duplicate key is refused")
  contains(select(2, Json.decode('{"a":1,"a":2}')), "duplicate", "the duplicate refusal says so")
end

Harness.group("decoder acceptance")
do
  local value = Json.decode(' \t\n {"a" : [ 1 , 2.5 , true , false ] } \r\n ')
  ok(value ~= nil, "whitespace between tokens is skipped")
  equal(value.a[2], 2.5, "a fraction decodes")
  equal(value.a[3], true, "true decodes")
  equal(value.a[4], false, "false decodes, and false is not a failure signal")
  equal(Json.decode("-0.5e2"), -50.0, "a signed exponent form decodes")
  equal(Json.decode('"\\u0000"'), "\0", "an escaped NUL decodes to a NUL byte")
  equal(Json.decode('"\\/"'), "/", "an escaped solidus decodes")
  equal(Json.decode('"\\ud83d\\udca9"'), "\240\159\146\169", "a surrogate pair decodes to UTF-8")
  equal(Json.decode('"\\u0414"'), "\208\148", "a BMP escape decodes to UTF-8")
end

-- ---------------------------------------------------------------------------
-- Kahlua/Java string units
-- ---------------------------------------------------------------------------
--[[
Under Kahlua a Java string answers string.byte with UTF-16 code units, which
can exceed 0xFF; under lua5.4 a string is bytes and byte() never can. To test
the unit-model paths the module is loaded a SECOND time into an environment
whose string table proxies byte(), so that a registered placeholder string
yields a synthetic unit sequence instead of its bytes. The placeholder has the
same length as the unit sequence, which is all the encoder observes besides
byte(). The global module loaded by the harness is untouched -- the proxied
build lives in its own PZAgent table -- so the two can be compared directly.
]]

local function loadJsonWithUnits()
  local registry = {}
  local counter = 0
  local proxied = {}
  for name, fn in pairs(string) do
    proxied[name] = fn
  end
  proxied.byte = function(text, from, to)
    local units = registry[text]
    if units == nil then
      return string.byte(text, from, to)
    end
    from = from or 1
    -- The harness runs under lua5.4, which spells 5.1's unpack as table.unpack.
    return table.unpack(units, from, to or from) -- luacheck: ignore 143
  end
  local env = {
    PZAgent = nil,
    math = math,
    table = table,
    string = proxied,
    setmetatable = setmetatable,
    getmetatable = getmetatable,
    type = type,
    pairs = pairs,
    tonumber = tonumber,
  }
  local path = Harness.root .. "pz-mod/42/media/lua/shared/PZAgent/Json.lua"
  local file = assert(io.open(path, "rb"))
  local source = file:read("a")
  file:close()
  local chunk = assert(load(source, "@" .. path, "t", env))
  chunk()
  --- Register a synthetic Java string. Each registration uses a distinct fill
  --- byte, so two unit sequences of the same length get distinct placeholders.
  local function javaString(units)
    counter = counter + 1
    local placeholder = string.rep(string.char(counter), #units)
    registry[placeholder] = units
    return placeholder
  end
  return env.PZAgent.Json, javaString
end

Harness.group("Kahlua/Java string units")
do
  local JJson, javaString = loadJsonWithUnits()

  -- Regression: byte strings must encode byte-identically in both builds.
  local utf8Privet = "\208\191\209\128\208\184\208\178\208\181\209\130"
  local escapedPrivet = '"\\u043f\\u0440\\u0438\\u0432\\u0435\\u0442"'
  equal(Json.encode(utf8Privet), escapedPrivet, "UTF-8 privet escapes as before")
  equal(JJson.encode(utf8Privet), escapedPrivet, "the proxied build encodes the same byte string identically")
  equal(JJson.encode("plain"), '"plain"', "pure ASCII is unchanged under the proxied build")

  -- THE key equivalence: the same text spelled as UTF-16 units must encode to
  -- the same bytes as its UTF-8 spelling, or snapshots would differ by which
  -- side of the Kahlua boundary a string came from.
  local unitsPrivet = javaString({ 0x043F, 0x0440, 0x0438, 0x0432, 0x0435, 0x0442 })
  equal(JJson.encode(unitsPrivet), escapedPrivet, "UTF-16 units encode identically to the UTF-8 spelling")

  local astral = javaString({ 0xD83D, 0xDE00 })
  equal(JJson.encode(astral), '"\\ud83d\\ude00"', "a surrogate pair combines to the astral escape")

  -- A unit string never gets the UTF-8 reading of 0x80..0xFF: one unit above
  -- 0xFF commits the whole string to the unit model.
  local mixed = javaString({ 0x043F, 0xE9 })
  equal(JJson.encode(mixed), '"\\u043f\\u00e9"', "classification is per-string, not per-character")

  -- A Latin-1 Java string has no unit above 0xFF, so through byte() it is a
  -- byte string; 0xE9 alone is invalid UTF-8, so only the fallback saves it.
  local latin = javaString({ 0x63, 0x61, 0x66, 0xE9 })
  equal(JJson.encode(latin), '"caf\\u00e9"', "a Latin-1 Java string rides the byte-model fallback")

  local encoded, err = JJson.encode(javaString({ 0x41, 0xD83D }))
  isNil(encoded, "a lone high surrogate is refused")
  contains(err, "surrogate", "the high-surrogate refusal says so")
  contains(err, "offset 2", "the high-surrogate refusal names the offset")

  encoded, err = JJson.encode(javaString({ 0xD83D, 0x0041 }))
  isNil(encoded, "a high surrogate followed by a non-low unit is refused")
  contains(err, "surrogate", "the mispaired refusal says so")

  encoded, err = JJson.encode(javaString({ 0xDC00 }))
  isNil(encoded, "a lone low surrogate is refused")
  contains(err, "low surrogate", "the low-surrogate refusal says so")
  contains(err, "offset 1", "the low-surrogate refusal names the offset")

  encoded, err = JJson.encode(javaString({ 0x41, 0x110000 }))
  isNil(encoded, "a unit above 0xFFFF is refused")
  contains(err, "0x110000", "the impossible-unit refusal names the value")
  contains(err, "offset 2", "the impossible-unit refusal names the offset")

  -- One bad string must fail the containing document as nil, message, with
  -- the key path the encoder already reports.
  encoded, err = JJson.encode({ name = javaString({ 0xD83D }) })
  isNil(encoded, "one unencodable string fails the whole document")
  contains(err, "name", "the document-level message includes the key path")
  contains(err, "surrogate", "the document-level message keeps the cause")
end

Harness.finish("test_json")
