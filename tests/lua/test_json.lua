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

  isNil(Json.encode("\255\254"), "invalid UTF-8 is refused rather than mangled")
  isNil(Json.encode("\192\175"), "an overlong encoding of '/' is refused")
  isNil(Json.encode("\237\160\128"), "a UTF-8 encoded surrogate is refused")
  isNil(Json.encode("\226\152"), "a truncated sequence is refused")
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

Harness.finish("test_json")
