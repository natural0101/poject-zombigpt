--[[
Print the acks the mod really appends, plus the tables that decide their status.

The ack is the document the whole action lifecycle turns on: `ActionResult`'s
`status` is what the sidecar's executor retires a command by, and `reason_code`
is what its recovery table is keyed on. Four seams are already checked by
running the producer -- adapter args, adapter capabilities, the observation
document, the protocol tables -- and this one was not. Every existing test that
exercises `ActionResult.from_dict` builds the payload from Python constructors,
and every Lua test that drives a command reads the ack back with Lua. Nothing
ever put one side's output into the other side's reader.

What that leaves unchecked is the NEVER TERMINAL family in the place it would
actually bite. The mod carries its own phase vocabulary and two tables over it:

    ActionRuntime.WIRE_STATUS[phase]      -- the status that goes on the wire
    ActionRuntime.TERMINAL_PHASES[phase]  -- whether the mod stops tracking

The sidecar has neither. It has `ActionStatus.is_terminal`, over the status the
mod sent. A phase the mod retires whose status the sidecar does not is a command
the sidecar waits on forever; the reverse is a command the mod keeps stepping
after the sidecar has moved on. Both are silent, and nothing compared the two.

The acks here are appended by `Handle:ack` through the real runtime, not built
by this file: the commands are driven through `ActionRuntime` with spy adapters
that resolve to each interesting phase, and what is printed is whatever came out
of the journal. The fakes stand in for the *filesystem and the engine*, never for
the document.

Run from the repository root:

    lua5.4 tests/lua/support/dump_action_acks.lua
]]

local ROOT = "tests/lua/"
local Harness = dofile(ROOT .. "support/harness.lua")
local Support = dofile(ROOT .. "support/command_support.lua")

local PZ = Harness.loadModules()
Support.loadModules(Harness.root)

local Mock = dofile(ROOT .. "support/mock_game.lua")

--- Drive one command to a phase and return the acks the mod wrote.
---
--- `resolve` is handed the adapter's step callback so a scenario can decide how
--- the action ends; everything else is the runtime's own path.
local function acksFor(action, resolve)
  local adapter = Support.spyAdapter(action, resolve)
  local agent, fs, runtime = Support.runtime(Mock, { adapter }, { controls = true })
  -- `Support.command` defaults to `action.wait`'s argument; the spy declares
  -- none, and the dispatcher would reject the command on the argument rather
  -- than let it reach the phase this scenario is about.
  local command = Support.command({ action = action, args = {} })
  Support.publish(fs, { command })
  for tick = 1, 8 do
    runtime:tick(agent, Support.NOW + tick)
  end
  return Support.acks(fs)
end

-- `action.wait` and the other control actions are registered by the runtime
-- itself, so a spy for one of them is refused as a duplicate. `movement.move_to`
-- is a plain game action and takes a spy.
local ACTION = "movement.move_to"

local document = {
  wire_status = PZ.ActionRuntime.WIRE_STATUS,
  phases = PZ.ActionRuntime.PHASE,
  terminal_phases = (function()
    local names = {}
    for phase, terminal in pairs(PZ.ActionRuntime.TERMINAL_PHASES) do
      if terminal then
        names[#names + 1] = phase
      end
    end
    table.sort(names)
    return names
  end)(),
  -- Keyed by what the adapter was told to do, never by the outcome expected of
  -- it. A spy that reports `done` still ends `failed / POSTCONDITION_FAILED`
  -- here, because the runtime asks for an observed postcondition and this
  -- fixture has no game to observe one in -- which is the mod declining to
  -- claim a success it cannot see. Naming the key `succeeded` would have
  -- written that assumption into the fixture and hidden it.
  acks = {
    plain = acksFor(ACTION, {}),
    raising = acksFor(ACTION, { raise = true }),
    refusing = acksFor(ACTION, { refuse = PZ.Protocol.REASON.TARGET_NOT_LOADED }),
    polling = acksFor(ACTION, { polls = 2 }),
  },
}

print((PZ.Json.encode(document)))
