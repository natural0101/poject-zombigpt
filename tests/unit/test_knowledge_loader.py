"""The knowledge loader: schema mirror, honesty gates, typed refusals.

Two halves. The first pins the loader's closed enums against the arrays in
``schemas/gameplay-knowledge.schema.json`` by reading the schema file itself,
so the two definitions cannot drift apart without this file failing. The
second exercises every refusal shape the loader owns — a claim dressed above
its evidence, a dead proof path, an unknown action, a duplicate id, an
oversized file — and checks that each refusal names the file and the rule id
while never echoing the document's free text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from pz_agent_core.goals.model import GoalKind
from pz_agent_core.knowledge import (
    MAX_ACTIONS,
    MAX_DOCUMENTS,
    MAX_FILE_BYTES,
    MAX_GOAL_KINDS,
    MAX_NEARBY_KINDS,
    MAX_NEEDS,
    MAX_NUMBERS,
    MAX_OBSERVED_INPUTS,
    MAX_POSTCONDITIONS,
    MAX_PRECONDITIONS,
    MAX_PROVEN_BY,
    MAX_RULES_PER_DOCUMENT,
    SCHEMA_VERSION,
    CorpusError,
    KnowledgeDomain,
    KnowledgeRefusal,
    KnowledgeRule,
    KnowledgeSource,
    KnowledgeStatus,
    NeedChannel,
    load_corpus,
)
from pz_agent_core.protocol import ActionName, RiskClass

SCHEMA_PATH: Final = (
    Path(__file__).resolve().parents[2] / "schemas" / "gameplay-knowledge.schema.json"
)

#: A distinctive free-text fragment used in the fixtures below; every refusal
#: test asserts it never leaks into the error message.
FREE_TEXT_CANARY: Final = "zzcanaryzz"


def _schema() -> dict[str, Any]:
    payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _rule_properties(schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema["properties"]["rules"]["items"]["properties"]
    assert isinstance(properties, dict)
    return properties


# ---------------------------------------------------------------------------
# enum mirrors: the schema file is read here, so the two cannot drift
# ---------------------------------------------------------------------------


def test_domain_enum_mirrors_the_schema() -> None:
    schema = _schema()
    assert [d.value for d in KnowledgeDomain] == schema["properties"]["domain"]["enum"]


def test_status_enum_mirrors_the_schema() -> None:
    assert [s.value for s in KnowledgeStatus] == _rule_properties(_schema())["status"]["enum"]


def test_source_enum_mirrors_the_schema() -> None:
    assert [s.value for s in KnowledgeSource] == _rule_properties(_schema())["source"]["enum"]


def test_needs_enum_mirrors_the_schema() -> None:
    needs = _rule_properties(_schema())["needs"]["items"]["enum"]
    assert [n.value for n in NeedChannel] == needs


def test_risk_class_enum_mirrors_the_schema() -> None:
    assert [r.value for r in RiskClass] == _rule_properties(_schema())["risk_class"]["enum"]


def test_number_status_enum_mirrors_the_schema() -> None:
    status = _rule_properties(_schema())["numbers"]["items"]["properties"]["status"]["enum"]
    assert [s.value for s in KnowledgeStatus] == status


def test_schema_version_const_mirrors_the_schema() -> None:
    assert _schema()["properties"]["schema_version"]["const"] == SCHEMA_VERSION


def test_array_bounds_mirror_the_schema() -> None:
    schema = _schema()
    properties = _rule_properties(schema)
    assert schema["properties"]["rules"]["maxItems"] == MAX_RULES_PER_DOCUMENT
    assert properties["observed_inputs"]["maxItems"] == MAX_OBSERVED_INPUTS
    assert properties["preconditions"]["maxItems"] == MAX_PRECONDITIONS
    assert properties["postconditions"]["maxItems"] == MAX_POSTCONDITIONS
    assert properties["actions"]["maxItems"] == MAX_ACTIONS
    assert properties["goal_kinds"]["maxItems"] == MAX_GOAL_KINDS
    assert properties["needs"]["maxItems"] == MAX_NEEDS
    assert properties["nearby_kinds"]["maxItems"] == MAX_NEARBY_KINDS
    assert properties["proven_by"]["maxItems"] == MAX_PROVEN_BY
    assert properties["numbers"]["maxItems"] == MAX_NUMBERS


# ---------------------------------------------------------------------------
# fixture corpus plumbing
# ---------------------------------------------------------------------------

#: The proof file every valid fixture rule points at, created under the root.
PIN_TEST: Final = "tests/unit/test_pin.py"

#: The code file every valid fixture rule cites, created under the root.
PIN_SOURCE: Final = "packages/demo/src/policy.py"


def _valid_rule(**overrides: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "id": "movement_demo_rule",
        "build": "42",
        "source": "code",
        "source_detail": f"{PIN_SOURCE}::DemoPolicy",
        "status": "verified_script",
        "claim": "A demonstration claim long enough to satisfy the schema bounds.",
        "observed_inputs": ["player.position", "nearby.zombies[].distance"],
        "preconditions": ["The demo precondition holds"],
        "decision": "The demo decision, made by DemoPolicy and nothing else.",
        "actions": ["movement.move_to"],
        "postconditions": ["The demo postcondition is observed"],
        "on_failure": "The demo replans once and then refuses.",
        "risk_class": "P2",
        "goal_kinds": ["navigate_to"],
        "needs": ["hunger"],
        "nearby_kinds": ["door"],
        "proven_by": [PIN_TEST],
        "numbers": [
            {"name": "demo_bound", "value": 30, "unit": "squares", "status": "verified_script"}
        ],
    }
    rule.update(overrides)
    return rule


def _write_corpus(root: Path, *documents: dict[str, Any], names: list[str] | None = None) -> Path:
    gameplay = root / "knowledge" / "gameplay"
    gameplay.mkdir(parents=True, exist_ok=True)
    pin = root / PIN_TEST
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text("# proof pin\n", encoding="utf-8")
    source = root / PIN_SOURCE
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# cited code\n", encoding="utf-8")
    for index, document in enumerate(documents):
        name = names[index] if names else f"doc_{index}.yaml"
        (gameplay / name).write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    return root


def _document(*rules: dict[str, Any], domain: str = "movement") -> dict[str, Any]:
    return {"schema_version": "1.0", "domain": domain, "rules": list(rules)}


def _refusal(root: Path) -> CorpusError:
    with pytest.raises(CorpusError) as excinfo:
        load_corpus(root)
    return excinfo.value


# ---------------------------------------------------------------------------
# the happy path, and the field-for-field round trip
# ---------------------------------------------------------------------------


def test_a_valid_corpus_loads(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule()))
    corpus = load_corpus(root)
    assert len(corpus.documents) == 1
    assert corpus.documents[0].domain is KnowledgeDomain.MOVEMENT
    rule = corpus.rule("movement_demo_rule")
    assert rule is not None
    assert rule.status is KnowledgeStatus.VERIFIED_SCRIPT
    assert rule.actions == (ActionName.MOVEMENT_MOVE_TO,)
    assert rule.goal_kinds == (GoalKind.NAVIGATE_TO,)
    assert rule.needs == (NeedChannel.HUNGER,)
    assert rule.numbers[0].value == 30.0
    assert corpus.rule("no_such_rule") is None


def test_a_sample_rule_round_trips_field_for_field(tmp_path: Path) -> None:
    payload = _valid_rule()
    rule = KnowledgeRule.from_mapping(payload)
    round_tripped = rule.as_dict()
    # value goes through float on the way in; normalise the expectation only.
    expected = dict(payload)
    expected["numbers"] = [dict(payload["numbers"][0], value=30.0)]
    assert round_tripped == expected


def test_a_missing_corpus_directory_is_an_empty_corpus(tmp_path: Path) -> None:
    assert load_corpus(tmp_path).documents == ()


def test_a_live_rule_with_a_scenario_pointer_loads(tmp_path: Path) -> None:
    live = _valid_rule(
        id="movement_live_rule",
        source="live_probe",
        source_detail="live run 2026-08-10, scenario S05",
        status="verified_live",
        proven_by=["S05"],
    )
    root = _write_corpus(tmp_path, _document(live))
    corpus = load_corpus(root)
    loaded = corpus.rule("movement_live_rule")
    assert loaded is not None
    assert loaded.status is KnowledgeStatus.VERIFIED_LIVE


# ---------------------------------------------------------------------------
# honesty gates: each refusal shape, named file, named rule, no free text
# ---------------------------------------------------------------------------


def test_verified_script_without_code_source_is_refused(tmp_path: Path) -> None:
    rule = _valid_rule(
        source="wiki",
        source_detail=f"https://pzwiki.net/{FREE_TEXT_CANARY}",
        claim=f"A wiki claim dressed as script-verified, {FREE_TEXT_CANARY} padding.",
    )
    root = _write_corpus(tmp_path, _document(rule))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.VERIFIED_SCRIPT_WITHOUT_CODE_SOURCE
    assert error.file == "knowledge/gameplay/doc_0.yaml"
    assert error.rule_id == "movement_demo_rule"
    assert FREE_TEXT_CANARY not in str(error)


def test_verified_script_without_tests_is_refused(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule(proven_by=[])))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.VERIFIED_SCRIPT_WITHOUT_TESTS
    assert error.rule_id == "movement_demo_rule"


def test_verified_script_with_a_non_test_proof_is_refused(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule(proven_by=["docs/notes.md"])))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.VERIFIED_SCRIPT_WITHOUT_TESTS


def test_a_dead_proof_path_is_refused_loudly(tmp_path: Path) -> None:
    rule = _valid_rule(proven_by=["tests/unit/test_deleted.py"])
    root = _write_corpus(tmp_path, _document(rule))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.DEAD_PROOF_PATH
    assert error.rule_id == "movement_demo_rule"
    assert "tests/unit/test_deleted.py" in str(error)


def test_verified_live_without_a_pointer_is_refused(tmp_path: Path) -> None:
    rule = _valid_rule(
        source="live_probe",
        source_detail="live run 2026-08-10",
        status="verified_live",
        proven_by=["a note that proves nothing"],
    )
    root = _write_corpus(tmp_path, _document(rule))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.VERIFIED_LIVE_WITHOUT_POINTER


def test_a_code_rule_without_a_repo_path_is_refused(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule(source_detail="somewhere in the code")))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.SOURCE_DETAIL_NOT_REPO_PATH


def test_a_code_rule_citing_a_missing_file_is_refused(tmp_path: Path) -> None:
    rule = _valid_rule(source_detail="packages/demo/src/deleted.py::Gone")
    root = _write_corpus(tmp_path, _document(rule))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.DEAD_SOURCE_PATH


def test_an_unknown_action_is_refused(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule(actions=["combat.swing"])))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.UNKNOWN_ACTION
    assert error.rule_id == "movement_demo_rule"


def test_an_unknown_goal_kind_is_refused(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule(goal_kinds=["conquer_map"])))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.UNKNOWN_GOAL_KIND


def test_a_duplicate_rule_id_across_files_is_refused(tmp_path: Path) -> None:
    root = _write_corpus(
        tmp_path,
        _document(_valid_rule()),
        _document(_valid_rule(), domain="doors_windows"),
        names=["a.yaml", "b.yaml"],
    )
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.DUPLICATE_RULE_ID
    assert error.file == "knowledge/gameplay/b.yaml"
    assert error.rule_id == "movement_demo_rule"
    assert "knowledge/gameplay/a.yaml" in str(error)


def test_an_oversized_file_is_refused_before_parse(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule()))
    oversized = root / "knowledge" / "gameplay" / "big.yaml"
    # Deliberately not valid YAML: the size gate must fire off stat, before
    # any parser sees a byte of it.
    oversized.write_bytes(b"{" * (MAX_FILE_BYTES + 1))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.FILE_TOO_LARGE
    assert error.file == "knowledge/gameplay/big.yaml"


def test_too_many_documents_are_refused(tmp_path: Path) -> None:
    gameplay = tmp_path / "knowledge" / "gameplay"
    gameplay.mkdir(parents=True)
    for index in range(MAX_DOCUMENTS + 1):
        (gameplay / f"doc_{index:02d}.yaml").write_text("x: 1\n", encoding="utf-8")
    error = _refusal(tmp_path)
    assert error.refusal is KnowledgeRefusal.TOO_MANY_DOCUMENTS


def test_unparseable_yaml_is_refused_without_echo(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule()))
    (root / "knowledge" / "gameplay" / "broken.yaml").write_text(
        f"rules: [unclosed {FREE_TEXT_CANARY}", encoding="utf-8"
    )
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.PARSE_FAILED
    assert FREE_TEXT_CANARY not in str(error)


def test_a_non_mapping_document_is_refused(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule()))
    (root / "knowledge" / "gameplay" / "list.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.NOT_A_MAPPING


def test_an_unknown_key_is_refused_without_echoing_it(tmp_path: Path) -> None:
    rule = _valid_rule(**{FREE_TEXT_CANARY: "smuggled"})
    root = _write_corpus(tmp_path, _document(rule))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.SCHEMA_MISMATCH
    assert FREE_TEXT_CANARY not in str(error)


def test_a_short_claim_is_refused_naming_the_rule(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule(claim="too short")))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.SCHEMA_MISMATCH
    assert error.rule_id == "movement_demo_rule"


def test_an_invalid_rule_id_is_never_echoed(tmp_path: Path) -> None:
    root = _write_corpus(tmp_path, _document(_valid_rule(id=f"BAD {FREE_TEXT_CANARY}")))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.SCHEMA_MISMATCH
    assert error.rule_id is None
    assert FREE_TEXT_CANARY not in str(error)


def test_a_document_with_too_many_rules_is_refused(tmp_path: Path) -> None:
    rules = [
        _valid_rule(id=f"movement_rule_{index:03d}") for index in range(MAX_RULES_PER_DOCUMENT + 1)
    ]
    root = _write_corpus(tmp_path, _document(*rules))
    error = _refusal(root)
    assert error.refusal is KnowledgeRefusal.SCHEMA_MISMATCH
