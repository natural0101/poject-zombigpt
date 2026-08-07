# Blockers

One entry per thing that stopped a step. A blocker is closed only by a fix plus
a regression test, never by a retry that happened to work.

Format: step, what failed, the exact command to reproduce it, the cause, the fix,
the regression test.

## Open

### B-001 — the Windows workflow is red (step 12 evidence, blocks step 30)

- **Step:** 30
- **Reproduce:** the Windows job of
  <https://github.com/natural0101/poject-zombigpt/actions/runs/31156087690>
  (head `873037c0`), or push to a branch and read the `windows package` job.
- **Failure:** `24 failed, 3633 passed, 22 skipped`.
- **Root causes:** recorded in `docs/control/evidence/step-01-10/windows-failures.txt`
  and split in `DECISIONS.md` under D-002.
- **Status:** open. Fixes are landing under steps 14–29; the blocker closes when
  a Windows run is green, which is step 30 and nothing earlier.

## Closed

_(none yet)_
