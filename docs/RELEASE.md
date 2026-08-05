# Release

A release is a claim that the software does what the documentation says. The
gate below exists so that claim is checkable rather than felt.

---

## Versions

Five, moving independently:

| Version | Source of truth | Restated in |
| --- | --- | --- |
| Product | `version.PRODUCT_VERSION` | `pyproject.toml`, `CHANGELOG.md` |
| Protocol | `version.PROTOCOL_VERSION` | `schemas/command.schema.json`, `Protocol.lua` |
| Schema | `version.SCHEMA_VERSION` | `schemas/*.schema.json`, `Protocol.lua` |
| Mod | `version.MOD_VERSION` | `pz-mod/42/mod.info`, `pz-mod/mod.info` |
| Supported builds | `version.SUPPORTED_BUILDS` | `docs/COMPATIBILITY.md` |

`scripts/check_versions.py` fails the build when they drift. Bump only what
actually changed — a protocol bump for a docs typo makes the version meaningless
as a compatibility signal.

**Protocol major** bumps break compatibility: the mod refuses a session whose
major differs. Renaming a reason code is a major bump, because clients key their
recovery tables on those strings. Adding a code is a minor bump; the codes are
append-only.

---

## Gate

Every item is a hard requirement.

### Code

- [ ] `scripts/check.sh` green on a clean checkout — not on your working tree
- [ ] CI green on all matrix entries (3.11, 3.12, luacheck, Lua tests, build)
- [ ] `git status` clean
- [ ] No stub on any critical path (`scripts/check_forbidden.py`)
- [ ] No secrets anywhere in history, not merely in HEAD
- [ ] No game source, assets or saves in the tree
- [ ] Versions in sync (`scripts/check_versions.py`)

### Evidence

Nothing here can be asserted from memory. Each produces an artefact.

- [ ] **Compatibility report** from a real installation, stamped with the build
- [ ] **Doctor output** — all checks accounted for, failures explained
- [ ] **Contract test report** — both directions, all enums in parity
- [ ] **Game smoke evidence** — S01–S15, each with the artefact its scenario
      names, against the build being released
- [ ] **Endurance report** — 30 minutes, asserting the absences in
      `tests/game-smoke/S99_endurance.yaml`

Any scenario that was not run is listed as **not run**. Not "expected to pass",
not silently omitted. A release with unrun scenarios is legitimate; a release
that implies it ran them is not.

### Documentation

- [ ] `CHANGELOG.md` updated, newest heading matches `PRODUCT_VERSION`
- [ ] `docs/PROGRESS.md` reflects reality, including what still needs a live
      session
- [ ] `docs/LIMITATIONS.md` reflects what is actually unsupported this release
- [ ] `docs/COMPATIBILITY.md` names the builds probed
- [ ] Every documented command exists and behaves as documented

That last one is worth doing by hand. Documentation drift is the failure mode
that no linter catches and every user hits.

### Artefact

- [ ] `uv build` produces wheel and sdist
- [ ] Windows package built with the launcher, mod files, installer and
      uninstaller
- [ ] Installs and runs **without administrator rights**
- [ ] Uninstaller leaves saves, backups and configuration alone
- [ ] `SHA256SUMS` published alongside
- [ ] Unsigned-binary warning documented (signing is out of scope)

---

## Cutting it

```bash
# 1. From dev, with everything green
git checkout dev && scripts/check.sh

# 2. Bump the versions that changed
$EDITOR packages/pz_agent_core/src/pz_agent_core/version.py
$EDITOR pyproject.toml pz-mod/42/mod.info pz-mod/mod.info
.venv/bin/python scripts/check_versions.py

# 3. Move [Unreleased] under the new heading
$EDITOR CHANGELOG.md

# 4. Write the final report from the evidence you actually have
$EDITOR FINAL_IMPLEMENTATION_REPORT.md

# 5. Merge to main and tag
git checkout main && git merge --no-ff dev
git tag -a v0.1.0 -m "pz-agent 0.1.0"
git push origin main --tags

# 6. Build and checksum
uv build --out-dir dist
sha256sum dist/* > dist/SHA256SUMS
```

---

## The final report

`FINAL_IMPLEMENTATION_REPORT.md` accompanies each release and must state:

1. What is implemented — by task id.
2. Which game APIs are **confirmed**, against which build, by which probe.
3. Which tests ran and what they returned.
4. Which smoke scenarios ran, with their evidence, and which did not.
5. Known limitations.
6. Exact commands to install and run.
7. The commit hash.
8. Where the release artefact is and its checksum.
9. **Every step that physically requires the user to launch the game**, listed
   individually.

Point 9 is the one that keeps the rest honest. A release note that says
"tested" without naming what a human still has to do is hiding the boundary
between what was verified and what was assumed.

Equally, the report may not end with "the architecture is ready", "it only
needs in-game testing", or "the user can take it from here". State what is
implemented, what is verified, and the precise remaining manual steps.

---

## After release

Watch for: doctor failures on builds outside `SUPPORTED_BUILDS`, capability
downgrades after a game update, and any report of a duplicated action — that
last one means the idempotency path failed, which is the most serious class of
bug this system can have.
