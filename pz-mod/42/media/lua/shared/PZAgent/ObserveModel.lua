--[[
PZAgent.ObserveModel -- pure construction of the observation document.

Invariant: every value in the document either came from the caller or is a
bound this module itself enforces. Nothing is inferred from something adjacent,
and nothing that was not read is given a plausible default -- an absent stat is
absent, because the sidecar's accessors treat a missing need as satisfied and a
fabricated 0.0 is indistinguishable from a measured one.

The document must satisfy schemas/observation.schema.json exactly. Two
consequences shape this file:

* **Where the observer's own limits are reported.** Every object in that schema
  is `additionalProperties: false` except item and nearby entries, so there is
  no top-level place to say "the walk stopped early". `player.stats` is the one
  open scalar map that also survives Observation.from_dict, so the limits go
  there under the LIMIT_PREFIX namespace, which no game stat can collide with.
  They are emitted only when they are true: a walked section with no limit key
  is a complete section, which is what lets a planner tell "no zombies" from
  "we stopped counting".
* **Which container kinds exist.** The protocol names six; PZAgent.Refs can
  build references for four. The mod emits only those four, because a reference
  this file formatted by hand is a reference the sidecar may parse differently.

Ordering is canonical, not incidental: PZAgent.Json sorts object keys, and the
arrays here are sorted by reference (nearby entries by distance first, so a cap
keeps the near threats rather than the ones the engine happened to list first).
The same world therefore produces the same bytes, which is what makes the
snapshot diffable.
]]

PZAgent = PZAgent or {}

local ObserveModel = {}
PZAgent.ObserveModel = ObserveModel

local Json = nil
local Protocol = nil
local Refs = nil

--- Resolved late so load order between shared modules does not matter.
local function json()
  Json = Json or PZAgent.Json
  return Json
end

local function protocol()
  Protocol = Protocol or PZAgent.Protocol
  return Protocol
end

local function refs()
  Refs = Refs or PZAgent.Refs
  return Refs
end

-- ---------------------------------------------------------------------------
-- bounds
--
-- Every one of these caps a loop that walks something the player controls the
-- size of. A hoarder's base has thousands of items; the observation tick runs
-- on the game thread.
-- ---------------------------------------------------------------------------

ObserveModel.MAX_CONTAINERS = 64
ObserveModel.MAX_ITEMS = 512
ObserveModel.MAX_ITEMS_PER_CONTAINER = 128
ObserveModel.MAX_DEPTH = 4
ObserveModel.MAX_OBJECTS = 64
ObserveModel.MAX_ZOMBIES = 64
ObserveModel.MAX_MOODLES = 24
ObserveModel.MAX_WOUNDS = 24
ObserveModel.MAX_STATS = 48
ObserveModel.MAX_TAGS = 12
ObserveModel.MAX_SEMANTICS = 8
ObserveModel.MAX_DOMAIN_FIELDS = 16
ObserveModel.MAX_TEXT_BYTES = 160
ObserveModel.MAX_HASH_INPUT_BYTES = 512

--- Namespace for the observer's own limit reporting inside `player.stats`.
--- The dot cannot appear in a Java accessor name, so no game stat collides.
ObserveModel.LIMIT_PREFIX = "observe."

--- Reported when the game exposed no readable save identity. A literal, not a
--- hash: sixteen hex characters mean "this save", and inventing them for a save
--- nobody identified would make two different saves compare equal.
ObserveModel.UNKNOWN_SAVE_ID = "unknown"

--- The container kinds PZAgent.Refs can mint a reference for. The protocol also
--- names `vehicle` and `floor`; neither has a builder, so neither is emitted.
ObserveModel.CONTAINER_KIND = {
  PLAYER_MAIN = "player_main",
  CARRIED = "carried",
  WORN = "worn",
  WORLD = "world",
}

--- Semantics implied by a world object's kind, added to whatever the reader
--- observed. Kept here rather than in the engine-facing reader so the mapping
--- is testable and so the vocabulary the planner sees is decided in one place.
ObserveModel.BASE_SEMANTICS = {
  door = { "door", "obstacle" },
  window = { "window", "obstacle" },
  tree = { "tree", "obstacle" },
  stairs = { "stairs", "traversal" },
  container = { "container" },
  water = { "water_source" },
  -- A corpse with loot is a container to inspect, even when the reader that
  -- emitted it could not say so itself.
  corpse = { "container" },
}

local floor = math.floor
local format = string.format
local sort = table.sort
local byte = string.byte

-- ---------------------------------------------------------------------------
-- scalars
-- ---------------------------------------------------------------------------

local function isNumber(value)
  -- NaN and both infinities are excluded: PZAgent.Json refuses them, so a
  -- single one of them anywhere would cost the whole snapshot.
  return type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge
end

--- A finite number, or nil when the caller did not read one.
function ObserveModel.number(value)
  if not isNumber(value) then
    return nil
  end
  return value
end

--- A finite number confined to [low, high], or nil.
function ObserveModel.clamp(value, low, high)
  if not isNumber(value) then
    return nil
  end
  if value < low then
    return low
  end
  if value > high then
    return high
  end
  return value
end

--- A whole number, rounded towards negative infinity, or nil.
function ObserveModel.integer(value)
  if not isNumber(value) then
    return nil
  end
  return floor(value)
end

--- True when `value` is a whole number, which is what a coordinate must be.
local function isInteger(value)
  return isNumber(value) and value == floor(value)
end

local function isContinuation(code)
  return code ~= nil and code >= 0x80 and code <= 0xBF
end

