--[[
PZAgent.Compat -- shims for what Kahlua actually provides in Build 42.

Invariant: no other PZAgent file may call a standard-library global that the
live game failed to provide. The 2026-08-08 live run on Build 42.20.2 proved
that Kahlua ships `pairs` but not the global `next`; every `next(t) == nil`
emptiness check in the mod crashed the surrounding event handler. This module
is the one place such gaps are papered over, so the next missing global costs
one edit here instead of a sweep.

Callers reference these helpers at call time (`PZAgent.Compat.hasEntries(t)`)
or capture them inside a function body, never as a file-scope local taken
before load order is settled: shared files load alphabetically and client
files after them, so `PZAgent.Compat` exists by the time any client code runs,
but a file-scope capture in another shared file would be a load-order bet.
]]

PZAgent = PZAgent or {}

local Compat = {}
PZAgent.Compat = Compat

--- True when `value` is a table with at least one entry.
---
--- This is the Kahlua-safe spelling of `next(t) ~= nil`. It relies only on
--- `pairs`, which the live game provides. A non-table answers false rather
--- than raising, because every call site asks "is there anything here" about
--- a value it is about to iterate, and the honest answer for a non-table is
--- that there is nothing to iterate.
function Compat.hasEntries(value)
  if type(value) ~= "table" then
    return false
  end
  -- luacheck: ignore 512 -- one iteration is the point: any entry proves non-emptiness.
  for _ in pairs(value) do
    return true
  end
  return false
end

return Compat
