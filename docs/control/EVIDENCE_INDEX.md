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
