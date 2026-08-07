# Installing pz-agent on Windows — the standalone installer

> **This is not the normal path, and it is not what the release archive
> contains.** Two installers exist in this repository and they are not
> interchangeable:
>
> | You have | Use | It gives you |
> | --- | --- | --- |
> | `pz-agent-windows-*.zip` | `install.bat` inside it | the bundled executables, the mod, the configs |
> | a Python 3.11+ checkout | [`docs/QUICKSTART.md`](../docs/QUICKSTART.md) — `pip install -e .` then `pz-agent install-mod` | the whole CLI |
> | neither, and no pip | **this document** | the mod, a configuration and a launcher, and nothing else |
>
> This installer deliberately imports nothing from `packages/`: it runs on a
> bare CPython with only the standard library, which is what makes it usable
> before anything is installed and also what limits it. It places the bridge
> mod, a `config.toml` and `Start-PZ-Agent.cmd`. It does **not** install the
> `pz-agent` command — `Start-PZ-Agent.cmd` expects one already on the machine —
> so on a machine with no `pz-agent` at all this gets the mod in place and
> nothing more.
>
> Nothing in the release archive runs this, and no shipped artefact contains it.

Everything here runs as your own user. Nothing needs administrator rights,
nothing is written to `Program Files`, and the Project Zomboid installation is
never modified — not even read. The three files this places all live under your
own `Zomboid` folder.

## What gets placed

| Path (under `%USERPROFILE%\Zomboid`) | What it is |
| --- | --- |
| `mods\pz_agent_bridge\` | the Lua bridge mod |
| `pz-agent\config.toml` | your configuration, created only if you have none |
| `pz-agent\Start-PZ-Agent.cmd` | the launcher |
| `pz-agent\installer-manifest.json` | the ledger of exactly what was placed |

The manifest is what makes uninstalling exact. It records every file with its
SHA-256, so `Uninstall-PZ-Agent.cmd` removes the files it put there and nothing
else — a file you edited after installing is left alone and reported, and your
saves, your backups, your logs and your `config.toml` are never touched.

## Install

Double-click `Install-PZ-Agent.cmd`, or from a terminal:

```text
Install-PZ-Agent.cmd
```

If your profile is somewhere the installer cannot guess — a relocated
`Documents`, a portable install started with `-cachedir` — point it at the
folder directly:

```text
Install-PZ-Agent.cmd --zomboid-dir "D:\Games\Zomboid"
```

Then:

1. Start Project Zomboid and enable **PZ Agent Bridge** in the Mods menu.
2. Load your save. Enabling a mod does not affect a game that is already loaded.
3. Run `pz-agent doctor`. It reports what it found and what it could not.
4. Run `Start-PZ-Agent.cmd`, or `pz-agent start`.

The sidecar attaches in **OBSERVE** and stays there. It performs no game action
until you explicitly run `pz-agent arm`, and it comes back up in OBSERVE after
every restart — including one that follows a crash. That is not a setting.

## Uninstall

```text
Uninstall-PZ-Agent.cmd
```

It reads the manifest and removes exactly what is in it. It refuses to run at all
if the manifest is missing: a directory this installer has no record of creating
is somebody else's, and deleting it on a guess is how an uninstaller takes a
user's work with it.

Your `config.toml` survives on purpose. Delete it by hand if you want it gone.

## The warning Windows will show you

`Install-PZ-Agent.cmd` and `Start-PZ-Agent.cmd` are **not code-signed**, and
neither is the Python script they call. Signing needs a certificate from a
commercial authority, and it is out of scope for this project — so you should
expect one of these:

* **"Windows protected your PC"** (SmartScreen) — click **More info**, then
  **Run anyway**.
* **"Do you want to allow this app to make changes?"** — you should *not* see
  this one. Nothing here requires elevation. If a prompt asks for administrator
  rights, something other than this installer is asking, and you should say no.
* A browser or antivirus quarantine on the downloaded archive, because the files
  carry the mark of the web.

Since the signature cannot vouch for the files, verify them instead. Every
release publishes SHA-256 checksums; compare before running:

```text
certutil -hashfile Install-PZ-Agent.cmd SHA256
certutil -hashfile pz_agent_installer.py SHA256
```

`.cmd` is used rather than `.ps1` deliberately. An unsigned PowerShell script is
blocked by the default execution policy, and the workaround — telling you to run
`Set-ExecutionPolicy Bypass` — trains exactly the habit that makes a machine easy
to attack. Batch files have no such gate, so nothing has to be loosened to run
this. All of the logic worth reading is in `pz_agent_installer.py`, which is
plain Python you can read end to end before running it.

## Running the installer without the wrapper

The `.cmd` files only find a Python interpreter and hand over. Anything they can
do, this does:

```text
py -3 pz_agent_installer.py install
py -3 pz_agent_installer.py uninstall
py -3 pz_agent_installer.py install --zomboid-dir "D:\Games\Zomboid" --mod-source "C:\src\pz-mod\42"
```
