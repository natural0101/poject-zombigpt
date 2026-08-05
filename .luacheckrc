-- luacheck configuration for the Project Zomboid mod.
--
-- The mod runs inside Kahlua with the game's globals injected. Declaring them
-- here is what lets luacheck flag a genuine typo instead of drowning us in
-- "accessing undefined variable" for every engine symbol we legitimately use.

std = "lua51"
max_line_length = 120
codes = true

-- Engine globals the mod reads. Anything not in this list is a typo or an
-- unverified API that must go through a capability probe first.
read_globals = {
  "getPlayer", "getSpecificPlayer", "getNumActivePlayers",
  "getGameTime", "getWorld", "getCell", "getSquare",
  "getCore", "getTimestampMs", "getTimestamp",
  "getFileWriter", "getFileReader", "getModFileReader", "getModFileWriter",
  "getSoundManager", "getTextManager",
  "ISBaseTimedAction", "ISTimedActionQueue", "ISInventoryTransferAction",
  "ISEatFoodAction", "ISDrinkFromBottle", "ISReadABook", "ISWalkToTimedAction",
  "ISInventoryPage", "ISPanelJoypad",
  "Events", "luautils", "instanceof", "sendClientCommand",
  "AdjacentFreeTileFinder", "PZMath",
  "UIManager", "ISUIElement", "ISPanel",
  "unpack", "loadstring", "getKeyName", "isKeyDown",
}

globals = {
  "PZAgent",
}

exclude_files = {
  ".venv",
  "**/vanilla/**",
}

-- The Lua tests run under a plain interpreter with no engine present, so they
-- have to install the globals the mod expects -- and swap an implementation
-- mid-test to exercise a failure branch. Declaring the engine symbols writable
-- here is the point of the mocks, not an oversight; without it every stub a
-- test installs reads as "setting a read-only global".
files["tests/lua"] = {
  globals = read_globals,
}
