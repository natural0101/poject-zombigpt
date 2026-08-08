# Agent matrix — ownership and review pairing

Full registry: TODAY_SWARM.yaml (orchestrator-only writer). Rules:
- IMPLEMENTER != REVIEWER for every P0/P1/P2 subsystem.
- Control files (MASTER_PLAN.yaml, STATUS.json, BLOCKERS.md, evidence index):
  writable only by AGENT-01..04 and the ORCHESTRATOR.
- Shared spine files (pyproject.toml, tests/conftest.py, schemas/, scripts/,
  protocol/, CHANGELOG.md): ORCHESTRATOR merges only; agents propose diffs.
- Two agents on one file: one IMPLEMENTATION OWNER, one REVIEW/TEST OWNER;
  the reviewer does not edit the production file.

Review pairing (P0/P1/P2):
- rpc/transport: impl AGENT-12, security review AGENT-17, failure review AGENT-18, final AGENT-50
- core_services: impl AGENT-19..24 (disjoint methods), review AGENT-50
- mcp: impl AGENT-25..31, adversarial AGENT-32, final AGENT-50
- goal: impl AGENT-34..37, rpc review AGENT-35, final AGENT-50
- voice: impl AGENT-38..43, integration AGENT-48, final AGENT-50
