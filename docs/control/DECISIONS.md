# Decisions

Each entry is a choice that constrains later steps, with the reason. A decision
recorded after the fact is a rationalisation; these are written when taken.

## D-001 — the control system was created after the first code changes

Steps 14–19 and 41–45 were implemented **before** `docs/control/` existed,
during the previous instruction. The plan says the control files come first.
They did not, and this file says so rather than back-dating them.

Consequence: those steps are not credited as PASS on the strength of having been
done. They are re-verified against the same rules as everything else — a test
that fails without the fix, a full suite, a commit, an evidence path — and the
evidence recorded is from runs made *after* the control system existed.

## D-002 — the 24 Windows failures split into six root causes

From `docs/control/evidence/step-01-10/windows-failures.txt`:

| Cause | Tests | Steps |
| --- | --- | --- |
| Evidence digests taken from a string, written in text mode (CRLF) | 10 | 14–19 |
| Redactor left the native separator in the placeholder | 3 | 23 |
| Redactor rule order — the profile matched before the Zomboid directory | 1 | 24 |
| Installer: manifest separator, `len(body)` for size, launcher path | 3 | 28, 29 |
| MCP config assembled from f-strings, so a Windows path is not JSON | 2 | 41–42 |
| POSIX-only calls in tests (`signal.SIGKILL`, chmod-based unreadable) | 3 | 25–27 |
| Document-root globs comparing `docs\X.md` with `docs/X.md` | 2 | 22 |

## D-003 — a Windows claim needs a green Windows workflow, and only where it is a claim

`scripts/check_progress.py` requires `windows_ci.status == "GREEN"` for steps
30, 39, 40 and 91–95 — the ones that assert Windows *works*. It deliberately
does not require it for step 12, whose evidence is a red run, nor for steps
14–29, whose own evidence is a cross-platform test. A rule that is strict in the
wrong place is one that gets loosened wholesale the first time it blocks
something legitimate.

## D-004 — regression tests for Windows defects must fail on Linux

Every fix in stage 2 is covered by a test that constructs the Windows shape
explicitly (`PureWindowsPath`, bytes with `\r\n`, an injected writer that
translates newlines) so it fails here as well as there. A regression test that
only fires on the platform nobody develops on is one that fires after the
release.
