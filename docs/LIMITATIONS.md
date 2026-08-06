# Known limitations

An honest list. Anything here is a limitation by design or by verified
constraint, not a feature that is nearly finished.

For live implementation status — what is built, what is tested, what still needs
a running game — see [`PROGRESS.md`](PROGRESS.md).

---

## Scope

**Single player only.** Automating a character on someone else's server is the
operator's decision, not this agent's, and the refusal has no workaround
setting. Two gates enforce that:

- `safety.allow_multiplayer = true` is a **configuration error**. The file does
  not load. It used to be a warning whose text claimed the session handshake
  would refuse multiplayer anyway — and no such refusal existed anywhere, so
  the setting was exactly the bypass it said it was not.
- Every mutating command is refused by the action engine unless the mod
  positively reported the session as single player. `observation.game.multiplayer`
  has three states and **an absent reading is refused exactly as `true` is**,
  because silence is not permission — the same rule that stops a missing
  `is_bleeding` from meaning "not bleeding".

Stopping, disarming, cancelling and the three read-only actions are deliberately
exempt: an agent that cannot be stopped in the one session it should not be
running in is worse than one that never had the gate.

**This has never been exercised against a real multiplayer session.** The mod
reads `isClient` and `isServer`, both unconfirmed against Build 42.20 like every
other engine symbol. If neither can be read, the agent refuses to act at all
rather than assuming single player — a conservative failure, but a failure, and
`docs/GAME_API_VERIFICATION.md` lists it among what a live run must settle.

**One character.** The protocol is session-scoped to a single active character.
Split-screen and multiple simultaneous characters are out of scope.

**Windows 10/11 x64, Steam.** Path discovery targets the Steam layout. The core
is platform-neutral and its tests run on Linux, but nobody has verified the
end-to-end path against a GOG or a Linux install of the game.

**Build 42.20 Stable.** Capability probes were authored against this build. On a
different build the doctor warns and every previously `verified` capability
downgrades — a report from 42.19 proves nothing about 42.20.

---

## What the agent will not do

**Combat.** There is no verified API for autonomous attack, so the capability is
reported `unsupported` with reason `NO_VERIFIED_API`. It stays that way. Faking
it by writing stats would be a lie shaped like a feature.

**Drive.** Vehicles are not modelled.

**Anything requiring an unverified API.** Where a capability cannot be probed,
it is reported unavailable. There is no synthetic-input fallback that pretends
otherwise.

**Write game statistics directly.** Setting `hunger = 0` always "succeeds",
which is precisely why it is forbidden. The agent plays the game; it does not
edit the save.

---

## Consequences of the design

**Latency is a tick or two.** The transport is a file journal, polled. That is
the right trade when a timed action takes seconds, but it means the agent is not
suitable for anything reflex-speed.

**Actions can fail.** Because success requires an observed postcondition, the
agent reports failures that a screen-scraping bot would report as success. This
is the intended behaviour and the reason to prefer it.

**Refs die on save/load.** Every item, container and square reference is scoped
to a session and a generation. After a save/load transition, references minted
earlier are `INVALID_REF` — not stale, invalid. A plan built before the
transition cannot be resumed; it is rebuilt from a fresh observation.

**Plans are short by construction.** The planner returns a few steps, not a
campaign. Long-horizon goals are pursued by repeatedly re-planning, which means
the agent can look indecisive if the world keeps changing under it.

**Recovery never re-arms.** After any crash, restart or save change, the agent
comes back in `OBSERVE`. You re-arm it. This is deliberate and is not
configurable.

---

## Operational bounds

Everything is capped, and hitting a cap is reported rather than silently
applied.

| Bounded thing | Consequence when the cap is hit |
| --- | --- |
| Observation ring buffer | Oldest observation evicted |
| Idempotency cache | Oldest key evicted; a very old duplicate could re-execute |
| Logs and journals | Rotated; oldest rotated file deleted |
| Memory store | Retention policy applies; derived facts kept, raw history dropped |
| Retries per command | Terminal failure with the last reason code |
| Plan length | Planner output rejected if longer |
| Autonomous radius | Movement beyond it refused |
| Backup source size | Backup refused with a clear error rather than filling the disk |
| Compatibility scan | Truncated, and the truncation is reported |

The idempotency cache bound is the one with a real edge: a duplicate command
whose key was evicted long ago would be treated as new. In practice keys are
per-goal-step-attempt and the cache outlives any plausible redelivery window,
but it is a bound, not an impossibility.

---

## Things mocks do not prove

`tests/lua/` runs the mod's pure logic under a plain Lua interpreter with mocked
engine globals. It proves the JSON encoder round-trips and that reference
parsing agrees with the Python side.

It does **not** prove that `ISInventoryTransferAction`, `ISEatFoodAction` or
`ISReadABook` behave as expected in Build 42.20. Only a live session does that,
which is why `tests/game-smoke/` exists and why its scenarios are tracked
individually rather than declared complete in aggregate.

Any claim of engine compatibility that is not backed by a run against the
installed game is a claim this project does not make.

---

## Privacy and provider caveats

With `provider = "none"` the agent is fully local and this section is empty.

If you configure an external LLM provider, the compact observation leaves your
machine: scalar character stats, capability flags, item references with display
names and categories, and the current goal. Not the full snapshot, not paths,
not chat text — but it does leave. That is your choice to make, and the agent
embeds no key and defaults to none.

See [`PRIVACY.md`](../PRIVACY.md).

---

## Not signed

Windows will show an unsigned-binary warning for the packaged launcher. Code
signing is not part of this project. The warning is expected, documented, and
not something to click past without understanding.
