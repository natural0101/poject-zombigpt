# Blockers

One entry per thing that stopped a step. A blocker is closed only by a fix plus
a regression test, never by a retry that happened to work.

Format: step, what failed, the exact command to reproduce it, the cause, the fix,
the regression test.

## Open

_(none)_



## Closed

### B-001 — the Windows workflow was red (step 12 evidence, blocked step 30)

- **Step:** 30
- **Reproduce:** the Windows job of
  <https://github.com/natural0101/poject-zombigpt/actions/runs/31156087690>
  (head `873037c0`), or push to a branch and read the `windows package` job.
- **Failure:** `24 failed, 3633 passed, 22 skipped`.
- **Root causes:** recorded in `docs/control/evidence/step-01-10/windows-failures.txt`
  and split in `DECISIONS.md` under D-002.
- **Status:** closed.
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
- **Closed at:** `a7c16ba2`, run
  <https://github.com/natural0101/poject-zombigpt/actions/runs/31163394899> —
  `3714 passed, 22 skipped`, and every later stage of the workflow succeeded:
  both PyInstaller builds, both executables answering, the archive and the
  release gate. Nothing skipped.
- **Not closed by:** loosening anything. No assertion was removed, no Windows
  failure became a skip, and the workflow still runs every check it ran at the
  branch point.