--- Cut `text` to at most `limit` bytes without splitting a UTF-8 sequence.
---
--- Splitting one would leave a string PZAgent.Json refuses to encode, which
--- would drop the entire observation over one long item name.
local function truncateUtf8(text, limit)
  if #text <= limit then
    return text
  end
  local cut = limit
  while cut > 0 and isContinuation(byte(text, cut + 1)) do
    cut = cut - 1
  end
  if cut > 0 and byte(text, cut) >= 0xC0 then
    -- A lead byte whose continuations fell outside the cut.
    cut = cut - 1
  end
  return text:sub(1, cut)
end

--- Game-authored display text, carried verbatim within a byte bound.
---
--- Deliberately not sanitised: §7.11 makes in-game text untrusted *data*, and
--- the layer that hands text to a model quarantines and redacts it there. A mod
--- that mangled the name here would only hide from the player what the item is
--- really called, while removing nothing -- the string is inert either way.
function ObserveModel.text(value)
  if type(value) ~= "string" or #value == 0 then
    return nil
  end
  return truncateUtf8(value, ObserveModel.MAX_TEXT_BYTES)
end

--- An identifier-shaped string, or nil. Uses the reference alphabet, so a
--- token can never smuggle a delimiter into a semantics or tag list.
function ObserveModel.token(value)
  if type(value) ~= "string" or #value == 0 or #value > 64 then
    return nil
  end
  if not refs().isSafeSegment(value) then
    return nil
  end
  return value
end

--- A room or building identifier as a token, or nil.
---
--- One normalisation and only one: every space becomes an underscore, applied
--- identically to the player's reading and to each object's, so the equality a
--- scope decision runs on survives the rename -- "main hall" compares equal on
--- both sides of "is this container in my room", as "main_hall". The
--- substitution can in principle collide a literal "main_hall" with
--- "main hall"; that is accepted and stated here rather than hidden behind a
--- cleverer encoding.
---
--- Any other byte outside the reference alphabet drops the field entirely,
--- which is the same absence outdoors produces -- on purpose. Mapping unsafe
--- bytes to a filler instead would collapse distinct rooms into one token
--- (every non-ASCII name would become a run of underscores), and two rooms
--- behind one name is a lie a criterion like "every reachable container in
--- this room was inspected" cannot survive.
function ObserveModel.place(value)
  if type(value) ~= "string" or #value == 0 then
    return nil
  end
  return ObserveModel.token((value:gsub(" ", "_")))
end

--- A runtime id normalised to a reference segment, or nil.
---
--- A negative id is refused rather than formatted. Java answers -1 for "there
--- is no id here" -- `getOnlineID` does exactly that outside multiplayer -- and
--- "-1" is a legal reference segment, so accepting it would mint one reference
--- shared by every zombie in the horde. Two objects behind one reference is the
--- failure the whole scheme exists to prevent, and it fails silently.
function ObserveModel.runtimeId(value)
  if isInteger(value) then
    if value < 0 then
      return nil
    end
    return format("%.0f", value)
  end
  local token = ObserveModel.token(value)
  if token == nil or token:sub(1, 1) == "-" then
    return nil
  end
  return token
end

-- ---------------------------------------------------------------------------
-- save identity
-- ---------------------------------------------------------------------------

local FNV_PRIME = 16777619
local FNV_OFFSET_A = 2166136261
local FNV_OFFSET_B = 811130621
local UINT32 = 4294967296

--- XOR of two bytes, computed arithmetically because Kahlua is Lua 5.1 and has
--- no bitwise operators.
local function xor8(left, right)
  local result = 0
  local bit = 1
  for _ = 1, 8 do
    local leftBit = left % 2
    local rightBit = right % 2
    if leftBit ~= rightBit then
      result = result + bit
    end
    left = (left - leftBit) / 2
    right = (right - rightBit) / 2
    bit = bit * 2
  end
  return result
end

--- FNV-1a over `text`, in 32 bits.
---
--- The multiply is done in halves: 2^32 * 16777619 is past the range in which a
--- double counts by ones, so the single-expression form would silently lose the
--- low bits and stop being a function of the whole input.
local function fnv1a(text, offset)
  local hash = offset
  for index = 1, #text do
    local low = hash % 256
    hash = hash - low + xor8(low, byte(text, index))
    local lowHalf = hash % 65536
    local highHalf = (hash - lowHalf) / 65536
    hash = (lowHalf * FNV_PRIME + ((highHalf * FNV_PRIME) % 65536) * 65536) % UINT32
  end
  return hash
end

--- A stable, non-reversing handle for one save (§3.13, PRIVACY.md).
---
--- The game identifies a save by a directory fragment that can embed the
--- profile name, and that must not cross into the observation. This is not a
--- cryptographic digest -- Kahlua has no sha256 and the mod may not load one --
--- so it is chosen for what the protocol actually needs: the same save hashes
--- to the same value all session long, and a different save to a different one,
--- which is what makes SAVE_CHANGED detectable without carrying the path.
function ObserveModel.saveId(raw)
  if type(raw) ~= "string" or #raw == 0 then
    return nil
  end
  local bounded = raw:sub(1, ObserveModel.MAX_HASH_INPUT_BYTES)
  return format("%08x%08x", fnv1a(bounded, FNV_OFFSET_A), fnv1a(bounded, FNV_OFFSET_B))
end

--- The in-game clock as a sortable string, or nil when a component is missing.
---
--- Every component must be present: a timestamp assembled from three read
--- fields and two guessed ones is worse than no timestamp, because it looks
--- exactly as authoritative as a complete one.
function ObserveModel.worldTime(parts)
  if type(parts) ~= "table" then
    return nil
  end
  local ordered = { parts.year, parts.month, parts.day, parts.hour, parts.minute }
  for index = 1, 5 do
    if not isInteger(ordered[index]) or ordered[index] < 0 then
      return nil
    end
  end
  return format(
    "%04.0f-%02.0f-%02.0fT%02.0f:%02.0f",
    ordered[1],
    ordered[2],
    ordered[3],
    ordered[4],
    ordered[5]
  )
