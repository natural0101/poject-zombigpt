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

files["tests/lua"] = {
  std = "+busted",
  globals = { "PZAgent", "describe", "it", "assert" },
}
