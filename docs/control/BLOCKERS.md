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
- **Measured progress:** 24 failures at `873037c0`; 13 at `df2db383`
  (run 31160929042); 1 at `ce70711` (run 31162211524). The last one is
  `test_verify_flags_a_member_that_reached_the_archive_unredacted`, and it was
  a real redactor defect rather than a test artefact: spellings of a literal
  path were enumerated whole, so a path mixing separators
  (`C:\Users\Иван/Zomboid` — what `f"{path}/name"` produces) matched none of
  them and fell through to the shorter `home_dir` literal. The path was still
  struck out, but under `<USER_HOME>` instead of `<ZOMBOID>`, so the same file
  produced a different line on each platform. Fixed by matching each separator
  position independently; regression tests in
  `tests/contract/test_windows_path_shapes.py`.

## Closed

_(none yet)_
