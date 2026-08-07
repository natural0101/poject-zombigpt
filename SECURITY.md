# Security policy

## Threat model

pz-agent runs on the user's own machine, against the user's own single-player
save, and is driven in part by a language model. The threats that matter are
therefore local ones.

| Threat | Mitigation |
| --- | --- |
| LLM induced to run code | The model can only emit a typed plan validated against a schema. No Lua, Python, shell, file path or keystroke can cross the MCP boundary. `eval`, `exec`, `os.system`, `shell=True`, `pickle.load` and Lua `loadstring` are rejected by a CI gate. |
| Prompt injection from in-game text | Chat, radio broadcasts, book contents, server names and mod names are treated as untrusted data. They are never concatenated into a system prompt and never interpreted as instructions. |
| Runaway automation | Every command carries a lease. The reflex guard cancels on manual input, threat, or heartbeat loss. Panic stop bypasses the queue and cannot be disabled. |
| Save corruption | A verified backup with a manifest and hashes is required before the first autonomous run. Restore refuses to run while the game is open. |
| Credential leakage | No key is ever embedded. Provider credentials come from the environment or the OS secret store. A secret scanner runs in CI over every tracked file. |
| Path and identity leakage | Absolute paths, the Windows username and Steam identifiers are redacted from logs, observations and support bundles by default. |
| Acting on someone else's server | Multiplayer is refused in config and at the session handshake. |

## Reporting a vulnerability

Open a private security advisory on the repository, or an issue that describes
the impact without a working exploit. Please include the build, the mode the
agent was in, and the relevant lines from `pz-agent logs`, which prints the
redacted view — paths under your profile and the install become placeholders.

Do **not** attach a raw support bundle to a public issue until you have checked
it with `pz-agent logs --bundle --verify`. That prints the archive's contents
after redaction and exits non-zero if anything still looks like a secret, so
what you read is what a reader would get.

## Scope

In scope: the sidecar, the MCP server, the Lua mod, the installer, and anything
that can cause the agent to act outside its declared policy.

Out of scope: the game itself, Steam, third-party mods, and any use of this
project against a server the reporter does not own.
