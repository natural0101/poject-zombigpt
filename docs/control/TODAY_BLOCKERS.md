# Today's blockers — live view (orchestrator-written)

## REMOTE (fixable here)
- TB-R1: CLOSED. Three fix rounds (AF_UNIX guard, poll-guard, redaction
  spelling) proved out: Windows run 31251581990 on ff63b38 is fully green and
  certified v1.0.0-rc1.
- TB-R2: CLOSED for actions (bounded ActionChannel drained on the tick
  thread through the real engine, commit 6b5ca25); plans remain a REASONED
  refusal recorded on UnservedPlanPort's docstring — serving a multi-step
  plan synchronously on the tick thread would make the stop levers
  unreachable for the plan's whole wall budget, and the free-text goal has
  no deterministic translator. The served multi-step shape is the goal
  channel.

## LIVE (not fixable here — do not disguise)
- TB-L1: E14/E15 (weight 599) and FINAL_RELEASE (200) need a running Project
  Zomboid Build 42. Stays NOT_RUN today. LOCAL_AGENT_PROMPT.md is the handoff.

## RELEASE
- TB-REL1: RB-002 stands — no v1.0.0 without live evidence. Permanent today.
