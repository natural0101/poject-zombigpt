# Windows packaging

What ships, how it is built, and what the build does not prove.

## The artefact

`dist/pz-agent-windows-v1.0.0-rc1.zip`, unpacked into any folder the user can
write to:

```
install.bat  uninstall.bat  doctor.bat  backup-save.bat
start.bat  status.bat  stop.bat
run-live-tests.bat  resume-live-tests.bat  collect-evidence.bat  finalize-release.bat
BUILD-MANIFEST.json
bin\      pz-agent.exe, pz-agent-mcp.exe
mod\      the bridge mod, as install.bat copies it
configs\  the sample config.toml and the MCP client configurations
docs\     what the operator needs offline
evidence\schema\   the schemas live-test artefacts are validated against
README.md  LICENSE  PRIVACY.md  SECURITY.md
```

Neither Python nor git is needed to run it. The executables are one-file
PyInstaller builds; the BAT files call `bin\pz-agent.exe` when it is beside
them and fall back to `pz-agent` on PATH, so the same eleven files work from a
source install.

## Building

The two executables need Windows, because a PyInstaller build produces a binary
for the platform it runs on. Everything else runs anywhere.

```
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]" pyinstaller

.venv\Scripts\pyinstaller --noconfirm --clean ^
  --distpath packaging\windows\dist --workpath packaging\windows\build ^
  packaging\windows\pz-agent.spec
.venv\Scripts\pyinstaller --noconfirm --clean ^
  --distpath packaging\windows\dist --workpath packaging\windows\build ^
  packaging\windows\pz-agent-mcp.spec

.venv\Scripts\python packaging\windows\build_rc.py
```

`build_rc.py` prints every component, the file count, and the SHA-256 of the
archive, and writes that digest beside it as `<archive>.sha256`.

On Linux the same command works and reports the two executables as missing:

```
python packaging/windows/build_rc.py
```

The archive is still written, `BUILD-MANIFEST.json` inside it says
`"complete": false` and names every absent file, and the exit code is 1. That
is the point — a partial archive that called itself complete is the failure
this arrangement exists to prevent. Pass `--allow-incomplete` when you want the
partial archive without the non-zero exit.

Two builds of the same tree produce the same bytes: every entry is written with
a fixed timestamp and the manifest carries no clock reading. A digest that
changed because the afternoon did would be worth nothing.

## Why the entry scripts are generated

`pyproject.toml` points its console scripts at `pz_agent_cli.__main__:main` and
`pz_agent_mcp.__main__:main`. Neither module can be handed to PyInstaller
directly: they are package members using relative imports, and executing one as
a top-level script raises `attempted relative import with no known parent
package` — at runtime, inside the built binary, which is the worst place for it.
Each spec writes a three-line launcher into `packaging/windows/build/entry/`
that imports the package properly. The generated files are build output and are
not tracked.

## Line endings

The repository normalises every text file to LF (`.gitattributes`), and cmd.exe
is only dependable with CRLF — a label or a `goto` in an LF-only batch file
fails on some machines and not others. `build_rc.py` converts every `.bat` as it
writes it into the archive, so the tree keeps its rule and the artefact gets
what Windows needs. The wrappers also avoid parenthesised blocks entirely, which
is the construct that suffers most from a stray line ending.

## The executables are unsigned

Code signing is out of scope for this project, so SmartScreen will warn on first
run and some antivirus products will want a moment with the file. Neither
stripping nor UPX is applied, deliberately: a *packed* unsigned binary is the
shape those products treat as hostile, and a few megabytes are not worth a user
being told that what they just downloaded is malware.

The SHA-256 that `build_rc.py` prints is what you publish beside the archive. It
lets someone check that the file they downloaded is the file that was built. It
says nothing about what the file does.

## What a build proves

That the code compiles, packages and can be assembled into an archive whose
contents match its own manifest. Nothing more. `BUILD-MANIFEST.json` states
`"live_evidence_claimed": false` rather than leaving a reader to infer it: no
part of building this observes anything happening inside Project Zomboid.

The two bars are kept apart by `scripts/check_release.py`:

```
python scripts/check_release.py --rc      --junit pytest.xml
python scripts/check_release.py --release --junit pytest.xml
```

`--rc` wants the archive to be complete, its digests to match, and a test report
showing a green suite. `--release` wants all of that **and**
`release/evidence-manifest.json` — twenty scenarios at PASS, each artefact
hashed. That file is written only by `pz-agent live-test finalize` after the
scenarios have been run inside the game, so `--release` fails here today and
will keep failing until somebody plays the game with this attached.
