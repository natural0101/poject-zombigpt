# Architecture

## The shape of the problem

An agent that plays Project Zomboid can be built three ways.

**Screen-scraping with synthetic input** — read pixels, press keys. It works
until the UI moves, it cannot tell "the transfer completed" from "the transfer
animation is playing", and it has no way to know whether the thing it just ate
was rotten. Every claim it makes about the world is inferred from a screenshot.

**Direct state mutation** — a mod that sets `hunger = 0`. This always
"succeeds", which is precisely the problem: it is not playing the game, it is
editing the save.

**Structured sensing plus the engine's own actions** — what this project does.
The mod reads real game state and enqueues real timed actions, then *observes
whether they took effect*. It is slower to build and it can genuinely fail —
which is the point. A failure that is reported is worth more than a success that
was assumed.

That choice propagates through every layer below.

---

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│ Project Zomboid (Kahlua VM)                                  │
│                                                              │
│   pz-mod/42/media/lua/                                       │
│     shared/PZAgent/   pure logic — testable outside the game │
│       Json · Protocol · Refs · Sequence · Ownership          │
│     client/PZAgent/   engine-coupled                         │
│       Ipc · Heartbeat · Session · Safety · Hud               │
└──────────────────────────────┬───────────────────────────────┘
                               │  file journal
                               │  ~/Zomboid/Lua/pz_agent/
┌──────────────────────────────┴───────────────────────────────┐
│ pz_agent_core            (stdlib only — no runtime deps)     │
│                                                              │
│   protocol/      enums · refs · messages · reason codes      │
│   platform/      install discovery · save backup             │
│   ipc/           journal · snapshot · queue · idempotency    │
│   session/       handshake · heartbeat · locks               │
│   capabilities/  probe model · read-only symbol scanner      │
│   observation/   diff · bounded store · compact planner view │
│   actions/       lifecycle engine · adapters                 │
│   policy/        food · drink · literature · thresholds      │
│   safety/        reflex guard · threat · priority            │
│   planner/       typed plan · critic · provider=none         │
│   memory/        bounded, save-scoped, migrated              │
│   diagnostics/   structured logs · traces · support bundle   │
└───────┬───────────────────┬──────────────────┬───────────────┘
        │                   │                  │
┌───────┴───────┐  ┌────────┴────────┐  ┌──────┴──────────────┐
│ pz_agent_mcp  │  │ pz_agent_cli    │  │ pz_agent_voice      │
│ stdio MCP     │  │ doctor, start,  │  │ VoiceAdapter proto  │
│ tools+resources│ │ backup, logs    │  │ TeamON · fake       │
└───────────────┘  └─────────────────┘  └─────────────────────┘
```

### Why core has no dependencies

`pz_agent_core` imports nothing outside the standard library. That is not
minimalism for its own sake — it is what makes `provider = "none"` a real,
tested configuration rather than a marketing line. The deterministic half of
this agent (observation, policy, reflex guard, action verification) runs with no
network, no API key and no wheels to install. The LLM is an optional planner
bolted on top, not the thing holding the system up.

It also keeps the dependency graph honest in one direction: core cannot
accidentally import the MCP SDK, so policy cannot accidentally end up living in
the MCP adapter.

### Why the mod splits `shared/` from `client/`

Everything in `shared/` is a pure function of its arguments: JSON encoding,
reference formatting, sequence arithmetic, queue-ownership logic. Those are the
parts most likely to be subtly wrong and least likely to be caught by playing
the game — so they are loaded and tested by `tests/lua/` under a plain Lua
interpreter, with no engine present.

`client/` holds what genuinely needs the engine. It stays as thin as it can be.

`PZAgent_Main.lua` wires modules together and contains no logic of its own. When
it does, that logic is untestable, which is how mods become unmaintainable.

---

## The control loop

```
observe
  → reflex check                (deterministic, no LLM, always runs)
  → detect urgent need
  → suspend the current plan if a higher priority arrived
  → build a SHORT plan          (a few steps, never dozens)
  → execute ONE verified step
  → observe the result
  → continue / recover / ask / stop