end

-- ---------------------------------------------------------------------------
-- limit reporting
-- ---------------------------------------------------------------------------

--- A fresh counter set for one build.
local function newLimits()
  return {
    containers_truncated = false,
    containers_omitted = 0,
    items_truncated = false,
    items_omitted = 0,
    stats_truncated = false,
    stats_omitted = 0,
    objects_truncated = false,
    objects_omitted = 0,
    zombies_truncated = false,
    zombies_omitted = 0,
    wounds_truncated = false,
    wounds_omitted = 0,
    wounds_unknown = false,
    moodles_truncated = false,
    moodles_omitted = 0,
    chasing_unknown = false,
    visible_unknown = false,
    paused_unknown = false,
    speed_unknown = false,
  }
end

local LIMIT_KEYS = {
  "containers_truncated",
  "containers_omitted",
  "items_truncated",
  "items_omitted",
  "stats_truncated",
  "stats_omitted",
  "objects_truncated",
  "objects_omitted",
  "zombies_truncated",
  "zombies_omitted",
  "wounds_truncated",
  "wounds_omitted",
  "wounds_unknown",
  "moodles_truncated",
  "moodles_omitted",
  "chasing_unknown",
  "visible_unknown",
  "paused_unknown",
  "speed_unknown",
}

--- Fold the limit counters into `stats`, emitting only what is true.
---
--- Silence therefore means "nothing was dropped", never "nobody counted": a
--- section that was not walked at all is absent from the document entirely.
function ObserveModel.applyLimits(stats, limits)
  for index = 1, #LIMIT_KEYS do
    local key = LIMIT_KEYS[index]
    local value = limits[key]
    if value == true or (type(value) == "number" and value > 0) then
      stats[ObserveModel.LIMIT_PREFIX .. key] = value
    end
  end
  return stats
end

-- ---------------------------------------------------------------------------
-- player
-- ---------------------------------------------------------------------------

--- Stat names whose value the schema confines to 0..1.
local UNIT_STATS = {
  health = true,
  endurance = true,
}

--- Stat names the schema requires to be non-negative.
local NON_NEGATIVE_STATS = {
  hunger = true,
  thirst = true,
  fatigue = true,
}

