# MCP client configurations

Three ready configurations for the stdio server, plus the one thing you need to
know before you paste any of them in.

## What happens when a client launches this

`pz-agent-mcp` connects to a running sidecar over the Local Core RPC link
(`docs/CORE_RPC.md`). When it cannot, **which refusal you get tells you what to
do next**, so every one has its own exit code.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Served, or answered a question about the surface. | — |
| 1 | No core services, and none could be reached. | Start the sidecar: `pz-agent start`. |
| 2 | The invocation was malformed. | Read `--help`. Passing both `--state-dir` and `--zomboid-dir` lands here: they are two ways of naming one directory, and honouring one silently would connect you to a sidecar you did not ask for. |
| 3 | The MCP SDK is not installed. | `pip install pz-agent[mcp]`. |
| 4 | The descriptor names a process that is gone. | The sidecar stopped without cleaning up. `pz-agent start`. |
| 5 | The sidecar speaks a different protocol major. | Two installs are present; both halves ship together, so this means one is stale. Reinstall. |
| 6 | The descriptor is unreadable or not ours. | Have it rewritten: stop and start the sidecar. Restarting a running one will not fix it. |
| 7 | The sidecar answered, and the answer could not be read. | A version skew or a corrupted link. `pz-agent doctor`. |
| 8 | No state directory could be determined. | Name one with `--state-dir`, or the Zomboid user directory with `--zomboid-dir`. |

Codes 3 and 8 fire before anything is dialled, so they are what you meet on a
machine that has never run the sidecar. Codes 4 to 7 mean the link was tried.

(An earlier revision of this section described only two gates and said you would
see status 1. On a machine without the extra you see 3, and the message is about
a missing package rather than a missing sidecar. A client author diagnosing the
wrong thing is what a first-contact document exists to prevent —
`tests/contract/test_mcp_exit_codes_documented.py` pins every code above against
the constant it names, so this table cannot fall behind the executable again.)

So today these configurations do two useful things: they are the exact shape a
client needs, and they let you confirm the executable is where the client
thinks it is. What answers right now, with no game, no sidecar and no SDK, is:

```
pz-agent-mcp --describe
```

which writes the whole published surface — every tool and resource, with the
protocol and product versions — as JSON. That is the document to read when
writing a client, and it is generated from the same catalogue the server
serves, so it cannot drift from it.

## The files

| File | For | Command it runs |
|---|---|---|
| `claude-desktop.json` | Claude Desktop (`claude_desktop_config.json`) | `pz-agent-mcp` |
| `claude-code.json` | Claude Code (`.mcp.json`) | `pz-agent-mcp` |
| `generic-stdio.json` | any stdio client | `python -m pz_agent_mcp` |

Merge the `mcpServers` entry into the file your client already has rather than
replacing it. The server registers itself as **`pz-agent`**; that name is what
appears in the client, and it is the same string the server sends in its
initialisation, so keep it.

`pz-agent-mcp` is a console script declared in `pyproject.toml`, so it is on
PATH in any environment the project is installed into. From the Windows release
ZIP there is no such environment — give the full path to the executable
instead:

```json
{
  "mcpServers": {
    "pz-agent": {
      "command": "C:\\pz-agent\\bin\\pz-agent-mcp.exe",
      "args": [],
      "env": {}
    }
  }
}
```

Backslashes are doubled because it is JSON. A forward-slash path works too and
avoids the question.

## Why `env` is empty

Because the server reads no environment variable of its own. Its paths come
from the same discovery the CLI uses — `USERPROFILE`, `OneDrive`, `HOME`,
`USERNAME` — which a client-launched process inherits from the client. Naming a
variable here that nothing reads would look like configuration and be
decoration, and the first person to change it would spend an evening finding
out that it does nothing.

If the game is in a place discovery cannot find, that is fixed once, in
`config.toml` (`game.install_dir`, `game.user_dir`), and every part of the
system reads it from there.

**No key, no token, no path to a secret belongs in these files.** The planner's
credentials are named as environment variables in `config.toml` and read from
the environment at the moment of the call; nothing in the MCP boundary needs
one.

## What the server publishes

Thirty-one tools, in seven groups. The names are stable and the schemas are served
with them:

- **session** — `pz_session_status`, `pz_session_arm`, `pz_session_disarm`
- **observation** — `pz_observe_snapshot`, `pz_observe_inventory`,
  `pz_observe_nearby`
- **query** — `pz_action_inspect_world`, `pz_action_inspect_container`,
  `pz_action_search_inventory`. These submit an action and return an action id
  like any other, and they need no arming, because the actions behind them only
  read. `pz_action_open_container` is deliberately *not* here: its name reads
  like a query, but opening a container is a timed action the character
  performs.
- **action** — `pz_action_move_to`, `pz_action_move_near`,
  `pz_action_open_container`, `pz_action_transfer`, `pz_action_ensure_main`,
  `pz_action_eat`, `pz_action_drink`, `pz_action_drink_source`,
  `pz_action_read`, `pz_action_equip`,
  `pz_action_unequip`, `pz_action_bandage`, `pz_action_rest`,
  `pz_action_sleep`, `pz_action_wait`, `pz_action_cancel`
- **plan** — `pz_plan_execute`, `pz_plan_status`
- **safety** — `pz_safety_stop`
- **memory and diagnostics** — `pz_memory_query`, `pz_debug_doctor`,
  `pz_debug_tail`

`pz_action_sleep` will usually be absent from `list_tools`. Its capability
resolves to `experimental` on a clean scan, and the reason is not caution for
its own sake: once the character is asleep there is no timed action to interrupt
and no queue entry to cancel, so a panic stop cannot reach them.

Seven resources are published beside them: `pz://session/current`,
`pz://observation/latest`, `pz://inventory/current`, `pz://capabilities`,
`pz://plan/current`, `pz://safety/status` and `pz://diagnostics/recent`. None
of them is subscribable — the server pushes no resource updates, so a client
polls and uses the `seq` each read carries.

Two behaviours are worth knowing before writing against this:

- **A tool whose capability is not usable on your install is not listed and not
  callable.** `pz://capabilities` says which ones are withheld and why, by
  name. A missing tool is a capability answer, not an error.
- **`succeeded` means a postcondition was observed.** A tool that submits an
  action answers with the action id and `accepted` while the work is queued.
  Polling is how you learn it finished; a result that says it succeeded carries
  the evidence under `data.evidence`.

`docs/MCP_TOOLS.md` documents each tool's arguments and what it verifies.
