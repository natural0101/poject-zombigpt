# Evidence index

Every path a step cites, with what it shows. `scripts/check_progress.py` refuses
a `PASS` whose evidence path does not exist, so this index and the tree agree by
construction.

## Steps 1–10 — baseline

| Path | What it shows |
| --- | --- |
| `docs/control/evidence/step-01-10/branches.txt` | every branch and its SHA at the start |
| `docs/control/evidence/step-01-10/windows-workflow-runs.txt` | the ten most recent Windows runs, all red |
| `docs/control/evidence/step-01-10/windows-failures.txt` | the 24 failing tests, named |
| `docs/control/evidence/step-01-10/linux-baseline.txt` | 3677 passed, 2 skipped at `873037c0` |
| `docs/control/PLAN.md` | the 100 steps |
| `docs/control/STATUS.json` | the recorded state |
| `scripts/check_progress.py` | the gate |
| `scripts/progress_report.py` | the counter |

## Steps 11–19 — evidence bytes are portable

| Path | What it shows |
| --- | --- |
| `docs/control/evidence/step-11-19/evidence-suites.txt` | the four affected suites, green after the fix |
| `docs/control/evidence/step-11-19/mutations.txt` | both mutations and the tests each turns red |
| `docs/control/evidence/step-11-19/linux-after.txt` | 3712 passed, 2 skipped after the fix |
| `tests/contract/test_evidence_bytes_are_portable.py` | 11 cross-platform digest tests |
| `tests/contract/test_windows_path_shapes.py` | Windows path shapes, asserted on Linux |
| `tests/contract/test_mcp_snippet_is_json.py` | the printed MCP block parsed back, 5 interpreter paths |

## The release candidate

The digest is the identity: an RC is *this* archive, from *this* commit, by
*this* run, and a claim about "the RC" that names none of the three is a claim
about nothing.

| Field | Value |
| --- | --- |
| archive | `pz-agent-windows-rc` (artifact 9229901035), `pz-agent-windows-v1.0.0-rc1.zip`, 77 entries |
| archive sha256 | `725ba4094806f2cd0ad222e80d7dfa77789596d13b5ee4401b12a7e0a880ebc6` |
| source commit | `25ee8921d42634f9bf1876ee747800e76799c3bd` |
| workflow run | https://github.com/natural0101/poject-zombigpt/actions/runs/31827800146 |
| certified by | `check_release.py --rc` printing `CERTIFIED v1.0.0-rc1: 8 check(s) passed` — archive complete, all 11 wrappers at the root, **both executables in `bin/`**, 76 file digests matching, 8540 of 8608 tests passed with no failures, 31 MCP end-to-end testcases green, and the archive claiming no live-test evidence. The packaged pair also completed an MCP `initialize` over the RPC link with `PATH` reduced to the system directories |
| what this RC does *not* certify | skips have moved 49 → 63 → 68 over the last three RCs, and every one of the nineteen added is a seam check that runs the mod's Lua and finds no interpreter on this runner: fourteen for the protocol tables at `1a5feb4`, five for the action-ack round trip at `8a803c2`. They join `test_adapter_args_agreement`, `test_capability_declaration_agreement` and the observation round trip, so **no seam check has ever been part of RC certification** — the count moves whenever a seam gains coverage, never because something stopped working. All five run on Linux CI and locally; each verifies a contract between the mod and the sidecar that has nothing to do with the host OS, and putting Lua on the release runner would add a dependency to the release path for coverage already taken elsewhere. Stated here so the growing skip count is read as what it is |
| current? | `docs/control/STATUS.json` → `release_candidate.status`; any code commit after the source commit makes it STALE until the workflow rebuilds it |
