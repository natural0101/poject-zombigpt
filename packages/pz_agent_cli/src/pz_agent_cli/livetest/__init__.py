"""The live-test harness: the scenarios that need a running Project Zomboid.

There are twenty-two of them since the crafting and building wave added S21 and
S22, and this docstring says so in words only because prose has to. The count
that decides anything is ``len(SCENARIO_IDS)``: the two places that used to
spell "twenty" into user-facing output were still saying it under a tally
printing 22.

Four modules, in dependency order:

* :mod:`~pz_agent_cli.livetest.scenarios` — the scenarios as data, with
  machine-checkable postconditions. ``docs/LIVE_TEST_PLAYBOOK.md`` is generated
  from it.
* :mod:`~pz_agent_cli.livetest.evidence` — paths, canonical bytes, SHA-256,
  schema validation, collection.
* :mod:`~pz_agent_cli.livetest.state` — the append-only attempt ledger. State is
  derived from the attempts, never stored, so ``PASS`` cannot be overwritten and
  ``NOT_RUN`` is what an empty history means.
* :mod:`~pz_agent_cli.livetest.runner` — the only writer of ``result.json`` and
  the only thing that decides ``PASS``.

The relationship to :mod:`pz_agent_cli.smoke` is deliberate: that harness
validates scenario *documents* and reports what a live run would ask of you,
and this one records what a live run actually observed. Both refuse to let an
unrun scenario report a pass; they differ in that this one also has to survive
an operator who would rather see green.
"""

from __future__ import annotations

from .commands import add_live_test_parser, run_live_test
from .evidence import (
    ArtefactDigest,
    EvidenceLayout,
    LiveTestError,
    ManifestEntry,
    TamperError,
    canonical_json,
    sha256_file,
    sha256_text,
)
from .runner import (
    FileDriver,
    FinalizeRefused,
    ObservedRun,
    PostconditionOutcome,
    ScenarioAudit,
    ScenarioDriver,
    ScenarioRun,
    UnavailableDriver,
    audit_scenario,
    decide,
    default_evidence_root,
    default_manifest_path,
    evaluate,
    finalize,
    first_unpassed,
    parse_observations,
    percentile,
    read_commit,
    run_scenario,
    summarise,
    verify_result,
)
from .scenarios import (
    SCENARIO_IDS,
    SCENARIOS,
    Check,
    LiveScenario,
    Postcondition,
    UnknownScenarioError,
    by_id,
    catalogue,
)
from .state import Attempt, LedgerError, LiveState, ScenarioState, StateStore

__all__ = [
    "SCENARIOS",
    "SCENARIO_IDS",
    "ArtefactDigest",
    "Attempt",
    "Check",
    "EvidenceLayout",
    "FileDriver",
    "FinalizeRefused",
    "LedgerError",
    "LiveScenario",
    "LiveState",
    "LiveTestError",
    "ManifestEntry",
    "ObservedRun",
    "Postcondition",
    "PostconditionOutcome",
    "ScenarioAudit",
    "ScenarioDriver",
    "ScenarioRun",
    "ScenarioState",
    "StateStore",
    "TamperError",
    "UnavailableDriver",
    "UnknownScenarioError",
    "add_live_test_parser",
    "audit_scenario",
    "by_id",
    "canonical_json",
    "catalogue",
    "decide",
    "default_evidence_root",
    "default_manifest_path",
    "evaluate",
    "finalize",
    "first_unpassed",
    "parse_observations",
    "percentile",
    "read_commit",
    "run_live_test",
    "run_scenario",
    "sha256_file",
    "sha256_text",
    "summarise",
    "verify_result",
]
