# Privacy

## What stays on your machine

Everything, unless you configure an external LLM provider.

With `provider = "none"` the agent is fully local: observation, policy, the
deterministic reflex guard and the scripted maintenance behaviours all run
without a network call. This configuration is a supported, tested path — not a
degraded mode.

## What is never transmitted or logged

By default the protocol and the logs exclude:

- your Windows username and any absolute filesystem path;
- Steam identifiers and tokens;
- in-game chat text;
- the process list or anything about other applications;
- the contents of files outside the agent's own IPC directory;
- your save data.

The save identifier that appears in observations is a truncated hash, not a
path.

## What an LLM provider would see, if you enable one

Only a *compact observation*: scalar character stats, capability flags, item
references with display names and categories, and the current goal. It is
assembled by the observation layer specifically for this purpose — the full
snapshot is never sent.

You choose the provider. The agent embeds no key and defaults to none.

## Support bundles

`pz-agent logs --bundle` produces a redacted archive: paths are replaced with
placeholders and any string matching a secret pattern is removed. Run
`pz-agent logs --bundle --verify` to print exactly what the archive contains
before you share it.

## Retention

Memory is bounded and scoped to a single save. Raw observation history is not
kept indefinitely — the store keeps a rolling window and derived facts
(known containers, home point, failed paths), with an explicit retention policy
and schema migrations. `pz-agent remember forget` clears it for the attached
save; `pz-agent remember list` shows what is held first.
