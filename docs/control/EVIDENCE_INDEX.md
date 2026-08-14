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
| archive | `pz-agent-windows-rc` (artifact 9203275522), `pz-agent-windows-v1.0.0-rc1.zip`, 77 entries |
| archive sha256 | `6053cbce2f2c8436c46eb45dbfcbb60dccaf91fae17011b7742bbcb382ead9cf` |
| source commit | `c98c52265164c07c2f41ceea6040202e40895715` |
| workflow run | https://github.com/natural0101/poject-zombigpt/actions/runs/31756579175 |
| certified by | `check_release.py --rc` printing `CERTIFIED v1.0.0-rc1: 8 check(s) passed` — archive complete, all 11 wrappers at the root, **both executables in `bin/`**, 76 file digests matching, 8468 of 8509 tests passed with no failures, 31 MCP end-to-end testcases green, and the archive claiming no live-test evidence. The packaged pair also completed an MCP `initialize` over the RPC link with `PATH` reduced to the system directories. The suite is 8509 rather than the 8508 the previous RC counted because the retraction added a second positive control to the dead-gate ledger |
| current? | `docs/control/STATUS.json` → `release_candidate.status`; any code commit after the source commit makes it STALE until the workflow rebuilds it |