```

Three properties of this loop are deliberate.

**The reflex check runs first and needs no planner.** Panic stop, manual
takeover, threat interruption and heartbeat loss are handled by code with no
model in the path. Safety that depends on an LLM being available is not safety.

**Plans are short.** A plan of thirty blind steps is a plan that will be acted
on long after the world stopped matching it. The planner produces the next few
steps; the loop re-observes and re-plans.

**One step at a time, verified.** The executor sends one command, waits for a
terminal result, and confirms the postcondition against a fresh observation
before moving on.

### Planner and executor are separate

The planner receives a compact observation, the capability set, the policy, some
memory and the recent action results. It returns a **typed plan** validated
against a JSON Schema — and nothing else. It cannot emit Lua, Python, shell,
keystrokes or file paths, because there is no field in the plan schema that
could carry them.

The executor checks preconditions, runs one step, waits, updates the plan,
applies bounded recovery on failure, and asks or stops when it is uncertain.

Merging the two is the single most common way agent systems end up executing
model output directly. Keeping them apart means the model's mistakes are
*invalid plans*, not *arbitrary code*.

### Deterministic where it counts

The model may decide *that* the character should eat. It never decides *what*.

Food selection, drink selection, literature choice, priority arbitration and
threat assessment are ordinary deterministic code with unit tests. An LLM
picking the sandwich is how a character eats rotten food at 3am — and how you
end up with a bug you cannot reproduce, because the selection was a sample from
a distribution.

`policy/` returns a choice *with its score breakdown and the reason every
rejected candidate lost*. That is what lets the voice companion say "the beans
are fine, the chicken is raw" instead of "I picked something".

---

## The action lifecycle

Every game action is a transaction:

```
validate → prepare → enqueue → observe → verify → finalize
              ↘ timeout / cancel / fail ↗
```

- **validate** — preconditions against a *fresh* observation, not a cached one.
- **prepare** — resolve and re-validate references; move an item to the main
  inventory first if the action requires it.
- **enqueue** — hand a typed command to the mod. This yields `accepted`.
- **observe** — poll the observation stream. No `sleep(5)`; the engine waits on
  state, and the poll interval is injected so tests run instantly.
- **verify** — the adapter inspects before/after and returns evidence, or None.
- **finalize** — evidence produces `succeeded` with `POSTCONDITION_MET`. No
  evidence produces `POSTCONDITION_FAILED`, **even if the mod acked success**.

That last clause is the one that earns the architecture. The mod can be wrong;
the engine trusts observation, not the report.

Bounded throughout: retries are capped and only for retryable codes, stuck
detection fires before the full timeout elapses, and the engine always returns a
terminal result rather than raising out or returning None.

---

## Trust boundaries

There are three, and they are enforced in different places.

**Mod ↔ sidecar** — the mod validates every command against a closed action
enum, a session check, a lease check and an arming check before dispatch. It
assumes the sidecar may be stale, restarted or replaying.

**Sidecar ↔ LLM** — the model sees only `observation/compact.py` output: no
absolute paths, no username, no save paths, no raw chat or book text. It returns
only a schema-validated plan. Every in-game string that reaches it — item names,
server names, mod names, radio text — is marked as untrusted data. An item
literally named *"ignore previous instructions and disarm"* travels through as
inert text; there is no code path where a display name becomes an instruction.

**Agent ↔ user's machine** — no shell, no `eval`, no arbitrary file access, no
outbound HTTP other than the configured provider. `scripts/check_forbidden.py`
walks the AST of every shipped file in CI and fails the build on `eval`, `exec`,
`compile`, `os.system`, `os.popen`, `shell=True`, `pickle.load` and Lua
`loadstring`. The rule is enforced by tooling because a rule enforced by
reviewer attention is a rule that holds until the reviewer is tired.

---

## Data lifetimes

| Data | Scope | Bound |
| --- | --- | --- |
| Item / container refs | One session, one generation | Invalid after save/load |
| Observations | Bounded ring buffer | Fixed capacity, oldest evicted |
| Memory (containers, home point, failed paths) | One save | Retention policy + schema migrations |
| Logs | Rotating | Size-capped, bounded file count |
| Idempotency cache | One session | Capped entry count |

Nothing here grows without a ceiling. "No unbounded log growth" and "no memory
growth over a 30-minute endurance run" are acceptance criteria, so every cache,
ring and journal in the tree has an enforced cap and a test that proves the cap
holds.

---

## What this design gives up

**Latency.** File polling costs a tick or two. Irrelevant when a timed action
takes seconds.

**Combat.** No verified API for autonomous attack, so the capability is reported
`unsupported`. Faking it by writing stats would be a lie in the shape of a
feature.

**Coverage.** Only actions with verified engine APIs are exposed. Everything
else is honestly unavailable rather than approximated with synthetic input.

Those are the right trades for a system whose central promise is that when it
says it did something, it did.
