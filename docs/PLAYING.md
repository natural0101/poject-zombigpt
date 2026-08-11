# Playing: one command from a cold start to an armed agent

`pz-agent play` is sections 6 and 7 of [`QUICKSTART.md`](QUICKSTART.md) in a
single command. It starts the sidecar if none is running, waits — bounded — for
you to launch the game and load a save, asks for authority, and then waits for
**the game itself** to say the authority was granted.

It composes the commands you already have. There is nothing `play` can do that
`start` and `arm` cannot; what it adds is the waiting, and an honest report of
which step stopped.

```powershell
.venv\Scripts\pz-agent play
```

That is the whole thing. Walk the steps by hand once, from `QUICKSTART.md`,
before you trust a command that does five of them without stopping.

---

## What it actually does

Five steps. Each one either observes what it was waiting for or ends the
command; none of them reports success on the strength of having asked.

**1. It validates `config.toml`.** The same gate `pz-agent start` applies, and
for the same reason: a sidecar that starts and then refuses every action on a
bad setting has spent your attention to tell you something the validator could
have said in a second. Nothing is started when this fails.

**2. It makes sure a sidecar is running.** If `pz-agent status` would say one is,
that one is reused — two loops on one exchange directory interleave the command
stream. Otherwise a detached sidecar is started exactly as `pz-agent start`
starts one, and `play` prints where its spawn log lives, because a child process
with no terminal has nowhere else to put a traceback.

**3. It waits for the game.** It prints the instructions once, before the wait,
and then polls the game's heartbeat about once a second until the mod has
attached to this session. `--wait-game` sets the ceiling in seconds (default
300, maximum 3600). The wait is bounded twice over — a deadline *and* a count of
polls — so a machine whose clock stops or steps backwards cannot turn it into a
hang.

Nothing here launches Project Zomboid. Starting your game, enabling the mod and
choosing a save stay yours; `play` waits for them and says so.

**4. It arms.** It writes the same single-shot control request `pz-agent arm`
writes, and then waits up to 30 seconds for the game's heartbeat to report
`armed=true` **in the mode that was asked for**. The sidecar's own two-phase arm
is what grants it: the loop sends a `session.arm` command to the mod and only
grants authority when the mod acks it *and* a fresh heartbeat confirms it. `play`
only reports that outcome. If the heartbeat already reports the mode you asked
for, no request is written at all.

`--observe` skips this step entirely and leaves the session where `start` leaves
one — attached, reading, acting on nothing.

**5. It prints what it observed**: the mode, the session id, the game build, and
where to go next.

---

## The flags

```powershell
.venv\Scripts\pz-agent play --mode assisted
.venv\Scripts\pz-agent play --mode autonomous
.venv\Scripts\pz-agent play --observe
.venv\Scripts\pz-agent play --wait-game 600
.venv\Scripts\pz-agent play --json
```

`--mode` is what to ask for; `assisted` is the default and the right first
session. `--observe` arms nothing. `--wait-game` is the ceiling on step 3 only —
the arm confirmation has its own fixed 30-second bound. `--json` prints one
document on stdout and sends the progress notes to stderr, so a script reads the
document and a human still sees that the command is waiting rather than wedged.

The `--json` document is `{played, mode, session_id, build, game, armed}` when
the command succeeded, and `{played: false, detail}` for every refusal below.

---

## Everything it can refuse, and what to do about it

Each of these is the whole message, and each ends the command. `play` never
carries on past one.

### The configuration does not validate

```
3 configuration error(s) in <Zomboid>\pz-agent\config.toml: the sidecar was not
started and nothing was armed. Run pz-agent validate-config to see them.
```

Without `--json` the individual errors are printed above it, in the form
`validate-config` uses. Fix them and run `play` again. Nothing was started.

### There is no Zomboid directory

```
no Zomboid directory was found, so there is no exchange directory to attach to.
Run pz-agent doctor and read PZD003.
```

The profile could not be found, so there is nowhere for the mod and the sidecar
to talk. `pz-agent doctor` distinguishes "the game is not installed" from "the
profile has moved"; `game.user_dir` in `config.toml` is the documented override.

