# Progress

Live status of the task graph in
[`docs/blueprint/task_graph.yaml`](blueprint/task_graph.yaml). This file is the
handover point between work sessions: read it first, update it last.

**Legend** — `done` implementation + tests + docs complete and `scripts/check.sh`
green · `wip` in progress · `todo` not started · `live` blocked on a step that
physically requires a running game.

Last updated: T001, T005 complete.

## Status

| Task | Title | Phase | Depends on | Status |
| --- | --- | --- | --- | --- |
| T001 | Initialize repository and quality toolchain | 1 | — | **done** |
| T005 | Define protocol domain models and JSON schemas | 1 | T001 | **done** |
| T002 | Detect Project Zomboid installation and user directory | 0 | T001 | todo |
| T003 | Build local API compatibility scanner | 0 | T002 | todo |
| T004 | Implement doctor CLI | 0 | T002, T003 | todo |
| T006 | Implement Lua mod skeleton and heartbeat | 2 | T003, T005 | todo |
| T007 | Implement sidecar handshake and locks | 2 | T005, T006 | todo |
| T008 | Implement command queue and acknowledgements | 2 | T007 | todo |
| T009 | Implement panic stop and manual takeover | 2 | T006, T008 | todo |
| T010 | Implement save backup subsystem | 2 | T002 | todo |
| T011 | Observe player scalar state | 3 | T006, T008 | todo |
| T012 | Observe nested inventory with stable refs | 3 | T011 | todo |
| T013 | Observe nearby world and threats | 3 | T011 | todo |
| T014 | Implement action lifecycle framework | 4 | T008, T011 | todo |
| T015 | Implement movement adapter | 4 | T013, T014 | todo |
| T016 | Implement inventory transfer adapter | 4 | T012, T014 | todo |
| T017 | Implement safe food selection and eat adapter | 4 | T016 | todo |
| T018 | Implement safe drink selection and drink adapter | 4 | T016 | todo |
| T019 | Implement literature selection and read adapter | 4 | T016 | todo |
| T020 | Implement deterministic reflex guard | 6 | T009, T013, T014 | todo |
| T021 | Implement MCP server | 5 | T011, T014 | todo |
| T022 | Implement permission and autonomy policy | 6 | T017–T020 | todo |
| T023 | Implement typed planner and critic | 7 | T021, T022 | todo |
| T024 | Implement memory store | 7 | T012, T013, T023 | todo |
| T025 | Implement TeamON voice adapter interface | 8 | T021, T023 | todo |
| T026 | Implement installer and launcher | 9 | T004, T006, T007, T010 | todo |
| T027 | Implement diagnostics and support bundle | 9 | T004, T008, T014 | todo |
| T028 | Build live game smoke harness | 9 | T015–T019 | todo |
| T029 | Run endurance and recovery tests | 9 | T020, T022, T028 | todo |
| T030 | Produce release artifact and final report | 9 | T021, T025–T027, T029 | todo |

## Completed in detail

### T001 — repository and quality toolchain

Package layout under `packages/`, `pyproject.toml` with a dependency-free core,
ruff (format + lint, security and stub rules on), mypy in strict mode, pytest
with contract/integration/game-smoke markers, `.luacheckrc` with the engine
globals enumerated, and a GitHub Actions matrix over Python 3.11/3.12 plus a
Lua job.

Three gates beyond the usual linting, all runnable standalone and all wired
into `scripts/check.sh`:

- `scripts/check_forbidden.py` — AST-level scan for stub bodies, `TODO` markers
  in shipped code, `eval`/`exec`/`shell=True`/`loadstring`, plus a secret
  scanner over every tracked text file.
- `scripts/check_versions.py` — the five versions must agree across
  `version.py`, `pyproject.toml`, `mod.info`, the schema consts and the
  changelog.
- `scripts/check_schemas.py` — every schema must itself compile as Draft 2020-12.

### T005 — protocol domain models and schemas

`pz_agent_core.protocol` holds the shared vocabulary:

- `enums.py` — closed vocabularies mirrored by the schemas: action names,
  ack statuses with a terminal set, session modes, danger levels with an
  ordering, container kinds, capability states, risk classes, and the interrupt
  priority ladder from the master prompt.
- `refs.py` — session-scoped references with generation tracking. Parsing is
  done from both ends because a container reference itself contains colons; a
  naive split would corrupt world refs. `belongs_to_session` is what turns a
  reference from a previous session into `INVALID_REF` rather than a silent
  mis-resolve.
- `messages.py` — strict, total parsers for `Command`, `ActionResult` and
  `Observation`. Optional fields are omitted rather than emitted as null,
  because the observation schema is `additionalProperties: false` throughout.

The central safety invariant is encoded here: constructing an `ActionResult`
with `status = succeeded` and any reason other than `POSTCONDITION_MET` raises,
and `ActionResult.succeeded()` refuses to build without evidence. "Queued" can
therefore not be reported as "done" by construction, not merely by convention.

## Requires a live game session

Nothing yet — no adapter has reached the point of needing one. Each entry added
here must name the exact scenario id from `tests/game-smoke/` and say what
evidence closes it.

## Deviations from the blueprint

| Blueprint | Here | Why |
| --- | --- | --- |
| Python 3.12+ | `requires-python = ">=3.11"` | The build environment runs 3.11; CI tests both 3.11 and 3.12 so the 3.12 target stays honest. No 3.12-only syntax is used. |
