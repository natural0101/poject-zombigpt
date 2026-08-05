# Testing

```bash
scripts/check.sh          # everything CI runs, in the same order
scripts/check.sh fast     # skip integration tests
```

A green run here is a green run in CI. If they disagree, that is a bug in
`check.sh`, not a reason to push and see.

---

## What the gate actually checks

| Step | Tool | Catches |
| --- | --- | --- |
| format | `ruff format --check` | Diff noise |
| lint | `ruff check` | Bugs, security patterns, unsorted imports, stray `print` |
| types | `mypy --strict` | Every untyped surface |
| forbidden | `scripts/check_forbidden.py` | Stubs, banned primitives, committed secrets |
| versions | `scripts/check_versions.py` | The five versions drifting apart |
| schemas | `scripts/check_schemas.py` | A schema that no longer compiles |
| tests | `pytest` | Behaviour |
| lua | `luacheck` + `lua5.4 tests/lua/*.lua` | Mod logic |

Three of those are specific to this project and worth explaining.

**`check_forbidden.py`** walks the AST of every shipped file. It fails on a
function whose body is a bare `pass`, a `NotImplementedError`, a `...` outside a
`typing.Protocol`, or a `TODO`/`FIXME`/`XXX`/`HACK` marker; on `eval`, `exec`,
`compile`, `os.system`, `os.popen`, `shell=True`, `pickle.load`, and Lua
`loadstring`; and on anything matching a secret pattern in any tracked text
file. The stub rules exist because "I'll fill that in later" is how a critical
path acquires a silent no-op, and the banned-primitive rules exist because the
LLM boundary must not be able to reach any of them.

**`check_versions.py`** holds five independently-moving versions in sync —
product, protocol, schema, mod, supported build — across `version.py`,
`pyproject.toml`, `mod.info`, the schema consts and the changelog. A mod that
reports a version the sidecar does not expect is a support ticket that looks
like a crash.

**`check_schemas.py`** asserts each document in `schemas/` still compiles as
Draft 2020-12. A broken `$ref` does not error at validation time — it silently
makes the validator accept everything, which is worse than failing.

---

## Levels

### `tests/unit/` — pure logic

No filesystem, no game, no clock you did not inject. Food scoring, drink
selection, literature choice, reference parsing, priority arbitration, sequence
handling, TTL, idempotency, schema migrations.

Files are prefixed by subsystem (`test_policy_*`, `test_ipc_*`, `test_safety_*`)
so it is obvious what a failure belongs to.

### `tests/contract/` — wire-format conformance

Marked `@pytest.mark.contract`. Checked in **both directions**, and both matter:

- **outbound** — what `to_dict()` emits validates against the schema, or the mod
  rejects our commands at runtime;
- **inbound** — what the schema permits parses, or we reject the mod's
  observations at runtime.

Plus the enum-parity tests: every `ActionName` is in the schema's enum and vice
versa, every `ActionStatus` and every `ReasonCode` serialises. Those catch the
drift that a one-directional test misses entirely.

The inbound fixtures are written by hand rather than produced by our own
serialiser. A round-trip through your own encoder proves the encoder is
self-consistent, not that it matches the contract.

### `tests/integration/` — more than one subsystem

Marked `@pytest.mark.integration`. Uses a fake mod bridge: real IPC files in a
`tmp_path`, real journal reader, real action engine, no game. This is where
recovery scenarios live — sidecar restart, torn snapshot, gap in the sequence,
duplicate command replay.

### `tests/lua/` — mod logic without the engine

Plain `lua5.4 tests/lua/<file>.lua`, no busted and no luarocks, because CI
should not need a Lua package manager to check a JSON encoder. `tests/lua/support/`
holds a small assertion helper and mocks of the engine globals the code touches.

**These tests prove logic, not compatibility.** They cannot demonstrate that
`ISInventoryTransferAction` behaves as expected in Build 42.20 — only a live
session does that. No comment, docstring or report in this repository may claim
otherwise.

### `tests/game-smoke/` — requires a live session

Marked `@pytest.mark.game_smoke` and skipped by default. These are the steps
that physically cannot be automated away, each with an explicit evidence
requirement.

| ID | Scenario | Evidence that closes it |
| --- | --- | --- |
| S01 | Heartbeat | `heartbeat.game.json` updating with the live build |
| S02 | Stop | Queue cleared of mod-owned entries only, agent disarmed |
| S03 | Move 3 tiles | Position within the target radius, correct floor |
| S04 | Backpack → main inventory | Item ref resolves in the destination container |
| S05 | Eat from backpack | Hunger decreased; item uses decremented |
| S06 | Drink | Thirst decreased or container volume decreased |
| S07 | Read | Reading started and progress observed |
| S08 | Cancel reading | Action terminated, pages-read preserved |
| S09 | Manual takeover | Automation cancelled on the first player keypress |
| S10 | Stale sidecar | No new action starts; in-flight closes as `lost` |
| S11 | Invalid item ref | `INVALID_REF`, no action attempted |
| S12 | Path blocked | `PATH_NOT_FOUND` or `PATH_STUCK`, bounded replanning |
| S13 | Zombie interruption | Action interrupted at the threshold, not after |
| S14 | Backup / restore | Hashes verified; restore refused while the game runs |
| S15 | Restart recovery | No command replay; re-arm required |

`docs/PROGRESS.md` tracks which of these have actually been run and against
which build. An unrun scenario is listed as unrun.

### Endurance

At least 30 minutes of real time in a safe test world, asserting the absence of
things rather than the presence of them: no infinite loop, no command replay, no
unbounded log growth, no lost control, no false success, no save corruption.

Most of these are absences that only show up over time — a ring buffer that is
"bounded" but grows by one per rotation looks perfect for the first five
minutes.

---

## Writing a good test here

**Test the failure branches.** The happy path is the least interesting third of
any of these subsystems. A journal reader that handles a well-formed file is
table stakes; what matters is the partial trailing line, the corrupt record, the
rotation mid-read.

**Do not assert that something happened — assert what.** `assert result` passes
against a gutted implementation. `assert result.reason_code is ReasonCode.NO_SAFE_FOOD`
and `assert "raw" in result.rejections[item_ref]` do not.

**Inject the clock and the poll interval.** No test in this repository sleeps.
If a test needs time to pass, time is a parameter.

**Prove the bounds.** Every cache, ring buffer and retry budget has a cap.
Push past it and assert the cap held — a documented bound with no test is a
comment.

**Determinism is a test.** Selection policies must return the same choice for
the same input, including tie-breaks. Shuffle the input and assert the choice
does not move.

---

## Running subsets

```bash
.venv/bin/python -m pytest tests/unit                    # fast, pure
.venv/bin/python -m pytest -m contract                   # wire format
.venv/bin/python -m pytest -m "not integration"          # skip the slow ones
.venv/bin/python -m pytest -m game_smoke --run-game-smoke  # needs a live session
.venv/bin/python -m pytest tests/unit/test_policy_food.py -q
lua5.4 tests/lua/test_refs.lua                           # one Lua test
```