--- Shape the stat map: drop what was not read, clamp what the schema bounds.
---
--- A name the caller supplies with a nil value never appears, which is the
--- whole point -- `PlayerState.hunger` reads an absent hunger as 0.0 ("not
--- hungry"), so an unread stat must not arrive as a number at all.
---
--- Bounded and ordered like every other map here. `player.stats` is the one
--- open scalar map in the schema, so it is the one place an unbounded caller
--- table would reach the exchange directory unchecked; the cap is taken in
--- sorted name order so the same reading always produces the same document.
function ObserveModel.stats(raw, limits)
  local out = {}
  if type(raw) ~= "table" then
    return out
  end
  local note = type(limits) == "table" and limits or nil
  local prefix = ObserveModel.LIMIT_PREFIX
  local names = {}
  local count = 0
  local dropped = 0
  for name in pairs(raw) do
    -- A game stat may not land in the observer's own namespace: the limit keys
    -- are how the sidecar learns the walk was cut short, and a stat that could
    -- overwrite one could hide exactly that.
    if ObserveModel.token(name) ~= nil and name:sub(1, #prefix) ~= prefix then
      count = count + 1
      names[count] = name
    else
      dropped = dropped + 1
    end
  end
  sort(names)
  local kept = 0
  for index = 1, count do
    local name = names[index]
    local value = raw[name]
    local shaped
    if type(value) == "boolean" then
      shaped = value
    elseif UNIT_STATS[name] then
      shaped = ObserveModel.clamp(value, 0, 1)
    elseif NON_NEGATIVE_STATS[name] then
      shaped = ObserveModel.clamp(value, 0, math.huge)
    else
      shaped = ObserveModel.number(value)
    end
    if shaped == nil then
      dropped = dropped + 1
    elseif kept >= ObserveModel.MAX_STATS then
      dropped = dropped + 1
      if note ~= nil then
        note.stats_truncated = true
      end
    else
      kept = kept + 1
      out[name] = shaped
    end
  end
  if note ~= nil and dropped > 0 then
    note.stats_omitted = note.stats_omitted + dropped
  end
  return out
end

--- Moodle levels, bounded and non-negative.
function ObserveModel.moodles(raw, limits)
  local out = {}
  if type(raw) ~= "table" then
    return out
  end
  local names = {}
  local count = 0
  for name in pairs(raw) do
    count = count + 1
    names[count] = name
  end
  sort(names)
  local kept = 0
  for index = 1, count do
    local name = names[index]
    local level = ObserveModel.integer(raw[name])
    if ObserveModel.token(name) == nil or level == nil or level < 0 then
      limits.moodles_omitted = limits.moodles_omitted + 1
    elseif kept >= ObserveModel.MAX_MOODLES then
      limits.moodles_truncated = true
      limits.moodles_omitted = limits.moodles_omitted + 1
    else
      kept = kept + 1
      out[name] = level
    end
  end
  return out
end

--- Injury kinds, most consequential first. The first one the reader observed
--- names the wound: a bitten limb that is also scratched is a bite.
local WOUND_KINDS = {
  { flag = "bitten", kind = "bite" },
  { flag = "deep_wounded", kind = "deep_wound" },
  { flag = "fractured", kind = "fracture" },
  { flag = "burnt", kind = "burn" },
  { flag = "cut", kind = "cut" },
  { flag = "scratched", kind = "scratch" },
  { flag = "bleeding", kind = "bleeding" },
}

--- A wound reference.
---
--- PZAgent.Refs has no wound builder and neither does pz_agent_core.protocol.refs,
--- so there is no cross-language format to match; the only consumer is the diff,
--- which keys array entries by `ref`. It is still assembled from the Refs kind
--- constant, the Refs separator and the Refs segment validator, so
--- `Refs.kindOf` and `Refs.belongsToSession` classify it like any other
--- reference and a session's wounds cannot be mistaken for the next session's.
function ObserveModel.woundRef(sessionId, part)
  local R = refs()
  if not R.isSessionSegment(sessionId) then
    return nil, "invalid session segment"
  end
  local segment = ObserveModel.token(part)
  if segment == nil then
    return nil, "invalid wound part segment"
  end
  return R.KIND.WOUND .. R.SEPARATOR .. sessionId .. R.SEPARATOR .. segment
end

--- One wound, or nil when the part reported no injury at all.
---
--- `bleeding` is first-class rather than one kind among many: the reflex guard
--- keys on it, so a bitten limb that is also bleeding must still say so.
function ObserveModel.wound(sessionId, descriptor)
  if type(descriptor) ~= "table" then
    return nil, "wound descriptor must be a table"
  end
  local ref, refError = ObserveModel.woundRef(sessionId, descriptor.part)
  if ref == nil then
    return nil, refError
  end
  local kind = nil
  for index = 1, #WOUND_KINDS do
    if descriptor[WOUND_KINDS[index].flag] == true then
      kind = WOUND_KINDS[index].kind
      break
    end
  end
  local severity = ObserveModel.clamp(descriptor.severity, 0, 1)
  if kind == nil then
    if severity == nil or severity <= 0 then
      return nil
    end
    -- Health below full with no readable cause is still an injury; naming it
    -- "damage" reports what was seen instead of picking a plausible cause.
    kind = "damage"
  end
  return {
    ref = ref,
    kind = kind,
    severity = severity or 0,
    bleeding = descriptor.bleeding == true,
  }
end

--- The wound list, bounded and ordered by reference.
function ObserveModel.wounds(sessionId, descriptors, limits)
  local out = {}
  local count = 0
  if type(descriptors) ~= "table" then
    return json().array(out)
  end
  for index = 1, #descriptors do
    if count >= ObserveModel.MAX_WOUNDS then
      limits.wounds_truncated = true
      limits.wounds_omitted = limits.wounds_omitted + 1
    else
      local wound = ObserveModel.wound(sessionId, descriptors[index])
      if wound ~= nil then
        count = count + 1
        out[count] = wound
      end
    end
  end
  sort(out, function(left, right)
    return left.ref < right.ref
  end)
  return json().array(out)
end

--- The position block. Returns nil when there is no position to report.
function ObserveModel.position(fields, withDirection)
  if type(fields) ~= "table" then
    return nil, "position fields must be a table"
  end
  local x = ObserveModel.number(fields.x)
  local y = ObserveModel.number(fields.y)
  local z = ObserveModel.integer(fields.z)
  if x == nil or y == nil or z == nil then
    return nil, "position is incomplete"
  end
  local position = { x = x, y = y, z = z }
  if withDirection then
    local direction = ObserveModel.token(fields.direction)
    if direction ~= nil then
      position.direction = direction
    end
  end
  return position
end

--- Eight compass points from a heading in degrees, or nil.
---
--- The mapping is here rather than in the reader so that "what does 200 degrees
--- mean" has one answer with a test, instead of one answer per call site.
local COMPASS = { "N", "NE", "E", "SE", "S", "SW", "W", "NW" }

function ObserveModel.direction(angleDegrees)
  if not isNumber(angleDegrees) then
    return nil
  end
  local normalised = angleDegrees % 360
  local sector = floor((normalised + 22.5) / 45) % 8
  return COMPASS[sector + 1]
end

--- The player block.
function ObserveModel.player(sessionId, fields, limits)
  if type(fields) ~= "table" then
    return nil, "player fields must be a table"
  end
  local position, positionError = ObserveModel.position(fields.position, true)
  if position == nil then
    return nil, positionError
  end
  local player = {
    present = fields.present ~= false,
    alive = fields.alive == true,
    position = position,
    stats = ObserveModel.stats(fields.stats, limits),
    moodles = ObserveModel.moodles(fields.moodles, limits),
  }
  -- Room and building are optional in the schema precisely so an unread value
  -- can stay unread: outdoors and "no room reader on this build" are the same
  -- absence, and a name the token rules cannot carry joins them -- see place.
  local room = ObserveModel.place(fields.room)
  if room ~= nil then
    player.room = room
  end
  local building = ObserveModel.place(fields.building)
  if building ~= nil then
    player.building = building
  end
  -- Tri-state, like `chasing` on a zombie and for the same reason. A body the
  -- reader walked publishes its list even when the list is empty, because an
  -- empty list is the observation "nothing is bleeding"; a body the reader
  -- could not walk publishes no list at all and says so through the limit
  -- counter, because the sidecar's PlayerState.wounds defaults to the empty
  -- list and a completion criterion of bleeding_observed == 0 would otherwise
  -- be met by a character nobody examined.
  if type(fields.wounds) == "table" then
    player.wounds = ObserveModel.wounds(sessionId, fields.wounds, limits)
  else
    limits.wounds_unknown = true
  end
  return player
end

-- ---------------------------------------------------------------------------
-- inventory
-- ---------------------------------------------------------------------------

--- Build the reference for one container node, always through PZAgent.Refs.
local function containerRefFor(sessionId, node)
  local R = refs()
  local kinds = ObserveModel.CONTAINER_KIND
  local kind = node.kind
  if kind == kinds.PLAYER_MAIN then
    return R.playerMainContainer(sessionId)
  elseif kind == kinds.WORN then
    return R.wornContainer(sessionId, ObserveModel.token(node.slot), ObserveModel.runtimeId(node.runtime_id))
  elseif kind == kinds.CARRIED then
    return R.carriedContainer(sessionId, ObserveModel.runtimeId(node.runtime_id))
  elseif kind == kinds.WORLD then
    return R.worldContainer(
      sessionId,
      ObserveModel.integer(node.x),
      ObserveModel.integer(node.y),
      ObserveModel.integer(node.z),
      ObserveModel.integer(node.object_index),
      ObserveModel.integer(node.container_index)
    )
  end
  return nil, format("no reference builder for container kind %q", tostring(kind))
end

ObserveModel.containerRefFor = containerRefFor

--- Copy a domain payload (food, literature, fluid) as scalars only.
---
--- Whitelisting by *type* rather than by name keeps a future game field
--- reaching the sidecar, while a nested table -- which is where an engine handle
--- or a block of free text would appear -- never does.
function ObserveModel.domain(raw)
  if type(raw) ~= "table" then
    return nil
  end
  local names = {}
  local count = 0
  for name in pairs(raw) do
    count = count + 1
    names[count] = name
  end
  sort(names)
  local out = {}
  local kept = 0
  for index = 1, count do
    if kept >= ObserveModel.MAX_DOMAIN_FIELDS then
      break
    end
    local name = names[index]
    local value = raw[name]
    local shaped = nil
    if type(value) == "boolean" then
      shaped = value
    elseif type(value) == "number" then
      shaped = ObserveModel.number(value)
    elseif type(value) == "string" then
      shaped = ObserveModel.token(value)
    end
    if ObserveModel.token(name) ~= nil and shaped ~= nil then
      kept = kept + 1
      out[name] = shaped
    end
  end
  if kept == 0 then
    return nil
  end
  return out
end

--- Validated, deduplicated, ordered token list.
local function tokenList(values, extra, limit)
  local seen = {}
  local out = {}
  local count = 0
  local function add(value)
    local token = ObserveModel.token(value)
    if token == nil or seen[token] or count >= limit then
      return
    end
    seen[token] = true
    count = count + 1
    out[count] = token
  end
  if type(extra) == "table" then
    for index = 1, #extra do
      add(extra[index])
    end
  end
  if type(values) == "table" then
    for index = 1, #values do
      add(values[index])
    end
  end
  sort(out)
  return json().array(out)
end

ObserveModel.tokenList = tokenList

--- One item, or nil when it is not identifiable enough to act on.
---
--- Every field the schema requires must have been read. An item emitted with a
--- fabricated weight or an empty type looks to the planner exactly like one
--- that was measured, and it is the planner that decides what to carry.
local function buildItem(sessionId, containerRef, containerTail, descriptor)
  if type(descriptor) ~= "table" then
    return nil, "item descriptor must be a table"
  end
  local runtimeId = ObserveModel.runtimeId(descriptor.runtime_id)
  if runtimeId == nil then
    return nil, "item has no usable runtime id"
  end
  local generation = ObserveModel.integer(descriptor.generation) or 0
  if generation < 0 then
    return nil, "item generation is negative"
  end
  local ref, refError = refs().buildItem(sessionId, containerTail, runtimeId, generation)
  if ref == nil then
    return nil, refError
  end
  local fullType = ObserveModel.text(descriptor.full_type)
  local displayName = ObserveModel.text(descriptor.display_name)
  local category = ObserveModel.text(descriptor.category)
  local weight = ObserveModel.clamp(descriptor.weight, 0, math.huge)
  if fullType == nil or displayName == nil or category == nil or weight == nil then
    return nil, "item is missing a field the schema requires"
  end
  local item = {
    ref = ref,
    container_ref = containerRef,
    full_type = fullType,
    display_name = displayName,
    category = category,
    weight = weight,
    favorite = descriptor.favorite == true,
    equipped = descriptor.equipped == true,
  }
  local tags = tokenList(descriptor.tags, nil, ObserveModel.MAX_TAGS)
  if #tags > 0 then
    item.tags = tags
  end
  item.food = ObserveModel.domain(descriptor.food)
  item.literature = ObserveModel.domain(descriptor.literature)
  item.fluid = ObserveModel.domain(descriptor.fluid)
  return item
end

--- Walk one container and everything carried inside it.
local function walkContainer(state, sessionId, node, parentRef, depth)
  if type(node) ~= "table" then
    state.limits.containers_omitted = state.limits.containers_omitted + 1
    return
  end
  if depth > ObserveModel.MAX_DEPTH or state.containerCount >= ObserveModel.MAX_CONTAINERS then
    state.limits.containers_truncated = true
    state.limits.containers_omitted = state.limits.containers_omitted + 1
    return
  end
  local ref = containerRefFor(sessionId, node)
  if ref == nil then
    state.limits.containers_omitted = state.limits.containers_omitted + 1
    return
  end
  local parsed = refs().parseContainer(ref)
  if parsed == nil then
    state.limits.containers_omitted = state.limits.containers_omitted + 1
    return
  end
  state.containerCount = state.containerCount + 1
  state.containers[state.containerCount] = {
    ref = ref,
    kind = node.kind,
    name = ObserveModel.text(node.name) or "",
    parent_ref = parentRef or json().null,
    capacity = ObserveModel.clamp(node.capacity, 0, math.huge),
    used_capacity = ObserveModel.clamp(node.used_capacity, 0, math.huge),
    accessible = node.accessible ~= false,
  }

  local items = type(node.items) == "table" and node.items or {}
  local present = #items
  local kept = 0
  for index = 1, present do
    if kept >= ObserveModel.MAX_ITEMS_PER_CONTAINER or state.itemCount >= ObserveModel.MAX_ITEMS then
      state.limits.items_truncated = true
      state.limits.items_omitted = state.limits.items_omitted + (present - index + 1)
      break
    end
    local descriptor = items[index]
    local item = buildItem(sessionId, ref, parsed.tail, descriptor)
    if item == nil then
      state.limits.items_omitted = state.limits.items_omitted + 1
    else
      kept = kept + 1
      state.itemCount = state.itemCount + 1
      state.items[state.itemCount] = item
      if descriptor.hand == "primary" or descriptor.hand == "secondary" then
        state.hands[descriptor.hand] = item.ref
      end
      if type(descriptor.container) == "table" then
        walkContainer(state, sessionId, descriptor.container, ref, depth + 1)
      end
    end
  end
  if node.truncated == true then
    -- The reader stopped before the engine's list ended. Adding its count here
    -- is what keeps "the bag holds four things" from being read off a bag whose
    -- fifth item was never looked at.
    state.limits.items_truncated = true
    state.limits.items_omitted = state.limits.items_omitted + (ObserveModel.integer(node.dropped) or 1)
  end
end

--- The inventory block: the container tree and every item inside it.
---
--- `roots` is an array of container nodes, each optionally carrying `items`,
--- and each item optionally carrying `container` for a bag inside a bag. The
--- parent reference of a nested container is the container holding the bag,
--- which is what lets the sidecar rebuild the tree from a flat list.
---
--- `roots.containers_dropped` is the reader's own count of containers it never
--- opened -- a bag past its walk budget, a worn bag whose slot this build does
--- not name. The caps here cannot see those, because a node that was never
--- built never arrives, so the count has to travel with the list.
function ObserveModel.inventory(sessionId, roots, limits)
  local state = {
    containers = {},
    items = {},
    containerCount = 0,
    itemCount = 0,
    hands = {},
    limits = limits,
  }
  if type(roots) == "table" then
    for index = 1, #roots do
      walkContainer(state, sessionId, roots[index], nil, 1)
    end
    local unopened = ObserveModel.integer(roots.containers_dropped)
    if unopened ~= nil and unopened > 0 then
      limits.containers_truncated = true
      limits.containers_omitted = limits.containers_omitted + unopened
    end
  end
  sort(state.containers, function(left, right)
    return left.ref < right.ref
  end)
  sort(state.items, function(left, right)
    return left.ref < right.ref
  end)
  return {
    containers = json().array(state.containers),
    items = json().array(state.items),
  }, state.hands
end

-- ---------------------------------------------------------------------------
-- nearby
-- ---------------------------------------------------------------------------

local function nearbyPosition(descriptor)
  return ObserveModel.position(descriptor, false)
end

--- One world object. Containers are referenced as containers, because that
--- reference is the one an inventory transfer can actually name; a door with a
--- known object index is referenced as an object, because that is the reference
--- door.open can actually resolve; everything else is referenced by its square.
local function buildObject(sessionId, descriptor)
  if type(descriptor) ~= "table" then
    return nil
  end
  local kind = ObserveModel.token(descriptor.kind)
  local distance = ObserveModel.clamp(descriptor.distance, 0, math.huge)
  if kind == nil or distance == nil then
    return nil
  end
  local ref
  if descriptor.object_index ~= nil and descriptor.container_index ~= nil then
    ref = containerRefFor(sessionId, {
      kind = ObserveModel.CONTAINER_KIND.WORLD,
      x = descriptor.x,
      y = descriptor.y,
      z = descriptor.z,
      object_index = descriptor.object_index,
      container_index = descriptor.container_index,
    })
  else
    if kind == "door" and descriptor.object_index ~= nil then
      ref = refs().buildObject(
        sessionId,
        ObserveModel.integer(descriptor.x),
        ObserveModel.integer(descriptor.y),
        ObserveModel.integer(descriptor.z),
        ObserveModel.integer(descriptor.object_index)
      )
    end
    if ref == nil then
      -- A door whose index the reader could not carry still has a square: a
      -- coarser reference is honest, a dropped door is a hole in the map.
      ref = refs().buildSquare(
        sessionId,
        ObserveModel.integer(descriptor.x),
        ObserveModel.integer(descriptor.y),
        ObserveModel.integer(descriptor.z)
      )
    end
  end
  if ref == nil then
    return nil
  end
  local object = {
    ref = ref,
    kind = kind,
    distance = distance,
  }
  if kind == "door" then
    -- Pass through only what was read: a boolean that is absent stays absent,
    -- because the sidecar reads a missing `locked` as "could not be read" and a
    -- fabricated false as "unlocked" -- opposite facts.
    if type(descriptor.open) == "boolean" then
      object.open = descriptor.open
    end
    if type(descriptor.locked) == "boolean" then
      object.locked = descriptor.locked
    end
    if type(descriptor.barricaded) == "boolean" then
      object.barricaded = descriptor.barricaded
    end
    local orientation = ObserveModel.token(descriptor.orientation)
    if orientation ~= nil then
      object.orientation = orientation
    end
  end
  -- The square's room and building, validated like every other token; an
  -- unread or unnameable one is absent, exactly as outdoors is.
  local room = ObserveModel.place(descriptor.room)
  if room ~= nil then
    object.room = room
  end
  local building = ObserveModel.place(descriptor.building)
  if building ~= nil then
    object.building = building
  end
  local position = nearbyPosition(descriptor)
  if position ~= nil then
    object.position = position
  end
  local semantics = tokenList(descriptor.semantics, ObserveModel.BASE_SEMANTICS[kind], ObserveModel.MAX_SEMANTICS)
  if #semantics > 0 then
    object.semantics = semantics
  end
  return object
end

--- One zombie. `visible` and `chasing` are omitted when the reader could not
--- read them, and the omission is declared through the limit counters -- the
--- sidecar reads a missing `chasing` as false, which understates the threat, so
--- "we could not tell" must not look like "it is not chasing".
local function buildZombie(sessionId, descriptor, limits)
  if type(descriptor) ~= "table" then
    return nil
  end
  local runtimeId = ObserveModel.runtimeId(descriptor.runtime_id)
  local distance = ObserveModel.clamp(descriptor.distance, 0, math.huge)
  if runtimeId == nil or distance == nil then
    return nil
  end
  local generation = ObserveModel.integer(descriptor.generation) or 0
  if generation < 0 then
    return nil
  end
  local ref = refs().buildZombie(sessionId, runtimeId, generation)
  if ref == nil then
    return nil
  end
  local zombie = { ref = ref, distance = distance }
  -- The body state travels only as a validated token. Absent stays absent --
  -- the reader's tri-state honesty must survive the model -- and a string the
  -- token alphabet cannot carry is dropped whole rather than mangled into a
  -- state nobody observed.
  local state = ObserveModel.token(descriptor.state)
  if state ~= nil then
    zombie.state = state
  end
  if type(descriptor.visible) == "boolean" then
    zombie.visible = descriptor.visible
  else
    limits.visible_unknown = true
  end
  if type(descriptor.chasing) == "boolean" then
    zombie.chasing = descriptor.chasing
  else
    limits.chasing_unknown = true
  end
  local position = nearbyPosition(descriptor)
  if position ~= nil then
    zombie.position = position
  end
  return zombie
end

--- Order by distance, then by reference so ties are still deterministic.
local function byDistance(left, right)
  if left.distance ~= right.distance then
    return left.distance < right.distance
  end
  return left.ref < right.ref
end

local function boundedNearby(built, cap, truncatedKey, omittedKey, limits)
  sort(built, byDistance)
  if #built > cap then
    limits[truncatedKey] = true
    limits[omittedKey] = limits[omittedKey] + (#built - cap)
    for index = #built, cap + 1, -1 do
      built[index] = nil
    end
  end
  return json().array(built)
end

--- The nearby block: what is around the player, nearest first.
function ObserveModel.nearby(sessionId, fields, limits)
  local objects = {}
  local zombies = {}
  if type(fields) == "table" then
    local rawObjects = type(fields.objects) == "table" and fields.objects or {}
    for index = 1, #rawObjects do
      local object = buildObject(sessionId, rawObjects[index])
      if object == nil then
        limits.objects_omitted = limits.objects_omitted + 1
      else
        objects[#objects + 1] = object
      end
    end
    local rawZombies = type(fields.zombies) == "table" and fields.zombies or {}
    for index = 1, #rawZombies do
      local zombie = buildZombie(sessionId, rawZombies[index], limits)
      if zombie == nil then
        limits.zombies_omitted = limits.zombies_omitted + 1
      else
        zombies[#zombies + 1] = zombie
      end
    end
    if fields.objects_truncated == true then
      limits.objects_truncated = true
      limits.objects_omitted = limits.objects_omitted + (ObserveModel.integer(fields.objects_dropped) or 1)
    end
    if fields.zombies_truncated == true then
      limits.zombies_truncated = true
      limits.zombies_omitted = limits.zombies_omitted + (ObserveModel.integer(fields.zombies_dropped) or 1)
    end
  end
  return {
    objects = boundedNearby(objects, ObserveModel.MAX_OBJECTS, "objects_truncated", "objects_omitted", limits),
    zombies = boundedNearby(zombies, ObserveModel.MAX_ZOMBIES, "zombies_truncated", "zombies_omitted", limits),
  }
end

-- ---------------------------------------------------------------------------
-- game, action, safety
-- ---------------------------------------------------------------------------

--- The game block. `build` is required: an observation that does not say which
--- game produced it cannot be checked against the capability report.
---
--- `paused` and `speed` are required by the schema and have no "absent" form, so
--- an unread one falls back to the reading that stops the agent -- not running,
--- speed zero -- and says through the limit counters that it was not read.
function ObserveModel.game(fields, limits)
  if type(fields) ~= "table" then
    return nil, "game fields must be a table"
  end
  local build = ObserveModel.text(fields.build)
  if build == nil then
    return nil, "the game build is unknown"
  end
  local speed = ObserveModel.clamp(fields.speed, 0, math.huge)
  if type(limits) == "table" then
    limits.paused_unknown = type(fields.paused) ~= "boolean"
    limits.speed_unknown = speed == nil
  end
  local game = {
    build = build,
    save_id = ObserveModel.saveId(fields.save_key) or ObserveModel.UNKNOWN_SAVE_ID,
    paused = fields.paused ~= false,
    speed = speed or 0,
  }
  local worldTime = ObserveModel.text(fields.world_time)
  if worldTime ~= nil then
    game.world_time = worldTime
  end
  -- Deliberately *not* given a fallback the way `paused` and `speed` are. Those
  -- fall back to the reading that stops the agent, which is safe because the
  -- schema requires them. This one is optional precisely so an unread value can
  -- stay unread: the sidecar refuses to arm on an omission, so writing `false`
  -- here to fill a hole would hand out the permission the gate exists to
  -- withhold.
  if type(fields.multiplayer) == "boolean" then
    game.multiplayer = fields.multiplayer
  end
  return game
end

--- The action block, from PZAgent.Ownership.describe output.
---
--- An absent description is `ambiguous` and busy, matching
--- `Ownership.unreadable`: a queue nobody could read is not an empty one.
function ObserveModel.action(description)
  local names = protocol().OWNERSHIP
  if type(description) ~= "table" then
    return { ownership = names.AMBIGUOUS, busy = true }
  end
  local ownership = description.ownership
  if ownership ~= names.NONE
    and ownership ~= names.MOD
    and ownership ~= names.MANUAL
    and ownership ~= names.AMBIGUOUS
  then
    ownership = names.AMBIGUOUS
  end
  local action = { ownership = ownership, busy = description.busy == true }
  local actionType = ObserveModel.token(description.action_type)
  if actionType ~= nil then
    action.type = actionType
  end
  local progress = ObserveModel.clamp(description.progress, 0, 1)
  if progress ~= nil then
    action.progress = progress
  end
  return action
end

--- The safety block, from PZAgent.Safety.snapshot output.
function ObserveModel.safety(snapshot)
  local P = protocol()
  local state = type(snapshot) == "table" and snapshot or {}
  local mode = P.normaliseMode(state.mode) or P.MODE.OFF
  local danger = state.danger_level
  if P.dangerRank(danger) == nil then
    danger = P.DANGER.NONE
  end
  return {
    armed = state.armed == true,
    mode = mode,
    danger_level = danger,
    manual_takeover = state.manual_takeover == true,
    -- Unknown liveness reads as stale, which is the direction that stops the
    -- agent rather than the one that lets it act unsupervised.
    sidecar_stale = state.sidecar_stale ~= false,
  }
end

-- ---------------------------------------------------------------------------
-- the document
-- ---------------------------------------------------------------------------

--- Assemble one observation.
---
--- Returns the document, or nil plus a reason. The failures are all of one
--- kind: something the schema requires could not be observed, and publishing a
--- snapshot with a plausible stand-in in its place is the failure this whole
--- protocol exists to prevent.
function ObserveModel.build(context)
  if type(context) ~= "table" then
    return nil, "observation context must be a table"
  end
  local sessionId = context.session_id
  if not refs().isSessionSegment(sessionId) then
    return nil, "invalid session id"
  end
  local seq = ObserveModel.integer(context.seq)
  if seq == nil or seq < 0 then
    return nil, "seq must be a non-negative integer"
  end
  local timestamp = ObserveModel.integer(context.timestamp_ms)
  if timestamp == nil or timestamp < 0 then
    return nil, "timestamp_ms must be a non-negative integer"
  end

  local limits = newLimits()
  local game, gameError = ObserveModel.game(context.game, limits)
  if game == nil then
    return nil, gameError
  end
  local player, playerError = ObserveModel.player(sessionId, context.player, limits)
  if player == nil then
    return nil, playerError
  end

  local document = {
    schema_version = protocol().SCHEMA_VERSION,
    session_id = sessionId,
    seq = seq,
    timestamp_ms = timestamp,
    full = context.full ~= false,
    capability_revision = ObserveModel.integer(context.capability_revision) or 0,
    game = game,
    player = player,
    action = ObserveModel.action(context.action),
    safety = ObserveModel.safety(context.safety),
  }

  if context.inventory ~= nil then
    local inventory, hands = ObserveModel.inventory(sessionId, context.inventory, limits)
    document.inventory = inventory
    if hands.primary ~= nil or hands.secondary ~= nil then
      player.hands = hands
    end
  end
  if context.nearby ~= nil then
    document.nearby = ObserveModel.nearby(sessionId, context.nearby, limits)
  end

  local goal = ObserveModel.token(context.active_goal_id)
  if goal ~= nil then
    document.active_goal_id = goal
  end

  ObserveModel.applyLimits(player.stats, limits)
  return document
end

--- Distances at which the mod's own danger floor changes rung.
---
--- Deliberately coarse. This is not the agent's threat policy — that lives in
--- `pz_agent_core.safety.threat`, is richer, and is what the reflex guard and
--- the planner use. This is the *floor* the mod applies to its own gate and its
--- HUD, computed from what it already observed, so that
--- `observation.safety.danger_level` carries a fact rather than a placeholder.
---
--- It errs upward. A mod that overstates danger refuses work the sidecar would
--- have allowed, which is a nuisance; one that understates it starts work
--- during a horde, which is the failure this rung exists to prevent.
ObserveModel.DANGER_CLOSE = 2
ObserveModel.DANGER_CROWD = 3

--- Derive a conservative danger floor from an already-built `nearby` table.
---
--- Pure: it reads the same fields the observation carries, so a change to the
--- reported shape cannot leave this reading something that is no longer there.
--- Zombies on another floor are counted as present but never as closing —
--- a horde one storey up is a reason to be wary, not to abort.
function ObserveModel.dangerFloor(nearby, playerPosition)
  local levels = protocol().DANGER
  if type(nearby) ~= "table" or type(nearby.zombies) ~= "table" then
    return levels.NONE
  end

  local playerFloor = nil
  if type(playerPosition) == "table" then
    playerFloor = playerPosition.z
  end

  local seen, chasing, close, crowd = 0, 0, 0, 0
  for index = 1, #nearby.zombies do
    local zombie = nearby.zombies[index]
    if type(zombie) == "table" then
      seen = seen + 1
      local sameFloor = playerFloor == nil
        or type(zombie.position) ~= "table"
        or zombie.position.z == playerFloor
      local distance = tonumber(zombie.distance)
      if sameFloor and zombie.chasing == true then
        chasing = chasing + 1
      end
      if sameFloor and distance ~= nil and distance <= ObserveModel.DANGER_CLOSE then
        close = close + 1
      end
      if distance ~= nil then
        crowd = crowd + 1
      end
    end
  end

  if seen == 0 then
    return levels.NONE
  end
  if chasing > 0 or close > 0 then
    return levels.HIGH
  end
  if crowd >= ObserveModel.DANGER_CROWD then
    return levels.MEDIUM
  end
  return levels.LOW
end

return ObserveModel
