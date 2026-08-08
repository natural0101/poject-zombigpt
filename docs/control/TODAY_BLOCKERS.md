# Today's blockers — live view (orchestrator-written)

## REMOTE (fixable here)
- TB-R1: Windows CI red streak on main (runs 31246415811, 31247921064). Two fix
  rounds landed on the integration branch; the next windows run on
  rescue/today-finalization is the judge. If red again: AGENT-06 diagnoses from
  the log, no fix without a named cause.
- TB-R2: REMOTE_ACTIONS_UNSERVED / REMOTE_PLANS_UNSERVED — actions and plans
  are honestly refused over the link rather than served cross-thread.
  Substantive; assigned to AGENT-21/22 (Wave B) once AGENT-19/24's interface
  audit confirms the seam.

## LIVE (not fixable here — do not disguise)
- TB-L1: E14/E15 (weight 599) and FINAL_RELEASE (200) need a running Project
  Zomboid Build 42. Stays NOT_RUN today. LOCAL_AGENT_PROMPT.md is the handoff.

## RELEASE
- TB-REL1: RB-002 stands — no v1.0.0 without live evidence. Permanent today.
