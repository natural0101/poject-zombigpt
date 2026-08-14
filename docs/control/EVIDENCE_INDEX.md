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
| archive | `pz-agent-windows-rc` (artifact 9208986035), `pz-agent-windows-v1.0.0-rc1.zip`, 77 entries |
| archive sha256 | `fcf8ad28dd6ac45832fc799a70c1b541e4d18e2329b7aa0340df221d6de1d0a8` |
| source commit | `5f073e918aef7e7dec2db903d518fa987cc058b0` |
| workflow run | https://github.com/natural0101/poject-zombigpt/actions/runs/31772782919 |
| certified by | `check_release.py --rc` printing `CERTIFIED v1.0.0-rc1: 8 check(s) passed` — archive complete, all 11 wrappers at the root, **both executables in `bin/`**, 76 file digests matching, 8469 of 8513 tests passed with no failures, 31 MCP end-to-end testcases green, and the archive claiming no live-test evidence. The packaged pair also completed an MCP `initialize` over the RPC link with `PATH` reduced to the system directories |
| a number worth reading twice | the passed count did **not** move from the previous RC (8469) while collected went 8510 → 8513 and skips 41 → 44. The three new tests are `test_observation_document_round_trip`, and they skip on this runner because no Lua interpreter is on its PATH. They run on Linux CI and locally. So the observation-seam round trip is **not** evidence this artefact carries; it is evidence about the tree, taken elsewhere |
| current? | `docs/control/STATUS.json` → `release_candidate.status`; any code commit after the source commit makes it STALE until the workflow rebuilds it |
