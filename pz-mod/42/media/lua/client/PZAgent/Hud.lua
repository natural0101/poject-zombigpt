--[[
PZAgent.Hud -- the on-screen state indicator.

Invariant: the player can always see whether the agent is armed, in what mode,
against which session, and what went wrong last. A safety control the player
cannot see the state of is not a safety control -- "is it still driving?" must
never require reading a log file.

`format` is pure and returns the lines to draw, so what the HUD claims is
testable outside the game. The drawing itself is behind a probe: if this build
does not expose the UI base class the panel derives from, the HUD reports itself
unsupported and the rest of the mod carries on without it. A missing indicator
is a degraded experience; a nil-index inside a render callback would take down
every other UI element on the screen with it.
]]

PZAgent = PZAgent or {}

local Hud = {}
PZAgent.Hud = Hud

Hud.WIDTH = 260
Hud.LINE_HEIGHT = 16
Hud.MARGIN = 8

--- Colour per mode, so the state is readable at a glance without parsing text.
local MODE_COLOUR = {
  OFF = { r = 0.60, g = 0.60, b = 0.60 },
  OBSERVE = { r = 0.45, g = 0.70, b = 1.00 },
  ASSISTED = { r = 0.40, g = 0.90, b = 0.50 },
  AUTONOMOUS = { r = 1.00, g = 0.75, b = 0.25 },
  REFLEX_ONLY = { r = 0.90, g = 0.55, b = 0.90 },
  EXPERIMENTAL_INPUT = { r = 1.00, g = 0.45, b = 0.45 },
}

local DISARMED_COLOUR = { r = 0.60, g = 0.60, b = 0.60 }
local ARMED_COLOUR = { r = 1.00, g = 0.35, b = 0.35 }

--- Truncate a value for display. The HUD renders text that originated outside
--- the game process, so it is bounded before it reaches the screen.
local function short(value, limit)
  local text = tostring(value)
  if #text <= limit then
    return text
  end
  -- Plain ASCII: the HUD font is not guaranteed to carry an ellipsis glyph, and
  -- Kahlua has no \u escape.
  return text:sub(1, limit - 3) .. "..."
end

--- The lines the HUD should draw, top to bottom.
---
--- `state` carries `safety` (a Safety.snapshot), `session` (the open session or
--- nil) and `last_error` (a string or nil).
function Hud.format(state)
  local Protocol = PZAgent.Protocol
  state = state or {}
  local safety = state.safety or {}
  local mode = safety.mode or Protocol.MODE.OFF
  local armed = safety.armed == true

  local lines = {}
  lines[1] = {
    text = string.format("PZ Agent  %s  %s", mode, armed and "ARMED" or "disarmed"),
    colour = armed and ARMED_COLOUR or (MODE_COLOUR[mode] or DISARMED_COLOUR),
  }

  local session = state.session
  local sessionText
  if session == nil or session.session_id == nil then
    sessionText = "session: none"
  else
    sessionText = string.format("session: %s", short(session.session_id, 12))
  end
  lines[2] = { text = sessionText, colour = MODE_COLOUR[mode] or DISARMED_COLOUR }

  local flags = {}
  if safety.manual_takeover == true then
    flags[#flags + 1] = "MANUAL"
  end
  if safety.sidecar_stale ~= false then
    flags[#flags + 1] = "NO SIDECAR"
  end
  local danger = safety.danger_level or Protocol.DANGER.NONE
  if danger ~= Protocol.DANGER.NONE then
    flags[#flags + 1] = "DANGER " .. string.upper(danger)
  end
  if #flags > 0 then
    lines[#lines + 1] = { text = table.concat(flags, "  "), colour = ARMED_COLOUR }
  end

  if state.last_error ~= nil then
    lines[#lines + 1] = { text = short(state.last_error, 48), colour = ARMED_COLOUR }
  end
  return lines
end

--- True when the UI base class the panel needs exists in this build.
function Hud.isSupported()
  return type(ISPanel) == "table" and type(ISPanel.derive) == "function"
end

--- Create the panel. Returns the panel, or nil plus a reason.
---
--- `provider` is a function returning the state `format` consumes; the panel
--- calls it on every render so it never holds a stale copy.
function Hud.create(provider, options)
  if not Hud.isSupported() then
    return nil, "ISPanel is not available in this build; the HUD stays off"
  end
  if type(provider) ~= "function" then
    return nil, "the HUD needs a state provider"
  end
  options = options or {}
  local panelClass = ISPanel:derive("PZAgentHud")

  function panelClass:render()
    local lines = Hud.format(provider())
    local y = Hud.MARGIN
    for index = 1, #lines do
      local line = lines[index]
      self:drawText(line.text, Hud.MARGIN, y, line.colour.r, line.colour.g, line.colour.b, 1.0)
      y = y + Hud.LINE_HEIGHT
    end
  end

  local height = Hud.MARGIN * 2 + Hud.LINE_HEIGHT * 4
  local panel = panelClass:new(options.x or 16, options.y or 16, Hud.WIDTH, height)
  local built, err = pcall(function()
    panel:initialise()
    panel:setAlwaysOnTop(true)
    panel:addToUIManager()
  end)
  if not built then
    return nil, "creating the HUD panel failed: " .. tostring(err)
  end
  return panel
end

--- Remove the panel. Safe to call when it was never created.
function Hud.destroy(panel)
  if panel == nil then
    return false
  end
  local removed = pcall(function()
    panel:removeFromUIManager()
  end)
  return removed
end

return Hud
