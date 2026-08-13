"""Contract tests: the seams where two halves must agree, checked mechanically.

Read this before writing a new checker. It exists because someone did not: a
second adapter-argument checker was written from scratch, found nothing the
existing one had not already guarded, and clobbered its Lua dumper on the way
past. The map below is what was already here.

**Seams with a standing agreement check.** Each of these compares two
independently-written halves and has caught real drift:

* ``test_adapter_args_agreement`` — the args the sidecar sends against the args
  each mod adapter declares, for both adapter families. Caught an ``origin``
  object no adapter declared (every transfer would have been refused) and
  ``action.wait``/``plan.cancel`` disagreeing about units and key names.
* ``test_capability_declaration_agreement`` — the capability the sidecar gates
  an action on against the one the mod publishes for it. Caught five Lua
  adapters declaring ``capability = nil``.
* ``test_capability_evidence_agreement`` — what a probe demands as proof
  against what its adapter actually observes.
* ``test_game_api_inventory`` — every engine class the mod names against the
  unconfirmed-API inventory.
* ``test_mcp_action_coverage`` — the action engine against the published tool
  surface.
* ``test_schema_conformance``, ``test_goal_schema_conformance``,
  ``test_core_rpc_schema_conformance``, ``test_teamon_schema_conformance`` —
  each schema against the bytes the code actually writes.
* ``test_cli_docs_agreement``, ``test_documented_commands_parse``,
  ``test_doctor_codes_documented``, ``test_mcp_exit_codes_documented`` — what
  the documents promise against what the parser and the exit paths do.

**The seam that had none, and what it cost.** Nothing compared the *observation
document's field vocabularies*: the mod builds a document, the sidecar reads it,
and each side's tests use its own fixtures. Eight dead gates accumulated there
before anybody swept for them, including the three that matter most — the agent
cannot walk, nothing loots, and a safety rung that has never fired. Those are
recorded in ``test_gates_without_producers`` and measured in
``test_item_domain_vocabularies``, which now checks the item blocks, the stats
map and the structural tiers on every run.

The lesson generalises past this repository: an agreement that is only kept by
review is kept until the day it is not, and the suite stays green through the
whole of that day. If you find yourself about to verify by hand that two sides
still match, look here first — and if the check is genuinely missing, add it
beside these rather than beginning a new tradition.
"""
