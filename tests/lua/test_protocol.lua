-- Vocabulary tests for PZAgent.Protocol.
--
-- The whitelist is the mod's first line of defence: a command name that is not
-- in it must be refused before anything looks at its arguments.

local Harness = dofile((arg[0]:match("^(.*)test_protocol%.lua$") or "") .. "support/harness.lua")
local PZ = Harness.loadModules()
local Protocol = PZ.Protocol

local equal, ok, isNil = Harness.equal, Harness.ok, Harness.isNil

Harness.group("versions match the Python source of truth")
do
  equal(Protocol.PROTOCOL_VERSION, "1.1", "protocol version")
  equal(Protocol.SCHEMA_VERSION, "1.0", "schema version")
  equal(Protocol.MOD_VERSION, "0.1.0", "mod version")
  equal(Protocol.TARGET_BUILD, "42.20", "target build")
  ok(Protocol.isSupportedBuild("42.20"), "the target build is supported")
  ok(not Protocol.isSupportedBuild("41.78"), "an older build is not silently accepted")
end

Harness.group("protocol compatibility is major-only")
do
  equal(Protocol.major("1.0"), 1, "major of 1.0")
  equal(Protocol.major("12.34"), 12, "a multi-digit major")
  isNil(Protocol.major("1"), "a version with no minor is malformed")
  isNil(Protocol.major("1.0.0"), "a three-part version is not this format")
  isNil(Protocol.major("x.y"), "a non-numeric version is malformed")
  isNil(Protocol.major(nil), "a nil version is malformed")

  ok(Protocol.isCompatible("1.0"), "the same version is compatible")
  ok(Protocol.isCompatible("1.7"), "a higher minor is compatible, minors being additive")
  ok(not Protocol.isCompatible("2.0"), "a different major is not compatible")
  ok(not Protocol.isCompatible("0.9"), "a lower major is not compatible")
  ok(not Protocol.isCompatible("banana"), "a malformed version is not compatible")
end

Harness.group("the action whitelist is closed")
do
  equal(#Protocol.ACTION_NAMES, 30, "the whitelist holds every ActionName")
  for index = 1, #Protocol.ACTION_NAMES do
    ok(Protocol.isKnownAction(Protocol.ACTION_NAMES[index]), Protocol.ACTION_NAMES[index] .. " is known")
  end
  ok(not Protocol.isKnownAction("world.destroy"), "an invented action is refused")
  ok(not Protocol.isKnownAction("SAFETY.STOP"), "the whitelist is case-sensitive")
  ok(not Protocol.isKnownAction(""), "an empty action name is refused")
  ok(not Protocol.isKnownAction(nil), "a nil action name is refused")
  ok(not Protocol.isKnownAction({ "safety.stop" }), "a table is not an action name")
end

Harness.group("action classes")
do
  ok(Protocol.isAlwaysAllowed("safety.stop"), "stop bypasses arming")
  ok(Protocol.isAlwaysAllowed("session.disarm"), "disarm bypasses arming")
  ok(Protocol.isAlwaysAllowed("plan.cancel"), "cancel bypasses arming")
  ok(not Protocol.isAlwaysAllowed("consume.eat"), "eating does not bypass arming")
  ok(Protocol.isReadOnly("world.inspect"), "inspect is read-only")
  ok(Protocol.isReadOnly("action.wait"), "waiting is read-only")
  ok(not Protocol.isReadOnly("movement.move_to"), "moving is not read-only")
end

Harness.group("statuses and modes")
do
  ok(Protocol.isTerminalStatus("succeeded"), "succeeded is terminal")
  ok(Protocol.isTerminalStatus("lost"), "lost is terminal")
  ok(not Protocol.isTerminalStatus("progress"), "progress is not terminal")
  ok(not Protocol.isTerminalStatus("received"), "received is not terminal")

  equal(Protocol.normaliseMode("OBSERVE"), "OBSERVE", "the enum spelling is accepted")
  equal(Protocol.normaliseMode("observe"), "OBSERVE", "the blueprint's lowercase spelling normalises")
  equal(Protocol.normaliseMode("Autonomous"), "AUTONOMOUS", "mixed case normalises")
  isNil(Protocol.normaliseMode("GODMODE"), "an unknown mode is refused")
  isNil(Protocol.normaliseMode(nil), "a nil mode is refused")

  ok(Protocol.MUTATING_MODES[Protocol.MODE.ASSISTED], "assisted may mutate")
  ok(Protocol.MUTATING_MODES[Protocol.MODE.AUTONOMOUS], "autonomous may mutate")
  ok(not Protocol.MUTATING_MODES[Protocol.MODE.OBSERVE], "observe may not mutate")
  ok(not Protocol.MUTATING_MODES[Protocol.MODE.OFF], "off may not mutate")
  ok(not Protocol.MUTATING_MODES[Protocol.MODE.REFLEX_ONLY], "reflex-only may not mutate")
end

Harness.group("danger ranking")
do
  equal(Protocol.dangerRank("none"), 0, "none ranks lowest")
  equal(Protocol.dangerRank("critical"), 4, "critical ranks highest")
  ok(Protocol.dangerRank("high") > Protocol.dangerRank("medium"), "high outranks medium")
  isNil(Protocol.dangerRank("apocalyptic"), "an unknown level has no rank")
end

Harness.group("reason codes")
do
  equal(Protocol.REASON.POSTCONDITION_MET, "POSTCONDITION_MET", "the only code a success may carry")
  equal(Protocol.REASON.PANIC_STOP, "PANIC_STOP", "panic stop")
  equal(Protocol.REASON.USER_TAKEOVER, "USER_TAKEOVER", "user takeover")
  equal(Protocol.REASON.LEASE_EXPIRED, "LEASE_EXPIRED", "lease expiry")
  equal(Protocol.REASON.NOT_ARMED, "NOT_ARMED", "not armed")
  for name, value in pairs(Protocol.REASON) do
    equal(value, name, "reason code " .. name .. " is spelled like its key")
  end
end

Harness.finish("test_protocol")
