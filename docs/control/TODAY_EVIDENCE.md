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
| A21/22 | actions served over the real link | disarmed submit → NOT_ARMED record over the socket; armed action.wait drained by the tick thread through the real ActionEngine to SUCCEEDED with observed elapsed_game_seconds >= 1.0; idempotent resubmit resolves to same id; plans kept as argued refusal (stop-lever unreachability + free-text goal) | tests/contract/test_remote_actions_served.py + tests/unit/test_action_channel.py (25) | 6b5ca25 |
| A32 | MCP surface survives six adversarial paths | typed refusals, clean streams, healthy follow-up per case | tests/contract/test_mcp_adversarial_e2e.py | 6ad0b29 |
| A49 | eight 'read the workflow' criteria executable | 11 tests, 13 mutations red-verified | tests/unit/test_windows_workflow_contract.py | f4fa0b2 |
| A44 (fix) | real walks survive their polls | runtime-level suite red pre-fix, green post-fix | tests/lua/test_movement_runtime.lua | 7ef3104 |
| A50 (red team) | all four orchestrator claims survive | 3 minor plan-pointer mismatches + accept-guard asymmetry recorded for control plane | — | — |
| A46 (audit) | GAME_API_VERIFICATION incomplete/wrong rows | ~60 missing symbols, 5 wrong rows — fix in flight (doc agent) | — | — |
