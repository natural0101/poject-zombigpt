# Today's critical path — rescue/today-finalization

The remote stage closes (section 12 of the directive) only through this chain.
Everything else parallelises around it.

1. **Windows CI GREEN on the integration branch HEAD.** Two fix rounds landed
   (`d6a7bf0`, `97c8e8c`); run on `rescue/today-finalization` is the judge.
   Owner: AGENT-06. Blocks: every reclaim below.
2. **Reclaim of the 56 reopened tasks** via `scripts/verify_carryover.py` —
   22 criterion-audit roots (observing assertions now exist in-tree) plus 34
   dependency-chained. Owner: AGENT-01 with orchestrator. Needs: green CI at
   a SHA the plan's predicate accepts.
3. **E11-M02-T005 / E07-M03-T007 witnesses**: the packaged-link CI step and
   the `tests.mcp-e2e` gate rule must be seen green in a Windows run on this
   branch; their PASS carries that run's URL. Owners: AGENT-08, AGENT-31.
4. **Cross-thread serving of actions/plans** — the two honest refusals
   (`REMOTE_ACTIONS_UNSERVED`, `REMOTE_PLANS_UNSERVED`) are the last
   substantive remote implementation gap. Owners: AGENT-21, AGENT-22;
   review AGENT-17/AGENT-50.
5. **MCP adversarial E2E** (AGENT-32) and **remaining E12 security chains**
   (AGENT-17/18) — close the security band.
6. **Release gate coherence** (AGENT-49): exact-HEAD provenance across
   Linux/Windows/RC, then STATUS regeneration.

Cannot move today, ever, from this environment: LIVE_GAME_VALIDATION
(599 weight, E14/E15) and FINAL_RELEASE (200) — a machine with Project
Zomboid holds those. No v1.0.0.