### `--wait-game` is not a number of seconds

```
--wait-game is a number of seconds between 1 and 3600, and 0 is not one.
Nothing was started.
```

Exits 2, like any other malformed invocation. A wait with no ceiling is the one
thing this command will not offer.

### The sidecar could not be started

```
the sidecar could not be started: <what the supervisor said>. Anything it
printed on the way out is in <Zomboid>\pz-agent\sidecar.out. Nothing was armed.
```

Read `sidecar.out`. A child that died during import wrote the reason there and
nowhere else. `pz-agent doctor` is the next step if it is empty.

### The game never appeared

```
the sidecar is running and the game never appeared: after 300 s the game
heartbeat says <what was read>, and no session is attached. Nothing was armed.
Launch Project Zomboid, load a singleplayer save with PZ Agent Bridge enabled,
then run pz-agent play again.
```

This is a **failure**, exit 1, not a quiet success — the sidecar is up and there
is no game behind it. The three usual causes, in the order worth checking:

- the mod is not enabled: **Mods → PZ Agent Bridge**, then restart the game.
  Enabling a mod does not affect a session that is already loaded;
- no save is open. A game sitting on the main menu writes no heartbeat, which
  looks exactly like a game that is closed;
- the save is multiplayer. It is refused at the handshake, by design.

The sidecar is left running. Fix the cause and run `play` again; it will reuse
that sidecar rather than start a second one.

### A panic-stop sentinel is present

```
a panic-stop sentinel is present; clear it in the game before arming. play does
not clear it and no command does — the latch is the game's. Nothing was
requested and nothing was armed.
```

You (or the reflex guard) hit the panic stop. It is a level, not an edge: while
the sentinel is there the loop disarms on every tick and starts nothing. It is
cleared in the game and nowhere else, and no flag on `play` will override it.
Nothing was written to the control file — the refusal happens before the request.

### The arm was requested and never confirmed

```
the arm into ASSISTED was requested and never confirmed within 30 s: the game
reports not armed, in OBSERVE, and the sidecar says <what status would say>.
Nothing was forced. Run pz-agent status for the loop's own reason, and pz-agent
arm to ask again.
```

The request reached the control file and the game did not confirm it. `play` does
not withdraw it — the loop may yet answer — and it never sets `armed` itself.
`pz-agent status` prints the loop's own reason, which is usually one of: the game
went silent, the journal was torn, the session is multiplayer, or the reflex
guard demanded a disarm on the same tick.

---

## The cycle: play, watch, goal, stop

```powershell
.venv\Scripts\pz-agent play
.venv\Scripts\pz-agent status --watch
.venv\Scripts\pz-agent goal status
.venv\Scripts\pz-agent stop
```

`play` leaves you armed. From there:

- **`pz-agent status --watch`** redraws the session — the heartbeats, the mode,
  the capability revision, the backup covering this save, and the goal queue when
  it is reachable — until you interrupt it. This is the window to leave open in a
  second terminal while you play.
- **`pz-agent goal status`** says what the agent is working on right now and what
  is queued behind it. Goals arrive from an MCP client or from the voice
  companion; see [`MCP_TOOLS.md`](MCP_TOOLS.md) and [`VOICE.md`](VOICE.md).
- **`pz-agent stop`** ends the session cleanly: the loop releases its lock,
  closes in-flight work and unpublishes the Core RPC link.

Between those, three things end automation without a command: the panic hotkey
in game (**F12**), the word "стоп" to the voice companion, and **moving your
character yourself** — manual input cancels automation immediately.

To hand authority back without ending the session, `pz-agent disarm` returns the
sidecar to `OBSERVE`; `pz-agent play --mode autonomous` is how you raise it once
you trust the assisted session. Read the envelope in
[`SAFETY.md`](SAFETY.md) before you do, and take a backup first —
`pz-agent backup-save`.

## When it does not work

[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) covers the doctor codes.
`pz-agent logs` prints the recent diagnostics, and `pz-agent logs --bundle
--verify` builds a redacted archive and prints its contents before you attach it
to anything public.
