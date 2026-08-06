# Compatibility

## The rule

**An API is not available because a wiki said so, because an old mod used it, or
because it existed in Build 41.** It is available because a probe confirmed it
against the build actually installed on this machine.

This document describes how that confirmation works and how it is recorded. The
generated result lives in `compat/generated_api_report.json`, which is
gitignored — it describes *your* installation, not the project's.

---

## Capability states

| State | What it means | May a write tool be published? |
| --- | --- | --- |
| `verified` | A probe ran against the live game and confirmed the behaviour | Yes |
| `available_unverified` | The symbol exists in the local files; nothing has exercised it | Yes, with a caveat |
| `experimental` | Works, but not reliably enough for unattended use | No |
| `unsupported` | No verified API. The reason is recorded | No |
| `disabled_by_policy` | Available, but configuration forbids it | No |

The state ladder is one-directional in an important way: **a static scan can
never produce `verified`.** Reading the game's Lua files tells you a symbol
exists, not that calling it does what you expect. The best a scan yields is
`available_unverified`, and the code makes that ordering structurally impossible
to skip rather than merely documenting it.

`verified` requires evidence — which probe, which symbol, which file, when,
against which build — enforced in the constructor the same way
`ActionResult.succeeded()` requires postcondition evidence.

---

## How the scan works

`pz-agent doctor` runs a **read-only** scanner over the game's local Lua
directory and builds a symbol index.

Hard constraints, enforced in code and asserted in tests:

- It never copies a game file anywhere.
- It never writes into the game directory.
- The report records symbol names, relative paths, a signature line and a
  sha256 of each file — **never the file's contents**. Vendoring game source is
  forbidden by licence and by this project's rules, and a scanner that quietly
  embedded a source snippet would violate both. There is a test asserting the
  report contains no line of the scanned source.
- It is bounded: a cap on files scanned and bytes read, with truncation
  reported rather than silently applied.

The extractor is line-based and tolerant. It recognises `function X:y(...)`,
`function X.y(...)`, `X = X or {}` declarations and
`ISBaseTimedAction:derive("...")`. It is not a Lua parser and does not try to
be — a tolerant extractor that reports what it found beats a strict one that
fails on the first unusual file.

---

## Probes

| Capability | What it gates | Default state |
| --- | --- | --- |
| `move_to_square` | `movement.move_to`, `movement.move_near` | `available_unverified` |
| `inventory_transfer` | `inventory.transfer`, `inventory.ensure_main` | `available_unverified` |
| `eat_percentage` | Partial eating; without it, whole units only | `available_unverified` |
| `drink_carried` | `consume.drink` from a carried container | `available_unverified` |
| `read_literature` | `literature.read` | `available_unverified` |
| `equipment_equip` | `equipment.equip` | `available_unverified` |
| `equipment_unequip` | `equipment.unequip` | `available_unverified` |
| `medical_bandage` | `medical.bandage` | `available_unverified` |
| `survival_rest` | `survival.rest` | `available_unverified` |
| `survival_sleep` | `survival.sleep` | **`experimental`** |
| `drink_world_source` | `consume.drink_source` — filling a vessel at a sink, well or rain collector and drinking from it | **`experimental`** |
| `autonomous_attack` | — | **`unsupported`** (`NO_VERIFIED_API`) |

Twelve, which is what `pz_agent_core.capabilities.probes.PROBES` holds. This
table listed seven for a while and omitted the five equipment, medical and
survival rows entirely — including `survival_sleep`, whose ceiling is the one a
reader most needs to know about.

**Two are experimental, not one.** `experimental` means upgradeable but not
usable: the tool is not published and the action is refused until a live ack
confirms it. `drink_world_source` is capped because §12.4 lists the world water
action as unconfirmed. `survival_sleep` is capped for a sharper reason — once
the character is asleep there is no timed action to interrupt and no queue entry
to cancel, so a panic stop cannot reach them.

`autonomous_attack` is permanently unsupported. It is listed so the report is
explicit about it rather than silent.

`eat_percentage` is a good example of why this matters. If percentage eating is
verified, the food policy picks a fraction so the character neither overeats nor
wastes a large item on a small need. If it is not, the policy falls back to
whole units — and says so in the rationale, rather than silently rounding and
letting you wonder why half a ham disappeared.

---

## Build changes

A capability report is stamped with the build it was established against.
Loading a report from a **different** build downgrades every `verified` entry.

This is not conservatism for its own sake. Point releases move Lua around; a
`verified` flag carried across a build boundary is a claim nobody checked. The
downgrade is tested explicitly.

`pz-agent doctor` warns — rather than hard-failing — when the detected build is
outside `SUPPORTED_BUILDS`. A point release usually keeps the API surface we
rely on, so refusing to start would be more annoying than useful; running with
every capability downgraded to `available_unverified` is the honest middle.

---

## What `pz-agent doctor` checks

| Check | Why it exists |
| --- | --- |
| Game installation found | Across *all* Steam libraries, not just the default one |
| Build detected | Reported explicitly; never guessed |
| User `Zomboid` directory found | Honours a custom home directory and non-ASCII paths |
| Directory permissions | The mod cannot write the IPC files without them |
| Mod installed and enabled | The most common cause of "nothing happens" |
| Heartbeat present | Distinguishes "mod not loaded" from "no save loaded" |
| IPC directory writable | Same |
| Timed actions available | The capability probes above |
| Conflicting old files | A previous install's IPC files can look like a live session |
| Active session | Whether a save is actually loaded |

Every check has a stable code and remediation text, so a failure tells you what
to do rather than that something is wrong.

---

## The journal

When a capability's state changes — a new build, a probe that now succeeds, a
capability disabled by policy — the change is recorded with its evidence. The
report is machine-readable; `pz-agent doctor --json` emits it directly.

Nothing in this repository claims engine compatibility that a probe has not
established. `tests/lua/` proves the mod's *logic* under mocked globals; it
proves nothing about the engine, and the test suite says so in as many words.
