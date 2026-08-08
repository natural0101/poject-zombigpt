# Today's evidence ledger (orchestrator-written)

Every merged swarm result lands here as: agent, claim, observed postcondition,
test, commit. No entry — no MERGED.

| agent | claim | observed postcondition | test | commit |
| --- | --- | --- | --- | --- |
| ORCH (pre-swarm) | a second OS process reaches the real core | child pid != parent, session_id minted this run + CHOSEN_SEQ read back over the socket; dead-link twin refuses SIDECAR_NOT_RUNNING | tests/contract/test_sidecar_serves_the_core.py | d6a7bf0 |
| ORCH (pre-swarm) | packaged pair completes MCP initialize over the link | driver exits 0 only on observed serverInfo result; negative exits 1 named | tests/unit/test_prove_packaged_link.py | d6a7bf0 |
| ORCH (pre-swarm) | the gate refuses a report without the E2E suite | tests.mcp-e2e finding both directions | tests/unit/test_check_release.py | d6a7bf0 |
| ORCH (pre-swarm) | a poll-raised drop no longer kills the server | _exchange absorbs BrokenPipeError from poll; seam test drives the Windows spelling | tests/unit/test_rpc_transport.py | 97c8e8c |
| A17 | RPC survives hostile frames, replay, stale/wrong-server descriptors | 10 adversarial tests over real servers, refusals typed and bounded, server outlives every barrage | tests/unit/test_rpc_adversarial.py | 7c34519 |
| A18 | RPC recovery: death mid-call, restart+rotation, partial frame, dead-pid descriptor | 5 recovery tests over real sockets; descriptor lifecycle split pinned at the CLI layer via unpublish_rpc | tests/unit/test_rpc_recovery.py | 7c34519 |
| A44 (audit) | movement.move_to/move_near fail on first real walk poll | live probe: real MoveTo through real ActionRuntime → INTERNAL_ERROR from pollWalk's "running" string bypassing Toolkit.declare | fix in flight | — |
| A45 (audit) | observation.action.action_id is written by no Lua file | grep + consumer guards: targeted cancel-verify degrades conservatively, never crashes | recorded, minor | — |
