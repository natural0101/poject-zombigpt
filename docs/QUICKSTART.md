# Quick start

> **Status:** the CLI commands below are the specified interface. See
> [`PROGRESS.md`](PROGRESS.md) for which are implemented and which still need a
> live game session to verify.

**Requirements:** Windows 10/11 x64 · Steam Project Zomboid Build 42.20 Stable ·
Python 3.11 or newer · a save you are willing to test against.

Do the first run on a **test save**, not your 6-month survivor. The backup
subsystem is real and tested, but the first time you let software drive your
character is not the moment to find out you trust it.

---

## 1. Install

```powershell
git clone https://github.com/natural0101/poject-zombigpt
cd poject-zombigpt

py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

No administrator rights are needed, and none should be granted. Everything
happens in your own user directories.

## 2. Check the environment

```powershell
.venv\Scripts\pz-agent doctor
```

This finds the game across **all** Steam libraries, detects the installed build,
locates your `Zomboid` directory, checks permissions, and runs the capability
probes. Every check has a stable code and remediation text.

Read the output before continuing. A capability reported `unsupported` here
means the matching action will be honestly unavailable later — not that it will
work and then fail quietly.

```powershell
.venv\Scripts\pz-agent doctor --json    # machine-readable, for a bug report
```

## 3. Back up the save

```powershell
.venv\Scripts\pz-agent backup-save
```

Required before the first autonomous run, and a good idea before the first
assisted one. The backup records a manifest with a sha256 per file; restore
verifies every hash before writing anything and **refuses to run while the game
is open**.

```powershell
.venv\Scripts\pz-agent backup-save --list
.venv\Scripts\pz-agent restore-save <backup-id>    # game must be closed
```

## 4. Install the mod

```powershell
.venv\Scripts\pz-agent install-mod
```

Copies the bridge into `%USERPROFILE%\Zomboid\mods\pz_agent_bridge\`. It does
not touch the game installation directory, and it never copies game files
anywhere.

## 5. Enable it in game

Launch Project Zomboid → **Mods** → enable **PZ Agent Bridge** → load your save.

The mod loads in the OFF state and does nothing. A small HUD indicator shows the
mode, the session and the last error.

## 6. Attach

```powershell
.venv\Scripts\pz-agent start
```

The sidecar performs the handshake and settles in `OBSERVE`. It reads state and
takes no action.

```powershell
.venv\Scripts\pz-agent status
```

You should see a live game heartbeat, the detected build, the session id and the
capability revision. If the heartbeat is missing, the mod is not loaded or no
save is open — `pz-agent doctor` distinguishes those.

## 7. Arm it

```powershell
.venv\Scripts\pz-agent arm --mode assisted
```

`ASSISTED` executes what you ask and nothing on its own. This is the right mode
for the first session.

Now ask for something — through the MCP client you have configured, or the
voice adapter if you enabled one:

> "Eat something safe from my backpack."

What happens:

1. The observation layer enumerates your inventory, recursing into nested
   carried containers.
2. `policy/food.py` — deterministic code, not the model — filters out anything
   raw, rotten, burnt, poisonous or tainted, applies the reserve rules, and
   scores what remains.
3. If the chosen item is in a bag, a transfer to the main inventory is verified
   first.
4. The eat action is enqueued as a real timed action.
5. The engine waits and checks: **did hunger actually drop?** If yes, you get
   `succeeded` with the before/after numbers as evidence. If not, you get
   `POSTCONDITION_FAILED` — even if the mod reported success.

You will also be told *why* it chose that item, and why it rejected the others.

## 8. Stop

At any moment, any of these:

```powershell
.venv\Scripts\pz-agent stop
```

- press the panic hotkey in game (`F12` by default);
- say "stop" to the voice adapter — it bypasses everything;
- **just move.** Any manual input cancels automation immediately.

Panic stop clears only the actions the mod queued. Anything you queued yourself
is left alone.

---

## Going autonomous

```powershell
.venv\Scripts\pz-agent arm --mode autonomous
```

In `AUTONOMOUS` the agent maintains hunger and thirst inside the policy
envelope: bounded radius, risk classes it has been granted, short plans, one
verified step at a time. It re-observes between every step and yields
immediately to you.

It will not travel outside the configured radius, open world containers unless
`P3` is granted, or do anything in the `P4` tier at all.

## Configuration

```toml
[game]
channel = "stable"
expected_build = "42.20"

[session]
default_mode = "observe"
require_backup = true

[safety]
panic_hotkey = "F12"
manual_takeover = true
max_autonomous_radius = 30
allow_multiplayer = false        # refused anyway, at the handshake

[planner]
provider = "none"                # fully local; no key, no network
max_steps = 8

[voice]
adapter = "teamon"
enabled = false
```

```powershell
.venv\Scripts\pz-agent validate-config
```

Configuration is validated before start, not on first use.

`provider = "none"` is a supported, tested path — observation, policy, the
reflex guard and scripted maintenance all work with no network call. An external
provider is opt-in, and the agent embeds no key: credentials come from the
environment or the OS secret store.

## Uninstall

```powershell
.venv\Scripts\pz-agent stop
.venv\Scripts\pz-agent uninstall-mod
```

Removes the bridge and its IPC directory. Your saves, backups and configuration
are left alone; delete the backup directory yourself if you want it gone.

## When something goes wrong

```powershell
.venv\Scripts\pz-agent logs                      # recent, human-readable
.venv\Scripts\pz-agent logs --bundle --verify    # redacted archive, printed first
.venv\Scripts\pz-agent replay <trace>            # step through what it did
```

`--verify` prints exactly what the archive contains after redaction. Run it
before attaching anything to a public issue.

See [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).
