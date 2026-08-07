# Handoff

## To the local agent (steps 96–98)

Nothing in this environment can produce evidence for a live scenario. Start at
[`docs/LOCAL_AGENT_PROMPT.md`](../LOCAL_AGENT_PROMPT.md); the operating
procedure is [`docs/LOCAL_GAME_HANDOFF.md`](../LOCAL_GAME_HANDOFF.md).

The local agent should **not** need to write MCP transport, voice routing,
sidecar IPC, the Windows build or evidence hashing. If any of those turns out to
be missing, that is a defect in steps 1–95 and belongs back here, not there.

## State

Read `STATUS.json`. `next_step` is where to resume. Anything `BLOCKED` has an
entry in `BLOCKERS.md` with a reproduction command.
